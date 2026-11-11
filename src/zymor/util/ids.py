from __future__ import annotations

import re
import secrets
from datetime import datetime

from zymor.util.time import utc_date

MEMORY_ID_RE = re.compile(r"^mem_(\d{8})_([0-9a-f]{10})$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def new_memory_id(now: datetime | None = None) -> str:
    """入参：可选时间。出参：`mem_<UTC YYYYMMDD>_<10 位小写 hex>` 标识。生成记忆节点 id。"""
    return f"mem_{utc_date(now).replace('-', '')}_{secrets.token_hex(5)}"


def is_memory_id(value: object) -> bool:
    """入参：任意值。出参：是否为合法的记忆节点 id。"""
    return isinstance(value, str) and MEMORY_ID_RE.match(value) is not None


def is_session_id(value: object) -> bool:
    """入参：任意值。出参：是否为合法的会话标识（字母数字开头，允许 `._-`）。"""
    return isinstance(value, str) and SESSION_ID_RE.match(value) is not None


def new_run_id(now: datetime | None = None) -> str:
    """入参：可选时间。出参：`dream_<UTC YYYYMMDD>_<8 位 hex>`。做梦运行标识。"""
    return f"dream_{utc_date(now).replace('-', '')}_{secrets.token_hex(4)}"


def safe_session_id(raw: str, fallback: str = "ingest") -> str:
    """入参：任意会话名。出参：合法的会话标识。"""
    cleaned = NAME_UNSAFE_RE.sub("-", raw.strip()).strip("-._")[:128]
    if cleaned and is_session_id(cleaned):
        return cleaned
    return fallback


def safe_project(raw: object) -> str | None:
    """入参：任意项目名。出参：合法的项目 slug；无法得到合法名称时返回 None。"""
    if not isinstance(raw, str):
        return None
    cleaned = NAME_UNSAFE_RE.sub("-", raw.strip()).strip("-._")[:64]
    return cleaned or None
