from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from zymor.errors import LockedError
from zymor.model.event import EventEntry, iter_events
from zymor.util.fs import atomic_write_text
from zymor.util.time import is_iso, now_iso, parse_iso, utc_now
from zymor.vault import Vault

WATERMARKS_VERSION = 1
LOCK_STALE_SECONDS = 2 * 3600


@dataclass
class Watermarks:
    schema_version: int = WATERMARKS_VERSION
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_success_at: str | None = None
    updated_at: str = ""

    @classmethod
    def load(cls, vault: Vault) -> Watermarks:
        """入参：vault。出参：水位线对象。文件缺失或损坏时按空水位线处理。"""
        path = vault.watermarks_path
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        sessions: dict[str, dict[str, Any]] = {}
        raw_sessions = data.get("sessions")
        if isinstance(raw_sessions, dict):
            for session_id, details in raw_sessions.items():
                if isinstance(details, dict):
                    sessions[str(session_id)] = dict(details)
        last_success = data.get("last_success_at")
        return cls(
            schema_version=int(data.get("schema_version") or WATERMARKS_VERSION),
            sessions=sessions,
            last_success_at=last_success if isinstance(last_success, str) else None,
            updated_at=str(data.get("updated_at") or ""),
        )

    def save(self, vault: Vault) -> None:
        """入参：vault。出参：无。原子写回 watermarks.json。"""
        self.updated_at = now_iso()
        payload = {
            "schema_version": self.schema_version,
            "sessions": self.sessions,
            "last_success_at": self.last_success_at,
            "updated_at": self.updated_at,
        }
        atomic_write_text(
            vault.watermarks_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def last_seq(self, session_id: str) -> int:
        """入参：会话标识。出参：该会话已处理到的最大 seq（未处理为 0）。"""
        details = self.sessions.get(session_id) or {}
        value = details.get("last_seq")
        return int(value) if isinstance(value, int) else 0

    def advance(self, session_id: str, seq: int) -> None:
        """入参：会话标识与已处理 seq。出参：无。推进该会话水位线。"""
        details = self.sessions.setdefault(session_id, {})
        current = details.get("last_seq")
        if not isinstance(current, int) or seq > current:
            details["last_seq"] = seq
        details["last_success_at"] = now_iso()

    def mark_success(self) -> None:
        """入参：无。出参：无。记录一次成功 run 的时间。"""
        self.last_success_at = now_iso()


def load_unprocessed_events(vault: Vault, watermarks: Watermarks, limit: int) -> list[EventEntry]:
    """入参：vault、水位线与上限。出参：水位线之后的事件（按 ts 排序，最多 limit 条）。"""
    events: list[EventEntry] = []
    for path in sorted(vault.l0_dir.glob("*.jsonl")):
        for entry in iter_events(path):
            if entry.seq > watermarks.last_seq(entry.session_id):
                events.append(entry)
    events.sort(key=lambda item: (item.ts, item.seq))
    return events[: max(0, limit)]


class DreamLock:
    def __init__(self, path: Path, payload: dict[str, Any]) -> None:
        self.path = path
        self.payload = payload

    @classmethod
    def acquire(cls, vault: Vault, *, command: str = "dream") -> DreamLock:
        """入参：vault 与命令名。出参：持有的 `.dream.lock`。锁被占用时抛 LockedError。"""
        lock_path = vault.root / ".dream.lock"
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": now_iso(),
            "command": command,
        }
        for _ in range(2):
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as error:
                holder = cls._read_holder(lock_path)
                if holder is not None and cls._is_stale(holder):
                    lock_path.unlink(missing_ok=True)
                    continue
                holder_text = (
                    f"pid={holder.get('pid')} host={holder.get('host')} "
                    f"started={holder.get('started_at')}"
                )
                raise LockedError(
                    f"另一个做梦任务正在运行: {holder_text}",
                    holder=holder,
                ) from error
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
            return cls(lock_path, payload)
        raise LockedError(
            "无法获取做梦锁：存在无法清理的 stale 锁", hint="手工删除 .dream.lock 后重试"
        )

    @staticmethod
    def _read_holder(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _is_stale(holder: dict[str, Any]) -> bool:
        started = holder.get("started_at")
        if not isinstance(started, str) or not is_iso(started):
            return True
        try:
            age = (utc_now() - parse_iso(started)).total_seconds()
        except ValueError:
            return True
        return age > LOCK_STALE_SECONDS

    def release(self) -> None:
        """入参：无。出参：无。释放做梦锁（仅当仍属于本进程时删除）。"""
        holder = self._read_holder(self.path) or {}
        if holder.get("pid") == self.payload.get("pid") and holder.get(
            "started_at"
        ) == self.payload.get("started_at"):
            self.path.unlink(missing_ok=True)


def run_dir(vault: Vault) -> Path:
    """入参：vault。出参：`dream/runs` 目录。"""
    return vault.runs_dir


def run_record_path(vault: Vault, run_id: str) -> Path:
    """入参：vault 与 run id。出参：run 记录路径。"""
    return vault.runs_dir / f"{run_id}.json"


def write_run_record(vault: Vault, run_id: str, record: dict[str, Any]) -> Path:
    """入参：vault、run id 与记录。出参：写入路径。原子写 `dream/runs/<run_id>.json`。"""
    path = run_record_path(vault, run_id)
    atomic_write_text(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return path


def append_dream_log(vault: Vault, record: dict[str, Any]) -> None:
    """入参：vault 与日志记录。出参：无。追加一行 `dream/log.jsonl`。"""
    path = vault.dream_log_path
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")


def cleanup_l0(vault: Vault, retention_days: int) -> list[str]:
    """入参：vault 与保留天数。出参：被删除的 L0 文件名。只删超过保留期的日期文件。"""
    if retention_days <= 0:
        return []
    threshold = (utc_now() - timedelta(days=retention_days)).date()
    removed: list[str] = []
    for path in sorted(vault.l0_dir.glob("*.jsonl")):
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < threshold:
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return removed


def utc_stamp(moment: datetime | None = None) -> str:
    """入参：可选时间。出参：UTC `YYYYMMDD_HHMMSS` 时间戳。拼 run id 用。"""
    value = (moment or utc_now()).astimezone(UTC)
    return value.strftime("%Y%m%d_%H%M%S")


def find_stale_focuses(vault: Vault, *, idle_hours: int) -> list[str]:
    """入参：vault 与闲置小时数。出参：已关闭未建胶囊、或闲置超时的焦点会话 id 列表。"""
    from zymor.model.focus import FocusFile
    from zymor.store.index import Index

    index = Index(vault.root)
    rows = index.layer_rows("L2", statuses=["hypothesis", "verified", "disputed"], limit=500)
    capsule_titles = {str(row["title"]) for row in rows}
    focuses: list[str] = []
    threshold = utc_now() - timedelta(hours=idle_hours)
    for path in sorted(vault.focus_dir.glob("*.md")):
        try:
            focus = FocusFile.from_markdown(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if focus.status == "closed" and focus.task not in capsule_titles:
            focuses.append(focus.session_id)
            continue
        if focus.status == "active":
            try:
                updated = parse_iso(focus.updated_at)
            except ValueError:
                continue
            if updated < threshold:
                focuses.append(focus.session_id)
    return focuses


def log_action(
    vault: Vault,
    run_id: str,
    phase: str,
    action: str,
    *,
    node_id: str | None = None,
    details: dict[str, Any] | str | None = None,
    tokens: int = 0,
    usd: float = 0.0,
) -> None:
    """入参：vault、run、阶段与动作。出参：无。追加一条 `dream/log.jsonl` 审计。"""
    record: dict[str, Any] = {
        "run_id": run_id,
        "ts": now_iso(),
        "phase": phase,
        "action": action,
        "details": details if details is not None else {},
        "tokens": tokens,
        "usd": usd,
    }
    if node_id is not None:
        record["node_id"] = node_id
    append_dream_log(vault, record)
