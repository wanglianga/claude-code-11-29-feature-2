"""Constraint-book evaluation (约束册求值).

A constraint book bundles the movement rules a corridor candidate must
respect:

* **seasonal speed limits** — per sound class (or wildcard), keyed by
  ``MM-DD`` season ranges that may wrap around the year end;
* **impassable zones** — circles or *convex* polygons the path cannot cross;
* **listening blind zones** — time/area/station scopes in which no detection
  would be produced, used to explain gaps (never to fabricate positions);
* **cross-station clock uncertainty** — extra microseconds of sync doubt
  added when an edge joins events heard by different station sets.

All functions are pure: a book is a plain dict (the JSON columns of
``models.ConstraintBook``), so corridor runs can freeze the exact version
they used and the geometry can be unit-tested without a database.

Geometry notes
--------------
``detour_distance`` returns a *lower bound* of the shortest zone-avoiding
path.  For one circle it is exact (tangent segments + arc); for one convex
polygon it is exact (vertex chain on the shorter side); with several zones
intersected it is ``max`` over the single-zone detours, which never
overstates the true detour.  Reachability verdicts built on this bound are
therefore conservative: an edge reported infeasible is truly impossible,
while a feasible edge may still hide a longer real detour.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Book canonicalisation / versioning
# --------------------------------------------------------------------------- #

CANONICAL_KEYS = (
    "default_max_speed_mps",
    "seasonal_speed_limits",
    "impassable_zones",
    "blind_zones",
    "clock_uncertainties",
    "default_clock_uncertainty_us",
)


def canonical_book(book: dict) -> str:
    """Canonical JSON of the constraint content (excludes name/notes/status)."""
    content = {k: book.get(k) for k in CANONICAL_KEYS}
    return json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)


def book_version_hash(book: dict) -> str:
    return hashlib.sha256(canonical_book(book).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Validation (called at book creation)
# --------------------------------------------------------------------------- #

def validate_book(book: dict) -> list[str]:
    """Return a list of content problems; empty means the book is usable."""
    problems: list[str] = []
    if not (book.get("default_max_speed_mps") or 0) > 0:
        problems.append("default_max_speed_mps must be positive")
    for i, rule in enumerate(book.get("seasonal_speed_limits") or []):
        if not (rule.get("max_speed_mps") or 0) > 0:
            problems.append(f"seasonal_speed_limits[{i}]: max_speed_mps must be positive")
        season = rule.get("season") or {}
        try:
            _parse_mmdd(season["start"])
            _parse_mmdd(season["end"])
        except (KeyError, ValueError) as exc:
            problems.append(f"seasonal_speed_limits[{i}]: bad season range: {exc}")
    for i, zone in enumerate(book.get("impassable_zones") or []):
        kind = zone.get("kind")
        if kind == "circle":
            if not (zone.get("radius_m") or 0) > 0:
                problems.append(f"impassable_zones[{i}]: circle radius_m must be positive")
            if "x" not in zone or "y" not in zone:
                problems.append(f"impassable_zones[{i}]: circle needs x and y")
        elif kind == "polygon":
            pts = zone.get("points") or []
            if len(pts) < 3:
                problems.append(f"impassable_zones[{i}]: polygon needs at least 3 points")
            elif not _is_convex(pts):
                problems.append(
                    f"impassable_zones[{i}]: polygon must be convex; "
                    "split non-convex areas into convex parts"
                )
        else:
            problems.append(f"impassable_zones[{i}]: kind must be circle or polygon")
        if not zone.get("zone_id"):
            problems.append(f"impassable_zones[{i}]: zone_id is required")
    for i, zone in enumerate(book.get("blind_zones") or []):
        if not zone.get("zone_id"):
            problems.append(f"blind_zones[{i}]: zone_id is required")
        try:
            _parse_iso(zone["from_ref_at"])
            _parse_iso(zone["to_ref_at"])
        except (KeyError, ValueError) as exc:
            problems.append(f"blind_zones[{i}]: bad time range: {exc}")
        geom = zone.get("geometry")
        if geom is not None:
            sub = {"zone_id": zone["zone_id"], **geom}
            sub_problems = validate_book(
                {
                    "default_max_speed_mps": 1.0,
                    "impassable_zones": [sub],
                }
            )
            problems.extend(f"blind_zones[{i}]: {p}" for p in sub_problems if "zone_id" not in p)
    for i, rule in enumerate(book.get("clock_uncertainties") or []):
        if not (rule.get("uncertainty_us") or 0) > 0:
            problems.append(f"clock_uncertainties[{i}]: uncertainty_us must be positive")
    if not (book.get("default_clock_uncertainty_us") or 0) >= 0:
        problems.append("default_clock_uncertainty_us must be >= 0")
    return problems


def _parse_mmdd(value: str) -> tuple[int, int]:
    parts = value.split("-")
    if len(parts) != 2:
        raise ValueError(f"expected MM-DD, got {value!r}")
    month, day = int(parts[0]), int(parts[1])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError(f"bad month/day in {value!r}")
    return month, day


def _parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_convex(points: list[list[float]]) -> bool:
    n = len(points)
    if n < 3:
        return False
    sign = 0
    for i in range(n):
        ax, ay = points[i]
        bx, by = points[(i + 1) % n]
        cx, cy = points[(i + 2) % n]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if abs(cross) < 1e-12:
            continue
        s = 1 if cross > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            return False
    return True


# --------------------------------------------------------------------------- #
# Seasonal speed limits
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SpeedRule:
    max_speed_mps: float
    source: dict  # the book rule (or a synthetic default descriptor)


def speed_limit_for(book: dict, class_label: str, ref_us: int) -> SpeedRule:
    """Most specific seasonal rule for a class at a reference instant.

    A class-specific rule beats the wildcard (``class_label: null``); among
    rules of the same specificity the tightest (smallest) limit wins.  With
    no matching rule the book default applies.
    """
    dt = datetime.fromtimestamp(ref_us / 1e6, tz=timezone.utc)
    md = (dt.month, dt.day)
    specific: list[SpeedRule] = []
    wildcard: list[SpeedRule] = []
    for rule in book.get("seasonal_speed_limits") or []:
        rule_class = rule.get("class_label")
        if rule_class is not None and rule_class != class_label:
            continue
        season = rule.get("season") or {}
        if not _in_season(md, _parse_mmdd(season["start"]), _parse_mmdd(season["end"])):
            continue
        bucket = specific if rule_class is not None else wildcard
        bucket.append(SpeedRule(float(rule["max_speed_mps"]), rule))
    for pool in (specific, wildcard):
        if pool:
            return min(pool, key=lambda r: r.max_speed_mps)
    return SpeedRule(
        float(book["default_max_speed_mps"]),
        {"rule": "default_max_speed_mps", "max_speed_mps": book["default_max_speed_mps"]},
    )


def _in_season(md: tuple[int, int], start: tuple[int, int], end: tuple[int, int]) -> bool:
    if start <= end:
        return start <= md <= end
    return md >= start or md <= end  # wraps around the year end


# --------------------------------------------------------------------------- #
# Impassable zones: conservative detour distance
# --------------------------------------------------------------------------- #

@dataclass
class Detour:
    distance_m: float
    direct_m: float
    zones_crossed: list[str] = field(default_factory=list)


def detour_distance(
    ax: float, ay: float, bx: float, by: float, zones: list[dict]
) -> Detour:
    """Lower bound of the shortest path from (ax, ay) to (bx, by) avoiding zones."""
    direct = math.hypot(bx - ax, by - ay)
    best = direct
    crossed: list[str] = []
    for zone in zones or []:
        single = _single_zone_detour(ax, ay, bx, by, zone)
        if single is not None:
            crossed.append(zone.get("zone_id", "?"))
            best = max(best, single)
    return Detour(distance_m=best, direct_m=direct, zones_crossed=crossed)


def _single_zone_detour(ax: float, ay: float, bx: float, by: float, zone: dict) -> float | None:
    """Detour length if the segment a->b is blocked by this zone, else None."""
    if zone.get("kind") == "circle":
        cx, cy, r = float(zone["x"]), float(zone["y"]), float(zone["radius_m"])
        if not _segment_hits_circle(ax, ay, bx, by, cx, cy, r):
            return None
        return _circle_detour(ax, ay, bx, by, cx, cy, r)
    if zone.get("kind") == "polygon":
        pts = [(float(p[0]), float(p[1])) for p in zone["points"]]
        if not _segment_hits_polygon(ax, ay, bx, by, pts):
            return None
        return _polygon_detour(ax, ay, bx, by, pts)
    return None


def _segment_hits_circle(ax, ay, bx, by, cx, cy, r) -> bool:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(ax - cx, ay - cy) < r
    t = max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / length_sq))
    px, py = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy) < r - 1e-9


def _circle_detour(ax, ay, bx, by, cx, cy, r) -> float:
    """Exact shortest path around a circular obstacle (both ends outside)."""
    d1 = math.hypot(ax - cx, ay - cy)
    d2 = math.hypot(bx - cx, by - cy)
    if d1 <= r or d2 <= r:
        # An endpoint lies inside the zone; fall back to the direct distance
        # (the caller's region overlap makes the edge implausible anyway).
        return math.hypot(bx - ax, by - ay)
    t1 = math.sqrt(d1 * d1 - r * r)
    t2 = math.sqrt(d2 * d2 - r * r)
    phi_a = math.atan2(ay - cy, ax - cx)
    phi_b = math.atan2(by - cy, bx - cx)
    alpha1 = math.acos(r / d1)
    alpha2 = math.acos(r / d2)
    two_pi = 2.0 * math.pi
    arcs = []
    for s in (1.0, -1.0):
        psi_a = phi_a + s * alpha1
        psi_b = phi_b - s * alpha2
        arc = (psi_b - psi_a) % two_pi
        arcs.append(arc)
    arc = min(arcs)
    return t1 + t2 + r * arc


def _point_in_polygon(px: float, py: float, pts: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _segments_intersect(ax, ay, bx, by, cx, cy, dx, dy) -> bool:
    def orient(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)

    o1 = orient(ax, ay, bx, by, cx, cy)
    o2 = orient(ax, ay, bx, by, dx, dy)
    o3 = orient(cx, cy, dx, dy, ax, ay)
    o4 = orient(cx, cy, dx, dy, bx, by)
    return (o1 * o2 < 0) and (o3 * o4 < 0)


def _segment_hits_polygon(ax, ay, bx, by, pts) -> bool:
    if _point_in_polygon(ax, ay, pts) or _point_in_polygon(bx, by, pts):
        return True
    n = len(pts)
    for i in range(n):
        cx, cy = pts[i]
        dx, dy = pts[(i + 1) % n]
        if _segments_intersect(ax, ay, bx, by, cx, cy, dx, dy):
            return True
    return False


def _polygon_detour(ax, ay, bx, by, pts) -> float:
    """Exact shortest detour around a convex polygon (vertex chain, both sides)."""
    n = len(pts)
    # Tangent vertices: neighbours on the same side of the line from the point.
    def tangents(px, py):
        out = []
        for i in range(n):
            vx, vy = pts[i]
            prev = pts[(i - 1) % n]
            nxt = pts[(i + 1) % n]
            c1 = (vx - px) * (prev[1] - py) - (vy - py) * (prev[0] - px)
            c2 = (vx - px) * (nxt[1] - py) - (vy - py) * (nxt[0] - px)
            if c1 * c2 >= 0:  # both neighbours on one side -> tangent vertex
                out.append(i)
        return out

    ta = tangents(ax, ay)
    tb = tangents(bx, by)
    best = math.inf
    for i in ta:
        for j in tb:
            for direction in (1, -1):
                length = math.hypot(pts[i][0] - ax, pts[i][1] - ay)
                k = i
                while True:
                    nxt = (k + direction) % n
                    length += math.hypot(pts[nxt][0] - pts[k][0], pts[nxt][1] - pts[k][1])
                    k = nxt
                    if k == j:
                        break
                length += math.hypot(bx - pts[j][0], by - pts[j][1])
                best = min(best, length)
    if best is math.inf:
        return math.hypot(bx - ax, by - ay)
    return best


# --------------------------------------------------------------------------- #
# Blind zones (监听盲区): gap explanation only
# --------------------------------------------------------------------------- #

@dataclass
class BlindOverlap:
    zone_id: str
    station_id: str | None
    overlap_from_us: int
    overlap_to_us: int
    reason: str | None


def blind_zones_for_gap(
    book: dict,
    from_us: int,
    to_us: int,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> list[BlindOverlap]:
    """Blind zones overlapping the gap in time *and* lying on the gap path.

    The spatial test uses the segment between the two events' representative
    points: a zone counts when its geometry contains an endpoint or crosses
    that segment.  Zones without geometry apply everywhere.
    """
    out: list[BlindOverlap] = []
    for zone in book.get("blind_zones") or []:
        z_from = int(_parse_iso(zone["from_ref_at"]).timestamp() * 1e6)
        z_to = int(_parse_iso(zone["to_ref_at"]).timestamp() * 1e6)
        lo = max(from_us, z_from)
        hi = min(to_us, z_to)
        if lo > hi:
            continue
        geom = zone.get("geometry")
        if geom is not None:
            kind = geom.get("kind")
            if kind == "circle":
                hit = _segment_hits_circle(
                    ax, ay, bx, by, float(geom["x"]), float(geom["y"]), float(geom["radius_m"])
                ) or math.hypot(ax - float(geom["x"]), ay - float(geom["y"])) < float(geom["radius_m"]) \
                    or math.hypot(bx - float(geom["x"]), by - float(geom["y"])) < float(geom["radius_m"])
            elif kind == "polygon":
                pts = [(float(p[0]), float(p[1])) for p in geom["points"]]
                hit = _segment_hits_polygon(ax, ay, bx, by, pts)
            else:
                hit = False
            if not hit:
                continue
        out.append(
            BlindOverlap(
                zone_id=zone.get("zone_id", "?"),
                station_id=zone.get("station_id"),
                overlap_from_us=lo,
                overlap_to_us=hi,
                reason=zone.get("reason"),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Cross-station clock uncertainty
# --------------------------------------------------------------------------- #

def clock_uncertainty_us(book: dict, stations_a: set[str], stations_b: set[str]) -> int:
    """Extra sync doubt (µs) for an edge joining two station sets.

    An exact pair rule (naming one station from each set) is authoritative;
    otherwise the largest matching single-station rule; with no rule the book
    default applies.
    """
    single: list[int] = []
    for rule in book.get("clock_uncertainties") or []:
        a, b = rule.get("station_a"), rule.get("station_b")
        value = int(rule["uncertainty_us"])
        if a and b:
            if (a in stations_a and b in stations_b) or (b in stations_a and a in stations_b):
                return value
        elif a or b:
            s = a or b
            if s in stations_a or s in stations_b:
                single.append(value)
    if single:
        return max(single)
    return int(book.get("default_clock_uncertainty_us") or 0)
