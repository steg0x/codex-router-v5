"""Instruction-driven live V5 bridge. No model calls or task execution occur here."""

from __future__ import annotations

# The direct logger avoids dataclasses, routing, registry scans and replay imports.
# Keep this branch before every heavyweight import; imported API still exposes log_task.
import sys
if __name__ == "__main__" and sys.argv[1:2] == ["log"]:
    from .lightweight import main as _log_main
    raise SystemExit(_log_main(sys.argv[2:]))


import argparse
import hashlib
import json
import os
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .arena import Arena, evaluate_promotion_views, metrics, pareto_frontier
from .core import (AUTO_ROUTES, CAPSULE_AXES, Decision, DecisionKind, Route, RuleStatus,
                   TaskCapsule, build_task_capsule, canonicalize_task_capsule,
                   is_supported_model_version, route_v4_1, route_v5)
from .registry import OutcomeRecord, OutcomeRegistry
from .lightweight import log_task

ROUTER_VERSION = "v5_prequential_history_sol61_live"
DEFAULT_STATE = Path.home() / ".codex" / "router_v5_live"
FALLBACK_REASONS = {"BRIDGE_INVOCATION_FAILED", "CONFIGURATION_ERROR", "INVALID_CAPSULE",
                    "INVALID_CAPSULE_AFTER_REPAIR",
                    "V4_1_VALIDATION_FAILURE", "REGISTRY_READ_FAILURE",
                    "ROUTING_STATE_UNREADABLE", "ROUTING_STATE_UNWRITABLE",
                    "ROUTER_INTERNAL_ERROR"}


def validate_request(request: dict) -> dict:
    """Validate all deterministic live capsule constraints without routing or I/O."""
    if not isinstance(request, dict):
        return {"valid": False, "errors": [{"field": "request", "value": request,
                                             "allowed": ["JSON object"]}],
                "v4_1_errors": [], "failure_stage": "PREFLIGHT_VALIDATION",
                "request": request, "normalizations_applied": []}
    raw = request.get("capsule")
    if not isinstance(raw, dict):
        errors = [{"field": "capsule", "value": raw, "allowed": ["object"]}]
        v41_errors = _validate_v41(request.get("v4_1"))
        errors.extend(v41_errors)
        return {"valid": False, "errors": errors,
                "v4_1_errors": v41_errors,
                "failure_stage": "V4_1_VALIDATION" if v41_errors else "PREFLIGHT_VALIDATION",
                "request": request, "normalizations_applied": []}
    errors = []
    v41_errors = _validate_v41(request.get("v4_1"))
    errors.extend(v41_errors)
    normalized = dict(request)
    if "local_repair_used" in request and type(request["local_repair_used"]) is not bool:
        errors.append({"field": "local_repair_used", "value": request["local_repair_used"],
                       "allowed": [True, False]})
    capsule, aliases = canonicalize_task_capsule(raw)
    normalized["capsule"] = capsule
    for field, enum_type in CAPSULE_AXES.items():
        value = capsule.get(field)
        allowed = [member.value for member in enum_type]
        if value not in allowed:
            errors.append({"field": field, "value": value, "allowed": allowed})
    task_id = capsule.get("task_id")
    if (not isinstance(task_id, str) or not task_id
            or not task_id.replace("_", "").replace("-", "").isalnum()):
        errors.append({"field": "task_id", "value": task_id,
                       "reason": "required simple identifier (letters, digits, underscore, hyphen)"})
    if not isinstance(capsule.get("task_family"), str) or not capsule.get("task_family"):
        errors.append({"field": "task_family", "value": capsule.get("task_family"),
                       "reason": "non-empty string required"})
    if not is_supported_model_version(capsule.get("model_version")):
        errors.append({"field": "model_version", "value": capsule.get("model_version"),
                       "reason": "GPT-6 model/version identifier required"})
    if capsule.get("model_generation", "gpt-6") != "gpt-6":
        errors.append({"field": "model_generation", "value": capsule.get("model_generation"),
                       "allowed": ["gpt-6"]})
    if capsule.get("role", "root") != "root":
        errors.append({"field": "role", "value": capsule.get("role"), "allowed": ["root"]})
    if capsule.get("manual_override") is not None:
        errors.append({"field": "manual_override", "value": capsule.get("manual_override"),
                       "reason": "explicit manual model override bypasses automatic V5 routing"})
    if capsule.get("lease") is not None:
        errors.append({"field": "lease", "value": capsule.get("lease"),
                       "reason": "lease continuation is handled by the existing root lease protocol"})
    for field in ("essential_information_unavailable", "consequential_choice_unresolved",
                  "verification_covers_failure_modes"):
        if field in capsule and type(capsule[field]) is not bool:
            errors.append({"field": field, "value": capsule[field], "allowed": [True, False]})
    if "capability_escalations" in capsule and type(capsule["capability_escalations"]) is not int:
        errors.append({"field": "capability_escalations", "value": capsule["capability_escalations"],
                       "reason": "integer required"})
    if not errors:
        try:
            # Reuse the real dataclass constructor and all of its cross-field/type checks.
            build_task_capsule(capsule, as_of=datetime(2000, 1, 1, tzinfo=timezone.utc))
        except Exception as exc:
            errors.append({"field": "capsule", "reason": f"{type(exc).__name__}: {exc}"})
    failure_stage = ("V4_1_VALIDATION" if v41_errors else
                     "PREFLIGHT_VALIDATION" if errors else None)
    return {"valid": not errors, "errors": errors, "v4_1_errors": v41_errors,
            "failure_stage": failure_stage, "request": normalized,
            "normalizations_applied": aliases}


def repair_capsule_field(request: dict, field: str, value: object) -> dict:
    """Apply one explicit, metadata-only capsule-field repair to a request copy."""
    if request.get("local_repair_used") is True:
        raise ValueError("only one local metadata repair is permitted")
    if field not in CAPSULE_AXES or not isinstance(request.get("capsule"), dict):
        raise ValueError("repair must target one declared TaskCapsule axis")
    repaired = dict(request)
    repaired["capsule"] = dict(request["capsule"])
    repaired["capsule"][field] = value
    repaired["local_repair_used"] = True
    return repaired


def _json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                       default=lambda obj: obj.isoformat() if isinstance(obj, datetime) else str(obj)) + "\n").encode("utf-8")


def _safe_capsule(raw: object) -> dict | None:
    """Keep only declared TaskCapsule metadata when a capsule cannot be built."""
    if not isinstance(raw, dict):
        return None
    allowed = TaskCapsule.__dataclass_fields__
    capsule = {key: value for key, value in raw.items() if key in allowed and key != "as_of"}
    capsule["role"] = capsule.get("role") or "root"
    return capsule


def _append(path: Path, value: dict, *, durable: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Python text append uses the platform's append mode and avoids relying on
    # Windows' low-level os.write/O_APPEND behavior. Serialize each line before
    # opening, then flush it to disk before reporting success.
    line = _json(value).decode("utf-8")
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line)
        stream.flush()
        if durable:
            os.fsync(stream.fileno())


def _decision(d: Decision) -> dict:
    return {"kind": d.kind.value, "route": asdict(d.route) if d.route else None,
            "reason_codes": list(d.reason_codes), "base_capability": d.base_capability,
            "base_effort": d.base_effort, "floors": list(d.floors), "credits": list(d.credits),
            "adjustments": list(d.adjustments), "history_adjustment": d.history_adjustment,
            "economic_adjustment": d.economic_adjustment, "manual_override": d.manual_override}


def _valid_auto(d: Decision) -> bool:
    return (d.kind in (DecisionKind.CLARIFY, DecisionKind.ROUTE, DecisionKind.KEEP_LEASE,
                       DecisionKind.MANUAL_ESCALATION_REQUIRED, DecisionKind.RETURN_TO_ROOT)
            and (d.route is None if d.kind in (DecisionKind.CLARIFY,
                                               DecisionKind.MANUAL_ESCALATION_REQUIRED,
                                               DecisionKind.RETURN_TO_ROOT) else d.route in AUTO_ROUTES))


def _capsule(raw: dict, decision_at: datetime) -> TaskCapsule:
    values = dict(raw)
    if "as_of" in values or "v4_1_rule_status" in values or "v4_1_high" in values:
        raise ValueError("decision clock and V4.1 adjudication are controlled by the live bridge")
    if values.get("manual_override") is not None:
        raise ValueError("explicit manual model override bypasses automatic V5 routing")
    if values.get("lease") is not None:
        raise ValueError("lease continuation is handled by the existing root lease protocol")
    if (not is_supported_model_version(values.get("model_version"))
            or values.get("model_generation", "gpt-6") != "gpt-6"):
        raise ValueError("exact GPT-6 model version is required")
    return build_task_capsule(values, as_of=decision_at)


def _validate_v41(raw: object) -> list[dict]:
    """Validate the complete frozen V4.1 adjudication without routing it."""
    errors = []
    if not isinstance(raw, dict):
        return [{"field": "v4_1", "value": raw, "allowed": ["object"]}]
    status = raw.get("rule_status")
    if not isinstance(status, str) or status not in ("KNOWN", "DEFINE", "MISSING"):
        errors.append({"field": "v4_1.rule_status", "value": status,
                       "allowed": ["KNOWN", "DEFINE", "MISSING"]})
    if type(raw.get("high")) is not bool:
        errors.append({"field": "v4_1.high", "value": raw.get("high"),
                       "allowed": [True, False]})
    codes = raw.get("reason_codes")
    if not isinstance(codes, list) or not codes:
        errors.append({"field": "v4_1.reason_codes", "value": codes,
                       "reason": "non-empty list required"})
    else:
        for index, code in enumerate(codes):
            if not isinstance(code, str) or not code.strip():
                errors.append({"field": f"v4_1.reason_codes[{index}]", "value": code,
                               "reason": "non-empty string required"})
    return errors


def _v41(raw: dict) -> Decision:
    errors = _validate_v41(raw)
    if errors:
        raise ValueError(f"invalid V4.1 adjudication: {errors}")
    return route_v4_1(RuleStatus(raw["rule_status"]), raw["high"])


def _difference(old: Decision, new: Decision) -> dict:
    same = (old.kind, old.route) == (new.kind, new.route)
    axes = []
    if old.kind != new.kind:
        axes.append("decision_kind")
    if old.route and new.route:
        if old.route.family != new.route.family:
            axes.append("capability")
        if old.route.effort != new.route.effort:
            axes.append("effort")
    return {"classification": "SAME_ROUTE" if same else "DIVERGENT_ROUTE", "axes": axes}


def decide(request: dict, state_dir: str | Path = DEFAULT_STATE, *,
           now: datetime | None = None, registry: OutcomeRegistry | None = None) -> dict:
    """Route one root task. Only infrastructure exceptions permit V4.1 fallback."""
    state = Path(state_dir)
    decision_at = now or datetime.now(timezone.utc)
    if decision_at.tzinfo is None:
        raise ValueError("decision_at needs timezone")
    validation = validate_request(request)
    request = validation["request"]
    if validation.get("v4_1_errors"):
        raise ValueError(f"V4.1 preflight validation failed (V4_1_VALIDATION_FAILURE): {validation['v4_1_errors']}")
    old = _v41(request.get("v4_1", {}))
    raw_cap = request.get("capsule")
    if not isinstance(raw_cap, dict):
        raise ValueError("capsule object required")
    task_id = raw_cap.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task_id required even to record a decision")
    if raw_cap.get("manual_override") is not None:
        raise ValueError("explicit manual model override bypasses automatic V5 routing")
    if raw_cap.get("role", "root") != "root":
        raise ValueError("live automatic routing is root-only")
    used: list[OutcomeRecord] = []
    fallback_reason = None
    fallback_error = None
    fatal_error = False
    cap = None
    failure_stage = None
    try:
        if not validation["valid"]:
            failure_stage = "PREFLIGHT_VALIDATION"
            raise ValueError("capsule validation failed")
        failure_stage = "CAPSULE_CONSTRUCTION"
        cap = _capsule(raw_cap, decision_at)
        failure_stage = "REGISTRY_READ"
        records = (registry or OutcomeRegistry(state / "outcome_registry.jsonl")).read()
        used = [r for r in records if r.task_id != cap.task_id
                and r.verification_available_at is not None
                and r.verification_available_at < decision_at
                and r.valid_for(cap, decision_at)]
        failure_stage = "V5_ROUTING"
        live = route_v5(cap, used, now=decision_at)
        if not isinstance(live, Decision) or not _valid_auto(live):
            failure_stage = "DECISION_VALIDATION"
            raise ValueError("invalid V5 decision object")
    except Exception as exc:
        live = old
        used = []
        fallback_error = f"{type(exc).__name__}: {exc}"
        if failure_stage == "PREFLIGHT_VALIDATION":
            fallback_reason = ("INVALID_CAPSULE_AFTER_REPAIR" if request.get("local_repair_used") is True
                               else "INVALID_CAPSULE")
        elif failure_stage == "REGISTRY_READ":
            fallback_reason = "REGISTRY_READ_FAILURE"
        else:
            fallback_reason = "ROUTER_INTERNAL_ERROR"
            fatal_error = True
            live = None
    diff = _difference(old, live) if live is not None else None
    entry = {"schema_version": "live_v5_decision_v1", "task_id": task_id,
             "decision_at": decision_at.isoformat(),
             "capsule": asdict(cap) if cap else _safe_capsule(raw_cap),
             "v5_live_decision": _decision(live) if live is not None else None,
             "v4_1_shadow_decision": _decision(old),
             "difference": diff, "history_records_used": [
                 {"task_id": r.task_id, "verification_available_at": r.verification_available_at.isoformat(),
                  "evidence_ref": r.verification_evidence_ref} for r in used],
             "fallback_used": fallback_reason is not None and not fatal_error,
             "fallback_reason": fallback_reason if not fatal_error else None,
             "fallback_error": fallback_error,
             "reason_code": fallback_reason,
             "request_validation_status": ("valid" if validation["valid"] else
                                            "invalid_after_repair" if request.get("local_repair_used") is True
                                            else "invalid"),
             "validation_errors": validation["errors"],
             "normalizations_applied": validation["normalizations_applied"],
             "local_repair_used": bool(request.get("local_repair_used", False)),
             "failure_stage": failure_stage if fallback_reason is not None else None,
             "error_code": fallback_reason if fatal_error else None,
             "router_version": ROUTER_VERSION, "model_generation": cap.model_generation if cap else raw_cap.get("model_generation"),
             "model_version": cap.model_version if cap else raw_cap.get("model_version"),
             "input_snapshot_ref": request.get("input_snapshot_ref")}
    try:
        _append(state / "live_decisions.jsonl", entry)
    except PermissionError as exc:
        # A sandbox/access denial is not a routing failure and must never
        # silently dispatch the frozen V4.1 route.  The caller may retry this
        # command with explicitly authorized live-state access.
        error = f"{type(exc).__name__}: {exc}"
        return {"task_id": task_id, "decision_at": decision_at.isoformat(),
                "v5_live_decision": None, "v4_1_shadow_decision": _decision(old),
                "difference": None, "history_records_used": 0,
                "fallback_used": False, "fallback_reason": None,
                "request_validation_status": entry["request_validation_status"],
                "normalizations_applied": entry["normalizations_applied"],
                "local_repair_used": entry["local_repair_used"],
                "failure_stage": "TELEMETRY_WRITE",
                "telemetry": str(state / "live_decisions.jsonl"),
                "telemetry_recorded": False, "telemetry_error": error,
                "routing_state_unwritable": True,
                "error_code": "ROUTING_STATE_UNWRITABLE",
                "dispatch_route": None}
    except OSError as exc:
        # Non-permission I/O failures retain the established V4.1 fallback.
        error = f"{type(exc).__name__}: {exc}"
        try:
            return fallback(task_id, request["v4_1"]["rule_status"],
                            request["v4_1"]["high"], "ROUTING_STATE_UNWRITABLE",
                            state, now=decision_at) | {
                                "telemetry_recorded": True,
                                "telemetry_error": error,
                                "failure_stage": "TELEMETRY_WRITE"}
        except PermissionError as fallback_exc:
            fallback_error = f"{type(fallback_exc).__name__}: {fallback_exc}"
            return {"task_id": task_id, "decision_at": decision_at.isoformat(),
                    "v5_live_decision": None, "v4_1_shadow_decision": _decision(old),
                    "difference": None, "history_records_used": 0,
                    "fallback_used": False, "fallback_reason": None,
                    "request_validation_status": entry["request_validation_status"],
                    "normalizations_applied": entry["normalizations_applied"],
                    "local_repair_used": entry["local_repair_used"],
                    "failure_stage": "TELEMETRY_WRITE",
                    "telemetry": str(state / "live_decisions.jsonl"),
                    "telemetry_recorded": False, "telemetry_error": fallback_error,
                    "routing_state_unwritable": True,
                    "error_code": "ROUTING_STATE_UNWRITABLE",
                    "dispatch_route": None}
        except OSError:
            return {"task_id": task_id, "decision_at": decision_at.isoformat(),
                    "v5_live_decision": _decision(old), "v4_1_shadow_decision": _decision(old),
                    "difference": _difference(old, old), "history_records_used": 0,
                    "fallback_used": True, "fallback_reason": "ROUTING_STATE_UNWRITABLE",
                    "request_validation_status": entry["request_validation_status"],
                    "normalizations_applied": entry["normalizations_applied"],
                    "local_repair_used": entry["local_repair_used"],
                    "failure_stage": "TELEMETRY_WRITE", "telemetry": str(state / "live_decisions.jsonl"),
                    "telemetry_recorded": False, "telemetry_error": error,
                    "unlogged_infrastructure_failure": True}
    telemetry_error = None
    return {"task_id": entry["task_id"], "decision_at": entry["decision_at"],
            "v5_live_decision": entry["v5_live_decision"], "v4_1_shadow_decision": entry["v4_1_shadow_decision"],
            "difference": diff, "history_records_used": len(used),
            "fallback_used": entry["fallback_used"], "fallback_reason": entry["fallback_reason"],
            "fallback_error": fallback_error,
            "reason_code": entry.get("reason_code"),
            "error_code": entry.get("error_code"),
            "dispatch_route": (entry["v5_live_decision"].get("route")
                               if entry["v5_live_decision"] else None),
            "request_validation_status": entry["request_validation_status"],
            "normalizations_applied": entry["normalizations_applied"],
            "local_repair_used": entry["local_repair_used"],
            "failure_stage": entry["failure_stage"],
            "telemetry": str(state / "live_decisions.jsonl"),
            "telemetry_recorded": telemetry_error is None,
            "telemetry_error": telemetry_error}


def fallback(task_id: str, v4_status: str, v4_high: bool, reason: str,
             state_dir: str | Path = DEFAULT_STATE, *, now: datetime | None = None,
             diagnostics: dict | None = None) -> dict:
    """Record a V4.1 infrastructure fallback when the normal bridge cannot produce a verdict."""
    if not task_id or not task_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError("simple task_id required")
    if reason not in FALLBACK_REASONS or type(v4_high) is not bool:
        raise ValueError("bounded infrastructure reason and explicit V4.1 high required")
    old = route_v4_1(RuleStatus(v4_status), v4_high)
    decision_at = now or datetime.now(timezone.utc)
    capsule = None
    validation_status = "not_validated"
    validation_errors = []
    normalizations = []
    local_repair_used = False
    input_snapshot_ref = None
    model_version = None
    failure_stage = {
        "INVALID_CAPSULE": "PREFLIGHT_VALIDATION",
        "INVALID_CAPSULE_AFTER_REPAIR": "PREFLIGHT_VALIDATION",
        "V4_1_VALIDATION_FAILURE": "V4_1_VALIDATION",
        "REGISTRY_READ_FAILURE": "REGISTRY_READ",
        "ROUTING_STATE_UNREADABLE": "REGISTRY_READ",
        "ROUTING_STATE_UNWRITABLE": "TELEMETRY_WRITE",
        "BRIDGE_INVOCATION_FAILED": "BRIDGE_INVOCATION",
        "ROUTER_INTERNAL_ERROR": "ROUTER_EXECUTION",
        "CONFIGURATION_ERROR": "CONFIGURATION",
    }[reason]
    if diagnostics is not None:
        checked = validate_request(diagnostics)
        normalized = checked["request"]
        if not isinstance(normalized, dict):
            raise ValueError("fallback diagnostics must be a request object")
        raw_capsule = normalized.get("capsule")
        if isinstance(raw_capsule, dict):
            # Task metadata only; raw task prompts and attachments are never copied.
            capsule = _safe_capsule(raw_capsule)
            model_version = raw_capsule.get("model_version")
        if (isinstance(normalized.get("capsule"), dict)
                and normalized["capsule"].get("task_id") not in (None, task_id)):
            raise ValueError("fallback task_id differs from diagnostic capsule")
        if isinstance(normalized, dict):
            input_snapshot_ref = normalized.get("input_snapshot_ref")
        validation_status = ("valid" if checked["valid"] else
                             "invalid_after_repair" if normalized.get("local_repair_used") is True
                             else "invalid")
        validation_errors = checked["errors"]
        normalizations = checked["normalizations_applied"]
        local_repair_used = bool(normalized.get("local_repair_used", False))
        if reason in ("INVALID_CAPSULE", "INVALID_CAPSULE_AFTER_REPAIR") and checked["valid"]:
            raise ValueError("invalid-capsule fallback requires an invalid diagnostic request")
        if reason == "INVALID_CAPSULE_AFTER_REPAIR" and not local_repair_used:
            raise ValueError("post-repair fallback requires local_repair_used=true")
        v41 = normalized.get("v4_1")
        if (not checked["v4_1_errors"] and isinstance(v41, dict)
                and (v41.get("rule_status") != v4_status or v41.get("high") is not v4_high)):
            raise ValueError("fallback adjudication differs from diagnostic request")
    entry = {"schema_version": "live_v5_decision_v1", "task_id": task_id,
             "decision_at": decision_at.isoformat(), "capsule": capsule,
             "v5_live_decision": _decision(old), "v4_1_shadow_decision": _decision(old),
             "difference": {"classification": "SAME_ROUTE", "axes": []},
             "history_records_used": [], "fallback_used": True, "fallback_reason": reason,
             "request_validation_status": validation_status, "validation_errors": validation_errors,
             "normalizations_applied": normalizations, "local_repair_used": local_repair_used,
             "failure_stage": failure_stage, "reason_code": reason, "fallback_error": None,
             "router_version": ROUTER_VERSION, "model_generation": "gpt-6",
             "model_version": model_version, "input_snapshot_ref": input_snapshot_ref}
    state = Path(state_dir)
    _append(state / "live_decisions.jsonl", entry)
    return {"task_id": task_id, "decision_at": entry["decision_at"],
            "v5_live_decision": entry["v5_live_decision"], "v4_1_shadow_decision": entry["v4_1_shadow_decision"],
            "difference": entry["difference"], "history_records_used": 0,
            "fallback_used": True, "fallback_reason": reason,
            "reason_code": reason, "failure_stage": failure_stage,
            "request_validation_status": validation_status,
            "validation_errors": validation_errors,
            "normalizations_applied": normalizations,
            "local_repair_used": local_repair_used,
            "telemetry": str(state / "live_decisions.jsonl")}


def observe(request: dict, state_dir: str | Path = DEFAULT_STATE, *,
            now: datetime | None = None) -> dict:
    """Append a verified live result; evidence availability is stamped at observation."""
    state = Path(state_dir)
    log = state / "live_decisions.jsonl"
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    matches = [row for row in entries if row["task_id"] == request.get("task_id")
               and row.get("router_version") == ROUTER_VERSION]
    if len(matches) != 1:
        raise ValueError("exactly one live decision required for task_id")
    entry = matches[0]
    if (entry["fallback_used"] or not entry.get("v5_live_decision")
            or entry["v5_live_decision"]["kind"] != "ROUTE"):
        raise ValueError("only executed V5 route decisions enter live V5 history")
    if request.get("final_outcome") not in ("verified_success", "verified_failure"):
        raise ValueError("verified success or failure required")
    evidence_file = Path(request["verification_evidence_file"]).resolve()
    if not evidence_file.is_file() or not request.get("verification_evidence_source_ref"):
        raise ValueError("existing verification evidence and source reference required")
    for key in ("retry_count", "rework_count"):
        if type(request.get(key)) is not int or request[key] < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    for key in ("manual_override", "escalation"):
        if type(request.get(key)) is not bool:
            raise ValueError(f"{key} must be explicit boolean")
    from .core import Failure
    available_at = now or datetime.now(timezone.utc)
    decision_at = datetime.fromisoformat(entry["decision_at"])
    if available_at <= decision_at:
        raise ValueError("verified evidence must be available after decision")
    c = entry["capsule"]
    expected = entry["v5_live_decision"]["route"]
    if not expected or Route(**expected) not in AUTO_ROUTES:
        raise ValueError("non-automatic route cannot enter live V5 history")
    if (request.get("actual_model") != expected["family"]
            or request.get("actual_effort") != expected["effort"]):
        raise ValueError("actual route drift cannot enter comparable live V5 history")
    actual = Route(request["actual_model"], request["actual_effort"])
    if request.get("model_version") != c["model_version"]:
        raise ValueError("observed model version differs from frozen decision version")
    if request.get("model_generation", c["model_generation"]) != c["model_generation"]:
        raise ValueError("observed model generation differs from frozen decision generation")
    evidence_hash = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
    result = OutcomeRecord(
        task_id=c["task_id"], task_family=c["task_family"], phase=c["phase"],
        specification_completeness=c["spec_completeness"], scope=c["scope"],
        uncertainty=c["uncertainty"], failcost=c["failcost"],
        verification_strength=c["verification"], mutation=c["mutation"],
        volume=c["volume"], dependencies=c["dependencies"],
        repository_familiarity=c["repo_familiarity"], history_support=c["history_support"],
        initial_route=actual, actual_model=actual.family.value, actual_effort=actual.effort.value,
        manual_override=request["manual_override"], escalation=request["escalation"],
        retry_count=request["retry_count"], rework_count=request["rework_count"],
        deterministic_verification_result=request.get("verification_result", "verified evidence inspected"),
        final_outcome=request["final_outcome"],
        final_successful_route=actual if request["final_outcome"] == "verified_success" else None,
        hindsight_cheapest_verified_success=None, router_version=ROUTER_VERSION,
        model_generation=c["model_generation"], model_version=c["model_version"],
        timestamp=decision_at, verification_available_at=available_at,
        evidence_kind="REAL_VERIFIED", verification_evidence_ref=f"sha256:{evidence_hash}",
        tokens_if_observable=request.get("tokens_if_observable"),
        cost_if_observable=request.get("cost_if_observable"),
        elapsed_time_if_observable=request.get("elapsed_time_if_observable"),
        failure_type=Failure("capability" if request["final_outcome"] == "verified_failure"
                             and request.get("failure_type") in (None, "none")
                             else request.get("failure_type") or "none"),
        input_snapshot_ref=None,
        notes=f"Evidence source: {request['verification_evidence_source_ref']}; input: {entry.get('input_snapshot_ref')}")
    registry = OutcomeRegistry(state / "outcome_registry.jsonl")
    if any(r.task_id == result.task_id and r.router_version == ROUTER_VERSION for r in registry.read()):
        raise ValueError("live V5 outcome already recorded for task_id")
    registry.append(result)
    return {"task_id": result.task_id, "verification_available_at": available_at.isoformat(),
            "verification_evidence_ref": result.verification_evidence_ref, "recorded": True}



def audit(state_dir: str | Path = DEFAULT_STATE, *, checkpoint: int) -> dict:
    if checkpoint not in (5, 15, 30):
        raise ValueError("checkpoint must be 5, 15, or 30")
    state = Path(state_dir)
    log = state / "live_decisions.jsonl"
    entries = [e for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
               if (e := json.loads(line)).get("router_version") == ROUTER_VERSION] if log.exists() else []
    records = OutcomeRegistry(state / "outcome_registry.jsonl").read()
    verified = [r for r in records if r.router_version == ROUTER_VERSION and r.evidence_kind == "REAL_VERIFIED"]
    counts = {"verified_live_v5_tasks": len({r.task_id for r in verified}),
              "decision_count": len(entries), "fallback_events": sum(bool(e["fallback_used"]) for e in entries),
              "router_internal_errors": sum(e.get("error_code") == "ROUTER_INTERNAL_ERROR" for e in entries),
              "capsule_construction_errors": sum(
                  e.get("fallback_reason") == "INVALID_CAPSULE" or
                  e.get("fallback_reason") == "INVALID_CAPSULE_AFTER_REPAIR" for e in entries),
              "divergent_decisions": sum(bool(e.get("difference") and
                                                  e["difference"]["classification"] == "DIVERGENT_ROUTE")
                                          for e in entries),
              "history_adjustments": sum(bool(e.get("v5_live_decision") and
                                                  e["v5_live_decision"]["history_adjustment"] != "none")
                                          for e in entries),
              "verified_successes": sum(r.final_outcome == "verified_success" for r in verified),
              "retries": sum(r.retry_count for r in verified), "rework": sum(r.rework_count for r in verified),
              "escalations": sum(r.escalation for r in verified)}
    flags = []
    if any(e.get("v5_live_decision") and e["v5_live_decision"].get("route")
           and e["v5_live_decision"]["route"]["family"] == "gpt-6-astra" for e in entries):
        flags.append("AUTOMATIC_ASTRA_RECOMMEND_ROLLBACK")
    if any(e.get("v5_live_decision") and e["v5_live_decision"]["kind"] == "ROUTE" and
           (not e["v5_live_decision"]["route"] or
            e["v5_live_decision"]["route"]["family"] not in ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol") or
            e["v5_live_decision"]["route"]["effort"] not in ("low", "medium", "high", "xhigh")) for e in entries):
        flags.append("INVALID_AUTOMATIC_ROUTE_RECOMMEND_ROLLBACK")
    if any(e["capsule"] and (e["capsule"].get("role") or "root") != "root" for e in entries):
        flags.append("ROOT_WORKER_INVARIANT_VIOLATION_RECOMMEND_ROLLBACK")
    if any(any(h["task_id"] == e["task_id"] or
                   datetime.fromisoformat(h["verification_available_at"]) >= datetime.fromisoformat(e["decision_at"])
                   for h in e["history_records_used"]) for e in entries):
        flags.append("HISTORY_LEAKAGE_RECOMMEND_ROLLBACK")
    if counts["fallback_events"] >= 2:
        flags.append("REPEATED_ROUTER_EXCEPTION_REVIEW_AND_CONSIDER_ROLLBACK")
    if counts["verified_live_v5_tasks"] < checkpoint:
        flags.append("CHECKPOINT_NOT_YET_REACHED")
    report = {"checkpoint": checkpoint, "counts": counts, "flags": flags,
              "promotion_authorized": False, "calibration_performed": False}
    if checkpoint >= 15:
        report["early_evidence"] = {
            "route_distribution": dict(Counter(
            f"{getattr(r.initial_route.family, 'value', r.initial_route.family)}/{getattr(r.initial_route.effort, 'value', r.initial_route.effort)}" for r in verified)),
            "divergence_axes": dict(Counter(
                axis for e in entries if e.get("difference") for axis in e["difference"]["axes"])),
            "history_adjustments_by_type": dict(Counter(
                e["v5_live_decision"]["history_adjustment"] for e in entries
                if e.get("v5_live_decision"))),
            "verified_success_rate": counts["verified_successes"] / len(verified) if verified else None,
            "observed_cost_total": sum(r.cost_if_observable for r in verified if r.cost_if_observable is not None),
            "observed_cost_records": sum(r.cost_if_observable is not None for r in verified),
            "coverage": {field: dict(Counter(e["capsule"][field] for e in entries if e["capsule"]))
                         for field in ("rule_status", "scope", "uncertainty", "failcost", "verification",
                                       "mutation", "volume", "dependencies", "spec_completeness",
                                       "repo_familiarity")},
        }
    if checkpoint == 30 and counts["verified_live_v5_tasks"] >= 30:
        capsules = []
        unbuildable_capsules = 0
        for e in entries:
            if not e["capsule"]:
                continue
            c = dict(e["capsule"])
            c.pop("as_of", None)
            c["v4_1_rule_status"] = e["v4_1_shadow_decision"]["kind"] == "CLARIFY" and "MISSING" or e["v4_1_shadow_decision"]["route"] and ("DEFINE" if e["v4_1_shadow_decision"]["route"]["family"] == "gpt-6.2-sol" else "KNOWN")
            c["v4_1_high"] = bool(e["v4_1_shadow_decision"]["route"] and e["v4_1_shadow_decision"]["route"]["effort"] == "high")
            if c.get("safety_floor"):
                c["safety_floor"] = Route(**c["safety_floor"])
            try:
                capsules.append(build_task_capsule(c, as_of=datetime.fromisoformat(e["decision_at"])))
            except (TypeError, ValueError):
                unbuildable_capsules += 1
        rows = Arena().replay(capsules, records)
        report["arena"] = {"metrics": metrics(rows), "pareto": pareto_frontier(metrics(rows)),
                           "promotion_views": {k: asdict(v) for k, v in evaluate_promotion_views(rows, records).items()},
                           "unbuildable_capsules_excluded": unbuildable_capsules}
    return report


def main(argv: list[str] | None = None) -> int:
    if argv and argv[:1] == ["log"]:
        from .lightweight import main as log_main
        return log_main(argv[1:])
    parser = argparse.ArgumentParser(description="V5 prequential live router bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("log", help="best-effort direct-routing telemetry")
    p.add_argument("--task-id", required=True)
    p.add_argument("--family", required=True, choices=("gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol"))
    p.add_argument("--effort", required=True, choices=("low", "medium", "high", "xhigh"))
    p.add_argument("--mode", required=True, choices=("root_direct", "worker", "strong_worker"))
    p.add_argument("--reason", required=True)
    p.add_argument("--phase", default="implementation", choices=("discovery", "design", "implementation", "verification", "cleanup"))
    p.add_argument("--outcome", choices=("verified_success", "verified_failure"))
    p.add_argument("--rework", type=int, default=0)
    p.add_argument("--retries", type=int, default=0)
    p.add_argument("--failure-type", default="none", choices=("none", "operational", "capability"))
    p.add_argument("--evidence-file")
    p.add_argument("--state-dir", default=str(DEFAULT_STATE))
    for name in ("decide", "validate", "observe"):
        p = sub.add_parser(name)
        p.add_argument("request_json")
        p.add_argument("--state-dir", default=str(DEFAULT_STATE))
    p = sub.add_parser("audit")
    p.add_argument("checkpoint", type=int, choices=(5, 15, 30))
    p.add_argument("--state-dir", default=str(DEFAULT_STATE))
    p = sub.add_parser("fallback")
    p.add_argument("task_id")
    p.add_argument("v4_status", choices=("KNOWN", "DEFINE", "MISSING"))
    p.add_argument("v4_high", choices=("true", "false"))
    p.add_argument("reason", choices=sorted(FALLBACK_REASONS))
    p.add_argument("--state-dir", default=str(DEFAULT_STATE))
    p.add_argument("--request-json", help="original capsule request for fallback diagnostics")
    args = parser.parse_args(argv)
    exit_code = 0
    if args.command == "log":
        result = log_task(**{key: value for key, value in vars(args).items() if key != "command"})
    elif args.command == "audit":
        result = audit(args.state_dir, checkpoint=args.checkpoint)
    elif args.command == "fallback":
        diagnostics = (json.loads(Path(args.request_json).read_text(encoding="utf-8"))
                       if args.request_json else None)
        result = fallback(args.task_id, args.v4_status, args.v4_high == "true",
                          args.reason, args.state_dir, diagnostics=diagnostics)
    else:
        request = json.loads(Path(args.request_json).read_text(encoding="utf-8"))
        if args.command == "decide":
            result = decide(request, args.state_dir)
        elif args.command == "validate":
            checked = validate_request(request)
            result = {key: value for key, value in checked.items() if key != "request"}
            result["normalized_request"] = checked["request"]
            exit_code = 0 if checked["valid"] else 2
        else:
            result = observe(request, args.state_dir)
    print(_json(result).decode("utf-8"), end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
