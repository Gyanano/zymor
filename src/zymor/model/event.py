from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zymor.errors import ValidationError
from zymor.util.ids import is_session_id
from zymor.util.time import is_iso, now_iso, parse_iso, utc_date

EVENT_KINDS = (
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "system_note",
    "dream_note",
)

MAX_CONTENT_BYTES = 8192

EventKind = Literal[
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "system_note",
    "dream_note",
]


class EventEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    ts: str
    session_id: str
    seq: int = Field(ge=1)
    kind: EventKind
    content: str
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ts")
    @classmethod
    def _validate_ts(cls, value: str) -> str:
        if not is_iso(value):
            raise ValueError("必须是带时区的 ISO8601 文本")
        return value

    @field_validator("session_id")
    @classmethod
    def _validate_session(cls, value: str) -> str:
        if not is_session_id(value):
            raise ValueError("会话标识必须字母数字开头，允许 `._-`，长度 ≤128")
        return value

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"v0 只支持 schema_version=1，收到 {value}")
        return value


def _truncate_content(content: str) -> tuple[str, bool]:
    data = content.encode("utf-8")
    if len(data) <= MAX_CONTENT_BYTES:
        return content, False
    return data[:MAX_CONTENT_BYTES].decode("utf-8", "ignore"), True


def l0_path(l0_dir: Path, moment: datetime | None = None) -> Path:
    """入参：L0 目录与可选时间。出参：该时间对应的 L0 文件路径（UTC 日期）。"""
    return l0_dir / f"{utc_date(moment)}.jsonl"


def max_session_seq(l0_dir: Path, session_id: str) -> int:
    """入参：L0 目录与会话标识。出参：该会话跨所有日期文件的最大 seq。"""
    highest = 0
    for path in sorted(l0_dir.glob("*.jsonl")):
        for entry in iter_events(path):
            if entry.session_id == session_id and entry.seq > highest:
                highest = entry.seq
    return highest


def max_seq(l0_file: Path, session_id: str) -> int:
    """入参：L0 文件与会话标识。出参：该会话在本文件中的最大 seq（无记录为 0）。"""
    if not l0_file.is_file():
        return 0
    highest = 0
    with l0_file.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("session_id") != session_id:
                continue
            seq = record.get("seq")
            if isinstance(seq, int) and seq > highest:
                highest = seq
    return highest


def append_event(
    l0_dir: Path,
    session_id: str,
    kind: str,
    content: str,
    meta: dict[str, Any] | None = None,
    *,
    ts: str | None = None,
    seq: int | None = None,
) -> EventEntry:
    """入参：L0 目录、会话、类型、内容，可选 caller 时间与 seq。出参：追加的事件。"""
    if kind not in EVENT_KINDS:
        raise ValidationError(
            f"未知事件类型: {kind}", field="kind", hint=f"可选：{', '.join(EVENT_KINDS)}"
        )
    if not is_session_id(session_id):
        raise ValidationError("非法会话标识", field="session_id")
    if ts is not None and not is_iso(ts):
        raise ValidationError("ts 必须是带时区的 ISO8601 文本", field="ts")
    session_meta = dict(meta or {})
    clipped, truncated = _truncate_content(content)
    if truncated:
        session_meta["truncated"] = True
    moment = parse_iso(ts) if ts is not None else None
    target = l0_path(l0_dir, moment)
    target.parent.mkdir(parents=True, exist_ok=True)
    highest = max_session_seq(l0_dir, session_id)
    assigned = seq if seq is not None and seq > highest else highest + 1
    entry = EventEntry(
        schema_version=1,
        ts=ts or now_iso(),
        session_id=session_id,
        seq=assigned,
        kind=cast(EventKind, kind),
        content=clipped,
        meta=session_meta,
    )
    line = json.dumps(entry.model_dump(), ensure_ascii=False, separators=(",", ":"))
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
    return entry


def iter_events(target: Path) -> Iterator[EventEntry]:
    """入参：L0 文件路径。出参：事件迭代器。跳过不可解析的行。"""
    if not target.is_file():
        return
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield EventEntry.model_validate(json.loads(line))
            except Exception:
                continue


def read_events(l0_dir: Path, day: str | None = None) -> list[EventEntry]:
    """入参：L0 目录与可选 UTC 日期。出参：事件列表（按文件顺序）。"""
    if day is not None:
        return list(iter_events(l0_dir / f"{day}.jsonl"))
    events: list[EventEntry] = []
    for path in sorted(l0_dir.glob("*.jsonl")):
        events.extend(iter_events(path))
    return events
