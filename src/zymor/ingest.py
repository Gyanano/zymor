"""Conversation ingest: turn host transcripts into L0 events, incrementally."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zymor.dream.state import seq_floor
from zymor.errors import UsageError, ValidationError
from zymor.model.event import append_events
from zymor.security import redact
from zymor.util.fs import atomic_write_text
from zymor.util.ids import safe_project, safe_session_id
from zymor.util.time import is_iso, now_iso
from zymor.vault import Vault

__all__ = ["safe_project", "safe_session_id"]

FORMATS = ("auto", "claude-code", "generic")
STATE_VERSION = 1
MAX_TOOL_CHARS = 600
MAX_LINE_BYTES = 4 * 1024 * 1024
FINGERPRINT_BYTES = 256
# 宿主注入的包装块不是用户说的话，写入 L0 会污染偏好提炼。
WRAPPER_TAGS = (
    "system-reminder",
    "local-command-stdout",
    "local-command-stderr",
    "command-name",
    "command-message",
    "command-args",
    "task-notification",
    "zymor-memory",
)
WRAPPER_OPEN_RE = re.compile("<(" + "|".join(WRAPPER_TAGS) + ")>")
MESSAGE_KINDS = ("user_message", "assistant_message", "tool_call", "tool_result", "system_note")
ROLE_KINDS = {
    "user": "user_message",
    "human": "user_message",
    "assistant": "assistant_message",
    "ai": "assistant_message",
    "model": "assistant_message",
    "system": "system_note",
}


@dataclass
class IngestItem:
    kind: str
    content: str
    ts: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestResult:
    path: str
    session_id: str
    format: str
    captured: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：导入结果字典。"""
        return {
            "path": self.path,
            "session_id": self.session_id,
            "format": self.format,
            "captured": self.captured,
            "skipped": self.skipped,
            "warnings": self.warnings,
        }


def _state_path(vault: Vault) -> Path:
    return vault.root / "capture" / "ingest.json"


def _load_state(vault: Vault) -> dict[str, dict[str, Any]]:
    path = _state_path(vault)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {str(key): dict(value) for key, value in files.items() if isinstance(value, dict)}


def _save_state(vault: Vault, files: dict[str, dict[str, Any]]) -> None:
    path = _state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": STATE_VERSION, "updated_at": now_iso(), "files": files}
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def strip_wrappers(text: str) -> str:
    """入参：消息文本。出参：删掉宿主注入的包装块后的文本。

    逐个找开标签，再用 str.find 找对应的闭标签，每个字符至多被扫描常数次。
    没有闭标签的开标签原样保留。（用一条带回溯的正则做这件事，遇到大量未闭合标签会变成平方时间。）
    """
    if "<" not in text:
        return text
    parts: list[str] = []
    position = 0
    missing: set[str] = set()
    while True:
        match = WRAPPER_OPEN_RE.search(text, position)
        if match is None:
            break
        tag = match.group(1)
        closing = f"</{tag}>"
        end = -1 if tag in missing else text.find(closing, match.end())
        if end < 0:
            # 这个标签之后再也找不到闭标签，后面的同名开标签都不必再找。
            missing.add(tag)
            parts.append(text[position : match.end()])
            position = match.end()
            continue
        parts.append(text[position : match.start()])
        position = end + len(closing)
    parts.append(text[position:])
    return "".join(parts)


def _clean_text(text: str) -> str:
    return redact(strip_wrappers(text).strip())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _claude_code_items(record: dict[str, Any], include_tools: bool) -> list[IngestItem]:
    record_type = record.get("type")
    if record_type not in ("user", "assistant"):
        return []
    if record.get("isSidechain") or record.get("isMeta"):
        return []
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    raw_ts = record.get("timestamp")
    ts = raw_ts if isinstance(raw_ts, str) and is_iso(raw_ts) else None
    base_meta: dict[str, Any] = {"source": "ingest:claude-code"}
    if isinstance(record.get("uuid"), str):
        base_meta["uuid"] = record["uuid"]
    content = message.get("content")
    kind = "user_message" if record_type == "user" else "assistant_message"
    items: list[IngestItem] = []
    text = _clean_text(_block_text(content))
    if text:
        items.append(IngestItem(kind=kind, content=text, ts=ts, meta=dict(base_meta)))
    if include_tools and isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                payload = json.dumps(block.get("input"), ensure_ascii=False, default=str)
                # 先脱敏再截断：先截断的话，被切成两半的密钥就认不出来了。
                body = f"{block.get('name')}: {_clip(redact(payload), MAX_TOOL_CHARS)}"
                items.append(
                    IngestItem(kind="tool_call", content=body, ts=ts, meta=dict(base_meta))
                )
            elif block.get("type") == "tool_result":
                body = _clip(_clean_text(_block_text(block.get("content"))), MAX_TOOL_CHARS)
                if body:
                    items.append(
                        IngestItem(kind="tool_result", content=body, ts=ts, meta=dict(base_meta))
                    )
    return items


def _generic_items(record: dict[str, Any]) -> list[IngestItem]:
    role = str(record.get("role") or record.get("author") or record.get("sender") or "").lower()
    explicit = record.get("kind")
    kind = explicit if explicit in MESSAGE_KINDS else ROLE_KINDS.get(role)
    if kind is None:
        return []
    text = _clean_text(_block_text(record.get("content", record.get("text"))))
    if not text:
        return []
    raw_ts = record.get("ts") or record.get("timestamp") or record.get("created_at")
    ts = raw_ts if isinstance(raw_ts, str) and is_iso(raw_ts) else None
    return [IngestItem(kind=kind, content=text, ts=ts, meta={"source": "ingest:generic"})]


def messages_to_items(messages: list[Any]) -> list[IngestItem]:
    """入参：`{role, content}` 消息列表。出参：可写入 L0 的条目。"""
    items: list[IngestItem] = []
    for message in messages:
        if isinstance(message, dict):
            items.extend(_generic_items(message))
    return items


def _detect_format(record: dict[str, Any]) -> str:
    if "sessionId" in record or ("type" in record and "message" in record):
        return "claude-code"
    return "generic"


def _iter_complete_lines(path: Path, offset: int) -> Iterator[tuple[bytes, int]]:
    with path.open("rb") as handle:
        handle.seek(offset)
        position = offset
        for raw in handle:
            if not raw.endswith(b"\n"):
                # 宿主还没写完这一行；留到下一次导入。
                return
            position += len(raw)
            yield raw, position


def _fingerprint(path: Path, length: int) -> str:
    with path.open("rb") as handle:
        return hashlib.sha256(handle.read(max(0, length))).hexdigest()[:16]


@dataclass
class TranscriptScan:
    """一次增量扫描的结果。lines 的每一项是（该行结束处的字节偏移，该行解析出的条目）。"""

    lines: list[tuple[int, list[IngestItem]]]
    offset: int
    format: str
    session_id: str | None
    skipped: int

    @property
    def items(self) -> list[IngestItem]:
        """入参：无。出参：全部条目，按出现顺序。"""
        return [item for _, parsed in self.lines for item in parsed]


def resume_offset(path: Path, entry: dict[str, Any]) -> int:
    """入参：对话文件与上次的进度记录。出参：本次开始读取的字节偏移。

    只校验已导入过的文件头：文件被替换或截断时从头读，正常追加时接着读。
    """
    try:
        offset = int(entry.get("offset") or 0)
        head = int(entry.get("head") or 0)
    except (TypeError, ValueError):
        return 0
    if offset <= 0:
        return 0
    size = path.stat().st_size
    same_file = head <= size and entry.get("fingerprint") == _fingerprint(path, head)
    if not same_file:
        return 0
    # 文件头没变但文件变短了：是同一个文件被截断，里面没有新内容，不能从头再导入一遍。
    return min(offset, size)


def progress_entry(path: Path, offset: int, **extra: Any) -> dict[str, Any]:
    """入参：对话文件、已处理到的偏移与附加字段。出参：可保存的进度记录。"""
    # 指纹只取第一行以内的内容：文件末尾被截掉时第一行还在，仍能认出是同一个文件。
    with path.open("rb") as handle:
        first_line = len(handle.readline(FINGERPRINT_BYTES))
    head = min(FINGERPRINT_BYTES, offset, first_line)
    return {
        "offset": offset,
        "head": head,
        "fingerprint": _fingerprint(path, head),
        "updated_at": now_iso(),
        **extra,
    }


def scan_transcript(
    path: Path,
    *,
    offset: int = 0,
    fmt: str = "auto",
    session_id: str | None = None,
    include_tools: bool = False,
) -> TranscriptScan:
    """入参：对话文件、起始偏移与格式。出参：偏移之后新增的完整行解析结果。不写任何东西。"""
    scan = TranscriptScan(lines=[], offset=offset, format=fmt, session_id=session_id, skipped=0)
    auto = fmt == "auto"
    detected: str | None = None
    for raw, position in _iter_complete_lines(path, offset):
        scan.offset = position
        if len(raw) > MAX_LINE_BYTES or not raw.strip():
            scan.skipped += 1
            continue
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            scan.skipped += 1
            continue
        if not isinstance(record, dict):
            scan.skipped += 1
            continue
        # 自动识别时逐条判断格式。对话文件的第一条记录常常是摘要之类的元数据，
        # 只看第一条就定格式，会把后面所有消息都当成另一种格式而丢掉。
        line_format = _detect_format(record) if auto else fmt
        if scan.session_id is None and isinstance(record.get("sessionId"), str):
            scan.session_id = record["sessionId"]
        parsed = (
            _claude_code_items(record, include_tools)
            if line_format == "claude-code"
            else _generic_items(record)
        )
        if not parsed:
            scan.skipped += 1
            continue
        detected = detected or line_format
        scan.lines.append((position, parsed))
    if auto:
        # 还没有任何一行解析出内容时保持 auto，下次继续识别。
        scan.format = detected or "auto"
    return scan


def ingest_transcript(
    vault: Vault,
    path: Path,
    *,
    session_id: str | None = None,
    fmt: str = "auto",
    include_tools: bool = False,
    project: str | None = None,
) -> IngestResult:
    """入参：vault、对话记录文件、可选会话、格式与来源项目。出参：导入结果。

    只导入上次导入之后新增的完整行，重复调用不产生重复事件。
    """
    if fmt not in FORMATS:
        raise UsageError(f"未知对话格式: {fmt}", hint=f"可选：{', '.join(FORMATS)}")
    path = path.expanduser().resolve()
    if not path.is_file():
        raise UsageError(f"对话记录不存在: {path}")
    if path.suffix.lower() == ".json":
        return _ingest_json_document(vault, path, session_id=session_id)
    with vault.writing("ingest"):
        files = _load_state(vault)
        key = str(path)
        entry = files.get(key, {})
        scan = scan_transcript(
            path,
            offset=resume_offset(path, entry),
            # 调用方明确指定的格式优先；自动识别时才沿用上次识别出的结果。
            fmt=fmt if fmt != "auto" else str(entry.get("format") or "auto"),
            session_id=session_id or entry.get("session_id"),
            include_tools=include_tools,
        )
        final_session = safe_session_id(str(scan.session_id or path.stem), fallback="ingest")
        result = IngestResult(
            path=key, session_id=final_session, format=scan.format, skipped=scan.skipped
        )
        items = scan.items
        slug = safe_project(project)
        if slug is not None:
            for item in items:
                item.meta["project"] = slug
        if items:
            written = append_events(
                vault.l0_dir,
                final_session,
                [(item.kind, item.content, item.meta, item.ts) for item in items],
                floor=seq_floor(vault, final_session),
            )
            result.captured = len(written)
        files[key] = progress_entry(path, scan.offset, format=scan.format, session_id=final_session)
        _save_state(vault, files)
    return result


def _ingest_json_document(vault: Vault, path: Path, *, session_id: str | None) -> IngestResult:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValidationError(f"对话记录不是合法 JSON: {path}: {error}", field="path") from error
    messages = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(messages, list):
        raise ValidationError(
            "JSON 对话记录必须是消息数组，或含 messages 数组的对象",
            field="path",
            hint='形如 [{"role": "user", "content": "..."}]',
        )
    final_session = safe_session_id(session_id or path.stem)
    with vault.writing("ingest"):
        files = _load_state(vault)
        key = str(path)
        done = int(files.get(key, {}).get("count") or 0)
        if done > len(messages):
            done = 0
        items = messages_to_items(messages[done:])
        result = IngestResult(path=key, session_id=final_session, format="generic")
        if items:
            written = append_events(
                vault.l0_dir,
                final_session,
                [(item.kind, item.content, item.meta, item.ts) for item in items],
                floor=seq_floor(vault, final_session),
            )
            result.captured = len(written)
        result.skipped = len(messages) - done - len(items)
        files[key] = {
            "count": len(messages),
            "format": "generic",
            "session_id": final_session,
            "updated_at": now_iso(),
        }
        _save_state(vault, files)
    return result


def ingest_messages(
    vault: Vault, session_id: str, messages: list[Any], *, project: str | None = None
) -> IngestResult:
    """入参：vault、会话、消息列表与来源项目。出参：导入结果。供 HTTP 入口直接推送对话。"""
    final_session = safe_session_id(session_id)
    items = messages_to_items(messages)
    slug = safe_project(project)
    if slug is not None:
        for item in items:
            item.meta["project"] = slug
    result = IngestResult(path="<messages>", session_id=final_session, format="generic")
    result.skipped = len(messages) - len(items)
    if not items:
        return result
    with vault.writing("ingest"):
        written = append_events(
            vault.l0_dir,
            final_session,
            [(item.kind, item.content, item.meta, item.ts) for item in items],
            floor=seq_floor(vault, final_session),
        )
    result.captured = len(written)
    return result


def ingest_paths(
    vault: Vault,
    paths: list[Path],
    *,
    session_id: str | None = None,
    fmt: str = "auto",
    include_tools: bool = False,
) -> list[IngestResult]:
    """入参：vault 与文件或目录列表。出参：逐文件导入结果。目录只取其中的 .jsonl 与 .json。"""
    targets: list[Path] = []
    for raw in paths:
        path = raw.expanduser()
        if path.is_dir():
            targets.extend(sorted(path.glob("*.jsonl")))
            targets.extend(sorted(path.glob("*.json")))
        else:
            targets.append(path)
    if session_id is not None and len(targets) > 1:
        raise UsageError("导入多个文件时不能指定 --session", hint="去掉 --session，按文件名分会话")
    return [
        ingest_transcript(
            vault, target, session_id=session_id, fmt=fmt, include_tools=include_tools
        )
        for target in targets
    ]


def ingest_hook_payload(vault: Vault, payload: dict[str, Any]) -> IngestResult | None:
    """入参：vault 与宿主 Hook 的 stdin JSON。出参：导入结果；没有对话记录路径时返回 None。"""
    raw_path = payload.get("transcript_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.is_file():
        return None
    from zymor.remote import project_name

    session = payload.get("session_id")
    raw_cwd = payload.get("cwd")
    return ingest_transcript(
        vault,
        path,
        session_id=session if isinstance(session, str) and session else None,
        fmt="claude-code",
        # 与云端上传一致：事件带上来源项目，做梦时节点才有项目归属。
        project=project_name(Path(raw_cwd)) if isinstance(raw_cwd, str) and raw_cwd else None,
    )
