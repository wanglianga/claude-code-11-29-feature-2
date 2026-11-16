"""Corridor run orchestration, invalidation and manual confirmations.

A corridor run freezes the event window it solved (ids + revisions), the
constraint-book version and every solver parameter, so its output is
reproducible and later evidence can be matched against it.

Late or split observations only ever touch *adjacent* windows: when the
event pipeline publishes or supersedes events, corridor runs whose window
intersects the affected time range are marked ``stale`` and corridors that
cited those events become ``pending_review``; runs outside the range are
untouched.  A manual confirmation that new evidence contradicts is kept and
flagged ``conflicted`` — never revoked automatically.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import constraints as constraint_eval
from . import corridor_flow, corridor_graph, corridor_hypothesis
from .config import settings
from .models import (
    AuditLog,
    ConstraintBook,
    Corridor,
    CorridorCompetitor,
    CorridorConfirmation,
    CorridorEvent,
    CorridorRun,
    CorridorUnattributed,
    Event,
)
from .timeutil import now_us

METHOD_MINCOST_FLOW = "mincost_flow"
METHOD_HYPOTHESIS = "hypothesis_pruning"
METHODS = {METHOD_MINCOST_FLOW, METHOD_HYPOTHESIS}


# --------------------------------------------------------------------------- #
# Run creation / execution
# --------------------------------------------------------------------------- #
def create_corridor_run(
    db: Session,
    *,
    method: str,
    window_start_us: int,
    window_end_us: int,
    constraint_book_id: int,
    params: dict,
    created_by: str | None,
) -> CorridorRun:
    book = db.get(ConstraintBook, constraint_book_id)
    if book is None:
        raise KeyError("constraint book not found")
    if book.status != "active":
        raise ValueError(f"constraint book {constraint_book_id} is {book.status}, not active")
    if window_end_us <= window_start_us:
        raise ValueError("window_end must be after window_start")
    run = CorridorRun(
        batch_id=str(uuid.uuid4()),
        method=method,
        status="pending",
        window_start_us=window_start_us,
        window_end_us=window_end_us,
        constraint_book_id=book.id,
        constraint_version_hash=book.version_hash,
        params_json=params,
        event_snapshot_json=[],
        created_by=created_by,
        created_at_us=now_us(),
    )
    db.add(run)
    db.flush()
    return run


def default_params() -> dict:
    return {
        "miss_cost": settings.corridor_miss_cost,
        "tie_epsilon": settings.corridor_tie_epsilon,
        "feature_weight": settings.corridor_feature_weight,
        "min_events": settings.corridor_min_events,
        "max_edge_gap_s": settings.corridor_max_edge_gap_s,
        "gap_explain_s": settings.corridor_gap_explain_s,
        "beam_width": settings.corridor_beam_width,
        "max_corridors": settings.corridor_max_corridors,
        "max_tie_alternates": settings.corridor_max_tie_alternates,
        "max_events": settings.corridor_max_events,
    }


def execute_corridor_run(db: Session, run: CorridorRun) -> CorridorRun:
    """Solve one corridor run and persist corridors, competitors and gaps."""
    book_row = db.get(ConstraintBook, run.constraint_book_id)
    if book_row is None:
        raise ValueError("constraint book vanished")
    book = {
        "default_max_speed_mps": book_row.default_max_speed_mps,
        "seasonal_speed_limits": book_row.seasonal_speed_limits_json or [],
        "impassable_zones": book_row.impassable_zones_json or [],
        "blind_zones": book_row.blind_zones_json or [],
        "clock_uncertainties": book_row.clock_uncertainties_json or [],
        "default_clock_uncertainty_us": book_row.default_clock_uncertainty_us,
    }
    params = {**default_params(), **(run.params_json or {})}
    run.params_json = params
    run.status = "running"

    class_limits = _class_limits(db)
    nodes = corridor_graph.build_event_snapshot(
        db,
        run.window_start_us,
        run.window_end_us,
        class_limits,
        default_feature_limit=settings.feature_distance_limit,
        max_events=int(params["max_events"]),
    )
    run.event_snapshot_json = [n.snapshot_row() for n in nodes]
    edges = corridor_graph.build_edges(
        nodes,
        book,
        feature_weight=float(params["feature_weight"]),
        max_edge_gap_s=float(params["max_edge_gap_s"]),
    )
    edge_map = {(e.from_event_id, e.to_event_id): e for e in edges}

    if run.method == METHOD_MINCOST_FLOW:
        result = corridor_flow.solve_flow(
            nodes,
            edges,
            miss_cost=float(params["miss_cost"]),
            tie_epsilon=float(params["tie_epsilon"]),
            min_events=int(params["min_events"]),
            max_alternates=int(params["max_tie_alternates"]),
        )
        persisted = _persist_flow_result(db, run, result, edge_map, params, nodes)
        summary_extra = {
            "alternates": len(result.alternates),
            "total_cost": result.total_cost,
        }
        notes = result.notes
        unattributed_ids = result.unattributed_ids
    elif run.method == METHOD_HYPOTHESIS:
        result = corridor_hypothesis.solve_hypotheses(
            nodes,
            edges,
            miss_cost=float(params["miss_cost"]),
            tie_epsilon=float(params["tie_epsilon"]),
            min_events=int(params["min_events"]),
            beam_width=int(params["beam_width"]),
            max_hypotheses=200_000,
        )
        persisted = _persist_hypothesis_result(db, run, result, edge_map, params, nodes)
        summary_extra = {
            "unsearched_branches": result.unsearched_branches,
            "prune_stats": [
                {"layer": s.layer, "reason": s.reason, "count": s.count, "examples": s.examples}
                for s in result.prune_stats
            ],
        }
        notes = result.notes
        unattributed_ids = result.unattributed_ids
    else:
        raise ValueError(f"unknown corridor method {run.method!r}")

    _persist_competitors(db, persisted, nodes, edge_map, float(params["tie_epsilon"]))
    unattributed = _persist_unattributed(db, run, unattributed_ids, nodes, edges, persisted)

    run.status = "done"
    run.finished_at_us = now_us()
    run.summary_json = {
        "method": run.method,
        "events_in_window": len(nodes),
        "edges_feasible": sum(1 for e in edges if e.feasible),
        "edges_infeasible": sum(1 for e in edges if not e.feasible),
        "corridors": len(persisted),
        "unattributed": len(unattributed),
        "constraint_book_id": run.constraint_book_id,
        "constraint_version_hash": run.constraint_version_hash,
        "miss_cost": params["miss_cost"],
        "tie_epsilon": params["tie_epsilon"],
        "notes": notes,
        **summary_extra,
    }
    db.flush()
    _flag_conflicts_for_run(db, run)
    return run


def _class_limits(db: Session) -> dict[str, float]:
    from .models import SoundClass

    return {
        row.id: row.feature_distance_limit
        for row in db.execute(select(SoundClass)).scalars()
        if row.feature_distance_limit is not None
    }


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #
@dataclass
class _Persisted:
    corridor: Corridor
    event_ids: list[int]


def _gap_entries(
    event_ids: list[int],
    edge_map: dict[tuple[int, int], corridor_graph.Edge],
    nodes_by_id: dict[int, corridor_graph.EventNode],
    gap_explain_s: float,
) -> list[dict]:
    """Gap entries between consecutive members — never any coordinates.

    A gap is a time interval with no attributed observation; the entry only
    carries its duration and the blind zones that may explain it.
    """
    gaps = []
    for k in range(len(event_ids) - 1):
        a = nodes_by_id[event_ids[k]]
        b = nodes_by_id[event_ids[k + 1]]
        gap_lo = (b.emission_min_us - a.emission_max_us) / 1e6
        gap_hi = (b.emission_max_us - a.emission_min_us) / 1e6
        if gap_hi < gap_explain_s:
            continue
        edge = edge_map.get((a.event_id, b.event_id))
        gaps.append(
            {
                "after_event_id": a.event_id,
                "before_event_id": b.event_id,
                "duration_s": {"min": round(max(gap_lo, 0.0), 3), "max": round(gap_hi, 3)},
                "blind_zones": edge.blind_zones if edge else [],
                "note": "空档内不提供位置估计；仅有事件两端各自的可行区域",
            }
        )
    return gaps


def _persist_corridor(
    db: Session,
    run: CorridorRun,
    key: str,
    rank: int,
    score: float,
    tie_group: int | None,
    status: str,
    event_ids: list[int],
    edge_map: dict[tuple[int, int], corridor_graph.Edge],
    nodes_by_id: dict[int, corridor_graph.EventNode],
    params: dict,
) -> Corridor:
    first = nodes_by_id[event_ids[0]]
    last = nodes_by_id[event_ids[-1]]
    corridor = Corridor(
        run_id=run.id,
        corridor_key=key,
        score=score,
        rank=rank,
        tie_group=tie_group,
        status=status,
        event_count=len(event_ids),
        first_emission_us=first.emission_min_us,
        last_emission_us=last.emission_max_us,
        notes_json={
            "identity": "anonymous candidate; no individual identity is assigned",
            "gaps": _gap_entries(event_ids, edge_map, nodes_by_id, float(params["gap_explain_s"])),
        },
        created_at_us=now_us(),
    )
    db.add(corridor)
    db.flush()
    for seq, event_id in enumerate(event_ids):
        edge = edge_map.get((event_id, event_ids[seq + 1])) if seq + 1 < len(event_ids) else None
        db.add(
            CorridorEvent(
                corridor_id=corridor.id,
                seq=seq,
                event_id=event_id,
                edge_json=edge.explanation() if edge else None,
            )
        )
    return corridor


def _persist_flow_result(
    db: Session,
    run: CorridorRun,
    result: corridor_flow.FlowResult,
    edge_map,
    params: dict,
    nodes: list[corridor_graph.EventNode],
) -> list[_Persisted]:
    nodes_by_id = {n.event_id: n for n in nodes}
    persisted: list[_Persisted] = []
    rank = 0
    for sol in sorted(result.corridors, key=lambda s: (s.score, s.event_ids)):
        rank += 1
        corridor = _persist_corridor(
            db, run, f"C-{rank:02d}", rank, sol.score, None, "candidate",
            sol.event_ids, edge_map, nodes_by_id, params,
        )
        persisted.append(_Persisted(corridor, sol.event_ids))
    # Near-tie alternative decompositions are published as competing
    # candidates in their own tie group (近似同分保留竞争候选).
    tie_group = 0
    for alternate in result.alternates:
        tie_group += 1
        for sol in sorted(alternate, key=lambda s: (s.score, s.event_ids)):
            rank += 1
            corridor = _persist_corridor(
                db, run, f"A{tie_group}-{rank:02d}", rank, sol.score, tie_group, "alternate",
                sol.event_ids, edge_map, nodes_by_id, params,
            )
            persisted.append(_Persisted(corridor, sol.event_ids))
    return persisted


def _persist_hypothesis_result(
    db: Session,
    run: CorridorRun,
    result: corridor_hypothesis.HypothesisResult,
    edge_map,
    params: dict,
    nodes: list[corridor_graph.EventNode],
) -> list[_Persisted]:
    nodes_by_id = {n.event_id: n for n in nodes}
    tie_of: dict[int, int] = {}
    for group_no, group in enumerate(result.tied_groups, start=1):
        for idx in group:
            tie_of[idx] = group_no
    persisted: list[_Persisted] = []
    for rank, sol in enumerate(result.corridors, start=1):
        status = "tied_competitor" if rank - 1 in tie_of else "candidate"
        corridor = _persist_corridor(
            db, run, f"C-{rank:02d}", rank, sol.score, tie_of.get(rank - 1), status,
            sol.event_ids, edge_map, nodes_by_id, params,
        )
        persisted.append(_Persisted(corridor, sol.event_ids))
    return persisted


def _persist_competitors(
    db: Session,
    persisted: list[_Persisted],
    nodes: list[corridor_graph.EventNode],
    edge_map: dict[tuple[int, int], corridor_graph.Edge],
    tie_epsilon: float,
) -> None:
    """Per-position competing events for every corridor.

    An event competes with member ``e_k`` when swapping it in keeps both
    neighbouring edges feasible and the corridor score worsens by no more
    than ``tie_epsilon`` — the pair is published so an investigator sees the
    alternative instead of a silent pick.
    """
    nodes_by_id = {n.event_id: n for n in nodes}
    for item in persisted:
        ids = item.event_ids
        id_set = set(ids)
        for seq, event_id in enumerate(ids):
            prev_edge = edge_map.get((ids[seq - 1], event_id)) if seq > 0 else None
            next_edge = edge_map.get((event_id, ids[seq + 1])) if seq + 1 < len(ids) else None
            old_cost = (prev_edge.cost if prev_edge else 0.0) + (next_edge.cost if next_edge else 0.0)
            for candidate in nodes:
                if candidate.event_id in id_set:
                    continue
                if candidate.class_label != nodes_by_id[event_id].class_label:
                    continue
                new_cost = 0.0
                ok = True
                if seq > 0:
                    edge = edge_map.get((ids[seq - 1], candidate.event_id))
                    if edge is None or not edge.feasible:
                        ok = False
                    else:
                        new_cost += edge.cost
                if ok and seq + 1 < len(ids):
                    edge = edge_map.get((candidate.event_id, ids[seq + 1]))
                    if edge is None or not edge.feasible:
                        ok = False
                    else:
                        new_cost += edge.cost
                if not ok:
                    continue
                delta = new_cost - old_cost
                if delta <= tie_epsilon:
                    db.add(
                        CorridorCompetitor(
                            corridor_id=item.corridor.id,
                            seq=seq,
                            event_id=candidate.event_id,
                            replaces_event_id=event_id,
                            delta_score=delta,
                            reason=(
                                f"swap keeps edges feasible; score delta {delta:+.4f} "
                                f"within tie_epsilon"
                            ),
                        )
                    )


def _persist_unattributed(
    db: Session,
    run: CorridorRun,
    unattributed_ids: list[int],
    nodes: list[corridor_graph.EventNode],
    edges: list[corridor_graph.Edge],
    persisted: list[_Persisted],
) -> list[CorridorUnattributed]:
    nodes_by_id = {n.event_id: n for n in nodes}
    feasible_touch: dict[int, int] = {}
    infeasible_reasons: dict[int, list[str]] = {}
    for edge in edges:
        if edge.feasible:
            feasible_touch[edge.from_event_id] = feasible_touch.get(edge.from_event_id, 0) + 1
            feasible_touch[edge.to_event_id] = feasible_touch.get(edge.to_event_id, 0) + 1
        else:
            infeasible_reasons.setdefault(edge.from_event_id, []).append(edge.reason or "?")
            infeasible_reasons.setdefault(edge.to_event_id, []).append(edge.reason or "?")
    competed: set[int] = set()
    rows: list[CorridorUnattributed] = []
    for event_id in unattributed_ids:
        node = nodes_by_id.get(event_id)
        if feasible_touch.get(event_id, 0) == 0:
            reason = "isolated"
            detail = {
                "note": "no feasible reachability edge to any other event in the window",
                "infeasible_pair_reasons": sorted(set(infeasible_reasons.get(event_id, []))),
            }
        else:
            reason = "not_selected"
            detail = {
                "note": "had feasible edges but no corridor claimed it at this miss_cost",
                "feasible_edges": feasible_touch[event_id],
            }
        row = CorridorUnattributed(
            run_id=run.id,
            event_id=event_id,
            reason=reason,
            detail_json={
                **detail,
                "class_label": node.class_label if node else None,
                "stations": sorted(node.stations) if node else [],
            },
        )
        db.add(row)
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Invalidation (late / split observations touch adjacent windows only)
# --------------------------------------------------------------------------- #
def invalidate_for_processing_run(db: Session, processing_run_id: int) -> dict:
    """Mark corridor output affected by one event-pipeline run.

    The affected range is the union of emission intervals of events the run
    created or superseded.  Corridor runs whose window intersects that range
    become ``stale``; corridors citing a changed event become
    ``pending_review``; confirmations contradicted by a supersession become
    ``conflicted`` and are kept, not revoked.
    """
    created = list(
        db.execute(select(Event).where(Event.run_id == processing_run_id)).scalars()
    )
    created_ids = {e.id for e in created}
    superseded = list(
        db.execute(
            select(Event).where(Event.superseded_by_event_id.in_(created_ids or {0}))
        ).scalars()
    )
    changed = created + superseded
    if not changed:
        return {"changed_events": 0, "stale_runs": [], "pending_review": [], "conflicts": []}
    lo = min(e.emission_min_us for e in changed)
    hi = max(e.emission_max_us for e in changed)
    changed_ids = {e.id for e in changed}

    runs = list(
        db.execute(
            select(CorridorRun).where(
                CorridorRun.status == "done",
                CorridorRun.window_start_us <= hi,
                CorridorRun.window_end_us >= lo,
            )
        ).scalars()
    )
    stale_runs: list[int] = []
    pending: list[int] = []
    conflicts: list[int] = []
    for run in runs:
        run.status = "stale"
        run.stale_reason = (
            f"event run {processing_run_id} changed events "
            f"{sorted(changed_ids)} inside this window"
        )
        corridors = list(
            db.execute(select(Corridor).where(Corridor.run_id == run.id)).scalars()
        )
        for corridor in corridors:
            member_ids = {
                row.event_id
                for row in db.execute(
                    select(CorridorEvent).where(CorridorEvent.corridor_id == corridor.id)
                ).scalars()
            }
            hit = member_ids & changed_ids
            if not hit:
                continue
            corridor.status = "pending_review"
            pending.append(corridor.id)
            superseded_hits = {e.id for e in superseded} & hit
            if superseded_hits:
                for confirmation in db.execute(
                    select(CorridorConfirmation).where(
                        CorridorConfirmation.corridor_id == corridor.id,
                        CorridorConfirmation.active.is_(True),
                    )
                ).scalars():
                    confirmation.status = "conflicted"
                    confirmation.conflict_json = {
                        "reason": "confirmed corridor cites events superseded by new evidence",
                        "superseded_event_ids": sorted(superseded_hits),
                        "by_processing_run_id": processing_run_id,
                        "kept": "confirmation retained; not auto-revoked",
                    }
                    confirmation.updated_at_us = now_us()
                    conflicts.append(confirmation.id)
        stale_runs.append(run.id)
    db.add(
        AuditLog(
            actor="system",
            action="corridor.invalidation",
            target_type="processing_run",
            target_id=str(processing_run_id),
            detail={
                "changed_events": sorted(changed_ids),
                "stale_corridor_runs": stale_runs,
                "pending_review_corridors": pending,
                "conflicted_confirmations": conflicts,
            },
            created_at_us=now_us(),
        )
    )
    return {
        "changed_events": len(changed_ids),
        "stale_runs": stale_runs,
        "pending_review": pending,
        "conflicts": conflicts,
    }


def _flag_conflicts_for_run(db: Session, run: CorridorRun) -> None:
    """Flag confirmations a fresh overlapping run contradicts.

    A confirmed corridor whose exact event chain is not reproduced by any
    corridor of a newer overlapping run (same constraint book) is flagged
    ``conflicted`` with the new run cited — the confirmation itself stays.
    """
    confirmations = list(
        db.execute(
            select(CorridorConfirmation)
            .join(Corridor, Corridor.id == CorridorConfirmation.corridor_id)
            .join(CorridorRun, CorridorRun.id == Corridor.run_id)
            .where(
                CorridorConfirmation.active.is_(True),
                CorridorRun.id != run.id,
                CorridorRun.constraint_book_id == run.constraint_book_id,
                CorridorRun.window_start_us <= run.window_end_us,
                CorridorRun.window_end_us >= run.window_start_us,
            )
        ).scalars()
    )
    if not confirmations:
        return
    new_chains = set()
    for corridor in db.execute(select(Corridor).where(Corridor.run_id == run.id)).scalars():
        ids = tuple(
            row.event_id
            for row in db.execute(
                select(CorridorEvent)
                .where(CorridorEvent.corridor_id == corridor.id)
                .order_by(CorridorEvent.seq)
            ).scalars()
        )
        new_chains.add(ids)
    for confirmation in confirmations:
        old_chain = tuple(
            row.event_id
            for row in db.execute(
                select(CorridorEvent)
                .where(CorridorEvent.corridor_id == confirmation.corridor_id)
                .order_by(CorridorEvent.seq)
            ).scalars()
        )
        if old_chain and old_chain not in new_chains:
            confirmation.status = "conflicted"
            confirmation.conflict_json = {
                "reason": "new corridor run did not reproduce the confirmed chain",
                "new_corridor_run_id": run.id,
                "kept": "confirmation retained; not auto-revoked",
            }
            confirmation.updated_at_us = now_us()


# --------------------------------------------------------------------------- #
# Manual confirmations
# --------------------------------------------------------------------------- #
def confirm_corridor(
    db: Session, corridor: Corridor, confirmed_by: str, note: str | None
) -> CorridorConfirmation:
    existing = db.execute(
        select(CorridorConfirmation).where(
            CorridorConfirmation.corridor_id == corridor.id,
            CorridorConfirmation.active.is_(True),
        )
    ).scalars().first()
    if existing:
        raise ValueError("corridor already has an active confirmation")
    confirmation = CorridorConfirmation(
        corridor_id=corridor.id,
        confirmed_by=confirmed_by,
        note=note,
        active=True,
        status="active",
        created_at_us=now_us(),
        updated_at_us=now_us(),
    )
    corridor.status = "confirmed"
    db.add(confirmation)
    db.add(
        AuditLog(
            actor=confirmed_by,
            action="corridor.confirm",
            target_type="corridor",
            target_id=str(corridor.id),
            detail={"note": note},
            created_at_us=now_us(),
        )
    )
    db.flush()
    return confirmation


def revoke_confirmation(db: Session, corridor: Corridor, actor: str) -> CorridorConfirmation:
    confirmation = db.execute(
        select(CorridorConfirmation).where(
            CorridorConfirmation.corridor_id == corridor.id,
            CorridorConfirmation.active.is_(True),
        )
    ).scalars().first()
    if confirmation is None:
        raise ValueError("corridor has no active confirmation")
    confirmation.active = False
    confirmation.status = "revoked"
    confirmation.updated_at_us = now_us()
    corridor.status = "candidate"
    db.add(
        AuditLog(
            actor=actor,
            action="corridor.confirmation_revoked",
            target_type="corridor",
            target_id=str(corridor.id),
            detail={},
            created_at_us=now_us(),
        )
    )
    db.flush()
    return confirmation
