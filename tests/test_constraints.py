"""Unit tests for the constraint-book evaluation primitives."""

from __future__ import annotations

import math

from app import constraints


def _book(**overrides):
    book = {
        "default_max_speed_mps": 2.0,
        "seasonal_speed_limits": [],
        "impassable_zones": [],
        "blind_zones": [],
        "clock_uncertainties": [],
        "default_clock_uncertainty_us": 0,
    }
    book.update(overrides)
    return book


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_validate_accepts_minimal_book():
    assert constraints.validate_book(_book()) == []


def test_validate_rejects_nonpositive_default_speed():
    problems = constraints.validate_book(_book(default_max_speed_mps=0))
    assert any("default_max_speed_mps" in p for p in problems)


def test_validate_rejects_nonconvex_polygon():
    dart = [[0, 0], [4, 0], [4, 4], [2, 1], [0, 4]]  # concave dart
    problems = constraints.validate_book(
        _book(impassable_zones=[{"zone_id": "Z1", "kind": "polygon", "points": dart}])
    )
    assert any("convex" in p for p in problems)


def test_validate_rejects_bad_season():
    problems = constraints.validate_book(
        _book(
            seasonal_speed_limits=[
                {"class_label": None, "season": {"start": "13-01", "end": "02-01"}, "max_speed_mps": 1.0}
            ]
        )
    )
    assert any("season" in p for p in problems)


def test_version_hash_changes_with_content_and_ignores_name():
    a = _book()
    b = _book(default_max_speed_mps=3.0)
    assert constraints.book_version_hash(a) != constraints.book_version_hash(b)
    assert constraints.book_version_hash(a) == constraints.book_version_hash(dict(a))


# --------------------------------------------------------------------------- #
# Seasonal speed limits
# --------------------------------------------------------------------------- #
def _us(month: int, day: int) -> int:
    from datetime import datetime, timezone

    return int(datetime(2026, month, day, tzinfo=timezone.utc).timestamp() * 1e6)


def test_seasonal_rule_beats_default_and_wraps_year_end():
    book = _book(
        seasonal_speed_limits=[
            {
                "class_label": None,
                "season": {"start": "11-01", "end": "02-01"},
                "max_speed_mps": 0.5,
                "note": "winter torpor",
            }
        ]
    )
    winter = constraints.speed_limit_for(book, "chirp", _us(1, 15))
    summer = constraints.speed_limit_for(book, "chirp", _us(6, 15))
    assert winter.max_speed_mps == 0.5
    assert summer.max_speed_mps == 2.0


def test_class_specific_rule_beats_wildcard():
    book = _book(
        seasonal_speed_limits=[
            {"class_label": None, "season": {"start": "01-01", "end": "12-31"}, "max_speed_mps": 0.4},
            {"class_label": "croak", "season": {"start": "01-01", "end": "12-31"}, "max_speed_mps": 1.5},
        ]
    )
    assert constraints.speed_limit_for(book, "croak", _us(3, 1)).max_speed_mps == 1.5
    assert constraints.speed_limit_for(book, "chirp", _us(3, 1)).max_speed_mps == 0.4


# --------------------------------------------------------------------------- #
# Impassable zones
# --------------------------------------------------------------------------- #
def test_circle_detour_exact_symmetric_case():
    zone = {"zone_id": "Z", "kind": "circle", "x": 0.0, "y": 0.0, "radius_m": 10.0}
    d = 20.0 * math.sqrt(2)  # endpoints at distance d from centre on the x axis
    det = constraints.detour_distance(-d, 0.0, d, 0.0, [zone])
    alpha = math.acos(10.0 / d)
    expected = 2 * math.sqrt(d * d - 100.0) + 10.0 * (math.pi - 2 * alpha)
    assert det.zones_crossed == ["Z"]
    assert det.distance_m == expected


def test_detour_unblocked_segment_keeps_direct_distance():
    zone = {"zone_id": "Z", "kind": "circle", "x": 0.0, "y": 100.0, "radius_m": 10.0}
    det = constraints.detour_distance(-50.0, 0.0, 50.0, 0.0, [zone])
    assert det.zones_crossed == []
    assert det.distance_m == 100.0


def test_polygon_detour_uses_vertex_chain():
    zone = {
        "zone_id": "P",
        "kind": "polygon",
        "points": [[-10, -10], [10, -10], [10, 10], [-10, 10]],
    }
    det = constraints.detour_distance(-30.0, 0.0, 30.0, 0.0, [zone])
    # Shortest way around the square: (-30,0) -> corner -> corner -> (30,0).
    expected = math.hypot(20.0, 10.0) * 2 + 20.0
    assert det.zones_crossed == ["P"]
    assert abs(det.distance_m - expected) < 1e-6


def test_detour_lower_bound_never_below_direct():
    zones = [
        {"zone_id": "A", "kind": "circle", "x": 0.0, "y": 0.0, "radius_m": 5.0},
        {"zone_id": "B", "kind": "circle", "x": 20.0, "y": 0.0, "radius_m": 5.0},
    ]
    det = constraints.detour_distance(-20.0, 0.0, 40.0, 0.0, zones)
    assert det.distance_m >= 60.0
    assert set(det.zones_crossed) == {"A", "B"}


# --------------------------------------------------------------------------- #
# Blind zones
# --------------------------------------------------------------------------- #
def test_blind_zone_matches_time_and_space():
    book = _book(
        blind_zones=[
            {
                "zone_id": "B1",
                "station_id": None,
                "from_ref_at": "2026-11-14T01:00:00Z",
                "to_ref_at": "2026-11-14T02:00:00Z",
                "geometry": {"zone_id": "B1", "kind": "circle", "x": 100.0, "y": 0.0, "radius_m": 50.0},
                "reason": "recorder maintenance",
            }
        ]
    )
    t0 = int(1_763_000_000 * 1e6)  # arbitrary
    hits = constraints.blind_zones_for_gap(book, t0, t0 + 3600_000_000, 0.0, 0.0, 200.0, 0.0)
    # Time range must intersect the zone's window to count.
    from datetime import datetime, timezone

    z0 = int(datetime(2026, 11, 14, 0, 30, tzinfo=timezone.utc).timestamp() * 1e6)
    z1 = int(datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc).timestamp() * 1e6)
    hits = constraints.blind_zones_for_gap(book, z0, z1, 0.0, 0.0, 200.0, 0.0)
    assert [h.zone_id for h in hits] == ["B1"]
    # A path far away from the zone geometry does not match.
    misses = constraints.blind_zones_for_gap(book, z0, z1, 0.0, 500.0, 200.0, 500.0)
    assert misses == []


# --------------------------------------------------------------------------- #
# Clock uncertainty
# --------------------------------------------------------------------------- #
def test_clock_uncertainty_pair_rule_beats_default():
    book = _book(
        clock_uncertainties=[
            {"station_a": "S1", "station_b": "S2", "uncertainty_us": 250_000},
            {"station_a": "S3", "station_b": None, "uncertainty_us": 100_000},
        ],
        default_clock_uncertainty_us=50_000,
    )
    assert constraints.clock_uncertainty_us(book, {"S1"}, {"S2"}) == 250_000
    assert constraints.clock_uncertainty_us(book, {"S2"}, {"S1"}) == 250_000  # symmetric
    assert constraints.clock_uncertainty_us(book, {"S3"}, {"S9"}) == 100_000
    assert constraints.clock_uncertainty_us(book, {"S8"}, {"S9"}) == 50_000
