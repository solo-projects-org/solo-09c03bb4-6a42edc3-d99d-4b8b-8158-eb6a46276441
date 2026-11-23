"""CSV parsing and whole-report rejection rules."""

from __future__ import annotations

import pytest

from app.csvio import parse_doses, parse_placements
from app.errors import ReportError
from app.timeutil import parse_utc_minute


def csv_bytes(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_parse_placements_ok():
    rows = parse_placements(
        csv_bytes(
            "exhibit_id,cabinet_id,start,end",
            "E1,C1,2026-05-01T09:00Z,2026-05-01T10:30Z",
            "E2,C2,2026-05-01T09:00Z,2026-05-01T09:01Z",
        )
    )
    assert len(rows) == 2
    assert rows[0].start_min == parse_utc_minute("2026-05-01T09:00Z")
    assert rows[0].end_min - rows[0].start_min == 90


def test_placement_overlap_rejected_within_exhibit():
    with pytest.raises(ReportError, match="重叠"):
        parse_placements(
            csv_bytes(
                "exhibit_id,cabinet_id,start,end",
                "E1,C1,2026-05-01T09:00Z,2026-05-01T10:00Z",
                "E1,C2,2026-05-01T09:30Z,2026-05-01T10:30Z",
            )
        )


def test_placement_touching_intervals_allowed():
    rows = parse_placements(
        csv_bytes(
            "exhibit_id,cabinet_id,start,end",
            "E1,C1,2026-05-01T09:00Z,2026-05-01T10:00Z",
            "E1,C1,2026-05-01T10:00Z,2026-05-01T10:20Z",
        )
    )
    assert len(rows) == 2


def test_different_exhibits_may_share_a_cabinet():
    rows = parse_placements(
        csv_bytes(
            "exhibit_id,cabinet_id,start,end",
            "E1,C1,2026-05-01T09:00Z,2026-05-01T10:00Z",
            "E2,C1,2026-05-01T09:00Z,2026-05-01T10:00Z",
        )
    )
    assert len(rows) == 2


@pytest.mark.parametrize(
    "row, message",
    [
        ("E1,C1,2026-05-01T10:00Z,2026-05-01T09:00Z", "end 必须晚于 start"),
        ("E1,C1,2026-05-01T09:00Z,2026-05-01T09:00Z", "end 必须晚于 start"),
        ("E1,C1,2026-05-01T09:00Z,2026-05-01T09:00:30Z", "整分钟"),
        ("E1,C1,2026-05-01T09:00+08:00,2026-05-01T10:00+08:00", "UTC"),
        ("E1,,2026-05-01T09:00Z,2026-05-01T10:00Z", "cabinet_id 不能为空"),
        (",C1,2026-05-01T09:00Z,2026-05-01T10:00Z", "exhibit_id 不能为空"),
    ],
)
def test_placement_row_rejections(row, message):
    with pytest.raises(ReportError, match=message):
        parse_placements(
            csv_bytes("exhibit_id,cabinet_id,start,end", row)
        )


def test_bad_header_rejected():
    with pytest.raises(ReportError, match="表头"):
        parse_placements(csv_bytes("id,cabinet,start,end", "E1,C1,a,b"))
    with pytest.raises(ReportError, match="表头"):
        parse_doses(csv_bytes("cabinet,hour,dose", "C1,2026-05-01T09:00Z,10"), 10)


def test_empty_placement_table_rejected():
    with pytest.raises(ReportError, match="没有数据行"):
        parse_placements(csv_bytes("exhibit_id,cabinet_id,start,end"))


def test_parse_doses_ok():
    rows = parse_doses(
        csv_bytes(
            "cabinet_id,hour,dose_luxmin",
            "C1,2026-05-01T09:00Z,0",
            "C1,2026-05-01T10:00Z,600",
        ),
        10.0,
    )
    assert [r.dose for r in rows] == [0.0, 600.0]


def test_dose_negative_rejected():
    with pytest.raises(ReportError, match="负"):
        parse_doses(
            csv_bytes("cabinet_id,hour,dose_luxmin", "C1,2026-05-01T09:00Z,-1"),
            10.0,
        )


def test_dose_above_cap_times_sixty_rejected():
    # cap 10 lx -> at most 600 lx·min per hour.
    with pytest.raises(ReportError, match="超过照度上限"):
        parse_doses(
            csv_bytes("cabinet_id,hour,dose_luxmin", "C1,2026-05-01T09:00Z,600.0001"),
            10.0,
        )
    parse_doses(
        csv_bytes("cabinet_id,hour,dose_luxmin", "C1,2026-05-01T09:00Z,600"),
        10.0,
    )


def test_dose_hour_must_be_whole_hour():
    with pytest.raises(ReportError, match="整点"):
        parse_doses(
            csv_bytes("cabinet_id,hour,dose_luxmin", "C1,2026-05-01T09:30Z,100"),
            10.0,
        )


def test_dose_duplicate_cabinet_hour_rejected():
    with pytest.raises(ReportError, match="重复"):
        parse_doses(
            csv_bytes(
                "cabinet_id,hour,dose_luxmin",
                "C1,2026-05-01T09:00Z,100",
                "C1,2026-05-01T09:00Z,200",
            ),
            10.0,
        )


def test_duplicate_placement_row_rejected():
    with pytest.raises(ReportError, match="重复登记"):
        parse_placements(
            csv_bytes(
                "exhibit_id,cabinet_id,start,end",
                "E1,C1,2026-05-01T09:00Z,2026-05-01T10:00Z",
                "E1,C1,2026-05-01T09:00Z,2026-05-01T10:00Z",
            )
        )


def test_naive_timestamp_treated_as_utc():
    rows = parse_placements(
        csv_bytes(
            "exhibit_id,cabinet_id,start,end",
            "E1,C1,2026-05-01T09:00,2026-05-01T10:00",
        )
    )
    assert rows[0].start_min == parse_utc_minute("2026-05-01T09:00Z")
