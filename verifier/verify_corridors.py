"""Independent corridor verifier (独立通道校验器).

Shares no code with the service.  Given a corridor scenario's ``truth.json``
it checks, through the public API only:

* **candidate edges** — every corridor edge's feasibility verdict is
  recomputed from the published event regions and the constraint book:
  min region distance vs. seasonal speed limit x widened time interval;
* **unattributed events** — the events no corridor claimed match the truth
  (noise sources, and nothing from a real track);
* **constraint versions** — each corridor run's recorded
  ``constraint_version_hash`` is recomputed from the fetched book content;
* **competition** — near-tie competitors / alternate decompositions exist
  where the scenario forces them (crossing, simultaneous calls);
* **gaps** — gap entries carry durations and blind-zone references but never
  coordinates (no fabricated positions inside a blind window);
* **identity** — corridor payloads contain no animal/individual fields.

Usage::

    python -m verifier.verify_corridors --scenario crossing \
        --base-url http://127.0.0.1:8000 --api-key dev-api-key
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'PASS' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


def _iso_to_us(value: str) -> int:
    text = value.replace("Z", "+00:00")
    return int(round(datetime.fromisoformat(text).timestamp() * 1_000_000))


# --------------------------------------------------------------------------- #
# Canonical constraint-book hash (re-implemented from the documented rule)
# --------------------------------------------------------------------------- #
CANONICAL_KEYS = (
    "default_max_speed_mps",
    "seasonal_speed_limits",
    "impassable_zones",
    "blind_zones",
    "clock_uncertainties",
    "default_clock_uncertainty_us",
)


def book_hash(content: dict) -> str:
    canonical = json.dumps(
        {k: content.get(k) for k in CANONICAL_KEYS},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Independent edge feasibility recomputation
# --------------------------------------------------------------------------- #
def bbox_min_distance(a: dict, b: dict) -> float:
    dx = max(a["x_min"] - b["x_max"], b["x_min"] - a["x_max"], 0.0)
    dy = max(a["y_min"] - b["y_max"], b["y_min"] - a["y_max"], 0.0)
    return math.hypot(dx, dy)


def seasonal_limit(book: dict, class_label: str, ref_us: int) -> float:
    dt = datetime.fromtimestamp(ref_us / 1e6, tz=timezone.utc)
    md = (dt.month, dt.day)
    best_specific = None
    best_wild = None
    for rule in book.get("seasonal_speed_limits") or []:
        start = tuple(int(p) for p in rule["season"]["start"].split("-"))
        end = tuple(int(p) for p in rule["season"]["end"].split("-"))
        in_season = start <= md <= end if start <= end else (md >= start or md <= end)
        if not in_season:
            continue
        if rule.get("class_label") is not None:
            if rule["class_label"] == class_label:
                best_specific = (
                    rule["max_speed_mps"]
                    if best_specific is None
                    else min(best_specific, rule["max_speed_mps"])
                )
        else:
            best_wild = (
                rule["max_speed_mps"] if best_wild is None else min(best_wild, rule["max_speed_mps"])
            )
    if best_specific is not None:
        return best_specific
    if best_wild is not None:
        return best_wild
    return book["default_max_speed_mps"]


def clock_uncertainty(book: dict, stations_a: set, stations_b: set) -> int:
    single = []
    for rule in book.get("clock_uncertainties") or []:
        a, b = rule.get("station_a"), rule.get("station_b")
        if a and b:
            if (a in stations_a and b in stations_b) or (b in stations_a and a in stations_b):
                return rule["uncertainty_us"]
        elif a or b:
            if (a or b) in stations_a or (a or b) in stations_b:
                single.append(rule["uncertainty_us"])
    if single:
        return max(single)
    return book.get("default_clock_uncertainty_us", 0)


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #
class Api:
    def __init__(self, base: str, key: str):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": key})

    def get(self, path: str, **params):
        r = self.session.get(self.base + path, params=params, timeout=60)
        r.raise_for_status()
        return r.json()


# --------------------------------------------------------------------------- #
# Truth matching
# --------------------------------------------------------------------------- #
def match_events_to_truth(events: list[dict], truth_events: list[dict]) -> dict[int, dict]:
    """Match published events to truth calls.

    Simultaneous calls make time-only matching ambiguous, so candidate pairs
    (within 5 s and 250 m) are assigned greedily by position distance —
    an independent re-derivation, not the service's own association.
    """
    pairs = []
    for ev in events:
        t0 = _iso_to_us(ev["emission"]["at"])
        for cand in truth_events:
            dt = abs(_iso_to_us(cand["emission"]) - t0)
            if dt > 5_000_000:
                continue
            dist = math.hypot(ev["position"]["x"] - cand["x"], ev["position"]["y"] - cand["y"])
            if dist <= 250.0:
                pairs.append((dist, ev["id"], cand["key"], ev, cand))
    pairs.sort(key=lambda p: p[0])
    mapping: dict[int, dict] = {}
    used_truth: set[str] = set()
    for dist, event_id, key, ev, cand in pairs:
        if event_id in mapping or key in used_truth:
            continue
        mapping[event_id] = cand
        used_truth.add(key)
    return mapping


# --------------------------------------------------------------------------- #
# Main verification
# --------------------------------------------------------------------------- #
def run_checks(scenario: str, truth_path: Path, base_url: str, api_key: str) -> int:
    truth = json.loads(truth_path.read_text())
    expect = truth["corridor"]["expect"]
    api = Api(base_url, api_key)
    checks: list[Check] = []

    events = api.get("/events", limit=1000)["events"]
    live = [e for e in events if e["status"] != "superseded"]
    truth_calls = [e for e in truth["events"] if e["heard_by"]]
    mapping = match_events_to_truth(live, truth_calls)
    event_by_id = {e["id"]: e for e in live}

    runs = api.get("/corridor-runs", limit=50)
    books = {b["id"]: b for b in api.get("/constraint-books")}
    corridors_by_run: dict[int, list[dict]] = {}
    details: dict[int, dict] = {}
    for run in runs:
        corridors_by_run[run["id"]] = api.get(f"/corridor-runs/{run['id']}/corridors")["corridors"]
        for c in corridors_by_run[run["id"]]:
            details[c["id"]] = api.get(f"/corridors/{c['id']}")

    # -- constraint versions -------------------------------------------------
    mismatches = []
    for run in runs:
        book = books.get(run["constraint_book_id"])
        if book is None:
            mismatches.append(f"run {run['id']}: book missing")
            continue
        recomputed = book_hash(book)
        if recomputed != run["constraint_version_hash"]:
            mismatches.append(
                f"run {run['id']}: recorded {run['constraint_version_hash']} != recomputed {recomputed}"
            )
    checks.append(
        Check(
            "corridor runs pin the recomputed constraint-book version hash",
            not mismatches,
            "; ".join(mismatches) if mismatches else f"{len(runs)} runs match their books",
        )
    )

    # -- identity / gap hygiene ----------------------------------------------
    identity_hits = []
    gap_coord_hits = []
    for det in details.values():
        payload = json.dumps(det)
        for bad in ('"animal"', '"individual"'):
            if bad in payload:
                identity_hits.append(f"corridor {det['id']} contains {bad}")
        for gap in det.get("gaps", []):
            for key in gap:
                if key.lower() in {"x", "y", "position", "coordinates", "lat", "lon"}:
                    gap_coord_hits.append(f"corridor {det['id']} gap carries {key}")
    checks.append(
        Check(
            "corridors carry no individual-identity fields",
            not identity_hits,
            "; ".join(identity_hits) if identity_hits else "identity_note only",
        )
    )
    checks.append(
        Check(
            "gap entries never carry coordinates",
            not gap_coord_hits,
            "; ".join(gap_coord_hits) if gap_coord_hits else f"{sum(len(d.get('gaps', [])) for d in details.values())} gaps checked",
        )
    )

    # -- candidate edges: independent feasibility recomputation ---------------
    edge_errors = []
    edges_checked = 0
    for det in details.values():
        run = next(r for r in runs if r["id"] == det["run_id"])
        book = books[run["constraint_book_id"]]
        members = det["members"]
        for m in members:
            edge = m.get("edge_to_next")
            if not edge:
                continue
            edges_checked += 1
            a = event_by_id[m["event_id"]]
            b = event_by_id[edge["to_event_id"]]
            # Independent recomputation from published regions.
            regions_a = a["regions"] or [{"bounds": a["position"]["bbox"]}]
            regions_b = b["regions"] or [{"bounds": b["position"]["bbox"]}]
            dist_min = min(
                bbox_min_distance(ra["bounds"], rb["bounds"])
                for ra in regions_a
                for rb in regions_b
            )
            a_stations = {o["station_id"] for o in api.get(f"/events/{a['id']}")["observations"]}
            b_stations = {o["station_id"] for o in api.get(f"/events/{b['id']}")["observations"]}
            clock_us = clock_uncertainty(book, a_stations, b_stations)
            dt_max = (
                _iso_to_us(b["emission"]["max"]) - _iso_to_us(a["emission"]["min"]) + 2 * clock_us
            ) / 1e6
            mid = (_iso_to_us(a["emission"]["at"]) + _iso_to_us(b["emission"]["at"])) // 2
            limit = seasonal_limit(book, a["class_label"], mid)
            feasible = dist_min <= limit * dt_max + 1e-9
            if feasible != edge["feasible"]:
                edge_errors.append(
                    f"corridor {det['id']} edge {a['id']}->{b['id']}: service={edge['feasible']} "
                    f"recomputed={feasible} (dist_min={dist_min:.0f} limit*dt={limit * dt_max:.0f})"
                )
            if abs(edge["clock_uncertainty_us"] - clock_us) > 1:
                edge_errors.append(
                    f"corridor {det['id']} edge {a['id']}->{b['id']}: clock uncertainty "
                    f"{edge['clock_uncertainty_us']} != recomputed {clock_us}"
                )
            if abs(edge["speed_limit_mps"] - limit) > 1e-9:
                edge_errors.append(
                    f"corridor {det['id']} edge {a['id']}->{b['id']}: speed limit "
                    f"{edge['speed_limit_mps']} != recomputed {limit}"
                )
    checks.append(
        Check(
            "candidate edges recompute to the same feasibility/limits",
            not edge_errors,
            "; ".join(edge_errors[:3]) if edge_errors else f"{edges_checked} edges recomputed",
        )
    )

    # -- track coverage --------------------------------------------------------
    covered_ids = set()
    for det in details.values():
        for m in det["members"]:
            covered_ids.add(m["event_id"])
    track_truth = [c for c in truth_calls if c["source_id"].startswith("T")]
    covered_truth = {mapping[eid]["key"] for eid in covered_ids if eid in mapping}
    missing = [c["key"] for c in track_truth if c["key"] not in covered_truth]
    checks.append(
        Check(
            "every recorded track call is covered by some corridor",
            not missing,
            f"missing: {missing}" if missing else f"{len(track_truth)} track calls covered",
        )
    )

    # -- unattributed ----------------------------------------------------------
    unattributed_by_run = {
        run["id"]: api.get(f"/corridor-runs/{run['id']}/unattributed")["events"] for run in runs
    }
    expected_un = expect.get("unattributed_events")
    if expected_un is not None:
        for run in runs:
            rows = unattributed_by_run[run["id"]]
            noise_keys = {mapping[r["event_id"]]["key"] for r in rows if r["event_id"] in mapping}
            expected_keys = {
                c["key"] for c in truth_calls if not c["source_id"].startswith("T")
            }
            ok = len(rows) == expected_un and (
                not expected_keys or noise_keys == expected_keys
            )
            checks.append(
                Check(
                    f"unattributed events match expectation (run {run['id']})",
                    ok,
                    f"{len(rows)} unattributed ({sorted(noise_keys)}), expected {expected_un}",
                )
            )

    # -- competition -----------------------------------------------------------
    if expect.get("competitors_present"):
        n_comp = sum(len(d.get("competitors", [])) for d in details.values())
        n_alt = sum(
            1 for d in details.values() if d["status"] in {"alternate", "tied_competitor"}
        )
        checks.append(
            Check(
                "near-tie competition is surfaced (competitors or alternates)",
                n_comp > 0 and n_alt > 0,
                f"{n_comp} competitor rows, {n_alt} alternate/tied corridors",
            )
        )
    if expect.get("ties_present"):
        tied = [d for d in details.values() if d["tie_group"] is not None]
        checks.append(
            Check(
                "tied corridors share a tie group",
                len(tied) >= 2,
                f"{len(tied)} corridors in tie groups",
            )
        )
    if expect.get("min_corridors"):
        for run in runs:
            n = len(corridors_by_run[run["id"]])
            checks.append(
                Check(
                    f"at least {expect['min_corridors']} corridors published (run {run['id']})",
                    n >= expect["min_corridors"],
                    f"{n} corridors",
                )
            )

    # -- simultaneous pairs never share a corridor ------------------------------
    if expect.get("no_simultaneous_pairs_in_one_corridor"):
        violations = []
        for det in details.values():
            times = []
            for m in det["members"]:
                ev = event_by_id.get(m["event_id"])
                if ev is None:
                    continue
                truth_ev = mapping.get(m["event_id"])
                if truth_ev:
                    times.append((truth_ev["emission_s"], det["id"]))
            seen: list[float] = []
            for t, cid in times:
                for t0 in seen:
                    if abs(t - t0) < 10.0:  # same calling slot (3 s offset in truth)
                        violations.append(f"corridor {cid}: calls at {t0}s and {t}s")
                seen.append(t)
        checks.append(
            Check(
                "no corridor chains two near-simultaneous calls",
                not violations,
                "; ".join(violations[:3]) if violations else "all corridor members time-separated",
            )
        )

    # -- blind gap --------------------------------------------------------------
    if expect.get("gap_present"):
        gap_rows = [
            (det, gap)
            for det in details.values()
            for gap in det.get("gaps", [])
        ]
        checks.append(
            Check(
                "a gap entry exists for the blind window",
                bool(gap_rows),
                f"{len(gap_rows)} gap entries",
            )
        )
        zone = expect.get("gap_blind_zone")
        if zone:
            with_zone = [g for _, g in gap_rows if any(z["zone_id"] == zone for z in g["blind_zones"])]
            without = [g for _, g in gap_rows if not g["blind_zones"]]
            checks.append(
                Check(
                    f"gap cites blind zone {zone} under the book that knows it",
                    bool(with_zone),
                    f"{len(with_zone)} gaps cite {zone}",
                )
            )
            checks.append(
                Check(
                    "the book version without the blind zone leaves the gap unexplained",
                    bool(without),
                    f"{len(without)} gaps without a blind-zone explanation",
                )
            )

    # -- clock-policy edge difference -------------------------------------------
    if expect.get("v1_corridor_event_count"):
        ordered = sorted(runs, key=lambda r: r["id"])
        v1_run, v2_run = ordered[0], ordered[1]
        v1_counts = sorted(c["event_count"] for c in corridors_by_run[v1_run["id"]])
        v2_counts = sorted(c["event_count"] for c in corridors_by_run[v2_run["id"]])
        checks.append(
            Check(
                "loose clock policy links the full track",
                v1_counts == [expect["v1_corridor_event_count"]],
                f"v1 corridor sizes {v1_counts}",
            )
        )
        checks.append(
            Check(
                "tight clock policy breaks the tight edge",
                v2_counts == sorted(expect["v2_corridor_event_counts"]),
                f"v2 corridor sizes {v2_counts}",
            )
        )
        checks.append(
            Check(
                "the two runs record different constraint versions",
                v1_run["constraint_version_hash"] != v2_run["constraint_version_hash"],
                f"{v1_run['constraint_version_hash']} vs {v2_run['constraint_version_hash']}",
            )
        )

    print(f"\n=== Corridor verification: {scenario} ({truth['title']}) ===")
    for check in checks:
        print(" ", check)
    failed = [c for c in checks if not c.ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    payload = {
        "scenario": scenario,
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "all_passed": not failed,
    }
    (truth_path.parent / "corridor_verification.json").write_text(json.dumps(payload, indent=2))
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent corridor verifier")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--truth", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default="dev-api-key")
    args = parser.parse_args(argv)
    truth = args.truth or Path("samples") / args.scenario / "truth.json"
    return run_checks(args.scenario, truth, args.base_url, args.api_key)


if __name__ == "__main__":
    raise SystemExit(main())
