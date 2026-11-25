"""Deterministic, phase-scoped Router V5 candidate; no production dispatch hook."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Iterable


class StrEnum(str, Enum):
    pass


class RuleStatus(StrEnum):
    KNOWN = "KNOWN"
    DEFINE = "DEFINE"
    MISSING = "MISSING"


class Scope(StrEnum):
    LOCAL = "local"
    MULTI_COMPONENT = "multi-component"
    CROSS_SYSTEM = "cross-system"


class Level(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Verification(StrEnum):
    STRONG = "strong"
    PARTIAL = "partial"
    WEAK = "weak"


class Mutation(StrEnum):
    READ_ONLY = "read-only"
    LOCAL = "local-mutation"
    STATEFUL = "stateful-or-high-blast-radius"


class Volume(StrEnum):
    BOUNDED = "bounded"
    LARGE = "large"


class Dependencies(StrEnum):
    SHALLOW = "shallow"
    COUPLED = "coupled"


class Phase(StrEnum):
    DISCOVERY = "discovery"
    DESIGN = "design"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    CLEANUP = "cleanup"


class HistorySupport(StrEnum):
    NONE = "none"
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"


class Profile(StrEnum):
    COST_FIRST = "COST_FIRST"
    BALANCED = "BALANCED"
    QUALITY_FIRST = "QUALITY_FIRST"


class Role(StrEnum):
    ROOT = "root"
    ROUTER = "router"
    WORKER = "worker"
    STRONG_WORKER = "strong_worker"


class Family(StrEnum):
    LUNA = "gpt-6-luna"
    TERRA = "gpt-6.1-terra"
    SOL = "gpt-6.2-sol"
    ASTRA = "gpt-6-astra"


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class Failure(StrEnum):
    NONE = "none"
    OPERATIONAL = "operational"
    CAPABILITY = "capability"


class DecisionKind(StrEnum):
    ROUTE = "ROUTE"
    CLARIFY = "CLARIFY"
    UNOBSERVED = "UNOBSERVED"
    KEEP_LEASE = "KEEP_LEASE"
    RETURN_TO_ROOT = "RETURN_TO_ROOT"
    MANUAL_ESCALATION_REQUIRED = "MANUAL_ESCALATION_REQUIRED"


@dataclass(frozen=True)
class Route:
    family: Family
    effort: Effort

    def __post_init__(self):
        object.__setattr__(self, "family", Family(self.family))
        object.__setattr__(self, "effort", Effort(self.effort))


AUTO_EFFORTS = (Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH)
AUTO_FAMILIES = (Family.LUNA, Family.TERRA, Family.SOL)
FAMILY_RANK = {Family.LUNA: 0, Family.TERRA: 1, Family.SOL: 2, Family.ASTRA: 3}
AUTO_ROUTES = tuple(Route(family, effort) for family in AUTO_FAMILIES
                    for effort in AUTO_EFFORTS)


@dataclass(frozen=True)
class Lease:
    phase: Phase
    route: Route
    task_id: str

    def __post_init__(self):
        object.__setattr__(self, "phase", Phase(self.phase))
        if not self.task_id:
            raise ValueError("lease task_id required")


@dataclass(frozen=True)
class TaskCapsule:
    task_id: str
    task_family: str
    phase: Phase
    rule_status: RuleStatus
    scope: Scope
    uncertainty: Level
    failcost: Level
    verification: Verification
    mutation: Mutation
    volume: Volume
    dependencies: Dependencies
    spec_completeness: Level
    repo_familiarity: Level
    history_support: HistorySupport = HistorySupport.NONE
    profile: Profile = Profile.BALANCED
    role: Role = Role.ROOT
    router_version: str = "v5_candidate"
    model_generation: str = "gpt-6"
    model_version: str = "gpt-6"
    manual_override: Route | None = None
    lease: Lease | None = None
    failure: Failure = Failure.NONE
    capability_evidence_ref: str | None = None
    capability_escalations: int = 0
    essential_information_unavailable: bool = False
    consequential_choice_unresolved: bool = False
    verification_covers_failure_modes: bool = False
    safety_floor: Route | None = None
    v4_1_rule_status: RuleStatus | None = None  # replay annotation; ignored by V5
    v4_1_high: bool | None = None
    as_of: datetime | None = None  # replay clock; avoids future evidence leakage

    def __post_init__(self):
        enums = {"phase": Phase, "rule_status": RuleStatus, "scope": Scope,
                 "uncertainty": Level, "failcost": Level, "verification": Verification,
                 "mutation": Mutation, "volume": Volume, "dependencies": Dependencies,
                 "spec_completeness": Level, "repo_familiarity": Level,
                 "history_support": HistorySupport, "profile": Profile,
                 "role": Role, "failure": Failure}
        for name, enum in enums.items():
            object.__setattr__(self, name, enum(getattr(self, name)))
        if self.v4_1_rule_status is not None:
            object.__setattr__(self, "v4_1_rule_status", RuleStatus(self.v4_1_rule_status))
        if self.as_of is not None and self.as_of.tzinfo is None:
            raise ValueError("as_of must include timezone")
        if self.manual_override is not None and not isinstance(self.manual_override, Route):
            raise ValueError("manual_override must be a Route")
        if self.lease is not None and not isinstance(self.lease, Lease):
            raise ValueError("lease must be a Lease")
        if (self.lease is not None and self.lease.route.family is Family.ASTRA
                and self.manual_override != self.lease.route):
            raise ValueError("Astra lease requires the matching explicit manual override")
        if self.safety_floor is not None and not isinstance(self.safety_floor, Route):
            raise ValueError("safety_floor must be a Route")
        if not self.task_id or not self.task_family:
            raise ValueError("task_id and task_family required")
        if self.router_version != "v5_candidate" or self.model_generation != "gpt-6":
            raise ValueError("V5 capsule must identify offline V5 and GPT-6")
        if not is_supported_model_version(self.model_version):
            raise ValueError("model_version must be a GPT-6 model/version identifier")
        for name in ("essential_information_unavailable", "consequential_choice_unresolved",
                     "verification_covers_failure_modes"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if type(self.capability_escalations) is not int:
            raise ValueError("capability_escalations must be an integer")
        if self.capability_escalations not in (0, 1):
            raise ValueError("at most one capability escalation")
        if self.failure is Failure.CAPABILITY and not self.capability_evidence_ref:
            raise ValueError("capability escalation requires evidence")
        if self.rule_status is RuleStatus.MISSING and not self.essential_information_unavailable:
            raise ValueError("MISSING requires genuinely unavailable essential information")
        if self.essential_information_unavailable and self.rule_status is not RuleStatus.MISSING:
            raise ValueError("unavailable essential information requires MISSING")
        if self.rule_status is RuleStatus.DEFINE and not self.consequential_choice_unresolved:
            raise ValueError("DEFINE requires an unresolved consequential choice")
        if self.consequential_choice_unresolved and self.rule_status is RuleStatus.KNOWN:
            raise ValueError("unresolved consequential choice cannot be KNOWN")
        if self.safety_floor and self.safety_floor.family is Family.ASTRA:
            raise ValueError("Astra cannot be an automatic safety floor")


MODEL_VERSION_PATTERN = re.compile(
    r"(?:gpt-6(?:\.\d+)?(?:-[a-z][a-z0-9]*(?:\.[a-z0-9]+)?)*(?:-\d{4}-\d{2}(?:-\d{2})?)?"
    r"|gpt-6-\d{4}-\d{2}(?:-\d{2})?)\Z"
)


def is_supported_model_version(value: object) -> bool:
    """Accept GPT-6 model identifiers and dated GPT-6 version markers only."""
    return isinstance(value, str) and MODEL_VERSION_PATTERN.fullmatch(value) is not None


CAPSULE_AXES = {
    "phase": Phase, "rule_status": RuleStatus, "scope": Scope,
    "uncertainty": Level, "failcost": Level, "verification": Verification,
    "mutation": Mutation, "volume": Volume, "dependencies": Dependencies,
    "spec_completeness": Level, "repo_familiarity": Level,
}


def canonicalize_task_capsule(values: dict) -> tuple[dict, list[dict]]:
    """Return a copied capsule with only exact underscore-to-enum aliases normalized."""
    if not isinstance(values, dict):
        raise TypeError("TaskCapsule data must be an object")
    canonical = dict(values)
    applied = []
    for field, enum_type in CAPSULE_AXES.items():
        value = canonical.get(field)
        allowed = {member.value for member in enum_type}
        if isinstance(value, str) and "_" in value:
            candidate = value.replace("_", "-")
            if candidate in allowed:
                canonical[field] = candidate
                applied.append({"field": field, "from": value, "to": candidate})
    return canonical, applied


def build_task_capsule(values: dict, *, as_of: datetime | None = None) -> TaskCapsule:
    """Canonical TaskCapsule builder shared by live routing and prospective capture."""
    canonical, _ = canonicalize_task_capsule(values)
    if "as_of" in canonical:
        raise ValueError("as_of is controlled by the capsule builder")
    for key in ("manual_override", "safety_floor"):
        if isinstance(canonical.get(key), dict):
            canonical[key] = Route(**canonical[key])
    if isinstance(canonical.get("lease"), dict):
        canonical["lease"] = Lease(**canonical["lease"])
    if as_of is not None:
        canonical["as_of"] = as_of
    return TaskCapsule(**canonical)


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    route: Route | None
    rule_status: RuleStatus
    base_capability: Family | None
    base_effort: Effort | None
    floors: tuple[str, ...] = ()
    credits: tuple[str, ...] = ()
    adjustments: tuple[str, ...] = ()
    history_adjustment: str = "none"
    economic_adjustment: str = "none"
    reason_codes: tuple[str, ...] = ()
    phase: Phase | None = None
    manual_override: bool = False
    escalation_eligible: bool = False

    @property
    def final_capability(self) -> Family | None:
        return self.route.family if self.route else None

    @property
    def final_effort(self) -> Effort | None:
        return self.route.effort if self.route else None


def _meets_floor(route: Route, capability: Family, effort: Effort) -> bool:
    return (FAMILY_RANK[route.family] >= FAMILY_RANK[capability]
            and AUTO_EFFORTS.index(route.effort) >= AUTO_EFFORTS.index(effort))


def _raise_effort(route: Route) -> Route:
    """Reasoning-effort adjustment never changes capability family."""
    return Route(route.family, AUTO_EFFORTS[min(AUTO_EFFORTS.index(route.effort) + 1, 3)])


def _lower_effort(route: Route, effort_floor: Effort) -> Route:
    """Verification and cost credit reduce effort only, within its floor."""
    index = max(AUTO_EFFORTS.index(effort_floor), AUTO_EFFORTS.index(route.effort) - 1)
    return Route(route.family, AUTO_EFFORTS[index])


def route_v4_1(status: RuleStatus, high: bool) -> Decision:
    """Versioned frozen mapping from the V4.1 prompt, for offline replay only."""
    status = RuleStatus(status)
    if status is RuleStatus.MISSING:
        return Decision(DecisionKind.CLARIFY, None, status, None, None, reason_codes=("V4_1_MISSING",))
    route = Route(Family.SOL if status is RuleStatus.DEFINE else Family.LUNA,
                  Effort.HIGH if high else Effort.MEDIUM)
    return Decision(DecisionKind.ROUTE, route, status, route.family, route.effort,
                    reason_codes=("V4_1_FROZEN_MAPPING",))


def _base_route(c: TaskCapsule) -> Route:
    sol = (c.rule_status is RuleStatus.DEFINE or c.phase is Phase.DESIGN
           or (c.uncertainty is Level.HIGH and
               (c.failcost is Level.HIGH or c.scope is not Scope.LOCAL
                or c.verification is not Verification.STRONG))
           or (c.scope is Scope.CROSS_SYSTEM and c.failcost is Level.HIGH
               and c.verification is not Verification.STRONG))
    if sol:
        if (c.rule_status is RuleStatus.DEFINE and c.uncertainty is Level.HIGH
                and c.failcost is Level.HIGH
                and (c.scope is Scope.CROSS_SYSTEM or c.mutation is Mutation.STATEFUL)):
            effort = Effort.XHIGH
        elif ((c.rule_status is RuleStatus.DEFINE and
               (c.uncertainty is Level.HIGH or c.failcost is Level.HIGH))
              or (c.scope is not Scope.LOCAL and c.uncertainty is Level.HIGH
                  and c.failcost is Level.HIGH and c.verification is not Verification.STRONG)):
            effort = Effort.HIGH
        elif ((c.scope is Scope.LOCAL or c.volume is Volume.BOUNDED)
              and c.uncertainty is Level.LOW and c.failcost is not Level.HIGH):
            effort = Effort.LOW
        else:
            effort = Effort.MEDIUM
        return Route(Family.SOL, effort)
    terra = (c.scope is not Scope.LOCAL and c.dependencies is Dependencies.COUPLED
             and (c.uncertainty is Level.MEDIUM or c.phase is Phase.DISCOVERY))
    if terra:
        if c.volume is Volume.LARGE and (c.failcost is Level.HIGH or c.mutation is Mutation.STATEFUL):
            effort = Effort.XHIGH
        elif c.failcost is Level.HIGH or c.mutation is Mutation.STATEFUL or c.scope is Scope.CROSS_SYSTEM:
            effort = Effort.HIGH
        elif c.uncertainty is Level.LOW and c.volume is Volume.BOUNDED and c.failcost is Level.LOW:
            effort = Effort.LOW
        else:
            effort = Effort.MEDIUM
        return Route(Family.TERRA, effort)
    if (c.rule_status is RuleStatus.KNOWN and c.volume is Volume.LARGE
            and c.dependencies is Dependencies.COUPLED
            and (c.failcost is Level.HIGH or c.mutation is Mutation.STATEFUL)
            and c.verification is Verification.STRONG):
        effort = Effort.XHIGH
    elif (c.scope is not Scope.LOCAL or c.dependencies is Dependencies.COUPLED
          or c.failcost is Level.HIGH or c.mutation is Mutation.STATEFUL):
        effort = Effort.HIGH
    elif (c.uncertainty is Level.LOW and c.scope is Scope.LOCAL
          and c.failcost is Level.LOW and c.dependencies is Dependencies.SHALLOW
          and c.volume is Volume.BOUNDED):
        effort = Effort.LOW
    else:
        effort = Effort.MEDIUM
    return Route(Family.LUNA, effort)


def route_v5(c: TaskCapsule, history: Iterable[object] = (), *, now: datetime | None = None) -> Decision:
    """Pure offline route. History records are accepted only through their validated evidence method."""
    if c.role is not Role.ROOT:
        return Decision(DecisionKind.RETURN_TO_ROOT, None, c.rule_status, None, None,
                        reason_codes=("NON_ROOT_CANNOT_REROUTE",), phase=c.phase)
    if c.rule_status is RuleStatus.MISSING:
        return Decision(DecisionKind.CLARIFY, None, c.rule_status, None, None,
                        reason_codes=("ESSENTIAL_INFORMATION_UNAVAILABLE",), phase=c.phase)
    if c.manual_override:
        return Decision(DecisionKind.ROUTE, c.manual_override, c.rule_status,
                        c.manual_override.family, c.manual_override.effort,
                        reason_codes=("EXPLICIT_MANUAL_OVERRIDE",), phase=c.phase,
                        manual_override=True)
    if c.lease and c.lease.task_id == c.task_id and c.lease.phase is c.phase and c.failure is Failure.NONE:
        return Decision(DecisionKind.KEEP_LEASE, c.lease.route, c.rule_status,
                        c.lease.route.family, c.lease.route.effort,
                        reason_codes=("PHASE_LEASE",), phase=c.phase)
    if (c.failure is Failure.OPERATIONAL and c.lease
            and c.lease.task_id == c.task_id and c.lease.phase is c.phase):
        return Decision(DecisionKind.KEEP_LEASE, c.lease.route, c.rule_status,
                        c.lease.route.family, c.lease.route.effort,
                        reason_codes=("OPERATIONAL_FAILURE_NO_ESCALATION",), phase=c.phase)
    base = _base_route(c)
    floors: list[str] = []
    credits: list[str] = []
    adjustments: list[str] = []
    capability_floor = base.family
    effort_floor = Effort.LOW
    # Incomplete specifications become an autonomy floor only with coupled,
    # consequential cross-system/stateful work. High completeness avoids it.
    if (c.rule_status is RuleStatus.KNOWN and c.spec_completeness is Level.LOW
            and (c.scope is Scope.CROSS_SYSTEM or c.dependencies is Dependencies.COUPLED)
            and (c.failcost is Level.HIGH or c.mutation is Mutation.STATEFUL)):
        capability_floor = Family.SOL
        floors.append("LOW_SPEC_AUTONOMY")
    if c.failcost is Level.HIGH:
        effort_floor = base.effort
        floors.append("HIGH_FAILCOST")
    if c.mutation is Mutation.STATEFUL:
        effort_floor = base.effort
        floors.append("BLAST_RADIUS")
    if c.safety_floor:
        capability_floor = max((capability_floor, c.safety_floor.family), key=lambda f: FAMILY_RANK[f])
        effort_floor = max((effort_floor, c.safety_floor.effort), key=AUTO_EFFORTS.index)
        floors.append("EXPLICIT_SAFETY_FLOOR")
    route = Route(max((base.family, capability_floor), key=lambda f: FAMILY_RANK[f]),
                  max((base.effort, effort_floor), key=AUTO_EFFORTS.index))
    if c.failure is Failure.OPERATIONAL:
        return Decision(DecisionKind.ROUTE, route, c.rule_status, base.family, base.effort,
                        tuple(floors), reason_codes=("OPERATIONAL_FAILURE_NO_ESCALATION",),
                        phase=c.phase, escalation_eligible=False)
    if c.failure is Failure.CAPABILITY:
        current = c.lease.route if c.lease and c.lease.task_id == c.task_id and c.lease.phase is c.phase else route
        if (c.capability_escalations >= 1
                or (current.family is Family.SOL and current.effort is Effort.XHIGH)):
            return Decision(DecisionKind.MANUAL_ESCALATION_REQUIRED, None, c.rule_status,
                            base.family, base.effort, tuple(floors),
                            reason_codes=("CAPABILITY_CEILING",), phase=c.phase)
        route = Route(Family.SOL, Effort.XHIGH)
        if not _meets_floor(route, capability_floor, effort_floor):
            route = Route(Family.SOL, Effort.HIGH)
        adjustments.append("ONE_CAPABILITY_ESCALATION")
    else:
        if c.verification is Verification.STRONG and c.verification_covers_failure_modes:
            lowered = _lower_effort(route, effort_floor)
            if lowered != route:
                route = lowered
                credits.append("STRONG_COVERED_VERIFICATION_EFFORT_DOWN")
        # History is a one-axis tie-breaker and never crosses floors.
        from .registry import OutcomeRecord
        evidence_time = now or c.as_of
        eligible = [r for r in history if isinstance(r, OutcomeRecord)
                    and r.task_id != c.task_id and r.valid_for(c, evidence_time)]
        positive = any(getattr(r, "final_outcome", None) == "verified_success" for r in eligible)
        negative = any(getattr(r, "final_outcome", None) == "verified_failure" for r in eligible)
        history_adjustment = "none"
        if positive and not negative:
            lowered = _lower_effort(route, effort_floor)
            if lowered != route:
                route = lowered
                history_adjustment = "effort_down"
        elif negative and not positive:
            if route.family in (Family.LUNA, Family.TERRA):
                route = Route(Family.SOL, route.effort)
                history_adjustment = "capability_up"
        economic_adjustment = "none"
        if c.profile is Profile.COST_FIRST and c.verification is Verification.STRONG and c.verification_covers_failure_modes:
            lowered = _lower_effort(route, effort_floor)
            if lowered != route:
                route = lowered
                economic_adjustment = "effort_down"
        elif c.profile is Profile.QUALITY_FIRST and c.verification is Verification.WEAK:
            raised = _raise_effort(route)
            if raised != route:
                route = raised
                economic_adjustment = "effort_up"
        return Decision(DecisionKind.ROUTE, route, c.rule_status, base.family, base.effort,
                        tuple(floors), tuple(credits), tuple(adjustments), history_adjustment,
                        economic_adjustment, ("STRUCTURAL_POLICY",), c.phase,
                        escalation_eligible=route.family in (Family.LUNA, Family.TERRA) and c.capability_escalations == 0)
    return Decision(DecisionKind.ROUTE, route, c.rule_status, base.family, base.effort,
                    tuple(floors), tuple(credits), tuple(adjustments),
                    reason_codes=("EVIDENCE_BACKED_ESCALATION",), phase=c.phase,
                    escalation_eligible=False)
