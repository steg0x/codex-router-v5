"""Prospective offline shadow capture. It never dispatches or executes a task."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .arena import Arena, evaluate_promotion_views
from .core import (Decision, Failure, Route, RuleStatus, TaskCapsule,
                   build_task_capsule, is_supported_model_version, route_v4_1, route_v5)
from .registry import OutcomeRecord, OutcomeRegistry

TASK_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{2,79}$")
FIELDS = ("phase", "rule_status", "scope", "uncertainty", "failcost", "verification",
          "mutation", "volume", "dependencies", "spec_completeness", "repo_familiarity")


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", check=False)
    if result.returncode:
        raise ValueError(f"repository snapshot command failed: {' '.join(args)}")
    return result.stdout.strip()


def _repo_state(base: Path, request: dict) -> dict | None:
    if not request.get("repository_path"):
        return None
    path = _resolve(base, request["repository_path"])
    if not path.is_dir():
        raise ValueError("repository_path must be an existing directory")
    try:
        top = Path(_git(path, "rev-parse", "--show-toplevel")).resolve()
    except ValueError:
        return {"path": str(path), "version_control": "unversioned",
                "head": None, "tree": None, "clean": False,
                "state": "SOURCE_TREE_NOT_REPRODUCIBLE"}
    head = _git(path, "rev-parse", "HEAD")
    tree = _git(path, "rev-parse", "HEAD^{tree}")
    status = _git(path, "status", "--porcelain", "--untracked-files=all")
    return {"path": str(top), "version_control": "git", "head": head, "tree": tree,
            "clean": not bool(status), "dirty_status_sha256": _hash(status.encode()) if status else None,
            "dirty_entry_count": len(status.splitlines()) if status else 0}


def _freeze_file(base: Path, value: str, objects: Path) -> dict:
    source = _resolve(base, value)
    if not source.is_file():
        raise ValueError(f"required snapshot file missing: {source}")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    sha = digest.hexdigest()
    target = objects / sha
    objects.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
            raise ValueError(f"snapshot object hash mismatch: {sha}")
    else:
        temp = objects / f".{sha}.tmp"
        if temp.exists():
            raise ValueError("snapshot object temporary file exists; inspect before retry")
        shutil.copyfile(source, temp)
        if hashlib.sha256(temp.read_bytes()).hexdigest() != sha:
            temp.unlink()
            raise ValueError("source changed while freezing; retry capture")
        temp.replace(target)
    return {"source_path": str(source), "sha256": sha, "bytes": target.stat().st_size,
            "object_ref": str(target.relative_to(objects.parent))}


def _route_dict(decision: Decision) -> dict:
    return {"kind": decision.kind.value,
            "family": decision.route.family.value if decision.route else None,
            "effort": decision.route.effort.value if decision.route else None,
            "reason_codes": list(decision.reason_codes), "floors": list(decision.floors),
            "credits": list(decision.credits), "adjustments": list(decision.adjustments),
            "history_adjustment": decision.history_adjustment,
            "economic_adjustment": decision.economic_adjustment}


def _difference(old: Decision, new: Decision) -> dict:
    if old.kind == new.kind and old.route == new.route:
        return {"classification": "SAME_ROUTE", "axes": [], "reason_codes": []}
    axes = []
    if old.kind != new.kind:
        axes.append("decision_kind")
    if old.route and new.route:
        if old.route.family != new.route.family:
            axes.append("capability")
        if old.route.effort != new.route.effort:
            axes.append("effort")
    return {"classification": "DIVERGENT_ROUTE", "axes": axes,
            "reason_codes": list(new.floors + new.credits + new.adjustments + new.reason_codes),
            "expected_cost_difference": None, "cost_basis": "UNOBSERVED_NO_PRICE_IMPUTATION"}


def _validate_request(request: dict) -> None:
    if not TASK_ID.fullmatch(request.get("task_id", "")):
        raise ValueError("task_id must be 3-80 simple path-safe characters")
    for key in ("task_family", "input_path", "capsule", "v4_1", "verification_rubric", "replay_assessment"):
        if not request.get(key):
            raise ValueError(f"missing required capture field: {key}")
    v41 = request["v4_1"]
    if v41.get("rule_status") not in ("KNOWN", "DEFINE", "MISSING") or type(v41.get("high")) is not bool:
        raise ValueError("V4.1 adjudication requires explicit rule_status and high boolean")
    if not v41.get("reason_codes") or not isinstance(v41["reason_codes"], list):
        raise ValueError("independent V4.1 reason_codes required")
    rubric = request["verification_rubric"]
    if not rubric.get("criteria") or not rubric.get("expected_completion_evidence"):
        raise ValueError("pre-execution verification criteria and expected evidence required")
    for item in rubric["criteria"]:
        if not all(item.get(key) for key in ("id", "pass_condition", "evidence_type")):
            raise ValueError("each rubric criterion needs id, pass_condition, evidence_type")
    assessment = request["replay_assessment"]
    for key in ("requires_repository", "requires_external_state", "source_state_complete", "safe_reversible",
                "side_effects_isolated", "identical_rubric", "coverage_useful"):
        if type(assessment.get(key)) is not bool:
            raise ValueError(f"replay_assessment.{key} must be explicit boolean")
    if request.get("repository_path") and not assessment["requires_repository"]:
        raise ValueError("a referenced repository must be marked required for replay")


def capture(request_path: str | Path, output_dir: str | Path) -> dict:
    """Freeze task inputs, compute shadow verdicts, and enqueue only metadata."""
    request_path = Path(request_path).resolve()
    base = request_path.parent
    request = json.loads(request_path.read_text(encoding="utf-8"))
    _validate_request(request)
    root = Path(output_dir).resolve()
    task_dir = root / "tasks" / request["task_id"]
    if task_dir.exists():
        raise ValueError("task_id already captured; immutable capture cannot be overwritten")
    decision_at = datetime.now(timezone.utc)
    cap_data = dict(request["capsule"])
    if any(key not in cap_data for key in FIELDS):
        raise ValueError("TaskCapsule fields incomplete")
    if not is_supported_model_version(cap_data.get("model_version")):
        raise ValueError("supported planned GPT-6 model_version required before execution")
    if any(key in cap_data for key in ("manual_override", "lease", "as_of", "failure")):
        raise ValueError("prospective new-task capture excludes manual overrides, leases and failure continuations")
    cap = build_task_capsule({
        **cap_data, "task_id": request["task_id"], "task_family": request["task_family"],
        "v4_1_rule_status": RuleStatus(request["v4_1"]["rule_status"]),
        "v4_1_high": request["v4_1"]["high"],
    }, as_of=decision_at)
    old = route_v4_1(cap.v4_1_rule_status, cap.v4_1_high)
    # Inspect source state before writing capture objects, which may themselves
    # live under the repository and would otherwise create a false dirty tree.
    repo = _repo_state(base, request)
    objects = root / "objects"
    input_ref = _freeze_file(base, request["input_path"], objects)
    attachment_refs = [_freeze_file(base, x, objects) for x in request.get("required_attached_files", [])]
    config_refs = [_freeze_file(base, x, objects) for x in request.get("configuration_files", [])]
    data_refs = [_freeze_file(base, x, objects) for x in request.get("local_data_files", [])]
    assessment = request["replay_assessment"]
    external_specs = request.get("external_state_refs", [])
    if not isinstance(external_specs, list):
        raise ValueError("external_state_refs must be a list")
    external = []
    for spec in external_specs:
        if not isinstance(spec, dict):
            raise ValueError("external state reference must be an object")
        if spec.get("cached_path") and spec.get("sha256"):
            frozen = _freeze_file(base, spec["cached_path"], objects)
            if frozen["sha256"] != spec["sha256"]:
                raise ValueError("external cached response hash differs from declared hash")
            external.append({"status": "FROZEN_HASH_MATCH", "cached_response": frozen,
                             "source_url": spec.get("source_url")})
        else:
            external.append({"status": "UNFROZEN_REFERENCE", "source_url": spec.get("source_url")})
    external_complete = bool(external) and all(x["status"] == "FROZEN_HASH_MATCH" for x in external)
    external_ok = external_complete if external else not assessment["requires_external_state"]
    output_inside_repo = bool(repo and root.is_relative_to(Path(repo["path"])))
    repo_stable = (repo == _repo_state(base, request)) if repo and not output_inside_repo else not output_inside_repo
    reproducible = (assessment["source_state_complete"]
                    and (not assessment["requires_repository"] or bool(repo and repo["clean"]))
                    and repo_stable
                    and external_ok)
    manifest = {
        "schema_version": "phase2_frozen_input_v1", "task_id": cap.task_id,
        "captured_at": decision_at.isoformat(), "input": input_ref,
        "attachments": attachment_refs, "repository": repo,
        "repository_snapshot_stable_during_capture": repo_stable,
        "capture_output_inside_repository": output_inside_repo,
        "configuration": config_refs, "local_data": data_refs,
        "external_state_refs": external, "reproducible_fixture": reproducible,
        "replay_assessment": assessment, "model_generation": cap.model_generation,
        "model_version": cap.model_version,
        "verification_rubric": request["verification_rubric"],
    }
    manifest_bytes = _canonical(manifest)
    manifest_digest = _hash(manifest_bytes)
    snapshot_ref = f"sha256:{manifest_digest}"
    no_history = route_v5(cap, (), now=decision_at)
    history_file = request.get("verified_history_file")
    prior = []
    if history_file:
        prior = [r for r in OutcomeRegistry(_resolve(base, history_file)).read()
                 if r.evidence_kind == "REAL_VERIFIED" and r.task_id != cap.task_id
                 and r.verification_available_at is not None
                 and r.verification_available_at < decision_at
                 and r.valid_for(cap, decision_at)]
    prequential = route_v5(cap, prior, now=decision_at)
    difference = _difference(old, prequential)
    pair_id = None
    paired_eligible = False
    if difference["classification"] == "DIVERGENT_ROUTE":
        pair_id = _hash(_canonical([cap.task_id, snapshot_ref, _route_dict(old),
                                    _route_dict(prequential)]))[:32]
        paired_eligible = (reproducible and old.route is not None and prequential.route is not None
                           and all(assessment[key] for key in ("safe_reversible", "side_effects_isolated",
                                                               "identical_rubric", "coverage_useful")))
    shadow = {
        "schema_version": "phase2_shadow_decision_v1", "task_id": cap.task_id,
        "decision_at": decision_at.isoformat(), "input_snapshot_ref": snapshot_ref,
        "capsule": {**cap_data, "task_id": cap.task_id, "task_family": cap.task_family,
                    "as_of": decision_at.isoformat(), "model_generation": cap.model_generation,
                    "model_version": cap.model_version},
        "v4_1_adjudication": request["v4_1"], "v4_1_live_route": _route_dict(old),
        "v5_no_history": _route_dict(no_history),
        "v5_prequential_history": _route_dict(prequential),
        "history_evidence_refs_used": [r.verification_evidence_ref for r in prior],
        "history_record_task_ids_used": [r.task_id for r in prior],
        "history_availability_times_used": [r.verification_available_at.isoformat() for r in prior],
        "difference": difference, "comparison_group_id": pair_id,
        "paired_replay_eligible": paired_eligible,
        "live_authority": "V4.1", "v5_execution_effect": "NONE",
    }
    queue_item = None
    if pair_id:
        queue_item = {
            "schema_version": "phase2_controlled_replay_queue_v1", "task_id": cap.task_id,
            "comparison_protocol": "CONTROLLED_PAIRED_REPLAY",
            "comparison_group_id": pair_id, "input_snapshot_ref": snapshot_ref,
            "v4_1_route": _route_dict(old), "v5_route": _route_dict(prequential),
            "divergence": difference, "verification_strength": cap.verification.value,
            "paired_replay_eligible": paired_eligible,
            "eligibility_checks": {"fixture_reproducible": reproducible,
                                   "source_state_complete": assessment["source_state_complete"],
                                   **{key: assessment[key] for key in ("safe_reversible", "side_effects_isolated",
                                                                          "identical_rubric", "coverage_useful")}},
            "status": "AWAIT_LIVE_V4_1_OUTCOME" if paired_eligible else "BLOCKED_FIXTURE_OR_SAFETY",
            "second_arm_execution": "NOT_AUTHORIZED_BY_CAPTURE",
        }
    task_dir.mkdir(parents=True, exist_ok=False)
    (task_dir / "frozen_input_manifest.json").write_bytes(manifest_bytes)
    (task_dir / "shadow_decision.json").write_bytes(_canonical(shadow) + b"\n")
    if queue_item:
        (task_dir / "controlled_replay_queue_item.json").write_bytes(_canonical(queue_item) + b"\n")
    with (root / "shadow_decisions.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(_canonical(shadow).decode() + "\n")
    if queue_item:
        with (root / "controlled_replay_queue.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(_canonical(queue_item).decode() + "\n")
    return {"task_id": cap.task_id, "input_snapshot_ref": snapshot_ref,
            "comparison": difference["classification"], "paired_replay_eligible": paired_eligible,
            "history_records_used": len(prior), "task_dir": str(task_dir),
            "live_authority": "V4.1", "executed": False}


def observe(observation_path: str | Path, output_dir: str | Path) -> dict:
    """Record externally completed execution after checking frozen evidence.

    This command never runs the live task or the queued second arm.
    """
    observation_path = Path(observation_path).resolve()
    request = json.loads(observation_path.read_text(encoding="utf-8"))
    root = Path(output_dir).resolve()
    task_id = request.get("task_id", "")
    task_dir = root / "tasks" / task_id
    if not TASK_ID.fullmatch(task_id) or not task_dir.is_dir():
        raise ValueError("task must have a pre-execution capture")
    arm = request.get("arm")
    if arm not in ("V4_1_LIVE", "V5_CONTROLLED_REPLAY"):
        raise ValueError("arm must identify live or controlled replay execution")
    target = task_dir / f"observation_{arm}.json"
    if target.exists():
        raise ValueError("arm already observed; immutable observation cannot be overwritten")
    shadow = json.loads((task_dir / "shadow_decision.json").read_text(encoding="utf-8"))
    manifest = json.loads((task_dir / "frozen_input_manifest.json").read_text(encoding="utf-8"))
    if arm == "V5_CONTROLLED_REPLAY":
        if not shadow["paired_replay_eligible"]:
            raise ValueError("second arm blocked by frozen fixture or safety assessment")
        if not (task_dir / "observation_V4_1_LIVE.json").exists():
            raise ValueError("record live V4.1 arm before any controlled replay observation")
    desired = (shadow["v4_1_live_route"] if arm == "V4_1_LIVE"
               else shadow["v5_prequential_history"])
    if desired["kind"] != "ROUTE":
        raise ValueError("this arm has no model execution route; record clarification separately")
    actual_model = request.get("actual_model")
    actual_effort = request.get("actual_effort")
    actual_version = request.get("model_version")
    if not actual_model or not actual_effort or not actual_version:
        raise ValueError("actual model, effort and version required")
    if (shadow["comparison_group_id"] is not None
            and (actual_model != desired["family"] or actual_effort != desired["effort"])):
        raise ValueError("controlled pair arm route drift; record separately, not as paired evidence")
    route = Route(actual_model, actual_effort)
    final = request.get("final_outcome")
    if final not in ("verified_success", "verified_failure"):
        raise ValueError("only explicitly verified success or failure can be recorded")
    evidence_path = request.get("verification_evidence_file")
    source_ref = request.get("verification_evidence_source_ref")
    if not evidence_path or not source_ref:
        raise ValueError("verification evidence file and original source reference required")
    evidence = _freeze_file(observation_path.parent, evidence_path, root / "objects")
    criteria = {item["id"] for item in manifest["verification_rubric"]["criteria"]}
    results = request.get("criterion_results")
    if not isinstance(results, dict) or set(results) != criteria or any(type(v) is not bool for v in results.values()):
        raise ValueError("criterion_results must cover preregistered rubric exactly")
    if final == "verified_success" and not all(results.values()):
        raise ValueError("verified_success requires every preregistered criterion to pass")
    if final == "verified_failure" and all(results.values()):
        raise ValueError("verified_failure requires a failed preregistered criterion")
    for key in ("retry_count", "rework_count"):
        if type(request.get(key)) is not int or request[key] < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    for key in ("manual_override", "escalation"):
        if type(request.get(key)) is not bool:
            raise ValueError(f"{key} must be explicit boolean")
    decision_at = datetime.fromisoformat(shadow["decision_at"])
    available_at = datetime.now(timezone.utc)
    c = shadow["capsule"]
    pair = shadow["difference"]["classification"] == "DIVERGENT_ROUTE"
    result = OutcomeRecord(
        task_id=task_id, task_family=c["task_family"], phase=c["phase"],
        specification_completeness=c["spec_completeness"], scope=c["scope"],
        uncertainty=c["uncertainty"], failcost=c["failcost"],
        verification_strength=c["verification"], mutation=c["mutation"],
        volume=c["volume"], dependencies=c["dependencies"],
        repository_familiarity=c["repo_familiarity"], history_support=c.get("history_support", "none"),
        initial_route=route, actual_model=actual_model, actual_effort=actual_effort,
        manual_override=request["manual_override"], escalation=request["escalation"],
        retry_count=request["retry_count"], rework_count=request["rework_count"],
        deterministic_verification_result=json.dumps(results, sort_keys=True),
        final_outcome=final, final_successful_route=route if final == "verified_success" else None,
        hindsight_cheapest_verified_success=None,
        router_version="v4_1_live" if arm == "V4_1_LIVE" else "v5_controlled_replay",
        model_generation=c["model_generation"], model_version=actual_version,
        timestamp=decision_at, verification_available_at=available_at,
        evidence_kind="REAL_VERIFIED", verification_evidence_ref=f"sha256:{evidence['sha256']}",
        tokens_if_observable=request.get("tokens_if_observable"),
        cost_if_observable=request.get("cost_if_observable"),
        elapsed_time_if_observable=request.get("elapsed_time_if_observable"),
        router_overhead_seconds=request.get("router_overhead_seconds"),
        failure_type=Failure(request.get("failure_type", "none")),
        notes=f"Prospective {arm}; evidence source {source_ref}; frozen fixture {shadow['input_snapshot_ref']}; route match: "
              f"{actual_model == desired['family'] and actual_effort == desired['effort']}",
        comparison_protocol="CONTROLLED_PAIRED_REPLAY" if pair else None,
        comparison_group_id=shadow["comparison_group_id"] if pair else None,
        input_snapshot_ref=shadow["input_snapshot_ref"] if pair else None,
    )
    envelope = {"schema_version": "phase2_observation_v1", "task_id": task_id,
                "arm": arm, "input_snapshot_ref": shadow["input_snapshot_ref"],
                "recorded_at": available_at.isoformat(), "evidence_object": evidence,
                "verification_evidence_source_ref": source_ref,
                "criterion_results": results,
                "actual_route_matches_shadow": actual_model == desired["family"] and actual_effort == desired["effort"],
                "outcome_record": result.to_dict()}
    registry = OutcomeRegistry(root / "outcome_registry.jsonl")
    if any(r.task_id == task_id and r.router_version == result.router_version for r in registry.read()):
        raise ValueError("duplicate task arm in prospective outcome registry")
    target.write_bytes(_canonical(envelope) + b"\n")
    try:
        registry.append(result)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {"task_id": task_id, "arm": arm, "evidence_kind": result.evidence_kind,
            "verified_at": available_at.isoformat(), "actual_route_matches_shadow": envelope["actual_route_matches_shadow"],
            "second_arm_executed_by_command": False}


def dashboard(output_dir: str | Path, *, phase1_corpus: str | Path | None = None) -> dict:
    root = Path(output_dir)
    log = root / "shadow_decisions.jsonl"
    rows = []
    if log.exists():
        with log.open(encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
    counts = {field: Counter(row["capsule"][field] for row in rows) for field in FIELDS}
    differences = Counter(row["difference"]["classification"] for row in rows)
    records = OutcomeRegistry(root / "outcome_registry.jsonl").read()
    capsules = []
    for row in rows:
        values = dict(row["capsule"])
        values["as_of"] = datetime.fromisoformat(values["as_of"])
        values["v4_1_rule_status"] = row["v4_1_adjudication"]["rule_status"]
        values["v4_1_high"] = row["v4_1_adjudication"]["high"]
        capsules.append(TaskCapsule(**values))
    views = evaluate_promotion_views(Arena().replay(capsules, records), records)
    view_summary = {name: {"candidate_strategy": result.candidate_strategy,
                           "status": result.status, "reason_codes": list(result.reason_codes),
                           "real_verified_task_count": result.real_verified_task_count}
                    for name, result in views.items()}
    observed_ids = {r.task_id for r in records if r.evidence_kind == "REAL_VERIFIED"}
    quality_ids = set()
    for row in rows:
        task_id = row["task_id"]
        task_records = [r for r in records if r.task_id == task_id and r.evidence_kind == "REAL_VERIFIED"]
        task_dir = root / "tasks" / task_id
        live_path = task_dir / "observation_V4_1_LIVE.json"
        if not live_path.exists():
            continue
        live = json.loads(live_path.read_text(encoding="utf-8"))
        if not any(r.router_version == "v4_1_live" and r.to_dict() == live["outcome_record"]
                   for r in task_records):
            continue
        if (live["input_snapshot_ref"] != row["input_snapshot_ref"]
                or not live["actual_route_matches_shadow"] or live["outcome_record"]["manual_override"]):
            continue
        if live["outcome_record"]["model_version"] != row["capsule"]["model_version"]:
            continue
        if row["difference"]["classification"] == "SAME_ROUTE":
            quality_ids.add(task_id)
            continue
        replay_path = task_dir / "observation_V5_CONTROLLED_REPLAY.json"
        if replay_path.exists():
            replay = json.loads(replay_path.read_text(encoding="utf-8"))
            if (any(r.router_version == "v5_controlled_replay" and r.to_dict() == replay["outcome_record"]
                    for r in task_records)
                    and replay["actual_route_matches_shadow"]
                    and not replay["outcome_record"]["manual_override"]
                    and replay["outcome_record"]["model_version"] == row["capsule"]["model_version"]
                    and replay["outcome_record"]["comparison_group_id"] == row["comparison_group_id"]
                    and replay["input_snapshot_ref"] == row["input_snapshot_ref"]):
                quality_ids.add(task_id)
    quality_counts = {field: Counter(row["capsule"][field] for row in rows
                                     if row["task_id"] in quality_ids) for field in FIELDS}
    phase1_count = 0
    if phase1_corpus is not None:
        with Path(phase1_corpus).open(encoding="utf-8") as stream:
            phase1_count = sum(bool(line.strip()) for line in stream)
    result = {"prospective_captured": len(rows), "verified_prospective_tasks": len(observed_ids),
              "promotion_quality_tasks": len(quality_ids), "target": 30,
              "remaining_to_target": max(0, 30 - len(quality_ids)),
              "same_route": differences["SAME_ROUTE"],
              "divergent_route": differences["DIVERGENT_ROUTE"],
              "divergence_rate": differences["DIVERGENT_ROUTE"] / len(rows) if rows else None,
              "paired_replay_eligible": sum(bool(row["paired_replay_eligible"]) for row in rows),
              "capture_coverage": {key: dict(value) for key, value in counts.items()},
              "promotion_quality_coverage": {key: dict(value) for key, value in quality_counts.items()},
              "phase1_classification_only_count": phase1_count,
              "promotion_comparisons": view_summary,
              "promotion_ready": False}
    root.mkdir(parents=True, exist_ok=True)
    (root / "coverage_dashboard.json").write_bytes(_canonical(result) + b"\n")
    report = ["# Prospective V5 shadow coverage", "",
              f"Captured: **{len(rows)}**; promotion-quality complete: **{len(quality_ids)}/30 target**. "
              f"Divergent: **{differences['DIVERGENT_ROUTE']}**; "
              f"same route: **{differences['SAME_ROUTE']}**.", "",
              "Capture count, verified observations, and promotion-quality complete tasks are separate.", "",
              f"Structural V4.1 vs V5_NO_HISTORY: **{views['structural'].status}** "
              f"({', '.join(views['structural'].reason_codes) or 'no gate blockers'}).", "",
              f"Full V4.1 vs V5_PREQUENTIAL_HISTORY: **{views['full_v5'].status}** "
              f"({', '.join(views['full_v5'].reason_codes) or 'no gate blockers'}).", "",
              "If history is part of the future V5 system, only the full prequential view can support "
              "a live-promotion decision; this report never promotes automatically.", "",
              f"Phase 1 classification-only benchmark: {phase1_count} tasks.", "",
              "## Structural cells", ""]
    for field in FIELDS:
        values = ", ".join(f"{key}={count}" for key, count in sorted(counts[field].items())) or "none"
        quality = ", ".join(f"{key}={count}" for key, count in sorted(quality_counts[field].items())) or "none"
        report.append(f"- {field}: captured [{values}]; quality complete [{quality}]")
    report += ["", "Underrepresented target cells: DEFINE, MISSING, local scope, low uncertainty, "
               "low fail cost, weak/strong verification, design, cleanup, HTML/email, research, router.",
               "Selection must remain natural; do not manufacture tasks for coverage.", ""]
    (root / "coverage_dashboard.md").write_text("\n".join(report), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline pre-execution V5 shadow capture")
    sub = parser.add_subparsers(dest="command", required=True)
    capture_cmd = sub.add_parser("capture")
    capture_cmd.add_argument("request_json")
    capture_cmd.add_argument("output_dir")
    observe_cmd = sub.add_parser("observe")
    observe_cmd.add_argument("observation_json")
    observe_cmd.add_argument("output_dir")
    dash_cmd = sub.add_parser("dashboard")
    dash_cmd.add_argument("output_dir")
    dash_cmd.add_argument("--phase1-corpus")
    args = parser.parse_args(argv)
    result = (capture(args.request_json, args.output_dir) if args.command == "capture"
              else observe(args.observation_json, args.output_dir) if args.command == "observe"
              else dashboard(args.output_dir, phase1_corpus=args.phase1_corpus))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
