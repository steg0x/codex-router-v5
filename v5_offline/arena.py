"""Offline replay, explicit observation coverage, and evidence-gated promotion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .core import (AUTO_ROUTES, FAMILY_RANK, Decision, DecisionKind, Effort, Family, Level,
                   Route, RuleStatus, TaskCapsule, route_v4_1, route_v5)
from .registry import OutcomeRecord


Strategy = Callable[[TaskCapsule, tuple[OutcomeRecord, ...]], Decision]


def _fixed(route: Route) -> Strategy:
    def choose(c: TaskCapsule, records: tuple[OutcomeRecord, ...]) -> Decision:
        if c.rule_status is RuleStatus.MISSING:
            return Decision(DecisionKind.CLARIFY, None, c.rule_status, None, None,
                            reason_codes=("ESSENTIAL_INFORMATION_UNAVAILABLE",), phase=c.phase)
        return Decision(DecisionKind.ROUTE, route, c.rule_status, route.family, route.effort,
                        reason_codes=("FIXED_BASELINE",), phase=c.phase)
    return choose


def _v41(c: TaskCapsule, records: tuple[OutcomeRecord, ...]) -> Decision:
    if c.v4_1_rule_status is None or c.v4_1_high is None:
        return Decision(DecisionKind.UNOBSERVED, None, c.rule_status, None, None,
                        reason_codes=("V4_1_CLASSIFICATION_NOT_RECONSTRUCTED",), phase=c.phase)
    return route_v4_1(c.v4_1_rule_status, c.v4_1_high)


def _v5_no_history(c: TaskCapsule, records: tuple[OutcomeRecord, ...]) -> Decision:
    """Structural policy only; suitable for frozen classification benchmarks."""
    return route_v5(c, (), now=c.as_of)


def _v5_prequential_history(c: TaskCapsule, records: tuple[OutcomeRecord, ...]) -> Decision:
    """Use only outcomes that were available strictly before decision time."""
    if c.as_of is None:
        return Decision(DecisionKind.UNOBSERVED, None, c.rule_status, None, None,
                        reason_codes=("DECISION_TIMESTAMP_REQUIRED_FOR_HISTORY",), phase=c.phase)
    prior = tuple(r for r in records if r.evidence_kind == "REAL_VERIFIED"
                  and r.verification_available_at is not None
                  and r.verification_available_at < c.as_of and r.task_id != c.task_id)
    return route_v5(c, prior, now=c.as_of)


def _oracle(c: TaskCapsule, records: tuple[OutcomeRecord, ...]) -> Decision:
    eligible = [r for r in records if r.evidence_kind == "REAL_VERIFIED"
                and _same_task_structure(c, r)
                and r.hindsight_cheapest_verified_success is not None]
    if not eligible:
        return Decision(DecisionKind.UNOBSERVED, None, c.rule_status, None, None,
                        reason_codes=("ORACLE_UNOBSERVED",), phase=c.phase)
    route = eligible[0].hindsight_cheapest_verified_success
    return Decision(DecisionKind.ROUTE, route, c.rule_status, route.family, route.effort,
                    reason_codes=("ANALYSIS_ONLY_ORACLE",), phase=c.phase)


DEFAULT_STRATEGIES: dict[str, Strategy] = {
    "v4_1": _v41,
    "v5_candidate": _v5_no_history,
    "v5_no_history": _v5_no_history,
    "v5_prequential_history": _v5_prequential_history,
    "always_gpt_6_luna_medium": _fixed(Route(Family.LUNA, Effort.MEDIUM)),
    "always_gpt_6_luna_high": _fixed(Route(Family.LUNA, Effort.HIGH)),
    "always_gpt_6_sol_medium": _fixed(Route(Family.SOL, Effort.MEDIUM)),
    "always_gpt_6_sol_high": _fixed(Route(Family.SOL, Effort.HIGH)),
    "hindsight_oracle": _oracle,
}


def _same_task_structure(c: TaskCapsule, r: OutcomeRecord) -> bool:
    return (r.task_id == c.task_id and r.task_family == c.task_family
            and r.phase == c.phase and r.specification_completeness == c.spec_completeness
            and r.scope == c.scope and r.uncertainty == c.uncertainty
            and r.failcost == c.failcost and r.verification_strength == c.verification
            and r.mutation == c.mutation and r.volume == c.volume
            and r.dependencies == c.dependencies)


@dataclass(frozen=True)
class CostValue:
    value: float
    kind: str  # observed, estimated, normalized
    generation: str

    def __post_init__(self):
        if self.value < 0 or self.kind not in ("observed", "estimated", "normalized"):
            raise ValueError("invalid cost")


@dataclass(frozen=True)
class ReplayRow:
    task_id: str
    strategy: str
    decision: Decision
    observed: bool
    verified_success: bool | None
    cost: CostValue | None
    retries: int | None
    rework: int | None
    manual_override: bool | None
    escalation: bool | None
    oracle_route: Route | None
    clarification_false_positive: bool | None = None
    escalation_justified: bool | None = None
    elapsed_seconds: float | None = None
    router_overhead_seconds: float | None = None
    classification_stable: bool | None = None
    comparison_protocol: str | None = None
    comparison_group_id: str | None = None
    input_snapshot_ref: str | None = None


class Arena:
    def __init__(self, strategies: dict[str, Strategy] | None = None,
                 normalized_costs: dict[str, dict[str, float]] | None = None):
        self.strategies = dict(DEFAULT_STRATEGIES)
        if strategies:
            self.strategies.update(strategies)
        self.normalized_costs = normalized_costs or {}

    def register(self, name: str, strategy: Strategy) -> None:
        if name in self.strategies:
            raise ValueError("strategy already registered")
        self.strategies[name] = strategy

    def replay(self, capsules: Iterable[TaskCapsule], outcomes: Iterable[OutcomeRecord]) -> list[ReplayRow]:
        records = tuple(outcomes)
        rows = []
        for c in capsules:
            candidates = tuple(r for r in records if r.evidence_kind == "REAL_VERIFIED"
                               and _same_task_structure(c, r))
            pair_keys = {(r.comparison_protocol, r.comparison_group_id, r.input_snapshot_ref)
                         for r in candidates if r.comparison_protocol is not None}
            if len(pair_keys) > 1:
                task_records = ()  # ambiguous frozen input; require a unique comparison group
            elif pair_keys:
                only_pair = next(iter(pair_keys))
                task_records = tuple(r for r in candidates
                                     if (r.comparison_protocol, r.comparison_group_id,
                                         r.input_snapshot_ref) == only_pair)
            else:
                task_records = candidates
            # Outcome completion is not the decision point. A prospective
            # capsule must carry its captured pre-execution timestamp.
            replay_capsule = c
            oracle = next((r.hindsight_cheapest_verified_success for r in task_records
                           if r.hindsight_cheapest_verified_success), None)
            prospective_pair = any(r.router_version in ("v4_1_live", "v5_controlled_replay")
                                   for r in task_records if r.comparison_protocol == "CONTROLLED_PAIRED_REPLAY")
            for name, strategy in self.strategies.items():
                decision = strategy(replay_capsule, task_records if name == "hindsight_oracle" else records)
                matches = [r for r in task_records if decision.kind is not DecisionKind.UNOBSERVED
                           and r.initial_route == decision.route
                           and r.model_generation == c.model_generation
                           and r.model_version == c.model_version
                           and (not prospective_pair or name not in ("v4_1", "v5_candidate",
                                                                     "v5_no_history", "v5_prequential_history")
                                or (name == "v4_1" and r.router_version == "v4_1_live")
                                or (name.startswith("v5") and r.router_version == "v5_controlled_replay"))]
                matched = matches[0] if len(matches) == 1 else None
                cost = None
                if matched and matched.cost_if_observable is not None:
                    cost = CostValue(matched.cost_if_observable, "observed", matched.model_generation)
                elif matched and decision.route:
                    route_key = f"{decision.route.family.value}:{decision.route.effort.value}"
                    normalized = self.normalized_costs.get(c.model_generation, {}).get(route_key)
                    if normalized is not None:
                        cost = CostValue(normalized, "normalized", c.model_generation)
                rows.append(ReplayRow(c.task_id, name, decision, matched is not None,
                                      matched.final_outcome == "verified_success" if matched else None,
                                      cost, matched.retry_count if matched else None,
                                      matched.rework_count if matched else None,
                                      matched.manual_override if matched else None,
                                      matched.escalation if matched else None, oracle,
                                      matched.clarification_false_positive if matched else None,
                                      matched.escalation_justified if matched else None,
                                      matched.elapsed_time_if_observable if matched else None,
                                      matched.router_overhead_seconds if matched else None,
                                      matched.classification_stable if matched else None,
                                      matched.comparison_protocol if matched else None,
                                      matched.comparison_group_id if matched else None,
                                      matched.input_snapshot_ref if matched else None))
        return rows


def _under_route(route: Route, oracle: Route) -> bool:
    """A deficit on either independent axis is reported; mixed routes stay visible."""
    return ((FAMILY_RANK[route.family] < FAMILY_RANK[oracle.family])
            or (list(Effort).index(route.effort) < list(Effort).index(oracle.effort)))


def metrics(rows: Iterable[ReplayRow]) -> dict[str, dict]:
    groups: dict[str, list[ReplayRow]] = {}
    for row in rows:
        groups.setdefault(row.strategy, []).append(row)
    result = {}
    for name, subset in groups.items():
        observed = [r for r in subset if r.observed]
        successes = [r for r in observed if r.verified_success]
        costs = [r.cost for r in observed if r.cost]
        cost_kinds = {(c.kind, c.generation) for c in costs}
        cost_to_success = sum(c.value for c in costs) / len(successes) if successes and len(cost_kinds) == 1 and len(costs) == len(observed) else None
        oracle_known = [r for r in subset if r.oracle_route in AUTO_ROUTES and r.decision.route in AUTO_ROUTES]
        under = [r for r in oracle_known if _under_route(r.decision.route, r.oracle_route)]
        unnecessary = [r for r in oracle_known if FAMILY_RANK[r.decision.route.family] > FAMILY_RANK[r.oracle_route.family]]
        escalations = [r for r in observed if r.escalation]
        clarifications = [r for r in observed if r.decision.kind is DecisionKind.CLARIFY]
        elapsed = [r.elapsed_seconds for r in observed if r.elapsed_seconds is not None]
        overhead = [r.router_overhead_seconds for r in observed if r.router_overhead_seconds is not None]
        stability = [r.classification_stable for r in observed if r.classification_stable is not None]
        result[name] = {
            "total": len(subset), "observed": len(observed), "verified_success": len(successes),
            "verified_success_rate": len(successes) / len(observed) if observed else None,
            "cost_to_success": cost_to_success,
            "cost_basis": next(iter(cost_kinds)) if len(cost_kinds) == 1 else None,
            "retries": sum(r.retries or 0 for r in observed),
            "rework": sum(r.rework or 0 for r in observed),
            "unnecessary_strong_model_rate": len(unnecessary) / len(oracle_known) if oracle_known else None,
            "under_routing_rate": len(under) / len(oracle_known) if oracle_known else None,
            "escalation_precision": sum(bool(r.escalation_justified) for r in escalations) / len(escalations) if escalations and all(r.escalation_justified is not None for r in escalations) else None,
            "clarification_false_positive_rate": (sum(bool(r.clarification_false_positive) for r in clarifications) / len(clarifications)
                                                 if clarifications and all(r.clarification_false_positive is not None for r in clarifications)
                                                 else 0.0 if observed and not clarifications else None),
            "latency_seconds": sum(elapsed) / len(elapsed) if len(elapsed) == len(observed) and observed else None,
            "router_overhead_seconds": sum(overhead) / len(overhead) if len(overhead) == len(observed) and observed else None,
            "classification_stability": sum(bool(v) for v in stability) / len(stability) if len(stability) == len(observed) and observed else None,
            "closed_on_first_route_rate": sum(r.retries == 0 and r.rework == 0 for r in successes) / len(successes) if successes else None,
            "manual_override_rate": sum(bool(r.manual_override) for r in observed) / len(observed) if observed else None,
            "escalation_rate": sum(bool(r.escalation) for r in observed) / len(observed) if observed else None,
        }
    return result


def pareto_frontier(summary: dict[str, dict]) -> list[str]:
    """Non-dominated points across success, cost, and rework; unknowns are excluded."""
    valid = {k: v for k, v in summary.items() if v["verified_success_rate"] is not None
             and v["cost_to_success"] is not None and v["observed"] == v["total"]}
    front = []
    for name, a in valid.items():
        dominated = any(other != name and b["verified_success_rate"] >= a["verified_success_rate"]
                        and b["cost_to_success"] <= a["cost_to_success"]
                        and b["rework"] <= a["rework"]
                        and (b["verified_success_rate"] > a["verified_success_rate"]
                             or b["cost_to_success"] < a["cost_to_success"] or b["rework"] < a["rework"])
                        for other, b in valid.items())
        if not dominated:
            front.append(name)
    return front


@dataclass(frozen=True)
class PromotionResult:
    status: str
    reason_codes: tuple[str, ...]
    real_verified_task_count: int
    candidate_strategy: str


def evaluate_promotion(rows: Iterable[ReplayRow], outcomes: Iterable[OutcomeRecord], *,
                       candidate_strategy: str,
                       min_real_tasks: int = 30, success_tolerance: float = 0.0,
                       manual_override_tolerance: float = 0.0,
                       false_clarification_tolerance: float = 0.0,
                       escalation_tolerance: float = 0.0) -> PromotionResult:
    if candidate_strategy not in ("v5_no_history", "v5_prequential_history", "v5_candidate"):
        raise ValueError("candidate_strategy must explicitly name a registered V5 evaluation")
    rows = list(rows)
    if any(v < 0 for v in (success_tolerance, manual_override_tolerance,
                           false_clarification_tolerance, escalation_tolerance)):
        raise ValueError("promotion tolerances cannot be negative")
    real_ids = {r.task_id for r in outcomes if r.evidence_kind == "REAL_VERIFIED"}
    if len(real_ids) < min_real_tasks:
        return PromotionResult("INSUFFICIENT_EVIDENCE", ("REAL_VERIFIED_CORPUS_BELOW_MINIMUM",),
                               len(real_ids), candidate_strategy)
    by_task = {(r.task_id, r.strategy): r for r in rows if r.task_id in real_ids}
    for task_id in real_ids:
        old = by_task.get((task_id, "v4_1"))
        new = by_task.get((task_id, candidate_strategy))
        if not old or not new or not old.observed or not new.observed:
            return PromotionResult("INSUFFICIENT_EVIDENCE", ("COUNTERFACTUAL_OUTCOMES_UNOBSERVED",),
                                   len(real_ids), candidate_strategy)
        if (old.decision.kind, old.decision.route) != (new.decision.kind, new.decision.route):
            old_pair = (old.comparison_protocol, old.comparison_group_id, old.input_snapshot_ref)
            new_pair = (new.comparison_protocol, new.comparison_group_id, new.input_snapshot_ref)
            if (old_pair != new_pair or not all(old_pair)
                    or old.comparison_protocol != "CONTROLLED_PAIRED_REPLAY"):
                return PromotionResult("INSUFFICIENT_EVIDENCE", ("CONTROLLED_PAIRED_REPLAY_MISSING",),
                                       len(real_ids), candidate_strategy)
    groups = metrics(r for r in rows if r.task_id in real_ids)
    a = groups.get("v4_1")
    b = groups.get(candidate_strategy)
    if not a or not b or a["observed"] != a["total"] or b["observed"] != b["total"]:
        return PromotionResult("INSUFFICIENT_EVIDENCE", ("COUNTERFACTUAL_OUTCOMES_UNOBSERVED",),
                               len(real_ids), candidate_strategy)
    required = ("verified_success_rate", "cost_to_success", "manual_override_rate", "escalation_rate",
                "clarification_false_positive_rate")
    if any(a[k] is None or b[k] is None for k in required) or a["cost_basis"] != b["cost_basis"]:
        return PromotionResult("INSUFFICIENT_EVIDENCE", ("PROMOTION_METRICS_OR_COMPARABLE_COST_MISSING",),
                               len(real_ids), candidate_strategy)
    reasons = []
    if b["verified_success_rate"] + success_tolerance < a["verified_success_rate"]:
        reasons.append("VERIFIED_SUCCESS_REGRESSION")
    if b["cost_to_success"] >= a["cost_to_success"]:
        reasons.append("COST_TO_SUCCESS_NOT_LOWER")
    if b["manual_override_rate"] > a["manual_override_rate"] + manual_override_tolerance:
        reasons.append("MANUAL_OVERRIDES_REGRESSION")
    if b["clarification_false_positive_rate"] > a["clarification_false_positive_rate"] + false_clarification_tolerance:
        reasons.append("FALSE_CLARIFICATIONS_REGRESSION")
    if b["escalation_rate"] > a["escalation_rate"] + escalation_tolerance:
        reasons.append("ESCALATION_NOT_CONTROLLED")
    return PromotionResult("FAIL" if reasons else "PASS", tuple(reasons), len(real_ids), candidate_strategy)


def evaluate_promotion_views(rows: Iterable[ReplayRow], outcomes: Iterable[OutcomeRecord], **gate_options) -> dict[str, PromotionResult]:
    """Separately report structural and full-history comparisons against V4.1.

    A future live-promotion decision, if history is part of V5, must use the
    `full_v5` result. This helper does not itself promote anything.
    """
    rows = tuple(rows)
    outcomes = tuple(outcomes)
    return {
        "structural": evaluate_promotion(rows, outcomes, candidate_strategy="v5_no_history", **gate_options),
        "full_v5": evaluate_promotion(rows, outcomes, candidate_strategy="v5_prequential_history", **gate_options),
    }
