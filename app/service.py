"""Report replacement and assessment orchestration.

Every replacement is one SQLite transaction: validate first, then swap the
whole table, recompute only the exhibits the swap can affect, prune roster
entries that lost their last placement, and record report metadata.  Any
failure rolls back everything, leaving both the data and the roster intact.

Concurrent replacements pass a FIFO gate before entering their transaction,
so they take effect strictly in submission order.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from . import assess, replay
from .assess import Placement
from .csvio import DoseRow, PlacementRow, parse_doses, parse_placements
from .db import Store
from .errors import ReportError
from .timeutil import now_iso

CONFIG_CAP = "illuminance_cap"
CONFIG_THRESHOLD = "dose_threshold"


class OrderGate:
    """Ticket gate that lets submissions through in arrival order.

    A ticket is taken the moment a submission enters the service.  SQLite's
    ``BEGIN IMMEDIATE`` serialises writers but does not promise the order in
    which waiting writers acquire the lock, so the ticket gate holds every
    submission back until all earlier tickets have either applied or been
    rejected.  Concurrent replacements therefore take effect strictly in
    submission order.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._next_ticket = 0
        self._serving = 0
        self._done: set[int] = set()

    def ticket(self) -> int:
        with self._cond:
            ticket = self._next_ticket
            self._next_ticket += 1
            return ticket

    @contextmanager
    def turn(self, ticket: int) -> Iterator[None]:
        with self._cond:
            while self._serving != ticket:
                self._cond.wait()
        try:
            yield
        finally:
            self.skip(ticket)

    def skip(self, ticket: int) -> None:
        """Release a ticket, whether it applied or was rejected.

        Tickets that were rejected before their turn must not block later
        submissions, so they are marked done and skipped over.
        """

        with self._cond:
            if ticket < self._serving:
                return  # already released
            self._done.add(ticket)
            while self._serving in self._done:
                self._done.discard(self._serving)
                self._serving += 1
            self._cond.notify_all()


@dataclass(frozen=True)
class Config:
    illuminance_cap: float
    dose_threshold: float


@dataclass
class ReplaySummary:
    """Outcome of the split-submission replay for one joint replacement."""

    scope_count: int = 0
    flagged: int = 0
    is_first_upload: bool = False
    performed: bool = False


@dataclass
class SubmitResult:
    ticket: int = 0
    replaced: list[str] = field(default_factory=list)
    placement_rows: int = 0
    dose_rows: int = 0
    affected: int = 0
    recomputed: int = 0
    pruned: int = 0
    missing_cabinet_hours: list[tuple[str, int]] = field(default_factory=list)
    status_counts: dict[str, int] = field(default_factory=dict)
    replay: ReplaySummary | None = None


class ReportService:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.gate = OrderGate()

    # ------------------------------------------------------------------ reads
    def get_config(self) -> Config | None:
        conn = self.store.connect()
        try:
            return _read_config(conn)
        finally:
            conn.close()

    def initialized(self) -> bool:
        return self.get_config() is not None

    def report_meta(self) -> dict[str, dict]:
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM report_meta ORDER BY seq"
            ).fetchall()
        finally:
            conn.close()
        return {row["kind"]: dict(row) for row in rows}

    def list_assessments(self, status: str | None = None) -> list[dict]:
        conn = self.store.connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM assessments WHERE status = ? "
                    "ORDER BY exhibit_id",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM assessments ORDER BY exhibit_id"
                ).fetchall()
        finally:
            conn.close()
        return [_decorate(dict(row)) for row in rows]

    def get_assessment(self, exhibit_id: str) -> dict | None:
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT * FROM assessments WHERE exhibit_id = ?", (exhibit_id,)
            ).fetchone()
            if row is None:
                return None
            placements = conn.execute(
                "SELECT cabinet_id, start_min, end_min FROM placements "
                "WHERE exhibit_id = ? ORDER BY start_min, end_min, cabinet_id",
                (exhibit_id,),
            ).fetchall()
        finally:
            conn.close()
        result = _decorate(dict(row))
        result["placements"] = [dict(item) for item in placements]
        return result

    def status_summary(self) -> dict[str, int]:
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM assessments GROUP BY status"
            ).fetchall()
        finally:
            conn.close()
        summary = {key: 0 for key in assess.STATUS_LABELS}
        for row in rows:
            summary[row["status"]] = row["n"]
        return summary

    def get_replay(self) -> dict | None:
        """Latest split-submission replay, or ``None`` when none exists.

        A replay exists only after a successful joint replacement that was
        not the first upload.  Single-table replacements and failed
        submissions keep the previous review, so the result stays queryable
        across restarts.
        """

        conn = self.store.connect()
        try:
            header = conn.execute(
                "SELECT * FROM replay_review WHERE id = 1"
            ).fetchone()
            if header is None:
                return None
            rows = conn.execute(
                "SELECT * FROM replay_review_rows ORDER BY exhibit_id"
            ).fetchall()
        finally:
            conn.close()

        result = dict(header)
        result["rows"] = [_decorate_replay_row(dict(row)) for row in rows]
        return result

    # ----------------------------------------------------------------- writes
    def submit(
        self,
        *,
        placements_raw: bytes | None,
        placements_name: str | None,
        doses_raw: bytes | None,
        doses_name: str | None,
        illuminance_cap: float | None,
        dose_threshold: float | None,
        ticket: int | None = None,
    ) -> SubmitResult:
        """Replace one or both reports atomically.

        On the very first submission both files and both configuration values
        are required.  Afterwards the configuration is immutable and either
        file may be omitted, in which case its current table is kept.

        ``ticket`` lets an async caller fix the submission order before any
        ``await`` (in particular before reading a request body); it must come
        from :meth:`OrderGate.ticket`.
        """

        if placements_raw is None and doses_raw is None:
            raise ReportError("请至少上传一张 CSV 表")

        ticket = self.gate.ticket() if ticket is None else ticket
        try:
            # Parse and validate both files *before* taking a turn, so a bad
            # file never blocks healthy concurrent submissions.
            config = self.get_config()
            pending = _Pending()
            if config is None:
                if placements_raw is None or doses_raw is None:
                    raise ReportError(
                        "首次上传需同时提交摆放表和剂量表，并填写照度上限与剂量阈值"
                    )
                if illuminance_cap is None or dose_threshold is None:
                    raise ReportError(
                        "首次上传需填写照度上限（勒克斯）与展品剂量阈值"
                    )
                pending.new_config = _validate_config(
                    illuminance_cap, dose_threshold
                )
                # Validate the dose file against the cap supplied *with this
                # first upload*; a concurrent upload cannot create a different
                # configuration without conflicting later in ``_apply``.
                effective = pending.new_config
            else:
                requested = _requested_config(illuminance_cap, dose_threshold)
                if requested is not None and not _same_config(requested, config):
                    raise ReportError(
                        "照度上限与剂量阈值已在首次上传时确定，后续换报不可更改"
                    )
                effective = config

            if placements_raw is not None:
                pending.placements = parse_placements(placements_raw)
                pending.placements_name = placements_name or "placements.csv"
            if doses_raw is not None:
                pending.doses = parse_doses(doses_raw, effective.illuminance_cap)
                pending.doses_name = doses_name or "doses.csv"

            result = SubmitResult(ticket=ticket, replaced=_replaced_kinds(pending))
            with self.gate.turn(ticket):
                with self.store.transaction() as conn:
                    _apply(conn, result, pending)
            return result
        except BaseException:
            self.gate.skip(ticket)
            raise


@dataclass
class _Pending:
    """Validated report contents waiting for their turn."""

    new_config: Config | None = None
    placements: list[PlacementRow] | None = None
    placements_name: str | None = None
    doses: list[DoseRow] | None = None
    doses_name: str | None = None


def _replaced_kinds(pending: _Pending) -> list[str]:
    kinds = []
    if pending.placements is not None:
        kinds.append("placements")
    if pending.doses is not None:
        kinds.append("doses")
    return kinds


def _apply(
    conn: sqlite3.Connection, result: SubmitResult, pending: _Pending
) -> None:
    # A concurrent first upload may have created the configuration between our
    # pre-validation and this turn; re-read it under the write transaction.
    config = _read_config(conn)
    if config is None:
        if pending.new_config is None:
            raise ReportError("首次上传需同时提交摆放表和剂量表")
        if pending.placements is None or pending.doses is None:
            raise ReportError("首次上传需同时提交摆放表和剂量表")
        config = pending.new_config
        _write_config(conn, config)
    elif pending.new_config is not None and not _same_config(
        pending.new_config, config
    ):
        # Another upload won the race and fixed the configuration first.
        raise ReportError(
            "照度上限与剂量阈值已在首次上传时确定，后续换报不可更改"
        )

    old_doses = _load_dose_map(conn)
    if pending.doses is None:
        new_doses = old_doses
    else:
        new_doses = {
            (row.cabinet_id, row.hour_min): row.dose for row in pending.doses
        }

    old_placements = _load_placements(conn)
    if pending.placements is None:
        new_placements = old_placements
    else:
        new_placements = [
            Placement(r.exhibit_id, r.cabinet_id, r.start_min, r.end_min)
            for r in pending.placements
        ]

    # The replay only accompanies a successful joint replacement that is not
    # the very first upload; single-table swaps leave the previous review alone.
    joint_replacement = (
        pending.placements is not None and pending.doses is not None
    )
    before_meta = _read_meta_seqs(conn) if joint_replacement else None

    affected: set[str] = set()
    if pending.placements is not None:
        # Whole-table replacement: an exhibit is affected only when its own
        # placement rows actually differ.  Exhibits that disappeared keep an
        # entry in ``affected`` so the prune step can report them, but they are
        # not recomputed.
        old_by_exhibit = _group_placements(old_placements)
        new_by_exhibit = _group_placements(new_placements)
        for exhibit_id in set(old_by_exhibit) | set(new_by_exhibit):
            if old_by_exhibit.get(exhibit_id) != new_by_exhibit.get(exhibit_id):
                affected.add(exhibit_id)
    if pending.doses is not None:
        affected |= assess.affected_exhibits_by_dose_change(
            old_doses, new_doses, new_placements
        )

    computed_at = now_iso()
    if pending.placements is not None:
        _replace_placements(conn, pending.placements)
        result.placement_rows = len(pending.placements)
        _write_meta(
            conn,
            "placements",
            len(pending.placements),
            pending.placements_name or "placements.csv",
            computed_at,
        )
    if pending.doses is not None:
        _replace_doses(conn, pending.doses)
        result.dose_rows = len(pending.doses)
        _write_meta(
            conn,
            "doses",
            len(pending.doses),
            pending.doses_name or "doses.csv",
            computed_at,
        )

    by_exhibit: dict[str, list[Placement]] = {}
    for placement in new_placements:
        by_exhibit.setdefault(placement.exhibit_id, []).append(placement)

    status_counts = {key: 0 for key in assess.STATUS_LABELS}
    for exhibit_id in sorted(affected):
        placements = by_exhibit.get(exhibit_id)
        if not placements:
            continue  # pruned below
        assessment = assess.evaluate(
            exhibit_id,
            placements,
            new_doses,
            config.illuminance_cap,
            config.dose_threshold,
        )
        _upsert_assessment(conn, assessment, computed_at)
        result.recomputed += 1
        status_counts[assessment.status] += 1

    # Roster follows the reports: no placements means no entry.
    cursor = conn.execute(
        "DELETE FROM assessments WHERE exhibit_id NOT IN "
        "(SELECT DISTINCT exhibit_id FROM placements)"
    )
    result.pruned = max(cursor.rowcount or 0, 0)

    result.affected = len(affected)
    result.status_counts = status_counts
    result.missing_cabinet_hours = missing_cabinet_hours(
        new_placements, new_doses
    )

    if joint_replacement:
        # ``config`` was created inside this transaction for a first upload;
        # an empty report_meta marks it, so the replay stays empty by rule.
        first_upload = not (before_meta or {}).get("placements")
        if not first_upload:
            scope, review_rows = replay.build_review(
                old_placements,
                new_placements,
                old_doses,
                new_doses,
                config.illuminance_cap,
                config.dose_threshold,
            )
            after_meta = _read_meta_seqs(conn)
            _write_replay(
                conn,
                review_rows,
                scope_count=len(scope),
                before_meta=before_meta,
                after_meta=after_meta,
                computed_at=computed_at,
            )
            result.replay = ReplaySummary(
                scope_count=len(scope),
                flagged=len(review_rows),
                is_first_upload=False,
                performed=True,
            )
        else:
            result.replay = ReplaySummary(is_first_upload=True)


# ------------------------------------------------------------------ helpers
def _validate_config(cap: float, threshold: float) -> Config:
    if not (cap > 0):
        raise ReportError("照度上限必须大于 0 勒克斯")
    if not (threshold >= 0):
        raise ReportError("展品剂量阈值不能为负数")
    return Config(float(cap), float(threshold))


def _requested_config(
    cap: float | None, threshold: float | None
) -> Config | None:
    if cap is None and threshold is None:
        return None
    if cap is None or threshold is None:
        raise ReportError("如需填写照度上限与剂量阈值，两项必须同时提供")
    return _validate_config(cap, threshold)


def _same_config(a: Config, b: Config) -> bool:
    return (
        abs(a.illuminance_cap - b.illuminance_cap) < 1e-9
        and abs(a.dose_threshold - b.dose_threshold) < 1e-9
    )


def _read_config(conn: sqlite3.Connection) -> Config | None:
    rows = conn.execute("SELECT key, value FROM config").fetchall()
    values = {row["key"]: row["value"] for row in rows}
    if CONFIG_CAP not in values or CONFIG_THRESHOLD not in values:
        return None
    return Config(float(values[CONFIG_CAP]), float(values[CONFIG_THRESHOLD]))


def _write_config(conn: sqlite3.Connection, config: Config) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        [
            (CONFIG_CAP, repr(config.illuminance_cap)),
            (CONFIG_THRESHOLD, repr(config.dose_threshold)),
        ],
    )


def _load_dose_map(conn: sqlite3.Connection) -> dict[tuple[str, int], float]:
    rows = conn.execute(
        "SELECT cabinet_id, hour_min, dose FROM hourly_doses"
    ).fetchall()
    return {(row["cabinet_id"], row["hour_min"]): row["dose"] for row in rows}


def _load_placements(conn: sqlite3.Connection) -> list[Placement]:
    rows = conn.execute(
        "SELECT exhibit_id, cabinet_id, start_min, end_min FROM placements"
    ).fetchall()
    return [
        Placement(
            exhibit_id=row["exhibit_id"],
            cabinet_id=row["cabinet_id"],
            start_min=row["start_min"],
            end_min=row["end_min"],
        )
        for row in rows
    ]


def _group_placements(rows: Iterable[Placement]) -> dict[str, list[Placement]]:
    grouped: dict[str, list[Placement]] = {}
    for placement in rows:
        grouped.setdefault(placement.exhibit_id, []).append(placement)
    for items in grouped.values():
        items.sort(
            key=lambda p: (p.start_min, p.end_min, p.cabinet_id)
        )
    return grouped


def _replace_placements(
    conn: sqlite3.Connection, rows: Iterable[PlacementRow]
) -> None:
    conn.execute("DELETE FROM placements")
    conn.executemany(
        "INSERT INTO placements (exhibit_id, cabinet_id, start_min, end_min) "
        "VALUES (?, ?, ?, ?)",
        [(r.exhibit_id, r.cabinet_id, r.start_min, r.end_min) for r in rows],
    )


def _replace_doses(conn: sqlite3.Connection, rows: Iterable[DoseRow]) -> None:
    conn.execute("DELETE FROM hourly_doses")
    conn.executemany(
        "INSERT INTO hourly_doses (cabinet_id, hour_min, dose) VALUES (?, ?, ?)",
        [(r.cabinet_id, r.hour_min, r.dose) for r in rows],
    )


def _upsert_assessment(
    conn: sqlite3.Connection, assessment: assess.Assessment, computed_at: str
) -> None:
    conn.execute(
        """
        INSERT INTO assessments (
            exhibit_id, total_minutes, total_hours, dose_low, dose_high,
            dose_threshold, status, detail_json, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(exhibit_id) DO UPDATE SET
            total_minutes = excluded.total_minutes,
            total_hours = excluded.total_hours,
            dose_low = excluded.dose_low,
            dose_high = excluded.dose_high,
            dose_threshold = excluded.dose_threshold,
            status = excluded.status,
            detail_json = excluded.detail_json,
            computed_at = excluded.computed_at
        """,
        (
            assessment.exhibit_id,
            assessment.total_minutes,
            assessment.total_hours,
            assessment.dose_low,
            assessment.dose_high,
            assessment.dose_threshold,
            assessment.status,
            assessment.detail_json(),
            computed_at,
        ),
    )


def _write_meta(
    conn: sqlite3.Connection, kind: str, row_count: int, source: str, at: str
) -> None:
    conn.execute("UPDATE sequence SET value = value + 1 WHERE id = 1")
    seq = conn.execute("SELECT value FROM sequence WHERE id = 1").fetchone()["value"]
    conn.execute(
        "INSERT OR REPLACE INTO report_meta "
        "(kind, row_count, reported_at, source_name, seq) "
        "VALUES (?, ?, ?, ?, ?)",
        (kind, row_count, at, source, seq),
    )


def _read_meta_seqs(conn: sqlite3.Connection) -> dict[str, int]:
    """Current report version numbers; a missing kind has no version yet."""

    rows = conn.execute("SELECT kind, seq FROM report_meta").fetchall()
    return {row["kind"]: row["seq"] for row in rows}


def _write_replay(
    conn: sqlite3.Connection,
    rows: list[replay.ReviewRow],
    *,
    scope_count: int,
    before_meta: dict[str, int],
    after_meta: dict[str, int],
    computed_at: str,
) -> None:
    """Persist one replay, replacing any previous one.

    Only the review outcomes and the four report version numbers (meta seq)
    are kept; the underlying reports are identified, not copied.
    """

    conn.execute("DELETE FROM replay_review_rows")
    conn.execute("DELETE FROM replay_review")
    conn.execute(
        """
        INSERT INTO replay_review (
            id, computed_at, scope_count,
            placements_before_seq, doses_before_seq,
            placements_after_seq, doses_after_seq
        ) VALUES (1, ?, ?, ?, ?, ?, ?)
        """,
        (
            computed_at,
            scope_count,
            before_meta["placements"],
            before_meta["doses"],
            after_meta["placements"],
            after_meta["doses"],
        ),
    )
    conn.executemany(
        """
        INSERT INTO replay_review_rows (
            exhibit_id, before_status, placement_only_status, dose_only_status,
            after_status, placement_only_triggers, dose_only_triggers
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row.exhibit_id,
                row.before_status,
                row.placement_only_status,
                row.dose_only_status,
                row.after_status,
                int(row.placement_only_triggers),
                int(row.dose_only_triggers),
            )
            for row in rows
        ],
    )


def missing_cabinet_hours(
    placements: Iterable[Placement],
    doses: dict[tuple[str, int], float],
) -> list[tuple[str, int]]:
    """Cabinet-hours touched by placements but without a measurement."""

    touched = {
        (p.cabinet_id, hour)
        for p in placements
        for hour, _ in assess.split_hours(p.start_min, p.end_min)
    }
    return sorted(touched - set(doses))


def _decorate(row: dict) -> dict:
    row["detail"] = json.loads(row.pop("detail_json") or "[]")
    row["status_label"] = assess.STATUS_LABELS.get(row["status"], row["status"])
    return row


def _decorate_replay_row(row: dict) -> dict:
    row["placement_only_triggers"] = bool(row["placement_only_triggers"])
    row["dose_only_triggers"] = bool(row["dose_only_triggers"])
    for key in (
        "before_status",
        "placement_only_status",
        "dose_only_status",
        "after_status",
    ):
        row[key.replace("_status", "_status_label")] = assess.STATUS_LABELS.get(
            row[key], row[key]
        )
    triggers = []
    if row["placement_only_triggers"]:
        triggers.append(replay.SCENARIO_LABELS[replay.SCENARIO_PLACEMENT_ONLY])
    if row["dose_only_triggers"]:
        triggers.append(replay.SCENARIO_LABELS[replay.SCENARIO_DOSE_ONLY])
    row["trigger_labels"] = triggers
    return row
