import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from v5_offline.prospective import capture, dashboard, observe
from v5_offline.registry import OutcomeRegistry


CAPSULE = {
    "phase": "implementation", "rule_status": "KNOWN", "scope": "local",
    "uncertainty": "medium", "failcost": "low", "verification": "strong",
    "mutation": "local-mutation", "volume": "bounded", "dependencies": "shallow",
    "spec_completeness": "high", "repo_familiarity": "medium",
    "verification_covers_failure_modes": False, "model_version": "gpt-6",
}


class ProspectiveCaptureTests(unittest.TestCase):
    def _fixture(self, directory: Path, *, divergent=False, dirty=False):
        repo = directory / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "module.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
        if dirty:
            (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        (directory / "input.txt").write_text("Implement the bounded test task.\n", encoding="utf-8")
        (directory / "attachment.txt").write_text("Exact fixture attachment.\n", encoding="utf-8")
        cap = dict(CAPSULE)
        if divergent:
            cap.update(scope="cross-system", dependencies="coupled",
                       mutation="stateful-or-high-blast-radius", spec_completeness="low")
        request = {
            "task_id": "prospective_001", "task_family": "local_implementation",
            "input_path": "input.txt", "required_attached_files": ["attachment.txt"],
            "repository_path": "repo", "configuration_files": [], "local_data_files": [],
            "external_state_refs": [], "capsule": cap,
            "v4_1": {"rule_status": "KNOWN", "high": False,
                     "reason_codes": ["BOUNDED_EXPLICIT_TASK"]},
            "verification_rubric": {"criteria": [{"id": "check", "pass_condition": "value verified",
                                                    "evidence_type": "test_log"}],
                                    "expected_completion_evidence": ["test log with exit code"]},
            "replay_assessment": {"requires_repository": True, "requires_external_state": False,
                                  "source_state_complete": True, "safe_reversible": True,
                                  "side_effects_isolated": True, "identical_rubric": True,
                                  "coverage_useful": True},
        }
        path = directory / "capture_request.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        return path

    def test_freezes_prompt_attachment_and_clean_repo_before_shadow(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            request = self._fixture(base)
            output = base / "captures"
            result = capture(request, output)
            self.assertEqual(result["comparison"], "SAME_ROUTE")
            self.assertFalse(result["executed"])
            manifest = json.loads((output / "tasks" / "prospective_001" /
                                   "frozen_input_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(result["input_snapshot_ref"], "sha256:" + hashlib.sha256(
                (output / "tasks" / "prospective_001" /
                 "frozen_input_manifest.json").read_bytes()).hexdigest())
            self.assertTrue(manifest["repository"]["clean"])
            self.assertEqual(len(manifest["repository"]["head"]), 40)
            self.assertEqual(len(manifest["repository"]["tree"]), 40)
            self.assertEqual(manifest["input"]["sha256"], hashlib.sha256(
                (base / "input.txt").read_bytes()).hexdigest())
            self.assertEqual(len(manifest["attachments"]), 1)
            shadow = json.loads((output / "tasks" / "prospective_001" /
                                 "shadow_decision.json").read_text(encoding="utf-8"))
            self.assertEqual(shadow["v4_1_live_route"]["family"], "gpt-6-luna")
            self.assertEqual(shadow["history_evidence_refs_used"], [])
            self.assertFalse((output / "controlled_replay_queue.jsonl").exists())
            self.assertNotIn("Implement the bounded test task", json.dumps(shadow))
            with self.assertRaisesRegex(ValueError, "already captured"):
                capture(request, output)

    def test_dirty_state_blocks_divergent_pair_even_with_positive_attestation(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            request = self._fixture(base, divergent=True, dirty=True)
            output = base / "captures"
            result = capture(request, output)
            self.assertEqual(result["comparison"], "DIVERGENT_ROUTE")
            self.assertFalse(result["paired_replay_eligible"])
            item = json.loads((output / "tasks" / "prospective_001" /
                               "controlled_replay_queue_item.json").read_text(encoding="utf-8"))
            self.assertEqual(item["comparison_protocol"], "CONTROLLED_PAIRED_REPLAY")
            self.assertEqual(item["status"], "BLOCKED_FIXTURE_OR_SAFETY")
            self.assertFalse(item["eligibility_checks"]["fixture_reproducible"])
            self.assertEqual(item["second_arm_execution"], "NOT_AUTHORIZED_BY_CAPTURE")

    def test_dashboard_counts_capture_not_outcome(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            request = self._fixture(base)
            output = base / "captures"
            capture(request, output)
            report = dashboard(output)
            self.assertEqual(report["prospective_captured"], 1)
            self.assertEqual(report["same_route"], 1)
            self.assertEqual(report["remaining_to_target"], 30)
            self.assertEqual(report["promotion_quality_tasks"], 0)
            self.assertFalse(report["promotion_ready"])
            self.assertEqual(report["promotion_comparisons"]["structural"]["candidate_strategy"],
                             "v5_no_history")
            self.assertEqual(report["promotion_comparisons"]["full_v5"]["candidate_strategy"],
                             "v5_prequential_history")
            self.assertTrue((output / "coverage_dashboard.md").exists())

    def test_mutable_url_cannot_make_divergent_pair_eligible(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            request = self._fixture(base, divergent=True)
            raw = json.loads(request.read_text(encoding="utf-8"))
            raw["replay_assessment"]["requires_external_state"] = True
            raw["external_state_refs"] = [{"source_url": "https://example.invalid/live"}]
            request.write_text(json.dumps(raw), encoding="utf-8")
            result = capture(request, base / "captures")
            self.assertFalse(result["paired_replay_eligible"])
            manifest = json.loads((base / "captures" / "tasks" / "prospective_001" /
                                   "frozen_input_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["external_state_refs"][0]["status"], "UNFROZEN_REFERENCE")

            raw["replay_assessment"]["requires_external_state"] = False
            request.write_text(json.dumps(raw), encoding="utf-8")
            self.assertFalse(capture(request, base / "captures_optional_url")["paired_replay_eligible"])

    def test_referenced_repo_cannot_be_declared_irrelevant(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            request = self._fixture(base)
            raw = json.loads(request.read_text(encoding="utf-8"))
            raw["replay_assessment"]["requires_repository"] = False
            request.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "referenced repository"):
                capture(request, base / "captures")

    def test_unversioned_repo_can_be_shadowed_but_not_paired(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            request = self._fixture(base, divergent=True)
            # A separate directory with source files but no immutable VCS tree.
            unversioned = base / "unversioned"
            unversioned.mkdir()
            (unversioned / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            raw = json.loads(request.read_text(encoding="utf-8"))
            raw["repository_path"] = "unversioned"
            request.write_text(json.dumps(raw), encoding="utf-8")
            result = capture(request, base / "captures")
            self.assertFalse(result["paired_replay_eligible"])
            manifest = json.loads((base / "captures" / "tasks" / "prospective_001" /
                                   "frozen_input_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["repository"]["state"], "SOURCE_TREE_NOT_REPRODUCIBLE")

    def test_capture_output_inside_task_repo_blocks_pair(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            request = self._fixture(base, divergent=True)
            output = base / "repo" / "phase2_capture"
            result = capture(request, output)
            self.assertFalse(result["paired_replay_eligible"])
            manifest = json.loads((output / "tasks" / "prospective_001" /
                                   "frozen_input_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["capture_output_inside_repository"])

    def test_v41_adjudication_must_be_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            request = self._fixture(base)
            raw = json.loads(request.read_text(encoding="utf-8"))
            del raw["v4_1"]["high"]
            request.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "explicit rule_status"):
                capture(request, base / "captures")

    def test_observe_requires_preregistered_rubric_and_records_availability(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            request = self._fixture(base)
            output = base / "captures"
            capture(request, output)
            (base / "verification.txt").write_text("check: pass; exit 0\n", encoding="utf-8")
            observation = {"task_id": "prospective_001", "arm": "V4_1_LIVE",
                           "actual_model": "gpt-6-luna", "actual_effort": "medium",
                           "model_version": "gpt-6", "final_outcome": "verified_success",
                           "verification_evidence_file": "verification.txt",
                           "verification_evidence_source_ref": "local-test:verification.txt",
                           "criterion_results": {"check": True}, "retry_count": 0,
                           "rework_count": 0, "manual_override": False, "escalation": False}
            source = base / "observation.json"
            source.write_text(json.dumps(observation), encoding="utf-8")
            result = observe(source, output)
            self.assertEqual(result["evidence_kind"], "REAL_VERIFIED")
            record = OutcomeRegistry(output / "outcome_registry.jsonl").read()[0]
            self.assertIsNotNone(record.verification_available_at)
            self.assertGreater(record.verification_available_at, record.timestamp)
            self.assertTrue(record.verification_evidence_ref.startswith("sha256:"))
            report = dashboard(output)
            self.assertEqual(report["promotion_quality_tasks"], 1)
            with self.assertRaisesRegex(ValueError, "already observed"):
                observe(source, output)

    def test_observe_cannot_fabricate_success_or_run_second_arm(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            request = self._fixture(base, divergent=True, dirty=True)
            output = base / "captures"
            capture(request, output)
            (base / "verification.txt").write_text("check: failed\n", encoding="utf-8")
            observation = {"task_id": "prospective_001", "arm": "V5_CONTROLLED_REPLAY",
                           "actual_model": "gpt-6-sol", "actual_effort": "high",
                           "model_version": "gpt-6", "final_outcome": "verified_success",
                           "verification_evidence_file": "verification.txt",
                           "verification_evidence_source_ref": "local-test:verification.txt",
                           "criterion_results": {"check": False}, "retry_count": 0,
                           "rework_count": 0, "manual_override": False, "escalation": False}
            source = base / "observation.json"
            source.write_text(json.dumps(observation), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "second arm blocked"):
                observe(source, output)
            observation["arm"] = "V4_1_LIVE"
            observation["actual_model"] = "gpt-6-luna"
            observation["actual_effort"] = "medium"
            source.write_text(json.dumps(observation), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "every preregistered criterion"):
                observe(source, output)
            self.assertFalse((output / "outcome_registry.jsonl").exists())

    def test_divergent_live_route_drift_cannot_become_paired_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            request = self._fixture(base, divergent=True)
            output = base / "captures"
            prepared = capture(request, output)
            self.assertTrue(prepared["paired_replay_eligible"])
            (base / "verification.txt").write_text("check: pass\n", encoding="utf-8")
            observation = {"task_id": "prospective_001", "arm": "V4_1_LIVE",
                           "actual_model": "gpt-6-sol", "actual_effort": "high",
                           "model_version": "gpt-6", "final_outcome": "verified_success",
                           "verification_evidence_file": "verification.txt",
                           "verification_evidence_source_ref": "local-test:verification.txt",
                           "criterion_results": {"check": True}, "retry_count": 0,
                           "rework_count": 0, "manual_override": False, "escalation": False}
            source = base / "observation.json"
            source.write_text(json.dumps(observation), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "route drift"):
                observe(source, output)
            self.assertFalse((output / "outcome_registry.jsonl").exists())

    def test_capture_history_requires_verified_availability_before_decision(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            request = self._fixture(base)
            configured = json.loads(request.read_text(encoding="utf-8"))
            configured["capsule"]["model_version"] = "gpt-6-2026-09-29"
            request.write_text(json.dumps(configured), encoding="utf-8")
            first_out = base / "first"
            capture(request, first_out)
            (base / "verification.txt").write_text("check: pass\n", encoding="utf-8")
            obs = {"task_id": "prospective_001", "arm": "V4_1_LIVE",
                   "actual_model": "gpt-6-luna", "actual_effort": "medium",
                   "model_version": "gpt-6-2026-09-29",
                   "final_outcome": "verified_success", "verification_evidence_file": "verification.txt",
                   "verification_evidence_source_ref": "local-test:verification.txt",
                   "criterion_results": {"check": True}, "retry_count": 0, "rework_count": 0,
                   "manual_override": False, "escalation": False}
            source = base / "observation.json"
            source.write_text(json.dumps(obs), encoding="utf-8")
            observe(source, first_out)
            original = json.loads(request.read_text(encoding="utf-8"))
            original["task_id"] = "prospective_002"
            original["verified_history_file"] = str(first_out / "outcome_registry.jsonl")
            request.write_text(json.dumps(original), encoding="utf-8")
            second = capture(request, base / "second")
            self.assertEqual(second["history_records_used"], 1)

            prior = OutcomeRegistry(first_out / "outcome_registry.jsonl").read()[0]
            future_path = base / "future_registry.jsonl"
            OutcomeRegistry(future_path).append(replace(
                prior, verification_available_at=prior.verification_available_at + timedelta(days=1)))
            original["task_id"] = "prospective_003"
            original["verified_history_file"] = str(future_path)
            request.write_text(json.dumps(original), encoding="utf-8")
            self.assertEqual(capture(request, base / "third")["history_records_used"], 0)

            legacy_path = base / "legacy_registry.jsonl"
            OutcomeRegistry(legacy_path).append(replace(prior, verification_available_at=None))
            original["task_id"] = "prospective_004"
            original["verified_history_file"] = str(legacy_path)
            request.write_text(json.dumps(original), encoding="utf-8")
            self.assertEqual(capture(request, base / "fourth")["history_records_used"], 0)


if __name__ == "__main__":
    unittest.main()
