"""UTC minute helpers.

All timestamps in the database are stored as integer minutes since the Unix
epoch (UTC).  CSV input/output uses ISO 8601 minute strings, e.g.
``2026-05-01T09:05Z``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

ISO_HINT = "2026-05-01T09:05Z"


def parse_utc_minute(value: str) -> int:
    """Parse an ISO-8601 UTC timestamp and return epoch minutes.

    Naive timestamps are interpreted as UTC (the whole app is UTC-only).
    Seconds/microseconds must be zero: the source data has minute granularity.
    """

    raw = value.strip()
    if not raw:
        raise ValueError("时间不能为空")

    text = raw
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"无法解析 UTC 时间“{raw}”，请使用 ISO 8601 整分钟格式（如 {ISO_HINT}）"
        ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        if dt.utcoffset() != timedelta(0):
            raise ValueError(f"时间“{raw}”不是 UTC 时区，请换算为 UTC 再上报")
        dt = dt.astimezone(timezone.utc)

    if dt.second or dt.microsecond:
        raise ValueError(f"时间“{raw}”必须精确到整分钟（秒和微秒须为 0）")

    return int(dt.timestamp()) // 60


def format_minute(minute: int) -> str:
    """Format epoch minutes as a human-readable UTC label."""

    dt = datetime.fromtimestamp(minute * 60, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def format_hour(hour_minute: int) -> str:
    """Format the epoch minute at the start of an hour as a label."""

    return format_minute(hour_minute)


def now_iso() -> str:
    """Current UTC time as a compact ISO-8601 string."""

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def format_iso(value: str | None) -> str:
    """Render a stored ISO timestamp for display; pass through unknown values."""

    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
