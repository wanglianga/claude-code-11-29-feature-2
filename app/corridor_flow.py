"""Min-cost-flow corridor solver (最小代价流).

The whole window is solved as one **minimum-cost path cover**: every event
must be covered by exactly one chain, starting a new chain costs the
*miss cost* (缺测代价 λ), and chaining event ``j`` behind event ``i`` costs
the reachability edge cost ``c(i, j)``.  An event therefore stays
unattributed (a singleton chain) exactly when every chain it could join
would cost more than the miss cost — which is precisely the semantics the
requirement asks to tune carefully.  Because the optimum is global, two
corridors competing for the same event are resolved by total window cost,
not by local greed; and because it is a single network solve, the solver
necessarily waits for the **whole window** before publishing anything.

Network (standard reduction to a min-cost circulation):

* split every event ``i`` into ``i_in -> i_out`` with lower bound 1 (the
  event must be covered);
* ``S -> i_in`` capacity 1, cost λ (start a corridor here);
* ``i_out -> T`` capacity 1, cost 0 (end a corridor here);
* ``i_out -> j_in`` capacity 1, cost ``c(i, j)`` for every feasible edge;
* ``T -> S`` unbounded, cost 0 (close the circulation).

Solved by successive shortest augmenting paths with Johnson potentials.

Near-ties: after the optimum is found, every used arc is forbidden in turn
and the network re-solved; any alternative decomposition within
``tie_epsilon`` of the optimum is published alongside as competing
candidates (近似同分时保留竞争候选).
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

from .corridor_graph import Edge, EventNode


@dataclass
class CorridorSolution:
    event_ids: list[int]  # ordered
    edge_cost: float
    score: float  # edge_cost - miss_cost * (len(event_ids) - 1)


@dataclass
class FlowResult:
    corridors: list[CorridorSolution]
    total_cost: float
    alternates: list[list[CorridorSolution]]  # near-tie alternative decompositions
    unattributed_ids: list[int]
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Successive shortest paths with potentials
# --------------------------------------------------------------------------- #
class _Net:
    """Residual network; edges are [to, capacity, cost, reverse_index]."""

    def __init__(self, n: int):
        self.graph: list[list[list[float]]] = [[] for _ in range(n)]
        self.n = n
        self.potential = [0.0] * n

    def add_edge(self, u: int, v: int, cap: int, cost: float) -> int:
        self.graph[u].append([v, cap, cost, len(self.graph[v])])
        self.graph[v].append([u, 0, -cost, len(self.graph[u]) - 1])
        return len(self.graph[u]) - 1

    def augment(self, source: int, sink: int) -> float | None:
        """Augment one unit along a cheapest path; return its true cost."""
        dist = [math.inf] * self.n
        prev_node = [-1] * self.n
        prev_edge = [-1] * self.n
        dist[source] = 0.0
        pq = [(0.0, source)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u] + 1e-9:
                continue
            for ei, e in enumerate(self.graph[u]):
                if e[1] <= 0:
                    continue
                v = e[0]
                nd = d + e[2] + self.potential[u] - self.potential[v]
                if nd < dist[v] - 1e-9:
                    dist[v] = nd
                    prev_node[v] = u
                    prev_edge[v] = ei
                    heapq.heappush(pq, (nd, v))
        if math.isinf(dist[sink]):
            return None
        for v in range(self.n):
            if dist[v] < math.inf:
                self.potential[v] += dist[v]
        true_cost = self.potential[sink] - self.potential[source] + 0.0
        # potential[source] stays 0 across iterations (dist[source] = 0).
        v = sink
        while v != source:
            u, ei = prev_node[v], prev_edge[v]
            e = self.graph[u][ei]
            e[1] -= 1
            self.graph[v][int(e[3])][1] += 1
            v = u
        return true_cost


@dataclass
class _Problem:
    nodes: list[EventNode]
    net: _Net
    super_source: int
    super_sink: int
    chain_edges: list[tuple[int, int, int, Edge]]  # (u, v, edge_idx, Edge)
    supply_edges: list[tuple[int, int]]  # (node, edge_idx) for SS -> i_out


def _build(
    nodes: list[EventNode],
    edges: list[Edge],
    miss_cost: float,
    index: dict[int, int],
    forbidden_arcs: set[tuple[int, int]] | None = None,
) -> _Problem:
    n = len(nodes)
    # Node ids: 0..n-1 = i_in, n..2n-1 = i_out, then S, T, SS, TT.
    S, T = 2 * n, 2 * n + 1
    SS, TT = 2 * n + 2, 2 * n + 3
    net = _Net(2 * n + 4)
    supply_edges: list[tuple[int, int]] = []
    for k in range(n):
        i_in, i_out = k, n + k
        # Lower-bound-1 cover edge i_in -> i_out is handled by the imbalance
        # transformation: SS -> i_out and i_in -> TT carry the pre-flow.
        supply_edges.append((i_out, net.add_edge(SS, i_out, 1, 0.0)))
        net.add_edge(i_in, TT, 1, 0.0)
        net.add_edge(S, i_in, 1, miss_cost)  # corridor starts here: pay miss cost
        net.add_edge(i_out, T, 1, 0.0)  # corridor ends here
    net.add_edge(T, S, n, 0.0)  # close the circulation
    chain_edges: list[tuple[int, int, int, Edge]] = []
    forbidden = forbidden_arcs or set()
    for edge in edges:
        if not edge.feasible:
            continue
        key = (edge.from_event_id, edge.to_event_id)
        if key in forbidden:
            continue
        i = index[key[0]]
        j = index[key[1]]
        u, v = n + i, j
        idx = net.add_edge(u, v, 1, edge.cost)
        chain_edges.append((u, v, idx, edge))
    return _Problem(nodes, net, SS, TT, chain_edges, supply_edges)


def _solve(nodes: list[EventNode], edges: list[Edge], miss_cost: float,
           forbidden_arcs: set[tuple[int, int]] | None = None) -> _Problem:
    index = {node.event_id: k for k, node in enumerate(nodes)}
    problem = _build(nodes, edges, miss_cost, index, forbidden_arcs)
    # Exactly n units of supply (one per event); the S -> i_in -> TT route
    # always exists, so the cover is always feasible.
    for _ in range(len(nodes)):
        cost = problem.net.augment(problem.super_source, problem.super_sink)
        assert cost is not None, "path-cover network is always feasible"
    return problem


def _extract(problem: _Problem, miss_cost: float) -> tuple[list[list[int]], dict[int, tuple[int, int]]]:
    """Read chain links out of the residual network."""
    n = len(problem.nodes)
    index = {node.event_id: k for k, node in enumerate(problem.nodes)}
    link: dict[int, int] = {}  # event_id -> next event_id
    for u, v, idx, edge in problem.chain_edges:
        if problem.net.graph[u][idx][1] == 0:  # capacity consumed -> flow 1
            frm = problem.nodes[u - n].event_id
            to = problem.nodes[v].event_id
            link[frm] = to
    has_prev = set(link.values())
    chains: list[list[int]] = []
    for node in problem.nodes:
        eid = node.event_id
        if eid in has_prev:
            continue
        chain = [eid]
        while chain[-1] in link:
            chain.append(link[chain[-1]])
        chains.append(chain)
    edge_lookup = {(e.from_event_id, e.to_event_id): e for _, _, _, e in problem.chain_edges}
    return chains, edge_lookup


def _to_solutions(
    chains: list[list[int]],
    edge_lookup: dict[tuple[int, int], Edge],
    miss_cost: float,
) -> list[CorridorSolution]:
    out = []
    for chain in chains:
        edge_cost = sum(
            edge_lookup[(chain[k], chain[k + 1])].cost for k in range(len(chain) - 1)
        )
        out.append(
            CorridorSolution(
                event_ids=chain,
                edge_cost=edge_cost,
                score=edge_cost - miss_cost * (len(chain) - 1),
            )
        )
    return out


def solve_flow(
    nodes: list[EventNode],
    edges: list[Edge],
    *,
    miss_cost: float,
    tie_epsilon: float,
    min_events: int,
    max_alternates: int,
) -> FlowResult:
    """Decompose the whole window into corridors by min-cost path cover."""
    notes: list[str] = []
    problem = _solve(nodes, edges, miss_cost)
    chains, edge_lookup = _extract(problem, miss_cost)
    solutions = _to_solutions(chains, edge_lookup, miss_cost)
    corridors = [s for s in solutions if len(s.event_ids) >= min_events]
    singletons = [s for s in solutions if len(s.event_ids) < min_events]
    unattributed = sorted(eid for s in singletons for eid in s.event_ids)
    best_total = sum(s.score for s in corridors)
    notes.append(
        f"min-cost path cover over the whole window: {len(corridors)} corridor(s), "
        f"{len(singletons)} singleton chain(s) at miss_cost={miss_cost}"
    )

    # Near-tie alternatives: forbid each used arc in turn and re-solve.
    alternates: list[list[CorridorSolution]] = []
    seen_signatures = {_signature(corridors)}
    used_arcs = {
        (c.event_ids[k], c.event_ids[k + 1]) for c in corridors for k in range(len(c.event_ids) - 1)
    }
    for arc in sorted(used_arcs):
        if len(alternates) >= max_alternates:
            notes.append(f"alternate search capped at {max_alternates}")
            break
        alt_problem = _solve(nodes, edges, miss_cost, forbidden_arcs={arc})
        alt_chains, alt_lookup = _extract(alt_problem, miss_cost)
        alt_solutions = _to_solutions(alt_chains, alt_lookup, miss_cost)
        alt_corridors = [s for s in alt_solutions if len(s.event_ids) >= min_events]
        alt_total = sum(s.score for s in alt_corridors)
        signature = _signature(alt_corridors)
        if alt_total <= best_total + tie_epsilon and signature not in seen_signatures:
            seen_signatures.add(signature)
            alternates.append(alt_corridors)
    if alternates:
        notes.append(
            f"{len(alternates)} alternative decomposition(s) within tie_epsilon={tie_epsilon}"
        )

    return FlowResult(
        corridors=corridors,
        total_cost=best_total,
        alternates=alternates,
        unattributed_ids=unattributed,
        notes=notes,
    )


def _signature(corridors: list[CorridorSolution]) -> tuple:
    return tuple(sorted(tuple(c.event_ids) for c in corridors))
