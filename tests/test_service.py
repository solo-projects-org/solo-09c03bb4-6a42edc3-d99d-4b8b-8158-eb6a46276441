"""Report replacement: affected-only recompute, pruning, atomic rollback,
concurrency ordering, and the web surface."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.errors import ReportError
from app.main import create_app
from app.service import ReportService
from app.db import Store
from app.timeutil import parse_utc_minute


def minute(text: str) -> int:
    return parse_utc_minute(text)


def placements_csv(*rows: tuple[str, str, str, str]) -> bytes:
    lines = ["exhibit_id,cabinet_id,start,end"]
    lines += [",".join(row) for row in rows]
    return ("\n".join(lines) + "\n").encode()


def doses_csv(*rows: tuple[str, str, float]) -> bytes:
    lines = ["cabinet_id,hour,dose_luxmin"]
    lines += [f"{cabinet},{hour},{dose}" for cabinet, hour, dose in rows]
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture()
def service(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.init_schema()
    svc = ReportService(store)
    svc.submit(
        placements_raw=placements_csv(
            ("E1", "C1", "2026-05-01T09:00Z", "2026-05-01T11:00Z"),
            ("E2", "C1", "2026-05-01T09:00Z", "2026-05-01T09:30Z"),
            ("E3", "C2", "2026-05-01T09:00Z", "2026-05-01T10:00Z"),
        ),
        placements_name="p0.csv",
        doses_raw=doses_csv(
            ("C1", "2026-05-01T09:00Z", 600),
            ("C1", "2026-05-01T10:00Z", 600),
            ("C2", "2026-05-01T09:00Z", 100),
        ),
        doses_name="d0.csv",
        illuminance_cap=10.0,
        dose_threshold=900.0,
    )
    return svc


def status_of(svc, exhibit_id):
    return svc.get_assessment(exhibit_id)["status"]


def test_initial_roster_and_bounds(service):
    rows = {a["exhibit_id"]: a for a in service.list_assessments()}
    assert set(rows) == {"E1", "E2", "E3"}
    # E1: full hours -> exact 1200 > 900 -> over.
    assert rows["E1"]["status"] == "over"
    assert (rows["E1"]["dose_low"], rows["E1"]["dose_high"]) == (1200.0, 1200.0)
    # E2: 30 of 60 min in C1 09:00 with total 600, cap 10 -> [300, 300]
    # (600 - 10*30 = 300; min(600, 10*30) = 300) -> exactly 300 <= 900.
    assert (rows["E2"]["dose_low"], rows["E2"]["dose_high"]) == (300.0, 300.0)
    assert rows["E2"]["status"] == "not_over"
    # E3: full hour, 100 lux-min <= 900 -> not over.
    assert rows["E3"]["status"] == "not_over"


def test_config_required_and_immutable(tmp_path):
    store = Store(tmp_path / "c.sqlite3")
    store.init_schema()
    svc = ReportService(store)
    order = ("E1", "C1", "2026-05-01T09:00Z", "2026-05-01T10:00Z")
    p = placements_csv(order)
    d = doses_csv(("C1", "2026-05-01T09:00Z", 100))
    with pytest.raises(ReportError, match="首次上传"):
        svc.submit(
            placements_raw=p,
            placements_name="p.csv",
            doses_raw=d,
            doses_name="d.csv",
            illuminance_cap=None,
            dose_threshold=None,
        )
    with pytest.raises(ReportError, match="首次上传"):
        svc.submit(
            placements_raw=p,
            placements_name="p.csv",
            doses_raw=None,
            doses_name=None,
            illuminance_cap=10.0,
            dose_threshold=500.0,
        )
    svc.submit(
        placements_raw=p,
        placements_name="p.csv",
        doses_raw=d,
        doses_name="d.csv",
        illuminance_cap=10.0,
        dose_threshold=500.0,
    )
    with pytest.raises(ReportError, match="不可更改"):
        svc.submit(
            placements_raw=None,
            placements_name=None,
            doses_raw=d,
            doses_name="d.csv",
            illuminance_cap=20.0,
            dose_threshold=500.0,
        )


def test_dose_replacement_only_recomputes_affected(service):
    result = service.submit(
        placements_raw=None,
        placements_name=None,
        doses_raw=doses_csv(
            ("C1", "2026-05-01T09:00Z", 550),  # affects E1, E2
            ("C1", "2026-05-01T10:00Z", 600),  # unchanged
            ("C2", "2026-05-01T09:00Z", 100),  # unchanged, E3 untouched
        ),
        doses_name="d1.csv",
        illuminance_cap=None,
        dose_threshold=None,
    )
    assert result.replaced == ["doses"]
    assert result.recomputed == 2
    assert {a["exhibit_id"] for a in service.list_assessments()} == {
        "E1",
        "E2",
        "E3",
    }
    # E1 total 1150 -> over.
    assert service.get_assessment("E1")["dose_low"] == 1150.0
    # E2 lower bound = 550 - 10*30 = 250.
    assert service.get_assessment("E2")["dose_low"] == 250.0


def test_unchanged_dose_report_recomputes_nothing(service):
    result = service.submit(
        placements_raw=None,
        placements_name=None,
        doses_raw=doses_csv(
            ("C1", "2026-05-01T09:00Z", 600),
            ("C1", "2026-05-01T10:00Z", 600),
            ("C2", "2026-05-01T09:00Z", 100),
        ),
        doses_name="same.csv",
        illuminance_cap=None,
        dose_threshold=None,
    )
    assert result.recomputed == 0
    assert result.pruned == 0


def test_placement_replacement_prunes_roster(service):
    result = service.submit(
        placements_raw=placements_csv(
            ("E1", "C1", "2026-05-01T09:00Z", "2026-05-01T11:00Z"),
            ("E3", "C2", "2026-05-01T09:00Z", "2026-05-01T10:00Z"),
        ),
        placements_name="p1.csv",
        doses_raw=None,
        doses_name=None,
        illuminance_cap=None,
        dose_threshold=None,
    )
    assert result.pruned == 1
    assert {a["exhibit_id"] for a in service.list_assessments()} == {"E1", "E3"}
    assert service.get_assessment("E2") is None
    # Unchanged placement row for E1 -> not recomputed, kept as-is.
    assert result.recomputed == 0


def test_placement_change_recomputes_only_that_exhibit(service):
    result = service.submit(
        placements_raw=placements_csv(
            ("E1", "C1", "2026-05-01T09:00Z", "2026-05-01T11:00Z"),
            ("E2", "C1", "2026-05-01T09:00Z", "2026-05-01T09:45Z"),
            ("E3", "C2", "2026-05-01T09:00Z", "2026-05-01T10:00Z"),
        ),
        placements_name="p2.csv",
        doses_raw=None,
        doses_name=None,
        illuminance_cap=None,
        dose_threshold=None,
    )
    assert result.recomputed == 1
    assert service.get_assessment("E2")["total_minutes"] == 45


def test_failed_replacement_keeps_data_and_roster(service):
    before = {a["exhibit_id"]: a for a in service.list_assessments()}
    with pytest.raises(ReportError, match="负"):
        service.submit(
            placements_raw=None,
            placements_name=None,
            doses_raw=doses_csv(("C1", "2026-05-01T09:00Z", -5)),
            doses_name="bad.csv",
            illuminance_cap=None,
            dose_threshold=None,
        )
    after = {a["exhibit_id"]: a for a in service.list_assessments()}
    assert before == after
    assert service.get_assessment("E1")["dose_low"] == 1200.0

    # A bad placements file must not disturb the dose table either.
    with pytest.raises(ReportError, match="重叠"):
        service.submit(
            placements_raw=placements_csv(
                ("E1", "C1", "2026-05-01T09:00Z", "2026-05-01T10:00Z"),
                ("E1", "C2", "2026-05-01T09:30Z", "2026-05-01T10:30Z"),
            ),
            placements_name="bad.csv",
            doses_raw=doses_csv(("C1", "2026-05-01T09:00Z", 1)),
            doses_name="bad.csv",
            illuminance_cap=None,
            dose_threshold=None,
        )
    assert {a["exhibit_id"]: a for a in service.list_assessments()} == before
    assert service.report_meta()["doses"]["source_name"] == "d0.csv"


def test_validation_failure_does_not_block_later_submission(service):
    """A rejected submission must release its gate ticket."""

    with pytest.raises(ReportError):
        service.submit(
            placements_raw=None,
            placements_name=None,
            doses_raw=doses_csv(("C1", "2026-05-01T09:00Z", -1)),
            doses_name="bad.csv",
            illuminance_cap=None,
            dose_threshold=None,
        )
    result = service.submit(
        placements_raw=None,
        placements_name=None,
        doses_raw=doses_csv(
            ("C1", "2026-05-01T09:00Z", 600),
            ("C1", "2026-05-01T10:00Z", 600),
            ("C2", "2026-05-01T09:00Z", 100),
        ),
        doses_name="ok.csv",
        illuminance_cap=None,
        dose_threshold=None,
    )
    assert result.ticket > 0


def test_concurrent_replacements_apply_in_submission_order(service):
    """Two concurrent dose replacements: the last submitter wins."""

    first_dose = doses_csv(
        ("C1", "2026-05-01T09:00Z", 580),
        ("C1", "2026-05-01T10:00Z", 600),
        ("C2", "2026-05-01T09:00Z", 100),
    )
    second_dose = doses_csv(
        ("C1", "2026-05-01T09:00Z", 600),
        ("C1", "2026-05-01T10:00Z", 590),
        ("C2", "2026-05-01T09:00Z", 100),
    )
    order: list[str] = []
    lock = threading.Lock()

    def run(name, payload):
        service.submit(
            placements_raw=None,
            placements_name=None,
            doses_raw=payload,
            doses_name=f"{name}.csv",
            illuminance_cap=None,
            dose_threshold=None,
        )
        with lock:
            order.append(name)

    t1 = threading.Thread(target=run, args=("first", first_dose))
    t2 = threading.Thread(target=run, args=("second", second_dose))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # The gate serialises in ticket order (thread start order here).
    assert order == ["first", "second"]
    assert service.report_meta()["doses"]["source_name"] == "second.csv"
    assert service.get_assessment("E1")["dose_low"] == 1190.0


def test_first_upload_race_keeps_single_config(tmp_path):
    store = Store(tmp_path / "race.sqlite3")
    store.init_schema()
    svc = ReportService(store)
    p = placements_csv(("E1", "C1", "2026-05-01T09:00Z", "2026-05-01T10:00Z"))
    d = doses_csv(("C1", "2026-05-01T09:00Z", 100))

    errors: list[Exception] = []

    def first():
        try:
            svc.submit(
                placements_raw=p, placements_name="p.csv",
                doses_raw=d, doses_name="d.csv",
                illuminance_cap=10.0, dose_threshold=500.0,
            )
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(exc)

    def second():
        try:
            svc.submit(
                placements_raw=p, placements_name="p2.csv",
                doses_raw=d, doses_name="d2.csv",
                illuminance_cap=20.0, dose_threshold=500.0,
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    config = svc.get_config()
    assert config.illuminance_cap == 10.0  # first submission wins
    assert len(errors) == 1 and isinstance(errors[0], ReportError)
    # Exactly one of the two full reports is stored, consistently.
    assert {a["exhibit_id"] for a in svc.list_assessments()} == {"E1"}


# ----------------------------------------------------------------- web layer
@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "web.sqlite3"))
    with TestClient(app) as c:
        yield c, app


def test_web_first_upload_and_pages(client):
    c, app = client
    assert c.get("/").status_code == 200
    response = c.post(
        "/submit",
        files={
            "placements": (
                "p.csv",
                placements_csv(
                    ("E1", "C1", "2026-05-01T09:00Z", "2026-05-01T10:00Z")
                ),
                "text/csv",
            ),
            "doses": (
                "d.csv",
                doses_csv(("C1", "2026-05-01T09:00Z", 500)),
                "text/csv",
            ),
        },
        data={"illuminance_cap": "10", "dose_threshold": "400"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "E1" in c.get("/").text
    detail = c.get("/exhibits/E1")
    assert detail.status_code == 200
    assert "确定超限" in detail.text
    assert c.get("/exhibits/UNKNOWN").status_code == 404


def test_web_error_keeps_previous_state(client):
    c, app = client
    c.post(
        "/submit",
        files={
            "placements": (
                "p.csv",
                placements_csv(
                    ("E1", "C1", "2026-05-01T09:00Z", "2026-05-01T10:00Z")
                ),
                "text/csv",
            ),
            "doses": (
                "d.csv",
                doses_csv(("C1", "2026-05-01T09:00Z", 500)),
                "text/csv",
            ),
        },
        data={"illuminance_cap": "10", "dose_threshold": "400"},
    )
    bad = c.post(
        "/submit",
        files={
            "doses": (
                "d2.csv",
                doses_csv(("C1", "2026-05-01T09:00Z", 999)),
                "text/csv",
            )
        },
    )
    assert bad.status_code == 400
    assert "整表退回" in bad.text or "超过照度上限" in bad.text
    service = app.state.service
    assert service.get_assessment("E1")["dose_low"] == 500.0
    assert service.report_meta()["doses"]["source_name"] == "d.csv"


def test_web_samples_download(client):
    c, _ = client
    response = c.get("/samples/placements.csv")
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbfexhibit_id")
