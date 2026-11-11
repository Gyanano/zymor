"""Pending candidate pool: single-occurrence candidates wait here for a later recurrence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zymor.util.fs import atomic_write_text
from zymor.util.time import days_ago_iso, now_iso, parse_iso
from zymor.vault import Vault

DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_ENTRIES = 500
FIELDS = (
    "title",
    "summary",
    "body",
    "category",
    "layer",
    "confidence",
    "evidence",
    "tags",
    "links",
    "session_id",
)


def pending_path(vault: Vault) -> Path:
    """入参：vault。出参：待定候选池文件路径。"""
    return vault.dream_dir / "pending.jsonl"


@dataclass
class PendingPool:
    entries: list[dict[str, Any]] = field(default_factory=list)
    dirty: bool = False

    @classmethod
    def load(cls, vault: Vault, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> PendingPool:
        """入参：vault 与保留天数。出参：候选池。过期或损坏的记录在加载时丢弃。"""
        path = pending_path(vault)
        if not path.is_file():
            return cls()
        threshold = parse_iso(days_ago_iso(max(1, retention_days)))
        entries: list[dict[str, Any]] = []
        dirty = False
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return cls()
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                seen = parse_iso(str(record["first_seen"]))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                dirty = True
                continue
            if not isinstance(record, dict) or any(name not in record for name in FIELDS):
                dirty = True
                continue
            if seen < threshold:
                dirty = True
                continue
            entries.append(record)
        return cls(entries=entries, dirty=dirty)

    def add(self, record: dict[str, Any], *, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        """入参：候选记录与容量上限。出参：无。同一会话同一批证据的旧记录被替换，超出容量丢最旧的。"""
        pointers = set(record.get("evidence") or [])
        kept: list[dict[str, Any]] = []
        for entry in self.entries:
            same_session = entry.get("session_id") == record.get("session_id")
            overlapping = bool(pointers & set(entry.get("evidence") or []))
            same_title = entry.get("title") == record.get("title")
            if same_session and (overlapping or same_title):
                continue
            kept.append(entry)
        kept.append({**record, "first_seen": now_iso()})
        overflow = len(kept) - max(1, max_entries)
        self.entries = kept[overflow:] if overflow > 0 else kept
        self.dirty = True

    def drop_batch(self, session_id: str, seq_range: tuple[int, int]) -> int:
        """入参：会话与一批事件的 seq 范围。出参：移除的记录数。

        同一批事件被重新消化时（上次 run 中途失败），它上次留下的待定记录要先清掉：
        这批事件会被重新提取一遍，旧记录留着就会被当成「另一次出现」。
        判断依据是 seq 范围而不是标题，因为 LLM 两次提取的措辞不一定相同。
        """
        low, high = seq_range
        kept: list[dict[str, Any]] = []
        for entry in self.entries:
            seq_min, seq_max = entry.get("seq_min"), entry.get("seq_max")
            overlapping = (
                entry.get("session_id") == session_id
                and isinstance(seq_min, int)
                and isinstance(seq_max, int)
                and seq_min <= high
                and seq_max >= low
            )
            if not overlapping:
                kept.append(entry)
        removed = len(self.entries) - len(kept)
        if removed:
            self.entries = kept
            self.dirty = True
        return removed

    def remove(self, records: list[dict[str, Any]]) -> None:
        """入参：要移除的记录。出参：无。"""
        if not records:
            return
        targets = {id(record) for record in records}
        self.entries = [entry for entry in self.entries if id(entry) not in targets]
        self.dirty = True

    def save(self, vault: Vault) -> None:
        """入参：vault。出参：无。有变化时原子写回。"""
        if not self.dirty:
            return
        path = pending_path(vault)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
            for entry in self.entries
        )
        atomic_write_text(path, text)
        self.dirty = False
