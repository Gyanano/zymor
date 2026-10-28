from __future__ import annotations

import fnmatch
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zymor.errors import ZymorError
from zymor.util.fs import atomic_write_text
from zymor.util.time import now_iso
from zymor.vault import Vault
from zymor.write_service import memory_log

DEFAULT_IGNORE = (
    ".git/**",
    ".zymor/**",
    ".venv/**",
    "**/__pycache__/**",
    "node_modules/**",
    "dist/**",
    ".pytest_cache/**",
    ".ruff_cache/**",
    ".mypy_cache/**",
    "*.pyc",
)
MAX_EVENTS_PER_POLL = 50


@dataclass(frozen=True)
class FileChange:
    action: str
    path: str


def _normalize(path: str) -> str:
    return path.replace("\\", "/")


def is_ignored(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """入参：相对路径与忽略模式。出参：是否忽略。"""
    normalized = _normalize(path)
    return any(fnmatch.fnmatch(normalized, _normalize(pattern)) for pattern in patterns)


def scan_files(
    root: Path, *, ignore: tuple[str, ...] | list[str] = DEFAULT_IGNORE
) -> dict[str, tuple[int, int]]:
    """入参：根目录与忽略模式。出参：相对路径到 (mtime_ns, size) 的快照。"""
    files: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(root).as_posix()
            if is_ignored(relative, ignore):
                continue
            stat = path.stat()
        except OSError:
            continue
        files[relative] = (stat.st_mtime_ns, stat.st_size)
    return files


def diff_snapshots(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[FileChange]:
    """入参：前后快照。出参：按路径排序的创建/修改/删除事件。"""
    changes: list[FileChange] = []
    for path in sorted(after.keys() - before.keys()):
        changes.append(FileChange("create", path))
    for path in sorted(before.keys() & after.keys()):
        if before[path] != after[path]:
            changes.append(FileChange("modify", path))
    for path in sorted(before.keys() - after.keys()):
        changes.append(FileChange("delete", path))
    return changes


def capture_changes(
    vault: Vault,
    changes: list[FileChange],
    *,
    session_id: str,
    max_events: int = MAX_EVENTS_PER_POLL,
) -> dict[str, Any]:
    """入参：vault、文件变化与会话。出参：捕获统计。变化写入 L0 system_note。"""
    captured = 0
    warnings: list[str] = []
    failed: list[FileChange] = []
    for change in changes[: max(0, max_events)]:
        try:
            memory_log(
                vault,
                session_id=session_id,
                kind="system_note",
                content=f"文件{change.action}: {change.path}",
                meta={
                    "source": "capture",
                    "event": change.action,
                    "path": change.path,
                    "captured_at": now_iso(),
                },
            )
        except ZymorError as error:
            warnings.append(f"{change.path}: {error.message}")
            failed.append(change)
            continue
        captured += 1
    return {
        "captured": captured,
        "changes": len(changes),
        "warnings": warnings,
        "failed": failed,
    }


def _state_path(vault: Vault) -> Path:
    return vault.root / "capture" / "state.json"


def _load_state(vault: Vault, root: Path) -> dict[str, Any] | None:
    path = _state_path(vault)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("root") != str(root.resolve()):
        return None
    snapshot = data.get("snapshot")
    if not isinstance(snapshot, dict):
        return None
    files: dict[str, tuple[int, int]] = {}
    for name, value in snapshot.items():
        if (
            isinstance(name, str)
            and isinstance(value, list)
            and len(value) == 2
            and all(isinstance(item, int) for item in value)
        ):
            files[name] = (value[0], value[1])
    pending: list[FileChange] = []
    raw_pending = data.get("pending")
    if isinstance(raw_pending, list):
        for item in raw_pending:
            if not isinstance(item, dict):
                continue
            action = item.get("action")
            name = item.get("path")
            if action in {"create", "modify", "delete"} and isinstance(name, str):
                pending.append(FileChange(action, name))
    return {"snapshot": files, "pending": pending}


def _save_state(
    vault: Vault, root: Path, snapshot: dict[str, tuple[int, int]], pending: list[FileChange]
) -> None:
    payload = {
        "root": str(root.resolve()),
        "snapshot": {key: [value[0], value[1]] for key, value in snapshot.items()},
        "pending": [{"action": item.action, "path": item.path} for item in pending],
        "updated_at": now_iso(),
    }
    atomic_write_text(_state_path(vault), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _capture_ignore(vault: Vault, root: Path, ignore: tuple[str, ...] | list[str]) -> list[str]:
    patterns = list(ignore)
    try:
        relative = vault.root.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return patterns
    if relative not in patterns:
        patterns.append(relative)
    marker = f"{relative}/**"
    if marker not in patterns:
        patterns.append(marker)
    return patterns


def run_capture(
    vault: Vault,
    root: Path,
    *,
    session_id: str,
    interval_s: float = 5.0,
    ignore: tuple[str, ...] | list[str] = DEFAULT_IGNORE,
    max_events: int = MAX_EVENTS_PER_POLL,
    once: bool = False,
) -> dict[str, Any]:
    """入参：vault、根目录、会话、轮询参数。出参：捕获统计。可选单次或常驻轮询。"""
    if interval_s <= 0:
        raise ValueError("interval_s 必须大于 0")
    root = root.resolve()
    patterns = _capture_ignore(vault, root, ignore)
    state = _load_state(vault, root)
    if state is None:
        previous = scan_files(root, ignore=patterns)
        pending: list[FileChange] = []
    else:
        previous = state["snapshot"]
        pending = state["pending"]
    captured = 0
    polls = 0
    warnings: list[str] = []
    while True:
        time.sleep(interval_s)
        current = scan_files(root, ignore=patterns)
        changes = pending + diff_snapshots(previous, current)
        previous = current
        polls += 1
        if changes:
            result = capture_changes(vault, changes, session_id=session_id, max_events=max_events)
            captured += result["captured"]
            warnings.extend(result["warnings"])
            pending = list(result["failed"]) + changes[max_events:]
        else:
            pending = []
        _save_state(vault, root, previous, pending)
        if once:
            break
    return {
        "captured": captured,
        "polls": polls,
        "pending": len(pending),
        "warnings": warnings,
    }
