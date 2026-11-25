import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_live_v5 import AT, request
from test_v5_offline import capsule, record
from v5_offline.core import (AUTO_EFFORTS, AUTO_ROUTES, Dependencies, Effort, Failure,
                             Family, Lease, Level, Mutation, Phase, Route, Scope,
                             Verification, Volume, _lower_effort, _raise_effort, route_v5)
from v5_offline.lightweight import log_task, main
from v5_offline.live import _safe_capsule, audit, decide, observe
from v5_offline.registry import OutcomeRegistry


class DirectEightTierTests(unittest.TestCase):
    def test_full_effort_order_and_manual_ceiling(self):
        self.assertEqual(len(AUTO_ROUTES), 12)
        for family in (Family.LUNA, Family.TERRA, Family.SOL):
            for i, effort in enumerate(AUTO_EFFORTS):
                with self.subTest(family=family, effort=effort):
                    route = Route(family, effort)
                    self.assertEqual(_raise_effort(route), Route(family, AUTO_EFFORTS[min(3, i + 1)]))
                    self.assertEqual(_lower_effort(route, Effort.LOW),
                                     Route(family, AUTO_EFFORTS[max(0, i - 1)]))
            self.assertNotIn(Route(family, Effort.MAX), AUTO_ROUTES)
            self.assertNotIn(Route(family, Effort.ULTRA), AUTO_ROUTES)

    def test_known_clear_design_selects_sol(self):
        low = capsule(phase=Phase.DESIGN)
        self.assertEqual(route_v5(low).route, Route(Family.SOL, Effort.LOW))
        self.assertEqual(route_v5(replace(low, uncertainty=Level.MEDIUM)).route,
                         Route(Family.SOL, Effort.MEDIUM))

    def test_high_uncertainty_strong_local_boundary(self):
        local = capsule(uncertainty=Level.HIGH, verification=Verification.STRONG)
        self.assertEqual(route_v5(local).route.family, Family.LUNA)
        for updates in ({"failcost": Level.HIGH}, {"scope": Scope.MULTI_COMPONENT},
                        {"verification": Verification.PARTIAL}):
            self.assertEqual(route_v5(replace(local, **updates)).route.family, Family.SOL)

    def test_known_large_deterministic_procedure_is_luna_xhigh(self):
        c = capsule(volume=Volume.LARGE, dependencies=Dependencies.COUPLED,
                    mutation=Mutation.STATEFUL, verification=Verification.STRONG)
        self.assertEqual(route_v5(c).route, Route(Family.LUNA, Effort.XHIGH))

    def test_sol_high_capacity_failure_goes_xhigh_then_stops(self):
        c = capsule(failure=Failure.CAPABILITY, capability_evidence_ref="test://proof",
                    lease=Lease(Phase.IMPLEMENTATION, Route(Family.SOL, Effort.HIGH), "t1"))
        self.assertEqual(route_v5(c).route, Route(Family.SOL, Effort.XHIGH))
        ceiling = replace(c, lease=Lease(Phase.IMPLEMENTATION, Route(Family.SOL, Effort.XHIGH), "t1"))
        self.assertEqual(route_v5(ceiling).kind.value, "MANUAL_ESCALATION_REQUIRED")

    def test_history_uses_project_structure_across_labels_and_versions(self):
        prior = capsule(task_id="accadueo_import_01", task_family="accadueo-badges")
        r = record(prior, timestamp=AT, verification_available_at=AT,
                   model_version="gpt-6-luna-2026-09-01")
        current = replace(prior, task_id="accadueo_import_02", task_family="accadueo-pdf",
                          model_version="gpt-6-luna-2026-10-01")
        self.assertTrue(r.valid_for(current, AT + timedelta(seconds=1)))
        self.assertFalse(r.valid_for(replace(current, scope=Scope.CROSS_SYSTEM), AT))
        self.assertFalse(r.valid_for(replace(current, task_id="dronitaly_01", task_family="dronitaly"), AT))
        self.assertFalse(replace(r, actual_model="gpt-6.2-sol").valid_for(current, AT))
        self.assertFalse(replace(r, verification_available_at=AT + timedelta(days=1)).valid_for(current, AT))
        failed = replace(r, final_outcome="verified_failure", final_successful_route=None)
        self.assertTrue(failed.valid_for(current, AT))  # Historical default none is a capacity failure.
        self.assertFalse(replace(failed, failure_type=Failure.OPERATIONAL).valid_for(current, AT))

    def test_safe_capsule_and_audit_normalize_old_null_role(self):
        self.assertEqual(_safe_capsule({})["role"], "root")
        self.assertEqual(_safe_capsule({"role": None})["role"], "root")
        with tempfile.TemporaryDirectory() as d:
            decide(request(), d, now=AT)
            path = Path(d, "live_decisions.jsonl")
            entry = json.loads(path.read_text())
            entry["capsule"]["role"] = None
            path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
            self.assertNotIn("ROOT_WORKER_INVARIANT_VIOLATION_RECOMMEND_ROLLBACK",
                             audit(d, checkpoint=5)["flags"])

    def test_observe_failure_defaults_to_capability_preserves_operational(self):
        for supplied in (None, "none", "operational"):
            with self.subTest(failure_type=supplied), tempfile.TemporaryDirectory() as d:
                decide(request(), d, now=AT)
                proof = Path(d, "proof.txt")
                proof.write_text("check failed", encoding="utf-8")
                observation = dict(task_id="live_task", final_outcome="verified_failure",
                                   verification_evidence_file=str(proof), verification_evidence_source_ref="test",
                                   actual_model="gpt-6-luna", actual_effort="low",
                                   model_version="gpt-6-2026-09", retry_count=0, rework_count=0,
                                   manual_override=False, escalation=False)
                if supplied is not None:
                    observation["failure_type"] = supplied
                observe(observation, d, now=AT + timedelta(seconds=1))
                r = OutcomeRegistry(Path(d, "outcome_registry.jsonl")).read()[0]
                self.assertEqual(r.failure_type.value, "operational" if supplied == "operational" else "capability")


class LightweightTelemetryTests(unittest.TestCase):
    def test_all_tiers_append_decision_and_outcome_without_history_inference(self):
        with tempfile.TemporaryDirectory() as d:
            proof = Path(d, "proof.txt")
            proof.write_text("source check passed", encoding="utf-8")
            for index, route in enumerate(AUTO_ROUTES):
                mode = ("root_direct" if route.family is Family.LUNA and route.effort in AUTO_EFFORTS[:2]
                        else "worker" if route.family is Family.LUNA else "strong_worker")
                result = log_task(task_id=f"tier_{index}", family=route.family.value,
                                  effort=route.effort.value, mode=mode, reason="TEST",
                                  outcome="verified_success", evidence_file=str(proof), state_dir=d)
                self.assertTrue(result["recorded"])
                self.assertTrue(result["outcome_recorded"])
            records = OutcomeRegistry(Path(d, "outcome_registry.jsonl")).read()
            self.assertEqual(len(records), 12)
            self.assertTrue(all(not r.structural_profile_complete for r in records))
            self.assertTrue(all(not r.valid_for(capsule()) for r in records))
            self.assertNotIn("INVALID_AUTOMATIC_ROUTE_RECOMMEND_ROLLBACK", audit(d, checkpoint=15)["flags"])

    def test_no_source_file_stays_reported_and_failure_default_is_capability(self):
        with tempfile.TemporaryDirectory() as d:
            log_task(task_id="reported", family="gpt-6-luna", effort="low", mode="root_direct",
                     reason="TEST", outcome="verified_failure", state_dir=d)
            r = OutcomeRegistry(Path(d, "outcome_registry.jsonl")).read()[0]
            self.assertEqual(r.evidence_kind, "REPORTED_OUTCOME")
            self.assertEqual(r.failure_type, Failure.CAPABILITY)

    def test_write_errors_invalid_routes_and_bad_flags_never_block(self):
        with patch("v5_offline.lightweight._append", side_effect=PermissionError("blocked")):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--task-id", "blocked", "--family", "gpt-6-luna", "--effort", "low",
                                       "--mode", "root_direct", "--reason", "TEST"]), 0)
            self.assertFalse(json.loads(output.getvalue())["recorded"])
        for args in (["--unknown", "value"], ["--task-id"], ["--rework", "bad"], []):
            with self.subTest(args=args), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(args), 0)
            self.assertIn("telemetry_error", json.loads(output.getvalue()))

    def test_cli_log_does_not_import_heavy_routing_modules(self):
        with tempfile.TemporaryDirectory() as d:
            script = """import runpy,sys
sys.argv=['v5_offline.live','log','--task-id','fast','--family','gpt-6-luna','--effort','medium','--mode','root_direct','--reason','TEST','--state-dir',sys.argv[1]]
try: runpy.run_module('v5_offline.live',run_name='__main__')
except SystemExit as exc: assert exc.code==0
assert not any(name in sys.modules for name in ('v5_offline.core','v5_offline.registry','v5_offline.arena','argparse'))
"""
            result = subprocess.run([sys.executable, "-c", script, d], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["recorded"])

    def test_evidence_is_referenced_without_reading_large_content(self):
        with tempfile.TemporaryDirectory() as d:
            proof = Path(d, "proof.txt")
            proof.write_text("proof", encoding="utf-8")
            with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
                result = log_task(task_id="bounded", family="gpt-6-luna", effort="low", mode="root_direct",
                                  reason="TEST", outcome="verified_success", evidence_file=str(proof), state_dir=d)
            self.assertTrue(result["outcome_recorded"])
