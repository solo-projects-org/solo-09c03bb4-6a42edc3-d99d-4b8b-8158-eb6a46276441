"""Dose-bound computation.

Physical model
--------------
A cabinet-hour has a measured total dose ``D`` in lux·minutes (if the hour is
reported) and an illuminance that is always within ``[0, cap]`` lux.

For an exhibit that spends ``m`` minutes of that cabinet-hour inside the
cabinet, sharing the hour with everything else placed there (repeated visits
to the *same* cabinet within the *same* hour share one total, so their minutes
are summed before bounding):

* reached dose ≤ ``min(D, cap·m)`` — a subset of the hour's total and at most
  full-cap light for ``m`` minutes;
* reached dose ≥ ``max(0, D − cap·(60 − m))`` — the rest of the hour can hold
  at most ``cap·(60 − m)`` lux·minutes.

Both bounds are tight, hence a bound over all cabinet-hours is tight as well.
A missing hour has no ``D`` and is bounded by ``[0, cap·m]`` only.

Verdict
-------
``over``     — the lower bound already exceeds the threshold;
``not_over`` — the upper bound does not exceed the threshold;
``pending``  — otherwise (a measurement is needed to decide).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .timeutil import format_hour

STATUS_OVER = "over"
STATUS_NOT_OVER = "not_over"
STATUS_PENDING = "pending"

STATUS_LABELS = {
    STATUS_OVER: "确定超限",
    STATUS_NOT_OVER: "确定未超",
    STATUS_PENDING: "待核",
}

_EPS = 1e-9


@dataclass(frozen=True)
class Placement:
    exhibit_id: str
    cabinet_id: str
    start_min: int
    end_min: int


@dataclass
class HourContribution:
    cabinet_id: str
    hour_min: int
    minutes: int
    dose: float | None
    low: float
    high: float

    def as_dict(self) -> dict:
        return {
            "cabinet_id": self.cabinet_id,
            "hour_min": self.hour_min,
            "hour_label": format_hour(self.hour_min),
            "minutes": self.minutes,
            "measured": self.dose is not None,
            "dose": self.dose,
            "low": round(self.low, 6),
            "high": round(self.high, 6),
        }


@dataclass
class Assessment:
    exhibit_id: str
    total_minutes: int
    dose_low: float
    dose_high: float
    dose_threshold: float
    status: str
    hours: list[HourContribution] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def total_hours(self) -> int:
        """Number of cabinet-hour buckets contributing to this exhibit."""

        return len(self.hours)

    @property
    def missing_hours(self) -> int:
        return sum(1 for h in self.hours if h.dose is None)

    @property
    def cabinets(self) -> list[str]:
        return sorted({h.cabinet_id for h in self.hours})

    def detail_json(self) -> str:
        return json.dumps(
            [h.as_dict() for h in self.hours], ensure_ascii=False, sort_keys=True
        )


def split_hours(start_min: int, end_min: int) -> list[tuple[int, int]]:
    """Split ``[start, end)`` into ``(hour_start, minutes)`` segments."""

    segments: list[tuple[int, int]] = []
    cursor = start_min
    while cursor < end_min:
        hour_start = cursor - (cursor % 60)
        hour_end = hour_start + 60
        stop = min(end_min, hour_end)
        segments.append((hour_start, stop - cursor))
        cursor = stop
    return segments


def evaluate(
    exhibit_id: str,
    placements: Iterable[Placement],
    doses: Mapping[tuple[str, int], float],
    illuminance_cap: float,
    dose_threshold: float,
) -> Assessment:
    """Compute tight dose bounds and the verdict for one exhibit."""

    # Sum minutes per (cabinet, hour): repeat visits to one cabinet within one
    # hour share that cabinet's hourly total.
    minutes_by_bucket: dict[tuple[str, int], int] = {}
    for placement in placements:
        for hour_min, minutes in split_hours(placement.start_min, placement.end_min):
            key = (placement.cabinet_id, hour_min)
            minutes_by_bucket[key] = minutes_by_bucket.get(key, 0) + minutes

    hours: list[HourContribution] = []
    low_total = 0.0
    high_total = 0.0
    for (cabinet_id, hour_min), minutes in sorted(
        minutes_by_bucket.items(), key=lambda item: (item[0][1], item[0][0])
    ):
        dose = doses.get((cabinet_id, hour_min))
        if dose is None:
            low = 0.0
            high = illuminance_cap * minutes
        else:
            low = max(0.0, dose - illuminance_cap * (60 - minutes))
            high = min(dose, illuminance_cap * minutes)
        low_total += low
        high_total += high
        hours.append(
            HourContribution(
                cabinet_id=cabinet_id,
                hour_min=hour_min,
                minutes=minutes,
                dose=dose,
                low=low,
                high=high,
            )
        )

    if low_total > dose_threshold + _EPS:
        status = STATUS_OVER
    elif high_total <= dose_threshold + _EPS:
        status = STATUS_NOT_OVER
    else:
        status = STATUS_PENDING

    return Assessment(
        exhibit_id=exhibit_id,
        total_minutes=sum(minutes_by_bucket.values()),
        dose_low=low_total,
        dose_high=high_total,
        dose_threshold=dose_threshold,
        status=status,
        hours=hours,
    )


def affected_exhibits_by_dose_change(
    old_doses: Mapping[tuple[str, int], float],
    new_doses: Mapping[tuple[str, int], float],
    placements: Iterable[Placement],
) -> set[str]:
    """Exhibits whose bounds can change when hourly doses are replaced.

    Only cabinet-hours whose reported total actually changed matter; an
    exhibit is affected when one of its placement segments touches such an
    hour.
    """

    changed = {
        key
        for key in set(old_doses) | set(new_doses)
        if old_doses.get(key) != new_doses.get(key)
    }
    if not changed:
        return set()

    affected_cabinets = {cabinet for cabinet, _ in changed}
    affected: set[str] = set()
    for placement in placements:
        if placement.cabinet_id not in affected_cabinets:
            continue
        for hour_min, _ in split_hours(placement.start_min, placement.end_min):
            if (placement.cabinet_id, hour_min) in changed:
                affected.add(placement.exhibit_id)
                break
    return affected
