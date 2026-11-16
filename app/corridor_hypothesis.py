"""Hypothesis-pruning corridor solver (路径假设 + 逐层剪枝).

Instead of one global flow, this method grows path hypotheses layer by
layer in time order and prunes aggressively:

* **reachability pruning** — an extension that violates the seasonal speed
  limit, the time order, an impassable zone or the feature limit is dropped
  with the *exact* constraint reason attached (排除理由直观);
* **beam pruning** — per frontier event only the best ``beam_width``
  hypotheses survive.  In dense windows this discards branches without
  exploring them; every such discard is counted and reported as an
  **unsearched branch** so the run summary states exactly what was not
  searched (密集窗口必须说明未搜索的分支).

Finished hypotheses are deduplicated (same event set keeps its best score),
dominated corridors (a strict subsequence scoring no better) are dropped,
and corridors within ``tie_epsilon`` of the best score over the same events
are all published as competing candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .corridor_graph import Edge, EventNode
from .corridor_flow import CorridorSolution


@dataclass
class PruneStat:
    layer: int
    reason: str
    count: int
    examples: list[str] = field(default_factory=list)


@dataclass
class HypothesisResult:
    corridors: list[CorridorSolution]
    tied_groups: list[list[int]]  # corridor index groups within tie_epsilon
    unattributed_ids: list[int]
    prune_stats: list[PruneStat]
    unsearched_branches: int
    notes: list[str] = field(default_factory=list)


@dataclass
class _Hyp:
    event_ids: tuple[int, ...]
    score: float  # sum(edge costs) - miss_cost * len

    @property
    def last(self) -> int:
        return self.event_ids[-1]


def solve_hypotheses(
    nodes: list[EventNode],
    edges: list[Edge],
    *,
    miss_cost: float,
    tie_epsilon: float,
    min_events: int,
    beam_width: int,
    max_hypotheses: int,
) -> HypothesisResult:
    notes: list[str] = []
    feasible: dict[int, list[Edge]] = {}
    infeasible: dict[tuple[int, int], Edge] = {}
    for edge in edges:
        if edge.feasible:
            feasible.setdefault(edge.from_event_id, []).append(edge)
        else:
            infeasible[(edge.from_event_id, edge.to_event_id)] = edge

    ordered_ids = [n.event_id for n in sorted(nodes, key=lambda n: (n.emission_us, n.event_id))]
    node_index = {eid: k for k, eid in enumerate(ordered_ids)}

    prune: dict[tuple[int, str], PruneStat] = {}

    def record_prune(layer: int, reason: str, example: str) -> None:
        key = (layer, reason)
        stat = prune.get(key)
        if stat is None:
            stat = prune[key] = PruneStat(layer=layer, reason=reason, count=0)
        stat.count += 1
        if len(stat.examples) < 3:
            stat.examples.append(example)

    # Seed one hypothesis per event.  Scores follow the same convention as
    # the flow solver: sum(edge costs) - miss_cost * (len - 1), so a single
    # event scores 0 and each extension adds (edge cost - miss_cost).
    frontier: dict[int, list[_Hyp]] = {
        eid: [_Hyp(event_ids=(eid,), score=0.0)] for eid in ordered_ids
    }
    finished: list[_Hyp] = []
    unsearched = 0
    total_hypotheses = len(ordered_ids)

    for layer, eid in enumerate(ordered_ids):
        hyps = frontier.get(eid, [])
        for hyp in hyps:
            extensions = feasible.get(eid, [])
            extended = False
            for edge in extensions:
                if edge.to_event_id in hyp.event_ids:
                    continue
                cand = _Hyp(
                    event_ids=hyp.event_ids + (edge.to_event_id,),
                    score=hyp.score + edge.cost - miss_cost,
                )
                total_hypotheses += 1
                if total_hypotheses > max_hypotheses:
                    record_prune(layer, "hypothesis_cap", f"{cand.event_ids}")
                    unsearched += 1
                    continue
                frontier.setdefault(edge.to_event_id, []).append(cand)
                extended = True
            if not extended and len(hyp.event_ids) >= min_events:
                finished.append(hyp)
        # Beam-prune each frontier bucket that just received extensions.
        for nxt_id in [e.to_event_id for e in feasible.get(eid, [])]:
            bucket = frontier.get(nxt_id)
            if not bucket or len(bucket) <= beam_width:
                continue
            bucket.sort(key=lambda h: h.score)
            for dropped in bucket[beam_width:]:
                unsearched += 1
                record_prune(
                    layer,
                    "beam_width",
                    f"frontier event {nxt_id}: dropped {dropped.event_ids} score={dropped.score:.3f}",
                )
            frontier[nxt_id] = bucket[:beam_width]

    # Anything still open at the end of the window finishes too.
    for eid in ordered_ids:
        for hyp in frontier.get(eid, []):
            if len(hyp.event_ids) >= min_events:
                finished.append(hyp)

    # Record infeasible-pair reasons for the report (sampled per layer).
    for (frm, to), edge in sorted(infeasible.items()):
        record_prune(
            node_index.get(frm, 0),
            edge.reason or "infeasible",
            f"{frm}->{to}: {edge.reason}",
        )

    # Deduplicate by event set, keep the best score per set.
    best_by_set: dict[tuple[int, ...], _Hyp] = {}
    for hyp in finished:
        key = tuple(sorted(hyp.event_ids))
        current = best_by_set.get(key)
        if current is None or hyp.score < current.score:
            best_by_set[key] = hyp
    candidates = sorted(best_by_set.values(), key=lambda h: (h.score, h.event_ids))

    # Primary decomposition: greedy disjoint set packing by score.  This is
    # the hypothesis-method analogue of the flow's optimal cover, and every
    # selected corridor is disjoint from the others.
    selected: list[_Hyp] = []
    selected_keys: set[tuple[int, ...]] = set()
    used_events: set[int] = set()
    for hyp in candidates:
        if set(hyp.event_ids) & used_events:
            continue
        selected.append(hyp)
        selected_keys.add(tuple(sorted(hyp.event_ids)))
        used_events.update(hyp.event_ids)

    # Near-tie competitors: an unselected corridor that overlaps a selected
    # one and scores within epsilon of it stays published (近似同分保留竞争
    # 候选); the rest are counted as score-pruned branches in the report so
    # the searched space is fully accounted for.
    kept: list[_Hyp] = list(selected)
    score_pruned = 0
    for hyp in candidates:
        key = tuple(sorted(hyp.event_ids))
        if key in selected_keys:
            continue
        overlapping = [s for s in selected if set(s.event_ids) & set(hyp.event_ids)]
        if overlapping and hyp.score <= min(s.score for s in overlapping) + tie_epsilon:
            kept.append(hyp)
        else:
            score_pruned += 1
    if score_pruned:
        record_prune(len(ordered_ids), "score_pruned", f"{score_pruned} dominated hypotheses")

    corridors = [
        CorridorSolution(
            event_ids=list(h.event_ids),
            edge_cost=h.score + miss_cost * (len(h.event_ids) - 1),
            score=h.score,
        )
        for h in kept
    ]
    corridors.sort(key=lambda c: (c.score, c.event_ids))

    # Tie groups: corridors sharing at least one event and within epsilon of
    # the best score in their overlap cluster.
    tied_groups = _tie_groups(corridors, tie_epsilon)

    used = {eid for c in corridors for eid in c.event_ids}
    unattributed = [eid for eid in ordered_ids if eid not in used]
    notes.append(
        f"hypothesis search: {total_hypotheses} hypotheses, beam_width={beam_width}, "
        f"{unsearched} unsearched branches"
    )
    if unsearched:
        notes.append(
            "dense-window disclosure: beam pruning skipped branches; see prune_stats"
        )
    return HypothesisResult(
        corridors=corridors,
        tied_groups=tied_groups,
        unattributed_ids=unattributed,
        prune_stats=sorted(prune.values(), key=lambda s: (s.layer, s.reason)),
        unsearched_branches=unsearched,
        notes=notes,
    )


def _tie_groups(corridors: list[CorridorSolution], epsilon: float) -> list[list[int]]:
    """Cluster corridor indices that compete for shared events within epsilon."""
    parent = list(range(len(corridors)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_event: dict[int, list[int]] = {}
    for idx, c in enumerate(corridors):
        for eid in c.event_ids:
            by_event.setdefault(eid, []).append(idx)
    for idxs in by_event.values():
        if len(idxs) < 2:
            continue
        base = min(corridors[i].score for i in idxs)
        close = [i for i in idxs if corridors[i].score <= base + epsilon]
        for i in close[1:]:
            union(close[0], i)
    groups: dict[int, list[int]] = {}
    for idx in range(len(corridors)):
        groups.setdefault(find(idx), []).append(idx)
    return [sorted(g) for g in groups.values() if len(g) > 1]
