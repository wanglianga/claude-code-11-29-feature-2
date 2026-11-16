"""Corridor-scenario driver: constraint books and corridor runs for a scenario.

Usage::

    python -m simulator.corridor_driver --scenario crossing --base-url http://127.0.0.1:8000

The scenario must already be loaded (stations, classes, uploads) and the event
pipeline must have run.  The driver reads the scenario's ``corridor`` block,
creates each constraint book (versions increment per name), executes the
configured corridor runs and prints a compact summary.  The independent
verifier (``verifier/verify_corridors.py``) then checks the output against
ground truth.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simulator.scenarios import build_scenario  # noqa: E402

API_KEY = "dev-api-key"


def run_corridor_config(base: str, api_key: str, scenario: str) -> dict:
    scn = build_scenario(scenario)
    session = requests.Session()
    session.headers.update({"X-API-Key": api_key})

    def req(method: str, path: str, **kwargs):
        r = session.request(method, base.rstrip("/") + path, timeout=120, **kwargs)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:600]}")
        return r.json()

    book_ids: dict[tuple[str, int], int] = {}
    for book in scn.corridor.get("books", []):
        created = req("POST", "/constraint-books", json=book)
        book_ids[(book["name"], created["version"])] = created["id"]
        print(f"constraint book {book['name']} v{created['version']}: id={created['id']} hash={created['version_hash']}")

    window = scn.corridor["window_s"]
    from datetime import datetime, timedelta, timezone

    from simulator.scenarios import REFERENCE_EPOCH

    def iso(offset_s: float) -> str:
        return (REFERENCE_EPOCH + timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")

    results = []
    for spec in scn.corridor.get("runs", []):
        version = spec.get("book_version", 1)
        book_id = book_ids[(spec["book"], version)]
        body = {
            "method": spec["method"],
            "window_start": iso(window[0]),
            "window_end": iso(window[1]),
            "constraint_book_id": book_id,
            **spec.get("params", {}),
        }
        run = req("POST", "/corridor-runs", json=body)
        corridors = req("GET", f"/corridor-runs/{run['id']}/corridors")
        unattributed = req("GET", f"/corridor-runs/{run['id']}/unattributed")
        print(
            f"corridor run {run['id']} ({spec['method']}, book {spec['book']} v{version}): "
            f"{corridors['total']} corridors, {unattributed['total']} unattributed"
        )
        results.append(
            {
                "corridor_run_id": run["id"],
                "method": spec["method"],
                "book": spec["book"],
                "book_version": version,
                "corridors": corridors["total"],
                "unattributed": unattributed["total"],
            }
        )
    return {"scenario": scenario, "runs": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corridor scenario driver")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default=API_KEY)
    args = parser.parse_args(argv)
    summary = run_corridor_config(args.base_url, args.api_key, args.scenario)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
