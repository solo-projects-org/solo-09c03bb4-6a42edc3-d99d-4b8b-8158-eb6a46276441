"""Split-submission replay ("分报复盘").

When both corrected reports (placements and doses) are replaced in one
successful submission, the conservator wants to know which exhibits would
*temporarily* be judged ``over`` if the two files were uploaded separately
instead of together.  For every exhibit that the change can touch we compute
four verdicts, reusing the exact dose-bound routine in :mod:`app.assess`:

before        old placements + old doses (the live state before the swap)
placement_only new placements + old doses ("only the placement report first")
dose_only     old placements + new doses ("only the dose report first")
after         new placements + new doses (the state the joint swap commits)

Trial scope
-----------
An exhibit is in scope when either

* its own placement rows actually differ between the two reports, or
* one of its placement segments (under *either* the old or the new report)
  touches a cabinet-hour whose measured dose was added, removed or changed.

Exhibits pruned by the new placement report are evaluated for scoping but
never listed, since the listing requires the exhibit to remain on the roster.

Listing rule
------------
An exhibit is reported only when it is present both before and after, is
``over`` in neither of those two states, yet is ``over`` in at least one of
the two split-submission intermediate states.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from . import assess
from .assess import Placement

SCENARIO_BEFORE = "before"
SCENARIO_PLACEMENT_ONLY = "placement_only"
SCENARIO_DOSE_ONLY = "dose_only"
SCENARIO_AFTER = "after"

SCENARIO_LABELS = {
    SCENARIO_BEFORE: "换报前",
    SCENARIO_PLACEMENT_ONLY: "只换摆放表",
    SCENARIO_DOSE_ONLY: "只换剂量表",
    SCENARIO_AFTER: "换报后",
}

# Intermediate scenarios that represent a separate ("split") submission.
SPLIT_SCENARIOS = (SCENARIO_PLACEMENT_ONLY, SCENARIO_DOSE_ONLY)


@dataclass(frozen=True)
class ReviewRow:
    exhibit_id: str
    before_status: str
    placement_only_status: str
    dose_only_status: str
    after_status: str
    placement_only_triggers: bool
    dose_only_triggers: bool

    @property
    def trigger_scenarios(self) -> list[str]:
        result = []
        if self.placement_only_triggers:
            result.append(SCENARIO_PLACEMENT_ONLY)
        if self.dose_only_triggers:
            result.append(SCENARIO_DOSE_ONLY)
        return result

    def as_dict(self) -> dict:
        return {
            "exhibit_id": self.exhibit_id,
            "before_status": self.before_status,
            "placement_only_status": self.placement_only_status,
            "dose_only_status": self.dose_only_status,
            "after_status": self.after_status,
            "placement_only_triggers": self.placement_only_triggers,
            "dose_only_triggers": self.dose_only_triggers,
            "trigger_scenarios": [
                SCENARIO_LABELS[key] for key in self.trigger_scenarios
            ],
        }


def _group(placements: list[Placement]) -> dict[str, list[Placement]]:
    grouped: dict[str, list[Placement]] = {}
    for placement in placements:
        grouped.setdefault(placement.exhibit_id, []).append(placement)
    for items in grouped.values():
        items.sort(key=lambda p: (p.start_min, p.end_min, p.cabinet_id))
    return grouped


def replay_scope(
    old_placements: list[Placement],
    new_placements: list[Placement],
    old_doses: Mapping[tuple[str, int], float],
    new_doses: Mapping[tuple[str, int], float],
) -> set[str]:
    """Exhibits the joint replacement can change.

    Placement-driven: the exhibit's own rows differ (including exhibits that
    appear or disappear).  Dose-driven: an old *or* new placement segment
    touches a cabinet-hour whose measured total changed — dose additions and
    deletions count as changes too.
    """

    old_by_exhibit = _group(old_placements)
    new_by_exhibit = _group(new_placements)

    scope = {
        exhibit_id
        for exhibit_id in set(old_by_exhibit) | set(new_by_exhibit)
        if old_by_exhibit.get(exhibit_id) != new_by_exhibit.get(exhibit_id)
    }

    changed_hours = {
        key
        for key in set(old_doses) | set(new_doses)
        if old_doses.get(key) != new_doses.get(key)
    }
    if changed_hours:
        for placements in (old_placements, new_placements):
            scope |= assess.affected_exhibits_by_dose_change(
                old_doses, new_doses, placements
            )
    return scope


def _verdict(
    exhibit_id: str,
    placements: list[Placement] | None,
    doses: Mapping[tuple[str, int], float],
    cap: float,
    threshold: float,
) -> str | None:
    """Status under one scenario, or ``None`` if the exhibit has no placements."""

    if not placements:
        return None
    return assess.evaluate(
        exhibit_id, placements, doses, cap, threshold
    ).status


def build_review(
    old_placements: list[Placement],
    new_placements: list[Placement],
    old_doses: Mapping[tuple[str, int], float],
    new_doses: Mapping[tuple[str, int], float],
    cap: float,
    threshold: float,
) -> tuple[set[str], list[ReviewRow]]:
    """Compute the replay scope and the rows that must be flagged.

    Returns the full trial scope (for reporting/auditing) and the ordered
    list of :class:`ReviewRow` instances satisfying the listing rule.
    """

    old_by_exhibit = _group(old_placements)
    new_by_exhibit = _group(new_placements)
    scope = replay_scope(old_placements, new_placements, old_doses, new_doses)

    rows: list[ReviewRow] = []
    for exhibit_id in sorted(scope):
        old_rows = old_by_exhibit.get(exhibit_id)
        new_rows = new_by_exhibit.get(exhibit_id)
        before = _verdict(exhibit_id, old_rows, old_doses, cap, threshold)
        placement_only = _verdict(
            exhibit_id, new_rows, old_doses, cap, threshold
        )
        dose_only = _verdict(exhibit_id, old_rows, new_doses, cap, threshold)
        after = _verdict(exhibit_id, new_rows, new_doses, cap, threshold)

        # Must stay on the roster both before and after the joint swap.
        if before is None or after is None:
            continue
        # Neither committed state may already be a definite over-limit.
        if before == assess.STATUS_OVER or after == assess.STATUS_OVER:
            continue

        placement_only_triggers = placement_only == assess.STATUS_OVER
        dose_only_triggers = dose_only == assess.STATUS_OVER
        if not (placement_only_triggers or dose_only_triggers):
            continue

        rows.append(
            ReviewRow(
                exhibit_id=exhibit_id,
                before_status=before,
                placement_only_status=placement_only,
                dose_only_status=dose_only,
                after_status=after,
                placement_only_triggers=placement_only_triggers,
                dose_only_triggers=dose_only_triggers,
            )
        )
    return scope, rows
