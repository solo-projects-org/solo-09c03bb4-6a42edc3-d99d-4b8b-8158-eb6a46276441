"""Split-submission replay: scoping and the four-scenario verdict table."""

from __future__ import annotations

from app.assess import (
    STATUS_NOT_OVER,
    STATUS_OVER,
    STATUS_PENDING,
    Placement,
)
from app.replay import (
    SCENARIO_DOSE_ONLY,
    SCENARIO_PLACEMENT_ONLY,
    build_review,
    replay_scope,
)


def hour(day: int = 1, h: int = 0) -> int:
    from app.timeutil import parse_utc_minute

    return parse_utc_minute(f"2026-05-{day:02d}T{h:02d}:00Z")


def placement(exhibit, cabinet, h0, h1, day=1) -> Placement:
    return Placement(exhibit, cabinet, hour(day, h0), hour(day, h1))


# --------------------------------------------------------------------- scoping
def test_scope_covers_placement_changes_and_dose_touch_hours():
    old_placements = [
        placement("E1", "C1", 9, 11),
        placement("E2", "C1", 9, 10),
        placement("E3", "C2", 9, 10),
    ]
    # E1's rows change; E2/E3 keep identical placements.
    new_placements = [
        placement("E1", "C1", 9, 10),
        placement("E2", "C1", 9, 10),
        placement("E3", "C2", 9, 10),
    ]
    old_doses = {
        ("C1", hour(1, 9)): 100.0,
        ("C1", hour(1, 10)): 100.0,
        ("C2", hour(1, 9)): 100.0,
    }
    # C2 09:00 changes -> E3 in scope even though its placements are identical.
    new_doses = dict(old_doses)
    new_doses[("C2", hour(1, 9))] = 200.0

    assert replay_scope(
        old_placements, new_placements, old_doses, new_doses
    ) == {"E1", "E3"}


def test_scope_dose_addition_and_deletion_count_as_change():
    placements = [placement("E1", "C1", 9, 10)]
    # Addition of a previously missing cabinet-hour.
    assert replay_scope(
        placements, placements, {}, {("C1", hour(1, 9)): 5.0}
    ) == {"E1"}
    # Deletion of a measured cabinet-hour likewise.
    assert replay_scope(
        placements, placements, {("C1", hour(1, 9)): 5.0}, {}
    ) == {"E1"}
    # No change at all -> empty scope.
    same = {("C1", hour(1, 9)): 5.0}
    assert replay_scope(placements, placements, same, dict(same)) == set()


def test_scope_uses_both_old_and_new_placement_segments():
    # The new placement moves E1 into a cabinet-hour whose dose changed;
    # the old placement never touched that hour.
    old_placements = [placement("E1", "C1", 9, 10)]
    new_placements = [placement("E1", "C2", 9, 10)]
    old_doses = {
        ("C1", hour(1, 9)): 100.0,
        ("C2", hour(1, 9)): 100.0,
    }
    new_doses = {
        ("C1", hour(1, 9)): 100.0,
        ("C2", hour(1, 9)): 300.0,
    }
    assert replay_scope(
        old_placements, new_placements, old_doses, new_doses
    ) == {"E1"}


def test_scope_includes_exhibits_that_appear_or_disappear():
    old_placements = [placement("E1", "C1", 9, 10), placement("E2", "C1", 9, 10)]
    new_placements = [placement("E1", "C1", 9, 10), placement("E3", "C1", 9, 10)]
    doses = {("C1", hour(1, 9)): 100.0}
    assert replay_scope(
        old_placements, new_placements, doses, dict(doses)
    ) == {"E2", "E3"}


# ----------------------------------------------------------------- listing rule
def test_dose_only_split_triggers_temporary_over():
    # The old placement covers two full hours; the new placement drops the
    # corrected (raised) 09:00 hour.  Under the old placements that raised
    # hour pushes the dose-only intermediate over the threshold, while the
    # committed after-state no longer touches it.
    cap, threshold = 10.0, 700.0
    old_placements = [placement("E1", "C1", 9, 11)]
    new_placements = [placement("E1", "C1", 10, 11)]
    old_doses = {
        ("C1", hour(1, 9)): 300.0,
        ("C1", hour(1, 10)): 300.0,
    }
    new_doses = {
        ("C1", hour(1, 9)): 590.0,  # raised; new placements never touch it
        ("C1", hour(1, 10)): 300.0,
    }
    # before:       300 + 300 = 600 -> not_over
    # placement-only (new P, old D): 300 -> not_over
    # dose-only (old P, new D): 590 + 300 = 890 -> over
    # after:        300 -> not_over
    scope, rows = build_review(
        old_placements, new_placements, old_doses, new_doses, cap, threshold
    )
    assert scope == {"E1"}
    assert len(rows) == 1
    row = rows[0]
    assert row.before_status == STATUS_NOT_OVER
    assert row.after_status == STATUS_NOT_OVER
    assert row.placement_only_status == STATUS_NOT_OVER
    assert row.dose_only_status == STATUS_OVER
    assert not row.placement_only_triggers
    assert row.dose_only_triggers
    assert row.trigger_scenarios == [SCENARIO_DOSE_ONLY]


def test_identical_placements_can_never_flag_dose_only():
    # With unchanged placements the dose-only state equals the after state,
    # which is required to be non-over, so no row can ever be listed.
    cap, threshold = 10.0, 600.0
    old_placements = [
        placement("E1", "C1", 9, 10),
        Placement("E1", "C1", hour(1, 10), hour(1, 10) + 55),
    ]
    new_placements = [
        placement("E1", "C1", 9, 10),
        Placement("E1", "C1", hour(1, 10), hour(1, 10) + 55),
    ]
    old_doses = {
        ("C1", hour(1, 9)): 100.0,
        ("C1", hour(1, 10)): 400.0,
    }
    new_doses = {
        ("C1", hour(1, 9)): 100.0,
        ("C1", hour(1, 10)): 460.0,
    }
    scope, rows = build_review(
        old_placements, new_placements, old_doses, new_doses, cap, threshold
    )
    assert scope == {"E1"}
    assert rows == []


def test_placement_only_split_triggers_temporary_over():
    # New placement adds an hour whose OLD measured total is high; the new
    # dose table lowers that hour, so the committed after-state stays safe.
    cap, threshold = 10.0, 600.0
    old_placements = [placement("E1", "C1", 9, 10)]  # one full hour
    new_placements = [
        placement("E1", "C1", 9, 10),
        placement("E1", "C1", 10, 11),                # adds the 10:00 hour
    ]
    old_doses = {
        ("C1", hour(1, 9)): 300.0,
        ("C1", hour(1, 10)): 500.0,  # high under the old dose table
    }
    new_doses = {
        ("C1", hour(1, 9)): 300.0,
        ("C1", hour(1, 10)): 100.0,  # corrected downward
    }
    # before (old P, old D): 300 -> not_over
    # placement-only (new P, old D): 300 + 500 = 800 -> over
    # dose-only (old P, new D): 300 -> not_over
    # after (new P, new D): 300 + 100 = 400 -> not_over
    scope, rows = build_review(
        old_placements, new_placements, old_doses, new_doses, cap, threshold
    )
    assert scope == {"E1"}
    assert len(rows) == 1
    row = rows[0]
    assert row.before_status == STATUS_NOT_OVER
    assert row.after_status == STATUS_NOT_OVER
    assert row.placement_only_status == STATUS_OVER
    assert row.dose_only_status == STATUS_NOT_OVER
    assert row.placement_only_triggers
    assert not row.dose_only_triggers
    assert row.trigger_scenarios == [SCENARIO_PLACEMENT_ONLY]
    assert row.as_dict()["trigger_scenarios"] == ["只换摆放表"]


def test_both_split_orders_can_trigger():
    cap, threshold = 50.0, 300.0
    # Full-hour placements make bounds equal the measured total.
    old_placements = [placement("E1", "C1", 9, 10)]
    new_placements = [placement("E1", "C2", 9, 10)]
    old_doses = {
        ("C1", hour(1, 9)): 250.0,
        ("C2", hour(1, 9)): 400.0,  # high, reached only under new placements
    }
    new_doses = {
        ("C1", hour(1, 9)): 400.0,  # raised, hits only under old placements
        ("C2", hour(1, 9)): 250.0,  # lowered, reached under new placements
    }
    # before: C1 250 -> not_over
    # placement-only: C2 400 -> over
    # dose-only: C1 400 -> over
    # after: C2 250 -> not_over
    scope, rows = build_review(
        old_placements, new_placements, old_doses, new_doses, cap, threshold
    )
    assert scope == {"E1"}
    assert len(rows) == 1
    row = rows[0]
    assert row.before_status == STATUS_NOT_OVER
    assert row.after_status == STATUS_NOT_OVER
    assert row.placement_only_status == STATUS_OVER
    assert row.dose_only_status == STATUS_OVER
    assert row.placement_only_triggers and row.dose_only_triggers
    assert set(row.trigger_scenarios) == {
        SCENARIO_PLACEMENT_ONLY,
        SCENARIO_DOSE_ONLY,
    }


def test_committed_over_states_are_excluded():
    cap, threshold = 10.0, 300.0
    old_placements = [placement("E1", "C1", 9, 10)]
    new_placements = [placement("E1", "C1", 9, 10)]
    # Before already over -> excluded even though an intermediate is over too.
    old_doses = {("C1", hour(1, 9)): 400.0}
    new_doses = {("C1", hour(1, 9)): 200.0}
    _, rows = build_review(
        old_placements, new_placements, old_doses, new_doses, cap, threshold
    )
    assert rows == []

    # After over -> excluded as well.
    old_doses = {("C1", hour(1, 9)): 200.0}
    new_doses = {("C1", hour(1, 9)): 400.0}
    _, rows = build_review(
        old_placements, new_placements, old_doses, new_doses, cap, threshold
    )
    assert rows == []


def test_pending_committed_states_are_eligible_and_safe_intermediates_pass():
    cap, threshold = 10.0, 250.0
    old_placements = [Placement("E1", "C1", hour(1, 9), hour(1, 9) + 30)]
    new_placements = [Placement("E1", "C1", hour(1, 9), hour(1, 9) + 30)]
    old_doses = {("C1", hour(1, 9)): 400.0}  # bounds [100, 300] -> pending
    new_doses = {("C1", hour(1, 9)): 420.0}  # bounds [120, 300] -> pending
    # No intermediate low exceeds 250, so nothing is flagged.
    _, rows = build_review(
        old_placements, new_placements, old_doses, new_doses, cap, threshold
    )
    assert rows == []
    # And the pending status is a real, distinct verdict.
    assert STATUS_PENDING != STATUS_OVER


def test_pruned_and_new_exhibits_are_never_listed():
    cap, threshold = 10.0, 100.0
    old_placements = [placement("E1", "C1", 9, 10)]
    new_placements: list[Placement] = []  # E1 disappears from the roster
    old_doses = {("C1", hour(1, 9)): 50.0}
    new_doses = {("C1", hour(1, 9)): 80.0}
    scope, rows = build_review(
        old_placements, new_placements, old_doses, new_doses, cap, threshold
    )
    assert "E1" in scope  # still evaluated for scoping
    assert rows == []      # but never listed
