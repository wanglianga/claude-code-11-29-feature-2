"""Run the independent corridor verifier inside pytest for corridor scenarios."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import verifier.verify_corridors as corridor_verifier
from simulator.run_simulator import load_scenario
from simulator.scenarios import build_scenario

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


class _SessionShim:
    """Adapt FastAPI's TestClient to the ``requests.Session`` subset used."""

    def __init__(self, client):
        self.client = client
        self.headers: dict = {}

    def get(self, url, params=None, timeout=None):
        path = url.split("testserver", 1)[1]
        return self.client.get(path, params=params)


class _RequestsShim:
    def __init__(self, client):
        self._client = client

    def Session(self):  # noqa: N802 - mirrors requests
        return _SessionShim(self._client)


def _run(client, **body):
    res = client.post("/runs", json={"wait": True, **body})
    assert res.status_code == 201, res.text
    return res.json()


def _setup_corridors(client, scenario: str):
    """Load the scenario, run the pipeline, create books and corridor runs."""
    load_scenario(scenario, "http://testserver", truth_dir=SAMPLES / scenario, reset=False)
    _run(client)
    scn = build_scenario(scenario)
    book_ids: dict[tuple[str, int], int] = {}
    for book in scn.corridor["books"]:
        res = client.post("/constraint-books", json=book)
        assert res.status_code == 201, res.text
        created = res.json()
        book_ids[(book["name"], created["version"])] = created["id"]
    from simulator.scenarios import REFERENCE_EPOCH
    from datetime import timedelta

    def iso(offset_s: float) -> str:
        return (REFERENCE_EPOCH + timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")

    window = scn.corridor["window_s"]
    for spec in scn.corridor["runs"]:
        version = spec.get("book_version", 1)
        res = client.post(
            "/corridor-runs",
            json={
                "method": spec["method"],
                "window_start": iso(window[0]),
                "window_end": iso(window[1]),
                "constraint_book_id": book_ids[(spec["book"], version)],
                **spec.get("params", {}),
            },
        )
        assert res.status_code == 201, res.text


@pytest.mark.parametrize("scenario", ["crossing", "simultaneous", "blindgap", "clockskew"])
def test_corridor_verifier_scenarios(client, api, scenario):
    _setup_corridors(client, scenario)
    truth = SAMPLES / scenario / "truth.json"
    report = truth.parent / "corridor_verification.json"
    original = corridor_verifier.requests
    corridor_verifier.requests = _RequestsShim(client)
    try:
        code = corridor_verifier.run_checks(scenario, truth, "http://testserver", "test-key")
        result = json.loads(report.read_text())
    finally:
        corridor_verifier.requests = original
        report.unlink(missing_ok=True)
    assert code == 0, [c for c in result["checks"] if not c["ok"]]


def test_corridor_verifier_is_independent_of_service_code():
    source = Path(corridor_verifier.__file__).read_text()
    for forbidden in ("from app", "import app"):
        assert forbidden not in source
