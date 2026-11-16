"""End-to-end corridor tests: books, runs, invalidation, confirmations."""

from __future__ import annotations

from pathlib import Path

import pytest

from simulator.run_simulator import load_scenario

SAMPLES = Path(__file__).resolve().parent.parent / "samples"

BOOK = {
    "name": "test-book",
    "default_max_speed_mps": 2.0,
    "seasonal_speed_limits": [
        {"class_label": None, "season": {"start": "11-01", "end": "02-01"}, "max_speed_mps": 1.5}
    ],
    "impassable_zones": [],
    "blind_zones": [],
    "clock_uncertainties": [],
    "default_clock_uncertainty_us": 2_000_000,
}

WINDOW = {"window_start": "2026-11-13T23:00:00Z", "window_end": "2026-11-14T02:00:00Z"}


@pytest.fixture()
def sample_dir(tmp_path):
    return tmp_path


def _run(client, **body):
    res = client.post("/runs", json={"wait": True, **body})
    assert res.status_code == 201, res.text
    return res.json()


def _book(client, **overrides):
    payload = {**BOOK, **overrides}
    res = client.post("/constraint-books", json=payload)
    assert res.status_code == 201, res.text
    return res.json()


def _corridor_run(client, book_id, method="mincost_flow", window=WINDOW, **params):
    res = client.post(
        "/corridor-runs",
        json={
            "method": method,
            "constraint_book_id": book_id,
            **window,
            **params,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


# --------------------------------------------------------------------------- #
# Constraint books
# --------------------------------------------------------------------------- #
def test_constraint_book_versioning_and_validation(client):
    v1 = _book(client)
    v2 = _book(client, notes="second edition")
    assert (v1["version"], v2["version"]) == (1, 2)
    # The version hash covers the constraint content only: identical rules
    # with different notes hash the same, changed rules hash differently.
    assert v1["version_hash"] == v2["version_hash"]
    v3 = _book(client, default_clock_uncertainty_us=5_000_000)
    assert v3["version"] == 3
    assert v3["version_hash"] != v1["version_hash"]
    other = _book(client, name="other-book")
    assert other["version"] == 1
    bad = client.post(
        "/constraint-books",
        json={**BOOK, "name": "bad", "impassable_zones": [
            {"zone_id": "Z", "kind": "polygon", "points": [[0, 0], [4, 0], [4, 4], [2, 1], [0, 4]]}
        ]},
    )
    assert bad.status_code == 422
    assert "convex" in bad.json()["detail"]["problems"][0]


def test_corridor_run_requires_existing_book(client):
    res = client.post(
        "/corridor-runs",
        json={"method": "mincost_flow", "constraint_book_id": 999, **WINDOW},
    )
    assert res.status_code == 404


# --------------------------------------------------------------------------- #
# Corridor runs over a loaded scenario
# --------------------------------------------------------------------------- #
def test_crossing_corridors_keep_competition(client, api, sample_dir):
    load_scenario("crossing", "http://testserver", truth_dir=sample_dir, reset=False)
    _run(client)
    book = _book(client)
    run = _corridor_run(client, book["id"])
    assert run["status"] == "done"
    assert run["constraint_version_hash"] == book["version_hash"]
    assert run["summary"]["corridors"] >= 2

    corridors = client.get(f"/corridor-runs/{run['id']}/corridors").json()["corridors"]
    assert len(corridors) >= 2
    detail = client.get(f"/corridors/{corridors[0]['id']}").json()
    # Linked events carry per-edge constraint explanations.
    edges = [m["edge_to_next"] for m in detail["members"] if m["edge_to_next"]]
    assert edges, "corridor must link events"
    for edge in edges:
        assert edge["speed_limit_mps"] == 1.5  # winter rule applies
        assert edge["speed_rule"]
        assert "clock_uncertainty_us" in edge
        assert edge["feasible"] is True
    # No individual identity anywhere in the payload.
    assert "animal" not in str(detail).lower()
    assert detail["identity_note"]
    # Competition is surfaced somewhere in the run.
    all_comps = []
    for c in corridors:
        det = client.get(f"/corridors/{c['id']}").json()
        all_comps.extend(det["competitors"])
    assert all_comps, "crossing must produce competing events"
    # Unattributed: the two noise events, with reasons.
    un = client.get(f"/corridor-runs/{run['id']}/unattributed").json()
    assert un["total"] == 2
    assert all(u["reason"] == "isolated" for u in un["events"])
    # Gaps (if any) never carry coordinates.
    for c in corridors:
        det = client.get(f"/corridors/{c['id']}").json()
        for gap in det["gaps"]:
            assert "x" not in gap and "y" not in gap and "position" not in gap


def test_both_methods_cover_the_window(client, api, sample_dir):
    load_scenario("crossing", "http://testserver", truth_dir=sample_dir, reset=False)
    _run(client)
    book = _book(client)
    flow = _corridor_run(client, book["id"], method="mincost_flow")
    hyp = _corridor_run(client, book["id"], method="hypothesis_pruning")
    assert flow["summary"]["corridors"] >= 2
    assert hyp["summary"]["corridors"] >= 2
    # The hypothesis method documents unsearched branches explicitly.
    assert "unsearched_branches" in hyp["summary"]
    assert "prune_stats" in hyp["summary"]
    # Both record the miss cost they used.
    assert flow["summary"]["miss_cost"] == 1.0
    assert hyp["summary"]["miss_cost"] == 1.0


def test_clock_policy_versions_change_candidate_edges(client, api, sample_dir):
    load_scenario("clockskew", "http://testserver", truth_dir=sample_dir, reset=False)
    _run(client)
    loose = _book(client, name="clock-policy", default_max_speed_mps=1.0,
                 seasonal_speed_limits=[], default_clock_uncertainty_us=120_000_000)
    tight = _book(client, name="clock-policy", default_max_speed_mps=1.0,
                  seasonal_speed_limits=[], default_clock_uncertainty_us=2_000_000)
    assert loose["version"] == 1 and tight["version"] == 2
    window = {"window_start": "2026-11-13T23:00:00Z", "window_end": "2026-11-14T03:00:00Z"}
    run_loose = _corridor_run(client, loose["id"], window=window, miss_cost=2.0)
    run_tight = _corridor_run(client, tight["id"], window=window, miss_cost=2.0)
    sizes_loose = sorted(
        c["event_count"] for c in client.get(f"/corridor-runs/{run_loose['id']}/corridors").json()["corridors"]
    )
    sizes_tight = sorted(
        c["event_count"] for c in client.get(f"/corridor-runs/{run_tight['id']}/corridors").json()["corridors"]
    )
    assert sizes_loose == [4]
    assert sizes_tight == [2, 2]
    assert run_loose["constraint_version_hash"] != run_tight["constraint_version_hash"]


# --------------------------------------------------------------------------- #
# Invalidation: late data touches adjacent windows only
# --------------------------------------------------------------------------- #
def test_late_revision_marks_adjacent_runs_stale_and_keeps_conflicts(
    client, api, sample_dir
):
    load_scenario("crossing", "http://testserver", truth_dir=sample_dir, reset=False)
    _run(client)
    book = _book(client)
    run = _corridor_run(client, book["id"])
    corridors = client.get(f"/corridor-runs/{run['id']}/corridors").json()["corridors"]
    assert corridors, "expected corridors over the events"
    target = corridors[0]
    member_events = [m["event_id"] for m in client.get(f"/corridors/{target['id']}").json()["members"]]
    # Confirm the corridor manually.
    conf = client.post(f"/corridors/{target['id']}/confirm", json={"confirmed_by": "bao", "note": "field check"})
    assert conf.status_code == 201, conf.text
    # A disjoint corridor run far outside the event window must stay untouched.
    far = _corridor_run(
        client, book["id"],
        window={"window_start": "2026-12-01T00:00:00Z", "window_end": "2026-12-01T01:00:00Z"},
    )
    # A second pipeline run re-computes the events (the late/revised evidence
    # path): every old event row is superseded by its replacement.
    _run(client)
    superseded = client.get("/events", params={"status": "superseded"}).json()
    assert superseded["total"] >= len(member_events)

    stale = client.get(f"/corridor-runs/{run['id']}").json()
    assert stale["status"] == "stale"
    assert stale["stale_reason"]
    corridor = client.get(f"/corridors/{target['id']}").json()
    assert corridor["status"] == "pending_review"
    confirmation = corridor["confirmations"][0]
    assert confirmation["status"] == "conflicted"
    assert confirmation["active"] is True  # kept, not auto-revoked
    assert confirmation["conflict"]["superseded_event_ids"]
    # The disjoint run is unaffected (late data touches adjacent windows only).
    far_after = client.get(f"/corridor-runs/{far['id']}").json()
    assert far_after["status"] == "done"


def test_confirmation_lifecycle(client, api, sample_dir):
    load_scenario("crossing", "http://testserver", truth_dir=sample_dir, reset=False)
    _run(client)
    book = _book(client)
    run = _corridor_run(client, book["id"])
    corridor = client.get(f"/corridor-runs/{run['id']}/corridors").json()["corridors"][0]
    conf = client.post(f"/corridors/{corridor['id']}/confirm", json={"confirmed_by": "bao"})
    assert conf.status_code == 201
    again = client.post(f"/corridors/{corridor['id']}/confirm", json={"confirmed_by": "bao"})
    assert again.status_code == 409
    detail = client.get(f"/corridors/{corridor['id']}").json()
    assert detail["status"] == "confirmed"
    revoked = client.delete(f"/corridors/{corridor['id']}/confirm")
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    detail = client.get(f"/corridors/{corridor['id']}").json()
    assert detail["status"] == "candidate"
