"""Constant-size, best-effort direct-routing telemetry; no routing imports or scans."""

import json
import os
from datetime import datetime, timezone

ROUTER_VERSION = "v5_prequential_history_sol61_live"
DEFAULT_STATE = os.path.join(os.path.expanduser("~"), ".codex", "router_v5_live")
FAMILIES = ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol")
EFFORTS = ("low", "medium", "high", "xhigh")
PHASES = ("discovery", "design", "implementation", "verification", "cleanup")
SCOPES = ("local", "multi-component", "cross-system")
LEVELS = ("low", "medium", "high")
VERIFICATIONS = ("strong", "partial", "weak")
MUTATIONS = ("read-only", "local-mutation", "stateful-or-high-blast-radius")
VOLUMES = ("bounded", "large")
DEPENDENCIES = ("shallow", "coupled")


def _append(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    # One append write, without copying the existing file or a durability barrier.
    with open(path, "ab", buffering=0) as stream:
        stream.write(line)


def log_task(*, task_id, family, effort, mode, reason, phase="implementation",
             outcome=None, rework=0, retries=0, failure_type="none", evidence_file=None,
             scope=None, uncertainty=None, failcost=None, verification=None,
             mutation=None, volume=None, dependencies=None, spec_completeness=None,
             repo_familiarity=None, profile_complete=False,
             state_dir=DEFAULT_STATE, now=None):
    result = {"task_id": task_id, "recorded": False, "outcome_recorded": False}
    try:
        if family not in FAMILIES or effort not in EFFORTS or not task_id or not reason:
            raise ValueError("automatic route, task identifier and reason required")
        if type(rework) is not int or type(retries) is not int or min(rework, retries) < 0:
            raise ValueError("nonnegative integer counts required")
        expected = ("root_direct" if family == "gpt-6-luna" and effort in EFFORTS[:2]
                    else "worker" if family == "gpt-6-luna"
                    else "worker" if family == "gpt-6.1-terra" and effort in EFFORTS[:2]
                    else "strong_worker")
        allowed_modes = (("root_direct",) if family == "gpt-6-luna" and effort in EFFORTS[:2]
                         else ("worker",) if family == "gpt-6-luna"
                         else ("worker", "strong_worker") if family == "gpt-6.1-terra"
                         else ("strong_worker",))
        if mode not in allowed_modes:
            raise ValueError(f"route {family}/{effort} requires spawned mode in {allowed_modes}, not mode={mode}")
        if phase not in PHASES or outcome not in (None, "verified_success", "verified_failure"):
            raise ValueError("invalid phase or outcome")
        if failure_type not in ("none", "operational", "capability"):
            raise ValueError("invalid failure type")
        has_structural_axes = any(v is not None for v in (
            scope, uncertainty, failcost, verification, mutation, volume, dependencies, spec_completeness
        )) or bool(profile_complete)
        eff_scope = (scope or "local").replace("_", "-")
        eff_uncertainty = uncertainty or "medium"
        eff_failcost = failcost or "medium"
        eff_verification = verification or "partial"
        eff_mutation = (mutation or "read-only").replace("_", "-")
        eff_volume = volume or "bounded"
        eff_dependencies = dependencies or "shallow"
        eff_spec = spec_completeness or "medium"
        eff_familiarity = repo_familiarity or "medium"
        if (eff_scope not in SCOPES or eff_uncertainty not in LEVELS or eff_failcost not in LEVELS
                or eff_verification not in VERIFICATIONS or eff_mutation not in MUTATIONS
                or eff_volume not in VOLUMES or eff_dependencies not in DEPENDENCIES
                or eff_spec not in LEVELS or eff_familiarity not in LEVELS):
            raise ValueError("invalid structural profile axis value")
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        route = {"family": family, "effort": effort}
        state = os.fspath(state_dir)
        decision = {"kind": "ROUTE", "route": route, "reason_codes": [reason],
                    "base_capability": family, "base_effort": effort,
                    "floors": [], "credits": [], "adjustments": [], "history_adjustment": "none",
                    "economic_adjustment": "none", "manual_override": False}
        entry = {"schema_version": "live_v51_lightweight_v1", "policy_version": "5.1",
                 "router_version": ROUTER_VERSION, "task_id": task_id, "decision_at": stamp,
                 "capsule": None, "phase": phase, "execution_mode": mode,
                 "v5_live_decision": decision, "v4_1_shadow_decision": None,
                 "difference": None, "history_records_used": [], "fallback_used": False,
                 "fallback_reason": None, "error_code": None}
        _append(os.path.join(state, "live_decisions.jsonl"), entry)
        result["recorded"] = True
        if outcome:
            evidence_ref = None
            if evidence_file:
                evidence = os.path.abspath(evidence_file)
                if not os.path.isfile(evidence):
                    raise ValueError("evidence file must exist")
                # Retain the source pointer without reading an unbounded evidence file.
                evidence_ref = str(evidence)
            is_complete = bool(has_structural_axes and evidence_ref)
            record = {
                "task_id": task_id, "task_family": task_id, "phase": phase,
                "specification_completeness": eff_spec, "scope": eff_scope, "uncertainty": eff_uncertainty,
                "failcost": eff_failcost, "verification_strength": eff_verification, "mutation": eff_mutation,
                "volume": eff_volume, "dependencies": eff_dependencies, "repository_familiarity": eff_familiarity,
                "history_support": "none", "initial_route": route, "actual_model": family,
                "actual_effort": effort, "manual_override": False, "escalation": False,
                "retry_count": retries, "rework_count": rework,
                "deterministic_verification_result": "evidence file supplied" if evidence_ref else "caller reported",
                "final_outcome": outcome,
                "final_successful_route": route if outcome == "verified_success" else None,
                "hindsight_cheapest_verified_success": None, "router_version": ROUTER_VERSION,
                "model_generation": "gpt-6", "model_version": family, "timestamp": stamp,
                "verification_available_at": stamp if evidence_ref else None,
                "evidence_kind": "REAL_VERIFIED" if evidence_ref else "REPORTED_OUTCOME",
                "verification_evidence_ref": evidence_ref,
                "failure_type": "capability" if outcome == "verified_failure" and failure_type == "none" else failure_type,
                "structural_profile_complete": is_complete,
                "notes": ("Lightweight telemetry with structural profile; eligible for prequential history."
                          if is_complete else
                          "Lightweight telemetry; structural placeholders excluded from routing history.")}
            _append(os.path.join(state, "outcome_registry.jsonl"), record)
            result.update(outcome_recorded=True, evidence_kind=record["evidence_kind"],
                          structural_profile_complete=is_complete)
    except Exception as exc:
        result["telemetry_error"] = f"{type(exc).__name__}: {exc}"
    return result


def main(argv=None):
    # This deliberately small flag parser avoids argparse's startup imports.
    import sys
    argv = sys.argv[1:] if argv is None else argv
    keys = {"task-id", "family", "effort", "mode", "reason", "phase", "outcome",
            "rework", "retries", "failure-type", "evidence-file", "state-dir",
            "scope", "uncertainty", "failcost", "verification", "mutation",
            "volume", "dependencies", "spec-completeness", "repo-familiarity",
            "profile-complete"}
    try:
        if argv in (["--help"], ["-h"]):
            print("log --task-id ID --family MODEL --effort EFFORT --mode MODE --reason CODE "
                  "[--phase PHASE] [--outcome OUTCOME] [--rework N] [--retries N] "
                  "[--failure-type TYPE] [--evidence-file PATH] [--scope SCOPE] "
                  "[--uncertainty LEVEL] [--failcost LEVEL] [--mutation MUTATION] "
                  "[--profile-complete true|false] [--state-dir PATH]")
            return 0
        args = {}
        index = 0
        while index < len(argv):
            token = argv[index]
            flag, equal, value = token.partition("=")
            if not flag.startswith("--") or flag[2:] not in keys:
                raise ValueError(f"unknown telemetry argument: {flag}")
            if not equal:
                index += 1
                if index >= len(argv) or argv[index].startswith("--"):
                    raise ValueError(f"missing value for {flag}")
                value = argv[index]
            args[flag[2:].replace("-", "_")] = value
            index += 1
        for key in ("rework", "retries"):
            if key in args:
                args[key] = int(args[key])
        if "profile_complete" in args:
            args["profile_complete"] = str(args["profile_complete"]).lower() in ("true", "1", "yes")
        result = log_task(**args)
    except Exception as exc:
        result = {"recorded": False, "outcome_recorded": False,
                  "telemetry_error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0
