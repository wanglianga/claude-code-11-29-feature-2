"""Unit tests for the two corridor solvers on synthetic event graphs."""

from __future__ import annotations

from app import corridor_flow, corridor_hypothesis
from app.corridor_graph import Edge, EventNode


def node(event_id: int, t_s: float, x: float, y: float, cls: str = "chirp") -> EventNode:
    us = int(t_s * 1e6)
    return EventNode(
        event_id=event_id,
        revision=1,
        class_label=cls,
        status="final",
        emission_us=us,
        emission_min_us=us - 5_000_000,
        emission_max_us=us + 5_000_000,
        region_boxes=[{"x_min": x - 10, "x_max": x + 10, "y_min": y - 10, "y_max": y + 10}],
        bbox={"x_min": x - 10, "x_max": x + 10, "y_min": y - 10, "y_max": y + 10},
        representative={"x": x, "y": y},
        features=[1.0, 0.5],
        feature_limit=0.3,
        stations={"S1", "S2"},
        observation_count=2,
    )


def edge(a: int, b: int, cost: float, feasible: bool = True, reason: str | None = None) -> Edge:
    return Edge(
        from_event_id=a,
        to_event_id=b,
        feasible=feasible,
        reason=reason,
        dt_min_s=1.0,
        dt_max_s=100.0,
        dist_min_m=10.0,
        dist_max_m=50.0,
        detour_min_m=10.0,
        required_speed_min_mps=0.1,
        required_speed_max_mps=1.0,
        speed_limit_mps=2.0,
        speed_rule={"rule": "default_max_speed_mps"},
        clock_uncertainty_us=0,
        zones_crossed=[],
        blind_zones=[],
        feature_distance=0.1,
        feature_limit=0.3,
        cost=cost,
    )


BOOK = {
    "default_max_speed_mps": 2.0,
    "seasonal_speed_limits": [],
    "impassable_zones": [],
    "blind_zones": [],
    "clock_uncertainties": [],
    "default_clock_uncertainty_us": 0,
}

PARAMS = dict(miss_cost=1.0, tie_epsilon=0.05, min_events=2)


# --------------------------------------------------------------------------- #
# Min-cost flow
# --------------------------------------------------------------------------- #
def test_flow_links_two_tracks_without_sharing_events():
    # Track A: 1 -> 2 -> 3 (cheap); track B: 4 -> 5 (cheap). Cross edges pricey.
    nodes = [node(1, 0, 0, 0), node(2, 100, 50, 0), node(3, 200, 100, 0),
             node(4, 10, 0, 500), node(5, 110, 50, 500)]
    edges = [
        edge(1, 2, 0.1), edge(2, 3, 0.1), edge(1, 3, 0.5),
        edge(4, 5, 0.1),
        edge(1, 4, 3.5), edge(2, 5, 3.5), edge(3, 5, 3.5), edge(1, 5, 3.5),
        edge(2, 4, 3.5), edge(4, 2, 3.5), edge(4, 3, 3.5),
    ]
    result = corridor_flow.solve_flow(nodes, edges, max_alternates=4, **PARAMS)
    chains = sorted(tuple(c.event_ids) for c in result.corridors)
    assert chains == [(1, 2, 3), (4, 5)]
    assert result.unattributed_ids == []


def test_flow_leaves_isolated_event_unattributed():
    nodes = [node(1, 0, 0, 0), node(2, 100, 50, 0), node(9, 150, 900, 900)]
    edges = [edge(1, 2, 0.1), edge(1, 9, 9.0, feasible=False, reason="speed_limit"),
             edge(2, 9, 9.0, feasible=False, reason="speed_limit")]
    result = corridor_flow.solve_flow(nodes, edges, max_alternates=4, **PARAMS)
    assert [tuple(c.event_ids) for c in result.corridors] == [(1, 2)]
    assert result.unattributed_ids == [9]


def test_flow_singletons_dissolve_below_min_events():
    nodes = [node(1, 0, 0, 0), node(2, 100, 50, 0), node(3, 200, 100, 0)]
    edges = [edge(1, 2, 0.1), edge(2, 3, 5.0), edge(1, 3, 5.0)]
    # Edge 2->3 costs 5 > miss_cost, so the flow prefers the singleton 3,
    # which then dissolves (min_events=2) into the unattributed list.
    result = corridor_flow.solve_flow(nodes, edges, max_alternates=4, **PARAMS)
    assert [tuple(c.event_ids) for c in result.corridors] == [(1, 2)]
    assert 3 in result.unattributed_ids


def test_flow_reports_near_tie_alternates():
    # Two equally good decompositions: (1->2) or (1->3); 2 and 3 are
    # simultaneous, so no edge exists between them and only one can follow 1.
    nodes = [node(1, 0, 0, 0), node(2, 100, 40, 0), node(3, 100, 60, 0)]
    edges = [edge(1, 2, 0.10), edge(1, 3, 0.10 + 1e-4)]
    result = corridor_flow.solve_flow(nodes, edges, max_alternates=8, **PARAMS)
    assert len(result.corridors) == 1
    assert result.alternates, "expected a near-tie alternate decomposition"
    alt_sets = {tuple(c.event_ids) for alt in result.alternates for c in alt}
    assert (1, 3) in alt_sets


# --------------------------------------------------------------------------- #
# Hypothesis pruning
# --------------------------------------------------------------------------- #
def test_hypothesis_matches_flow_on_simple_tracks():
    nodes = [node(1, 0, 0, 0), node(2, 100, 50, 0), node(3, 200, 100, 0),
             node(4, 10, 0, 500), node(5, 110, 50, 500)]
    edges = [
        edge(1, 2, 0.1), edge(2, 3, 0.1), edge(1, 3, 0.5),
        edge(4, 5, 0.1),
        edge(1, 4, 3.5), edge(2, 5, 3.5), edge(3, 5, 3.5), edge(1, 5, 3.5),
        edge(2, 4, 3.5), edge(4, 2, 3.5), edge(4, 3, 3.5),
    ]
    result = corridor_hypothesis.solve_hypotheses(
        nodes, edges, beam_width=8, max_hypotheses=10000, **PARAMS
    )
    chains = sorted(tuple(c.event_ids) for c in result.corridors)
    assert (1, 2, 3) in chains
    assert (4, 5) in chains
    assert result.unattributed_ids == []


def test_hypothesis_records_prune_reasons_and_unsearched_branches():
    # Beam width 1 with two equal continuations forces unsearched branches.
    nodes = [node(i, i * 100, i * 10, 0) for i in range(1, 8)]
    edges = []
    for i in range(1, 7):
        for j in range(i + 1, 8):
            edges.append(edge(i, j, 0.1 * (j - i)))
    result = corridor_hypothesis.solve_hypotheses(
        nodes, edges, beam_width=1, max_hypotheses=10000, **PARAMS
    )
    assert result.unsearched_branches > 0
    beam_stats = [s for s in result.prune_stats if s.reason == "beam_width"]
    assert beam_stats, "beam pruning must be reported"
    assert any("unsearched" in n or "beam" in n for n in result.notes)


def test_hypothesis_infeasible_edges_listed_in_prune_stats():
    nodes = [node(1, 0, 0, 0), node(2, 100, 50, 0), node(3, 200, 5000, 0)]
    edges = [
        edge(1, 2, 0.1),
        edge(1, 3, 9.0, feasible=False, reason="speed_limit"),
        edge(2, 3, 9.0, feasible=False, reason="speed_limit"),
    ]
    result = corridor_hypothesis.solve_hypotheses(
        nodes, edges, beam_width=8, max_hypotheses=10000, **PARAMS
    )
    speed_stats = [s for s in result.prune_stats if s.reason == "speed_limit"]
    assert speed_stats and sum(s.count for s in speed_stats) == 2
    assert result.unattributed_ids == [3]


def test_hypothesis_keeps_tied_competitors():
    # Events 2 and 3 are simultaneous interchangeable continuations of 1:
    # both full chains must survive as tied competitors.
    nodes = [node(1, 0, 0, 0), node(2, 100, 40, 0), node(3, 100, 60, 0), node(4, 200, 100, 0)]
    edges = [
        edge(1, 2, 0.10), edge(1, 3, 0.10), edge(2, 4, 0.10), edge(3, 4, 0.10),
        edge(1, 4, 0.5),
    ]
    result = corridor_hypothesis.solve_hypotheses(
        nodes, edges, beam_width=8, max_hypotheses=10000, **PARAMS
    )
    chains = sorted(tuple(c.event_ids) for c in result.corridors)
    assert (1, 2, 4) in chains and (1, 3, 4) in chains
    assert result.tied_groups, "tied corridors must be grouped"
    tied_members = {i for g in result.tied_groups for i in g}
    assert len(tied_members) >= 2
