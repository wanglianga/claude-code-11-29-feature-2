"""Corridor graph: frozen event snapshot plus reachability edges.

The corridor layer consumes the *event library* (finalised or provisional
events with time intervals, feasible regions, features and revisions) and
evaluates pairwise reachability under one constraint book.  Every edge
carries a human-readable explanation of the constraints that were applied,
and every rejected pair records the reason — the hypothesis solver surfaces
those reasons directly, and the flow solver uses the same edge set.

No individual identity is ever inferred here: an edge only says "these two
events *could* be consecutive observations of one passage under this
constraint book".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import constraints
from .models import Event, EventObservation, Observation


# --------------------------------------------------------------------------- #
# Event snapshot
# --------------------------------------------------------------------------- #
@dataclass
class EventNode:
    event_id: int
    revision: int
    class_label: str
    status: str
    emission_us: int
    emission_min_us: int
    emission_max_us: int
    # Feasible position: per-region bounding boxes plus the overall box.
    region_boxes: list[dict]
    bbox: dict
    representative: dict  # event x/y (a region representative, not a gap point)
    features: list[float]
    feature_limit: float
    stations: set[str]
    observation_count: int

    def snapshot_row(self) -> dict:
        return {
            "event_id": self.event_id,
            "revision": self.revision,
            "class_label": self.class_label,
            "status": self.status,
            "emission_us": self.emission_us,
            "emission_min_us": self.emission_min_us,
            "emission_max_us": self.emission_max_us,
            "bbox": self.bbox,
            "features": self.features,
            "stations": sorted(self.stations),
        }


def build_event_snapshot(
    db: Session,
    window_start_us: int,
    window_end_us: int,
    class_limits: dict[str, float],
    default_feature_limit: float,
    class_labels: list[str] | None = None,
    max_events: int = 400,
) -> list[EventNode]:
    """Freeze the live events overlapping the window into corridor input.

    Superseded events are excluded (their replacements carry the current
    evidence); the snapshot records each event's revision so a later change
    can be detected without re-reading mutable rows.
    """
    stmt = (
        select(Event)
        .where(Event.status != "superseded")
        .where(Event.emission_max_us >= window_start_us)
        .where(Event.emission_min_us <= window_end_us)
        .order_by(Event.emission_us, Event.id)
    )
    if class_labels:
        stmt = stmt.where(Event.class_label.in_(class_labels))
    events = list(db.execute(stmt).scalars())[:max_events]
    if not events:
        return []
    links = list(
        db.execute(
            select(EventObservation, Observation)
            .join(Observation, Observation.id == EventObservation.observation_id)
            .where(EventObservation.event_id.in_([e.id for e in events]))
        ).all()
    )
    by_event: dict[int, list[tuple[EventObservation, Observation]]] = {}
    for link, obs in links:
        by_event.setdefault(link.event_id, []).append((link, obs))
    nodes: list[EventNode] = []
    for event in events:
        rows = by_event.get(event.id, [])
        stations = {link.station_id for link, _ in rows}
        feats = [obs.features for _, obs in rows if obs.features]
        dim = len(feats[0]) if feats else 0
        mean_features = (
            [sum(f[i] for f in feats) / len(feats) for i in range(dim)] if feats else []
        )
        region_boxes = [
            {
                "x_min": r["bounds"]["x_min"],
                "x_max": r["bounds"]["x_max"],
                "y_min": r["bounds"]["y_min"],
                "y_max": r["bounds"]["y_max"],
            }
            for r in (event.regions_json or [])
        ] or [
            {
                "x_min": event.x_min,
                "x_max": event.x_max,
                "y_min": event.y_min,
                "y_max": event.y_max,
            }
        ]
        nodes.append(
            EventNode(
                event_id=event.id,
                revision=event.revision,
                class_label=event.class_label,
                status=event.status,
                emission_us=event.emission_us,
                emission_min_us=event.emission_min_us,
                emission_max_us=event.emission_max_us,
                region_boxes=region_boxes,
                bbox={
                    "x_min": event.x_min,
                    "x_max": event.x_max,
                    "y_min": event.y_min,
                    "y_max": event.y_max,
                },
                representative={"x": event.x, "y": event.y},
                features=mean_features,
                feature_limit=class_limits.get(event.class_label, default_feature_limit),
                stations=stations,
                observation_count=event.observation_count,
            )
        )
    return nodes


# --------------------------------------------------------------------------- #
# Reachability edges
# --------------------------------------------------------------------------- #
@dataclass
class Edge:
    from_event_id: int
    to_event_id: int
    feasible: bool
    reason: str | None  # why infeasible (None when feasible)
    dt_min_s: float
    dt_max_s: float
    dist_min_m: float
    dist_max_m: float
    detour_min_m: float
    required_speed_min_mps: float
    required_speed_max_mps: float
    speed_limit_mps: float
    speed_rule: dict
    clock_uncertainty_us: int
    zones_crossed: list[str]
    blind_zones: list[dict]
    feature_distance: float | None
    feature_limit: float
    cost: float

    def explanation(self) -> dict:
        """Constraint-by-constraint account attached to corridor members."""
        return {
            "to_event_id": self.to_event_id,
            "feasible": self.feasible,
            "reason": self.reason,
            "dt_s": {"min": round(self.dt_min_s, 3), "max": round(self.dt_max_s, 3)},
            "distance_m": {"min": round(self.dist_min_m, 1), "max": round(self.dist_max_m, 1)},
            "detour_min_m": round(self.detour_min_m, 1),
            "required_speed_mps": {
                "min": round(self.required_speed_min_mps, 4),
                "max": None if math.isinf(self.required_speed_max_mps) else round(self.required_speed_max_mps, 4),
            },
            "speed_limit_mps": self.speed_limit_mps,
            "speed_rule": self.speed_rule,
            "clock_uncertainty_us": self.clock_uncertainty_us,
            "zones_crossed": self.zones_crossed,
            "blind_zones": self.blind_zones,
            "feature_distance": self.feature_distance,
            "feature_limit": self.feature_limit,
            "cost": round(self.cost, 6),
        }


def _bbox_min_distance(a: dict, b: dict) -> float:
    dx = max(a["x_min"] - b["x_max"], b["x_min"] - a["x_max"], 0.0)
    dy = max(a["y_min"] - b["y_max"], b["y_min"] - a["y_max"], 0.0)
    return math.hypot(dx, dy)


def _bbox_max_distance(a: dict, b: dict) -> float:
    dx = max(abs(a["x_min"] - b["x_max"]), abs(a["x_max"] - b["x_min"]))
    dy = max(abs(a["y_min"] - b["y_max"]), abs(a["y_max"] - b["y_min"]))
    return math.hypot(dx, dy)


def regions_min_distance(a: EventNode, b: EventNode) -> float:
    """Closest approach between the two events' feasible regions (box bound)."""
    return min(
        _bbox_min_distance(ra, rb) for ra in a.region_boxes for rb in b.region_boxes
    )


def _feature_distance(a: EventNode, b: EventNode) -> float | None:
    if not a.features or not b.features or len(a.features) != len(b.features):
        return None
    return math.dist(a.features, b.features)


def build_edges(
    nodes: list[EventNode],
    book: dict,
    *,
    feature_weight: float,
    max_edge_gap_s: float,
    speed_slack: float = 1.0,
) -> list[Edge]:
    """Evaluate every time-ordered pair once.

    An edge is feasible when there *exists* a combination of emission
    instants (inside the published intervals, widened by the book's
    cross-station clock uncertainty) and positions (inside the feasible
    regions) such that the required speed stays under the seasonal limit and
    impassable zones are avoided.  The cost used for ranking is a *typical*
    estimate (representative points and central instants), documented as
    such — feasibility never depends on it.
    """
    zones = book.get("impassable_zones") or []
    edges: list[Edge] = []
    ordered = sorted(nodes, key=lambda n: (n.emission_us, n.event_id))
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            dt_raw_max = (b.emission_max_us - a.emission_min_us) / 1e6
            if dt_raw_max > max_edge_gap_s:
                # Events are time-sorted: a later b only widens the gap.
                if b.emission_min_us - a.emission_max_us > max_edge_gap_s * 1e6:
                    break
                continue
            edges.append(_evaluate_pair(a, b, book, zones, feature_weight, speed_slack))
    return edges


def _evaluate_pair(
    a: EventNode,
    b: EventNode,
    book: dict,
    zones: list[dict],
    feature_weight: float,
    speed_slack: float,
) -> Edge:
    clock_us = constraints.clock_uncertainty_us(book, a.stations, b.stations)
    dt_min_s = max(0.0, (b.emission_min_us - a.emission_max_us - 2 * clock_us) / 1e6)
    dt_max_s = (b.emission_max_us - a.emission_min_us + 2 * clock_us) / 1e6
    mid_us = (a.emission_us + b.emission_us) // 2
    rule = constraints.speed_limit_for(book, a.class_label, mid_us)
    limit = rule.max_speed_mps * speed_slack

    dist_min = regions_min_distance(a, b)
    dist_max = _bbox_max_distance(a.bbox, b.bbox)
    # Detour lower bound between the two closest region corners: use the
    # representative segment as the zone-crossing probe (conservative: zones
    # the segment misses cannot block the pair).
    det = constraints.detour_distance(
        a.representative["x"], a.representative["y"],
        b.representative["x"], b.representative["y"],
        zones,
    )
    detour_min = max(dist_min, det.distance_m if det.zones_crossed else dist_min)

    req_speed_min = dist_min / dt_max_s if dt_max_s > 0 else math.inf
    req_speed_max = dist_max / dt_min_s if dt_min_s > 0 else math.inf

    feature_distance = _feature_distance(a, b)
    feature_limit = min(a.feature_limit, b.feature_limit)

    blind = constraints.blind_zones_for_gap(
        book,
        a.emission_max_us,
        b.emission_min_us,
        a.representative["x"],
        a.representative["y"],
        b.representative["x"],
        b.representative["y"],
    )
    blind_rows = [
        {
            "zone_id": z.zone_id,
            "station_id": z.station_id,
            "overlap_s": round((z.overlap_to_us - z.overlap_from_us) / 1e6, 3),
            "reason": z.reason,
        }
        for z in blind
    ]

    reason = None
    if a.class_label != b.class_label:
        reason = "class_mismatch"
    elif dt_max_s <= 0:
        reason = "time_order"
    elif detour_min > limit * dt_max_s + 1e-9:
        reason = "speed_limit"
    elif feature_distance is not None and feature_distance > feature_limit:
        reason = "feature_distance"
    feasible = reason is None

    # Typical-cost estimate (representative points, central instants).
    dx = a.representative["x"] - b.representative["x"]
    dy = a.representative["y"] - b.representative["y"]
    dist_typ = math.hypot(dx, dy)
    dt_typ = max((b.emission_us - a.emission_us) / 1e6, 1e-3)
    speed_pressure = min(dist_typ / dt_typ / max(limit, 1e-9), 4.0)
    feature_pressure = (
        min(feature_distance / max(feature_limit, 1e-9), 4.0) if feature_distance is not None else 0.5
    )
    cost = speed_pressure + feature_weight * feature_pressure

    return Edge(
        from_event_id=a.event_id,
        to_event_id=b.event_id,
        feasible=feasible,
        reason=reason,
        dt_min_s=dt_min_s,
        dt_max_s=dt_max_s,
        dist_min_m=dist_min,
        dist_max_m=dist_max,
        detour_min_m=detour_min,
        required_speed_min_mps=req_speed_min,
        required_speed_max_mps=req_speed_max,
        speed_limit_mps=rule.max_speed_mps,
        speed_rule=rule.source,
        clock_uncertainty_us=clock_us,
        zones_crossed=det.zones_crossed,
        blind_zones=blind_rows,
        feature_distance=feature_distance,
        feature_limit=feature_limit,
        cost=cost,
    )
