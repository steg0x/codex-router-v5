"""Privacy-safe JSONL outcome registry with explicit generation and TTL boundaries."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import (AUTO_ROUTES, Dependencies, Failure, Family, HistorySupport, Level, Mutation,
                   Phase, Route, Scope, TaskCapsule, Verification, Volume)


@dataclass(frozen=True)
class HistoricalRoute:
    """Recorded model identity without treating old generations as current routes."""
    family: str
    effort: str

    def __post_init__(self):
        if not self.family or not self.effort:
            raise ValueError("historical route identity required")


@dataclass(frozen=True)
class OutcomeRecord:
    task_id: str
    task_family: str
    phase: Phase
    specification_completeness: Level
    scope: Scope
    uncertainty: Level
    failcost: Level
    verification_strength: Verification
    mutation: Mutation
    volume: Volume
    dependencies: Dependencies
    repository_familiarity: Level
    history_support: HistorySupport
    initial_route: Route | HistoricalRoute | None
    actual_model: str
    actual_effort: str
    manual_override: bool
    escalation: bool
    retry_count: int
    rework_count: int
    deterministic_verification_result: str
    final_outcome: str
    final_successful_route: Route | HistoricalRoute | None
    hindsight_cheapest_verified_success: Route | HistoricalRoute | None
    router_version: str
    model_generation: str
    model_version: str
    timestamp: datetime
    evidence_kind: str
    tokens_if_observable: int | None = None
    cost_if_observable: float | None = None
    elapsed_time_if_observable: float | None = None
    verification_evidence_ref: str | None = None
    notes: str | None = None
    clarification_false_positive: bool | None = None
    escalation_justified: bool | None = None
    router_overhead_seconds: float | None = None
    classification_stable: bool | None = None
    failure_type: Failure = Failure.NONE
    comparison_protocol: str | None = None
    comparison_group_id: str | None = None
    input_snapshot_ref: str | None = None
    verification_available_at: datetime | None = None  # first auditable evidence availability
    structural_profile_complete: bool = True

    def __post_init__(self):
        enum_fields = {"phase": Phase, "specification_completeness": Level,
                       "scope": Scope, "uncertainty": Level, "failcost": Level,
                       "verification_strength": Verification, "mutation": Mutation,
                       "volume": Volume, "dependencies": Dependencies,
                       "repository_familiarity": Level, "history_support": HistorySupport}
        for name, enum in enum_fields.items():
            object.__setattr__(self, name, enum(getattr(self, name)))
        object.__setattr__(self, "failure_type", Failure(self.failure_type))
        if isinstance(self.timestamp, str):
            object.__setattr__(self, "timestamp", datetime.fromisoformat(self.timestamp))
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        if isinstance(self.verification_available_at, str):
            object.__setattr__(self, "verification_available_at",
                           datetime.fromisoformat(self.verification_available_at))
        if self.verification_available_at is not None:
            if self.verification_available_at.tzinfo is None:
                raise ValueError("verification availability must include timezone")
            if self.verification_available_at < self.timestamp:
                raise ValueError("verification cannot precede task timestamp")
        if self.evidence_kind not in ("REAL_VERIFIED", "SYNTHETIC_FIXTURE", "REPORTED_OUTCOME"):
            raise ValueError("explicit evidence_kind required")
        if self.final_outcome not in ("verified_success", "verified_failure", "unverified"):
            raise ValueError("invalid outcome")
        if self.evidence_kind == "REAL_VERIFIED" and (self.final_outcome == "unverified" or not self.verification_evidence_ref):
            raise ValueError("real verified outcomes require a result and evidence reference")
        if self.final_outcome == "verified_failure" and self.final_successful_route is not None:
            raise ValueError("failed outcome cannot identify a final successful route")
        if self.final_outcome == "verified_success" and self.final_successful_route is None:
            raise ValueError("successful outcome requires a final successful route")
        if self.retry_count < 0 or self.rework_count < 0:
            raise ValueError("counts cannot be negative")
        if self.cost_if_observable is not None and self.cost_if_observable < 0:
            raise ValueError("cost cannot be negative")
        if not self.model_generation or not self.model_version or not self.actual_model:
            raise ValueError("actual model generation and version required")
        if (self.model_generation == "gpt-6" and self.actual_model != "none"
                and not self.actual_model.startswith(("gpt-6-", "gpt-6.1-", "gpt-6.2-"))):
            raise ValueError("actual model/generation mismatch")
        if self.model_generation == "gpt-5.6" and not self.actual_model.startswith("gpt-5.6-"):
            raise ValueError("historical actual model/generation mismatch")
        comparison = (self.comparison_protocol, self.comparison_group_id, self.input_snapshot_ref)
        if any(value is not None for value in comparison):
            if not all(comparison) or self.comparison_protocol != "CONTROLLED_PAIRED_REPLAY":
                raise ValueError("controlled comparison requires protocol, group and input snapshot")
            if self.evidence_kind != "REAL_VERIFIED":
                raise ValueError("controlled comparison requires real verified evidence")
        for route in (self.initial_route, self.final_successful_route, self.hindsight_cheapest_verified_success):
            if route is not None and not isinstance(route, (Route, HistoricalRoute)):
                raise ValueError("invalid recorded route")
            if isinstance(route, Route) and self.model_generation != "gpt-6":
                raise ValueError("current route cannot represent an older generation")
            if isinstance(route, HistoricalRoute) and not (
                route.family.startswith(self.model_generation + "-")
                or (self.model_generation == "gpt-6" and route.family.startswith(("gpt-6-", "gpt-6.1-", "gpt-6.2-")))
            ):
                raise ValueError("historical route/generation mismatch")

    def valid_for(self, c: TaskCapsule, now: datetime | None = None, ttl_days: int = 90) -> bool:
        now = now or datetime.now(timezone.utc)
        def prefix(value):
            parts = re.split(r"[\W_]+", value.casefold())
            return next((part for part in parts if part and not part.isdigit()), "")
        same_structure = (self.phase == c.phase and self.scope == c.scope
                          and self.uncertainty == c.uncertainty and self.failcost == c.failcost
                          and self.mutation == c.mutation)
        same_project = any(prefix(left) and prefix(left) == prefix(right)
                           for left, right in ((self.task_id, c.task_id),
                                               (self.task_family, c.task_family)))
        exact_profile = (self.task_family == c.task_family and same_structure
                         and self.dependencies == c.dependencies
                         and self.verification_strength == c.verification
                         and self.specification_completeness == c.spec_completeness)
        return (self.evidence_kind == "REAL_VERIFIED"
                and self.structural_profile_complete
                and self.final_outcome != "unverified"
                and (self.final_outcome != "verified_failure"
                     or self.failure_type in (Failure.NONE, Failure.CAPABILITY))
                and not self.manual_override
                and self.initial_route in AUTO_ROUTES
                and self.actual_model == self.initial_route.family.value
                and (self.verification_available_at is None or self.verification_available_at <= now)
                and self.model_generation == c.model_generation
                and timedelta(0) <= now - self.timestamp <= timedelta(days=ttl_days)
                and (exact_profile or (same_project and same_structure)))

    def to_dict(self) -> dict:
        def encode(v):
            if isinstance(v, (Route, HistoricalRoute)):
                return {"family": v.family.value if hasattr(v.family, "value") else v.family,
                        "effort": v.effort.value if hasattr(v.effort, "value") else v.effort}
            if isinstance(v, datetime):
                return v.isoformat()
            if hasattr(v, "value"):
                return v.value
            return v
        return {f.name: encode(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_dict(cls, value: dict) -> "OutcomeRecord":
        value = dict(value)
        for key in ("initial_route", "final_successful_route", "hindsight_cheapest_verified_success"):
            if value.get(key):
                value[key] = (Route(**value[key]) if value[key]["family"] in (Family.LUNA.value, Family.TERRA.value, Family.SOL.value)
                              else HistoricalRoute(**value[key]))
        return cls(**value)


class OutcomeRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, record: OutcomeRecord) -> None:
        self.append_many([record])

    def append_many(self, records: list[OutcomeRecord]) -> None:
        """Replace the local JSONL file atomically after the full batch is validated."""
        if not records:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize before touching the destination.
        lines = [json.dumps(r.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
                 for r in records]
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n",
                                             dir=self.path.parent, prefix=".outcomes-",
                                             suffix=".tmp", delete=False) as out:
                temp_path = Path(out.name)
                if self.path.exists():
                    with self.path.open(encoding="utf-8") as source:
                        existing = source.read()
                    out.write(existing)
                    if existing and not existing.endswith("\n"):
                        out.write("\n")
                out.writelines(lines)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def read(self) -> list[OutcomeRecord]:
        if not self.path.exists():
            return []
        records = []
        with self.path.open(encoding="utf-8") as source:
            for line_no, line in enumerate(source, 1):
                if line.strip():
                    try:
                        records.append(OutcomeRecord.from_dict(json.loads(line)))
                    except (ValueError, TypeError, KeyError) as exc:
                        raise ValueError(f"invalid registry line {line_no}: {exc}") from exc
        return records

    def query(self, *, task_family: str | None = None, evidence_kind: str | None = None,
              generation: str | None = None, phase: Phase | None = None) -> list[OutcomeRecord]:
        return [r for r in self.read()
                if (task_family is None or r.task_family == task_family)
                and (evidence_kind is None or r.evidence_kind == evidence_kind)
                and (generation is None or r.model_generation == generation)
                and (phase is None or r.phase == phase)]

    def verified_for(self, capsule: TaskCapsule, *, now: datetime | None = None,
                     ttl_days: int = 90) -> list[OutcomeRecord]:
        return [r for r in self.read() if r.valid_for(capsule, now, ttl_days)]
