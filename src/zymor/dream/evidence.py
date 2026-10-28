from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from zymor.errors import NotFoundError, UserError
from zymor.model.event import read_events
from zymor.model.node import MemoryNode
from zymor.vault import Vault

KNOWN_LIMITS: tuple[str, ...] = (
    "readonly-command-execution: not-implemented",
    "network-fetch: disabled",
)
PRUNE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        "dist",
        "build",
    }
)
FILE_LINE_RE = re.compile(r"^(?P<path>.+):(?P<line>\d+)$")
L0_RE = re.compile(r"^l0:(?P<session>[^:\s]+):(?P<seq>\d+)$")
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
MAX_ITEM_CHARS = 2000
MAX_GREP_MATCHES = 200
MAX_GREP_LINE_CHARS = 300


def collect_evidence(vault: Vault, node: MemoryNode, *, max_items: int = 5) -> dict[str, Any]:
    """入参：vault、节点与解析上限。出参：证据条目与已知边界。只读解析 file:line、L0 与 URL。"""
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    for pointer in node.evidence[: max(0, max_items)]:
        text = str(pointer).strip()
        if not text:
            continue
        item = _parse_pointer(vault, text)
        if item.get("status") not in {"ok", "reference-only"}:
            warnings.append(f"证据指针不可用: {text}（{item.get('status')}）")
        items.append(item)
    return {
        "node_id": node.id,
        "items": items,
        "known_limits": list(KNOWN_LIMITS),
        "warnings": warnings,
    }


def grep_workspace(
    pattern: str,
    root: Path,
    *,
    max_files: int = 50,
    max_bytes: int = 200_000,
) -> dict[str, Any]:
    """入参：字面量模式、搜索根与文件/字节上限。出参：命中行、扫描统计与截断标记。在工作区内做只读文本搜索。"""
    if not pattern:
        raise UserError("grep 模式不能为空", hint="传入要精确匹配的字面量文本")
    cwd = Path.cwd().resolve()
    resolved_root = root.expanduser().resolve()
    if not _is_within(resolved_root, cwd):
        raise UserError(
            f"grep 根目录越界: {root}",
            hint="只允许在工作区（Path.cwd()）内搜索",
        )
    if not resolved_root.is_dir():
        raise NotFoundError(f"grep 目录不存在: {root}", hint="检查路径后重试")
    file_limit = max(0, max_files)
    byte_limit = max(0, max_bytes)
    matches: list[dict[str, Any]] = []
    warnings: list[str] = []
    scanned_files = 0
    scanned_bytes = 0
    truncated = False
    for path in _iter_files(resolved_root):
        if scanned_files >= file_limit or scanned_bytes >= byte_limit:
            truncated = True
            break
        if len(matches) >= MAX_GREP_MATCHES:
            truncated = True
            break
        resolved = path.resolve()
        if not _is_within(resolved, cwd):
            continue
        try:
            size = resolved.stat().st_size
        except OSError as error:
            warnings.append(f"文件不可读: {resolved}: {error}")
            continue
        if size > byte_limit - scanned_bytes:
            truncated = True
            break
        try:
            data = resolved.read_bytes()
        except OSError as error:
            warnings.append(f"文件不可读: {resolved}: {error}")
            continue
        scanned_files += 1
        scanned_bytes += len(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern not in line:
                continue
            matches.append(
                {
                    "path": _display_path(resolved, cwd),
                    "line": number,
                    "text": line.strip()[:MAX_GREP_LINE_CHARS],
                }
            )
            if len(matches) >= MAX_GREP_MATCHES:
                truncated = True
                break
        if truncated:
            break
    return {
        "pattern": pattern,
        "root": _display_path(resolved_root, cwd),
        "matches": matches,
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "truncated": truncated,
        "known_limits": list(KNOWN_LIMITS),
        "warnings": warnings,
    }


def _parse_pointer(vault: Vault, pointer: str) -> dict[str, Any]:
    if URL_RE.match(pointer):
        return {"kind": "url", "pointer": pointer, "status": "reference-only"}
    l0_match = L0_RE.match(pointer)
    if l0_match is not None:
        return _l0_item(
            vault,
            pointer,
            l0_match.group("session"),
            int(l0_match.group("seq")),
        )
    file_match = FILE_LINE_RE.match(pointer)
    if file_match is not None:
        return _file_item(vault, pointer, file_match.group("path"), int(file_match.group("line")))
    if " " in pointer:
        return {
            "kind": "command",
            "pointer": pointer,
            "status": "not-implemented",
            "note": "v0 不执行子进程命令",
        }
    return {"kind": "other", "pointer": pointer, "status": "unsupported"}


def _file_item(vault: Vault, pointer: str, raw_path: str, line: int) -> dict[str, Any]:
    path = _safe_path(raw_path, vault)
    item: dict[str, Any] = {
        "kind": "file",
        "pointer": pointer,
        "path": _display_vault_path(path, vault),
        "line": line,
        "status": "ok",
    }
    if not path.is_file():
        item["status"] = "missing"
        return item
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        item["status"] = "binary"
        return item
    except OSError as error:
        item["status"] = "unreadable"
        item["error"] = str(error)
        return item
    lines = text.splitlines()
    if line < 1 or line > len(lines):
        item["status"] = "out-of-range"
        item["line_count"] = len(lines)
        return item
    start = max(0, line - 3)
    end = min(len(lines), line + 2)
    fragment = "\n".join(f"{number + 1}: {lines[number]}" for number in range(start, end))
    item["content"] = fragment[:MAX_ITEM_CHARS]
    return item


def _l0_item(vault: Vault, pointer: str, session_id: str, seq: int) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": "l0",
        "pointer": pointer,
        "session_id": session_id,
        "seq": seq,
        "status": "missing",
    }
    for event in read_events(vault.l0_dir):
        if event.session_id != session_id or event.seq != seq:
            continue
        item["status"] = "ok"
        item["event_kind"] = event.kind
        item["ts"] = event.ts
        item["content"] = event.content[:MAX_ITEM_CHARS]
        break
    return item


def _evidence_roots(vault: Vault) -> list[Path]:
    return [Path.cwd().resolve(), vault.root.resolve()]


def _safe_path(raw: str, vault: Vault) -> Path:
    roots = _evidence_roots(vault)
    candidate = Path(raw).expanduser()
    candidates = [candidate] if candidate.is_absolute() else [root / candidate for root in roots]
    allowed: list[Path] = []
    for path in candidates:
        resolved = path.resolve()
        if any(_is_within(resolved, root) for root in roots):
            allowed.append(resolved)
    if not allowed:
        raise UserError(
            f"证据路径越界: {raw}",
            hint="只允许读取工作区（Path.cwd()）或 vault 根内的文件",
        )
    for resolved in allowed:
        if resolved.exists():
            return resolved
    return allowed[0]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _display_vault_path(path: Path, vault: Vault) -> str:
    for root in _evidence_roots(vault):
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            continue
    return path.as_posix()


def _iter_files(root: Path) -> Iterator[Path]:
    for current, dir_names, file_names in os.walk(root):
        dir_names[:] = sorted(name for name in dir_names if name not in PRUNE_DIRS)
        for name in sorted(file_names):
            yield Path(current) / name
