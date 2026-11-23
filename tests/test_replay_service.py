"""Service- and web-level behaviour of the split-submission replay."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import Store
from app.errors import ReportError
from app.main import create_app
from app.service import ReportService


def placements_csv(*rows: tuple[str, str, str, str]) -> bytes:
    lines = ["exhibit_id,cabinet_id,start,end"]
    lines += [",".join(row) for row in rows]
    return ("\n".join(lines) + "\n").encode()


def doses_csv(*rows: tuple[str, str, float]) -> bytes:
    lines = ["cabinet_id,hour,dose_luxmin"]
    lines += [f"{cabinet},{hour},{dose}" for cabinet, hour, dose in rows]
    return ("\n".join(lines) + "\n").encode()


# Scenario shared by the tests below (cap 10 lx, threshold 600 lx·min):
#
# E1 moves from one full hour (C1 09:00 = 300) to two full hours (09:00 = 300
# and 10:00).  The 10:00 hour is corrected from 500 down to 100.
#   before:        300                -> not_over
#   placement-only: 300 + 500 = 800   -> over  (只换摆放表 triggers)
#   dose-only:     300                -> not_over
#   after:         300 + 100 = 400    -> not_over
# E2 is a full-hour exhibit in C2, untouched by any change -> out of scope.
FIRST_PLACEMENTS = placements_csv(
    ("E1", "C1", "2026-05-01T09:00Z", "2026-05-01T10:00Z"),
    ("E2", "C2", "2026-05-01T09:00Z", "2026-05-01T10:00Z"),
)
FIRST_DOSES = doses_csv(
    ("C1", "2026-05-01T09:00Z", 300),
    ("C2", "2026-05-01T09:00Z", 200),
)
SECOND_PLACEMENTS = placements_csv(
    ("E1", "C1", "2026-05-01T09:00Z", "2026-05-01T11:00Z"),
    ("E2", "C2", "2026-05-01T09:00Z", "2026-05-01T10:00Z"),
)
SECOND_DOSES = doses_csv(
    ("C1", "2026-05-01T09:00Z", 300),
    ("C1", "2026-05-01T10:00Z", 100),
    ("C2", "2026-05-01T09:00Z", 200),
)
# Old dose table for the second submission: the 10:00 hour initially read 500.
OLD_DOSES = doses_csv(
    ("C1", "2026-05-01T09:00Z", 300),
    ("C1", "2026-05-01T10:00Z", 500),
    ("C2", "2026-05-01T09:00Z", 200),
)


@pytest.fixture()
def service(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.init_schema()
    svc = ReportService(store)
    svc.submit(
        placements_raw=FIRST_PLACEMENTS,
        placements_name="p0.csv",
        doses_raw=FIRST_DOSES,
        doses_name="d0.csv",
        illuminance_cap=10.0,
        dose_threshold=600.0,
    )
    # Establish the "old" joint state via a dose-only replacement; this must
    # not create a replay.
    svc.submit(
        placements_raw=None,
        placements_name=None,
        doses_raw=OLD_DOSES,
        doses_name="d1.csv",
        illuminance_cap=None,
        dose_threshold=None,
    )
    return svc


def test_first_upload_leaves_review_empty(service):
    # The fixture's first upload happened before this point; a fresh store
    # makes the first-upload rule explicit.
    store = Store(service.store.path + ".fresh")
    store.init_schema()
    fresh = ReportService(store)
    fresh.submit(
        placements_raw=FIRST_PLACEMENTS,
        placements_name="p.csv",
        doses_raw=FIRST_DOSES,
        doses_name="d.csv",
        illuminance_cap=10.0,
        dose_threshold=600.0,
    )
    assert fresh.get_replay() is None


def test_joint_replacement_records_review_with_versions(service):
    meta_before = service.report_meta()
    result = service.submit(
        placements_raw=SECOND_PLACEMENTS,
        placements_name="p2.csv",
        doses_raw=SECOND_DOSES,
        doses_name="d2.csv",
        illuminance_cap=None,
        dose_threshold=None,
    )
    assert result.replay is not None
    assert result.replay.performed
    assert result.replay.flagged == 1
    # Only E1 is in scope; E2's rows and cabinet-hours never changed.
    assert result.replay.scope_count == 1

    review = service.get_replay()
    assert review is not None
    assert review["scope_count"] == 1
    assert len(review["rows"]) == 1
    row = review["rows"][0]
    assert row["exhibit_id"] == "E1"
    assert row["before_status"] == "not_over"
    assert row["placement_only_status"] == "over"
    assert row["dose_only_status"] == "not_over"
    assert row["after_status"] == "not_over"
    assert row["placement_only_triggers"] is True
    assert row["dose_only_triggers"] is False
    assert row["trigger_labels"] == ["只换摆放表"]

    # Version numbers identify the four report versions, old then new.
    assert review["placements_before_seq"] == meta_before["placements"]["seq"]
    assert review["doses_before_seq"] == meta_before["doses"]["seq"]
    meta_after = service.report_meta()
    assert review["placements_after_seq"] == meta_after["placements"]["seq"]
    assert review["doses_after_seq"] == meta_after["doses"]["seq"]
    assert meta_after["placements"]["seq"] > meta_before["placements"]["seq"]
    assert meta_after["doses"]["seq"] > meta_before["doses"]["seq"]


def test_single_table_replacement_keeps_previous_review(service):
    service.submit(
        placements_raw=SECOND_PLACEMENTS,
        placements_name="p2.csv",
        doses_raw=SECOND_DOSES,
        doses_name="d2.csv",
        illuminance_cap=None,
        dose_threshold=None,
    )
    saved = service.get_replay()
    assert saved is not None

    # A placement-only replacement must not touch the stored review.
    result = service.submit(
        placements_raw=SECOND_PLACEMENTS,
        placements_name="p3.csv",
        doses_raw=None,
        doses_name=None,
        illuminance_cap=None,
        dose_threshold=None,
    )
    assert result.replay is None
    assert service.get_replay() == saved

    # A dose-only replacement likewise.
    result = service.submit(
        placements_raw=None,
        placements_name=None,
        doses_raw=SECOND_DOSES,
        doses_name="d3.csv",
        illuminance_cap=None,
        dose_threshold=None,
    )
    assert result.replay is None
    assert service.get_replay() == saved


def test_failed_replacement_keeps_previous_review(service):
    service.submit(
        placements_raw=SECOND_PLACEMENTS,
        placements_name="p2.csv",
        doses_raw=SECOND_DOSES,
        doses_name="d2.csv",
        illuminance_cap=None,
        dose_threshold=None,
    )
    saved = service.get_replay()

    with pytest.raises(ReportError, match="负"):
        service.submit(
            placements_raw=None,
            placements_name=None,
            doses_raw=doses_csv(("C1", "2026-05-01T09:00Z", -1)),
            doses_name="bad.csv",
            illuminance_cap=None,
            dose_threshold=None,
        )
    with pytest.raises(ReportError):
        service.submit(
            placements_raw=placements_csv(
                ("E1", "C1", "2026-05-01T09:00Z", "2026-05-01T10:00Z"),
                ("E1", "C2", "2026-05-01T09:30Z", "2026-05-01T10:30Z"),
            ),
            placements_name="bad.csv",
            doses_raw=SECOND_DOSES,
            doses_name="d2.csv",
            illuminance_cap=None,
            dose_threshold=None,
        )
    assert service.get_replay() == saved


def test_review_survives_restart(tmp_path, service):
    db_path = service.store.path
    service.submit(
        placements_raw=SECOND_PLACEMENTS,
        placements_name="p2.csv",
        doses_raw=SECOND_DOSES,
        doses_name="d2.csv",
        illuminance_cap=None,
        dose_threshold=None,
    )
    expected = service.get_replay()

    # A brand-new service over the same database file simulates a restart.
    revived = ReportService(Store(db_path))
    assert revived.get_replay() == expected


def test_joint_replacement_with_no_trigger_still_stores_review(service):
    # Replace both tables with identical contents renamed: no trigger, but
    # the review header and version numbers must still be refreshed.
    result = service.submit(
        placements_raw=FIRST_PLACEMENTS,
        placements_name="same-p.csv",
        doses_raw=OLD_DOSES,
        doses_name="same-d.csv",
        illuminance_cap=None,
        dose_threshold=None,
    )
    assert result.replay is not None
    assert result.replay.performed
    assert result.replay.flagged == 0
    review = service.get_replay()
    assert review["rows"] == []
    assert review["scope_count"] == 0


# --------------------------------------------------------------------- web UI
@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "web.sqlite3"))
    with TestClient(app) as c:
        yield c, app


def _joint_setup(c):
    # First upload (both tables) -> no replay by rule.
    c.post(
        "/submit",
        files={
            "placements": ("p0.csv", FIRST_PLACEMENTS, "text/csv"),
            "doses": ("d0.csv", FIRST_DOSES, "text/csv"),
        },
        data={"illuminance_cap": "10", "dose_threshold": "600"},
    )
    # Establish the "old" dose readings via a single-table replacement,
    # which must not create a replay either.
    c.post(
        "/submit",
        files={"doses": ("d1.csv", OLD_DOSES, "text/csv")},
    )


def test_web_review_panel_lifecycle(client):
    c, _ = client
    # First upload: empty-state copy, no panel table.
    _joint_setup(c)
    page = c.get("/").text
    assert "分报复盘" in page
    assert "暂无复盘" in page

    # Joint corrected replacement: panel shows the flagged exhibit.
    response = c.post(
        "/submit",
        files={
            "placements": ("p2.csv", SECOND_PLACEMENTS, "text/csv"),
            "doses": ("d2.csv", SECOND_DOSES, "text/csv"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = c.get("/").text
    assert "E1" in page
    assert "只换摆放表" in page
    assert "▲ 触发" in page
    assert "版本号" in page
    # All four scenario columns render.
    for label in ("换报前", "只换摆放表", "只换剂量表", "换报后"):
        assert label in page

    # A later single-table replacement keeps the panel content.
    c.post(
        "/submit",
        files={"doses": ("d3.csv", SECOND_DOSES, "text/csv")},
    )
    page_after = c.get("/").text
    assert "▲ 触发" in page_after
    assert "E1" in page_after


def test_web_failed_submit_keeps_review_panel(client):
    c, _ = client
    _joint_setup(c)
    c.post(
        "/submit",
        files={
            "placements": ("p2.csv", SECOND_PLACEMENTS, "text/csv"),
            "doses": ("d2.csv", SECOND_DOSES, "text/csv"),
        },
    )
    bad = c.post(
        "/submit",
        files={"doses": ("bad.csv", doses_csv(
            ("C1", "2026-05-01T09:00Z", -1)
        ), "text/csv")},
    )
    assert bad.status_code == 400
    page = c.get("/").text
    assert "▲ 触发" in page
