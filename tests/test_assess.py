"""Dose bounds and verdicts."""

from __future__ import annotations

from app.assess import (
    Placement,
    STATUS_NOT_OVER,
    STATUS_OVER,
    STATUS_PENDING,
    affected_exhibits_by_dose_change,
    evaluate,
    split_hours,
)


def hour(day: int = 1, h: int = 0) -> int:
    """Epoch minute at the start of an hour in May 2026 (UTC)."""

    from app.timeutil import parse_utc_minute

    return parse_utc_minute(f"2026-05-{day:02d}T{h:02d}:00Z")


def test_split_hours_boundaries():
    start = hour(1, 9) + 15
    assert split_hours(start, start + 90) == [
        (hour(1, 9), 45),
        (hour(1, 10), 45),
    ]
    # Contiguous, inclusive start/exclusive end: exactly 60 minutes in one hour.
    assert split_hours(hour(1, 9), hour(1, 10)) == [(hour(1, 9), 60)]
    # Single minute at the last minute of an hour.
    assert split_hours(hour(1, 9) + 59, hour(1, 9) + 60) == [(hour(1, 9), 1)]


def test_full_hour_dose_is_exact():
    placements = [Placement("E1", "C1", hour(1, 9), hour(1, 10))]
    result = evaluate("E1", placements, {("C1", hour(1, 9)): 420.0}, 50.0, 1000.0)
    assert (result.dose_low, result.dose_high) == (420.0, 420.0)
    assert result.status == STATUS_NOT_OVER


def test_partial_hour_bounds():
    # 30 of 60 minutes, measured total 420, cap 10 lx -> [120, 300].
    placements = [Placement("E1", "C1", hour(1, 9) + 10, hour(1, 9) + 40)]
    result = evaluate("E1", placements, {("C1", hour(1, 9)): 420.0}, 10.0, 200.0)
    assert (result.dose_low, result.dose_high) == (120.0, 300.0)
    assert result.status == STATUS_PENDING


def test_verdict_thresholds_inclusive():
    placements = [Placement("E1", "C1", hour(1, 9), hour(1, 10))]
    doses = {("C1", hour(1, 9)): 500.0}
    assert evaluate("E1", placements, doses, 50.0, 500.0).status == STATUS_NOT_OVER
    assert evaluate("E1", placements, doses, 50.0, 499.0).status == STATUS_OVER


def test_over_is_decided_by_lower_bound():
    # 10 of 60 minutes, total 500, cap 50: lower = max(0, 500-50*50)=0 -> pending
    placements = [Placement("E1", "C1", hour(1, 9), hour(1, 9) + 10)]
    assert (
        evaluate("E1", placements, {("C1", hour(1, 9)): 500.0}, 50.0, 1.0).status
        == STATUS_PENDING
    )
    # 55 of 60 minutes, total 500: lower = 500 - 50*5 = 250 > 200 -> over
    placements = [Placement("E1", "C1", hour(1, 9), hour(1, 9) + 55)]
    assert (
        evaluate("E1", placements, {("C1", hour(1, 9)): 500.0}, 50.0, 200.0).status
        == STATUS_OVER
    )


def test_missing_hour_uses_range_only():
    placements = [Placement("E1", "C1", hour(1, 9) + 30, hour(1, 10) + 30)]
    result = evaluate("E1", placements, {}, 50.0, 1500.0)
    assert (result.dose_low, result.dose_high) == (0.0, 3000.0)
    assert result.status == STATUS_PENDING
    assert result.missing_hours == 2
    # With a huge threshold the upper bound alone proves compliance.
    assert evaluate("E1", placements, {}, 50.0, 5000.0).status == STATUS_NOT_OVER


def test_repeat_visits_share_hourly_total():
    # Two visits to the same cabinet inside one hour: 15 + 15 = 30 minutes.
    placements = [
        Placement("E1", "C1", hour(1, 9), hour(1, 9) + 15),
        Placement("E1", "C1", hour(1, 9) + 40, hour(1, 9) + 55),
    ]
    result = evaluate("E1", placements, {("C1", hour(1, 9)): 600.0}, 50.0, 2000.0)
    assert len(result.hours) == 1
    assert result.hours[0].minutes == 30
    assert (result.dose_low, result.dose_high) == (0.0, 600.0)
    # The bound must not double count the shared 600 lx·min.
    assert result.total_minutes == 30


def test_two_cabinets_same_hour_count_separately():
    placements = [
        Placement("E1", "C1", hour(1, 9), hour(1, 9) + 30),
        Placement("E1", "C2", hour(1, 9), hour(1, 9) + 30),
    ]
    result = evaluate(
        "E1",
        placements,
        {("C1", hour(1, 9)): 300.0, ("C2", hour(1, 9)): 300.0},
        50.0,
        10_000.0,
    )
    # Each cabinet-hour is its own budget: 0 + 0 lower, 300 + 300 upper.
    assert (result.dose_low, result.dose_high) == (0.0, 600.0)
    assert result.total_hours == 2
    assert result.total_minutes == 60


def test_affected_exhibits_only_touched_hours():
    placements = [
        Placement("E1", "C1", hour(1, 9), hour(1, 10)),
        Placement("E2", "C2", hour(1, 9), hour(1, 10)),
        Placement("E3", "C1", hour(1, 10), hour(1, 11)),
    ]
    old = {("C1", hour(1, 9)): 100.0, ("C1", hour(1, 10)): 100.0}
    new = {("C1", hour(1, 9)): 200.0, ("C1", hour(1, 10)): 100.0}
    assert affected_exhibits_by_dose_change(old, new, placements) == {"E1"}


def test_affected_handles_new_and_removed_hours():
    placements = [Placement("E1", "C1", hour(1, 9), hour(1, 10))]
    assert affected_exhibits_by_dose_change(
        {}, {("C1", hour(1, 9)): 5.0}, placements
    ) == {"E1"}
    assert affected_exhibits_by_dose_change(
        {("C1", hour(1, 9)): 5.0}, {}, placements
    ) == {"E1"}
    assert affected_exhibits_by_dose_change(
        {("C1", hour(1, 9)): 5.0},
        {("C1", hour(1, 9)): 5.0, ("C2", hour(1, 9)): 9.0},
        placements,
    ) == set()
