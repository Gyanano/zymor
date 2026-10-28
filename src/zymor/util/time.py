from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    """入参：无。出参：当前 UTC 时间（aware）。统一时间基准。"""
    return datetime.now(tz=UTC)


def now_iso() -> str:
    """入参：无。出参：本地时区 ISO8601 文本（秒精度）。用于写入时间字段。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """入参：ISO8601 文本。出参：带时区 datetime。缺时区或不可解析时抛 ValueError。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"缺少时区: {value}")
    return dt


def is_iso(value: object) -> bool:
    """入参：任意值。出参：是否为合法的带时区 ISO8601 文本。"""
    if not isinstance(value, str):
        return False
    try:
        parse_iso(value)
    except ValueError:
        return False
    return True


def utc_date(value: datetime | None = None) -> str:
    """入参：可选时间。出参：UTC 日期 `YYYY-MM-DD`。L0 按 UTC 日期滚动。"""
    moment = value or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d")


def utc_date_of_iso(value: str) -> str:
    """入参：ISO8601 文本。出参：对应的 UTC 日期。"""
    return utc_date(parse_iso(value))


def days_between(older: str, newer: str) -> float:
    """入参：两个 ISO8601 文本。出参：间隔天数（newer - older）。"""
    delta = parse_iso(newer) - parse_iso(older)
    return delta.total_seconds() / 86400.0


def days_ago_iso(days: float) -> str:
    """入参：天数。出参：days 天前的时间文本（本地时区）。"""
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
