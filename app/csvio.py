"""CSV parsing and whole-report validation.

Two CSV kinds are accepted:

* placements: ``exhibit_id,cabinet_id,start,end`` with inclusive start and
  exclusive end (UTC, minute precision);
* doses: ``cabinet_id,hour,dose_luxmin`` where ``hour`` is the UTC hour start.

Parsing is intentionally strict: any malformed row rejects the whole file, as
required ("整表退回").
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Iterable

from .errors import ReportError
from .timeutil import ISO_HINT, parse_utc_minute

PLACEMENT_HEADERS = ("exhibit_id", "cabinet_id", "start", "end")
DOSE_HEADERS = ("cabinet_id", "hour", "dose_luxmin")


@dataclass(frozen=True)
class PlacementRow:
    exhibit_id: str
    cabinet_id: str
    start_min: int
    end_min: int
    line: int


@dataclass(frozen=True)
class DoseRow:
    cabinet_id: str
    hour_min: int
    dose: float
    line: int


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ReportError("CSV 文件不是 UTF-8 或 GB18030 编码，无法读取")


def _to_float(text: str, field: str, line: int) -> float:
    try:
        value = float(text.strip())
    except (TypeError, ValueError):
        raise ReportError(f"第 {line} 行：{field}“{text}”不是数字") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise ReportError(f"第 {line} 行：{field} 不是有限数字")
    return value


def format_luxmin(value: float) -> str:
    """Format a lux-minute quantity without trailing float noise."""

    if value == int(value):
        return f"{int(value)}"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def parse_placements(raw: bytes) -> list[PlacementRow]:
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    try:
        header = tuple(h.strip() for h in next(reader))
    except StopIteration:
        raise ReportError("摆放表为空文件") from None

    if header != PLACEMENT_HEADERS:
        raise ReportError(
            "摆放表表头应为 “exhibit_id,cabinet_id,start,end”，"
            f"实际为 “{','.join(header)}”"
        )

    rows: list[PlacementRow] = []
    seen: dict[tuple[str, str, int, int], int] = {}
    for line, record in enumerate(reader, start=2):
        if not record or all(not cell.strip() for cell in record):
            continue
        if len(record) != 4:
            raise ReportError(
                f"第 {line} 行：应有 4 列，实际 {len(record)} 列"
            )
        exhibit_id, cabinet_id, start_text, end_text = (
            cell.strip() for cell in record
        )
        if not exhibit_id:
            raise ReportError(f"第 {line} 行：exhibit_id 不能为空")
        if not cabinet_id:
            raise ReportError(f"第 {line} 行：cabinet_id 不能为空")
        start_min = _parse_time(start_text, "start", line)
        end_min = _parse_time(end_text, "end", line)
        if end_min <= start_min:
            raise ReportError(
                f"第 {line} 行：end 必须晚于 start（{start_text} → {end_text}）"
            )
        key = (exhibit_id, cabinet_id, start_min, end_min)
        if key in seen:
            raise ReportError(
                f"第 {line} 行：与第 {seen[key]} 行的摆放完全相同"
                f"（{exhibit_id} 在 {cabinet_id} 的同一时段重复登记），整表退回"
            )
        seen[key] = line
        rows.append(
            PlacementRow(exhibit_id, cabinet_id, start_min, end_min, line)
        )

    if not rows:
        raise ReportError("摆放表没有数据行")

    _check_overlaps(rows)
    return rows


def _parse_time(text: str, field: str, line: int) -> int:
    try:
        return parse_utc_minute(text)
    except ValueError as exc:
        raise ReportError(f"第 {line} 行：{field} {exc}") from None


def _check_overlaps(rows: Iterable[PlacementRow]) -> None:
    """Reject overlapping intervals belonging to the same exhibit.

    Touching intervals (one ends exactly when the next begins) are allowed;
    a zero-length interval is impossible because end > start is enforced.
    """

    by_exhibit: dict[str, list[PlacementRow]] = {}
    for row in rows:
        by_exhibit.setdefault(row.exhibit_id, []).append(row)

    for exhibit_id, items in by_exhibit.items():
        items.sort(key=lambda r: (r.start_min, r.end_min, r.cabinet_id))
        for previous, current in zip(items, items[1:]):
            if current.start_min < previous.end_min:
                raise ReportError(
                    "摆放表同一展品时段重叠："
                    f"展品 {exhibit_id} 第 {previous.line} 行"
                    f"（{_span(previous)}）与第 {current.line} 行"
                    f"（{_span(current)}）重叠"
                )


def _span(row: PlacementRow) -> str:
    from .timeutil import format_minute

    return f"{format_minute(row.start_min)} → {format_minute(row.end_min)}"


def parse_doses(raw: bytes, illuminance_cap: float) -> list[DoseRow]:
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    try:
        header = tuple(h.strip() for h in next(reader))
    except StopIteration:
        raise ReportError("剂量表为空文件") from None

    if header != DOSE_HEADERS:
        raise ReportError(
            "剂量表表头应为 “cabinet_id,hour,dose_luxmin”，"
            f"实际为 “{','.join(header)}”"
        )

    rows: list[DoseRow] = []
    seen: dict[tuple[str, int], int] = {}
    max_dose = illuminance_cap * 60.0
    for line, record in enumerate(reader, start=2):
        if not record or all(not cell.strip() for cell in record):
            continue
        if len(record) != 3:
            raise ReportError(f"第 {line} 行：应有 3 列，实际 {len(record)} 列")
        cabinet_id, hour_text, dose_text = (cell.strip() for cell in record)
        if not cabinet_id:
            raise ReportError(f"第 {line} 行：cabinet_id 不能为空")
        hour_min = _parse_hour(hour_text, line)
        dose = _to_float(dose_text, "dose_luxmin", line)
        if dose < 0:
            raise ReportError(
                f"第 {line} 行：小时剂量为负（{dose_text}），整表退回"
            )
        if dose > max_dose + 1e-9:
            raise ReportError(
                f"第 {line} 行：小时剂量 {dose_text} 超过照度上限 {illuminance_cap:g}"
                f" 勒克斯 × 60 分钟 = {format_luxmin(max_dose)}，整表退回"
            )
        key = (cabinet_id, hour_min)
        if key in seen:
            raise ReportError(
                f"第 {line} 行：柜子 {cabinet_id} 的同一小时重复出现"
                f"（第 {seen[key]} 行已记录），整表退回"
            )
        seen[key] = line
        rows.append(DoseRow(cabinet_id, hour_min, dose, line))

    if not rows:
        raise ReportError("剂量表没有数据行")

    return rows


def _parse_hour(text: str, line: int) -> int:
    try:
        minute = parse_utc_minute(text)
    except ValueError as exc:
        raise ReportError(f"第 {line} 行：hour {exc}") from None
    if minute % 60 != 0:
        raise ReportError(
            f"第 {line} 行：hour “{text}”不是整点（需为 UTC 整小时，如 2026-05-01T09:00Z）"
        )
    return minute


def sample_placements_csv() -> str:
    return (
        "exhibit_id,cabinet_id,start,end\n"
        "MS-001,CAB-A,2026-05-01T09:00Z,2026-05-01T11:30Z\n"
        "MS-001,CAB-A,2026-05-01T11:40Z,2026-05-01T12:00Z\n"
        "MS-002,CAB-A,2026-05-01T09:15Z,2026-05-01T09:45Z\n"
        "MS-003,CAB-B,2026-05-01T09:00Z,2026-05-01T10:00Z\n"
    )


def sample_doses_csv() -> str:
    return (
        "cabinet_id,hour,dose_luxmin\n"
        "CAB-A,2026-05-01T09:00Z,420\n"
        "CAB-A,2026-05-01T10:00Z,380\n"
        "CAB-A,2026-05-01T11:00Z,450\n"
        "CAB-A,2026-05-01T12:00Z,200\n"
        "CAB-B,2026-05-01T09:00Z,500\n"
    )


__all__ = [
    "PlacementRow",
    "DoseRow",
    "parse_placements",
    "parse_doses",
    "sample_placements_csv",
    "sample_doses_csv",
    "PLACEMENT_HEADERS",
    "DOSE_HEADERS",
    "ISO_HINT",
]
