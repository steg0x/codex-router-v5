import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from v5_offline.live import (ROUTER_VERSION, _append, audit, decide, fallback, observe,
                             main, repair_capsule_field, validate_request)
from v5_offline.registry import HistoricalRoute, OutcomeRecord, OutcomeRegistry
from v5_offline.core import Route


AT = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def request(task_id="live_task", **capsule_updates):
    capsule = {"task_id": task_id, "task_family": "bounded_implementation", "phase": "implementation",
               "rule_status": "KNOWN", "scope": "local", "uncertainty": "low", "failcost": "low",
               "verification": "partial", "mutation": "local-mutation", "volume": "bounded",
               "dependencies": "shallow", "spec_completeness": "high", "repo_familiarity": "medium",
               "model_version": "gpt-6-2026-09"}
    capsule.update(capsule_updates)
    return {"capsule": capsule, "v4_1": {"rule_status": "KNOWN", "high": False,
                                         "reason_codes": ["BOUNDED_STANDARD"]}}


def history(task_id, *, available_at, final="verified_success"):
    return OutcomeRecord(
        task_id=task_id, task_family="bounded_implementation", phase="implementation",
        specification_completeness="high", scope="local", uncertainty="low", failcost="low",
        verification_strength="partial", mutation="local-mutation", volume="bounded",
        dependencies="shallow", repository_familiarity="medium", history_support="none",
        initial_route=Route("gpt-6-luna", "medium"), actual_model="gpt-6-luna",
        actual_effort="medium", manual_override=False, escalation=False, retry_count=0,
        rework_count=0, deterministic_verification_result="pass", final_outcome=final,
        final_successful_route=Route("gpt-6-luna", "medium") if final == "verified_success" else None,
        hindsight_cheapest_verified_success=None, router_version=ROUTER_VERSION,
        model_generation="gpt-6", model_version="gpt-6-2026-09",
        timestamp=min(AT - timedelta(days=1), available_at), verification_available_at=available_at,
        evidence_kind="REAL_VERIFIED", verification_evidence_ref="sha256:test",
        failure_type="capability" if final == "verified_failure" else "none")


class LiveBridgeTests(unittest.TestCase):
    def test_append_creates_preserves_and_emits_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "nested", "live.jsonl")
            _append(path, {"n": 1, "text": "Ã¨"})
            before = path.read_bytes()
            _append(path, {"n": 2})
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual([json.loads(line)["n"] for line in lines], [1, 2])
            self.assertTrue(path.read_bytes().startswith(before))
            self.assertTrue(path.read_bytes().endswith(b"\n"))

    def test_permission_error_before_open_preserves_existing_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "live.jsonl")
            path.write_text('{"prior":true}\n', encoding="utf-8")
            original = path.read_bytes()
            with patch.object(Path, "open", side_effect=PermissionError(13, "denied")):
                with self.assertRaises(PermissionError):
                    _append(path, {"new": True})
            self.assertEqual(path.read_bytes(), original)

    def test_decision_write_failure_falls_back_and_observe_refuses_it(self):
        with tempfile.TemporaryDirectory() as d:
            from v5_offline import live
            real_append = live._append
            calls = []
            def fail_first(path, value):
                calls.append(value)
                if len(calls) == 1:
                    raise OSError(5, "io failure", str(path))
                return real_append(path, value)
            with patch("v5_offline.live._append", side_effect=fail_first):
                result = decide(request("write_failure"), d, now=AT)
            self.assertTrue(result["fallback_used"])
            self.assertEqual(result["fallback_reason"], "ROUTING_STATE_UNWRITABLE")
            self.assertEqual(len(Path(d, "live_decisions.jsonl").read_text().splitlines()), 1)
            evidence = Path(d, "evidence.txt")
            evidence.write_text("proof", encoding="utf-8")
            obs = {"task_id": "write_failure", "final_outcome": "verified_success",
                   "verification_evidence_file": str(evidence), "verification_evidence_source_ref": "test",
                   "actual_model": "gpt-6-luna", "actual_effort": "medium",
                   "model_version": "gpt-6-2026-09", "retry_count": 0, "rework_count": 0,
                   "manual_override": False, "escalation": False}
            with self.assertRaisesRegex(ValueError, "only executed V5 route"):
                observe(obs, d, now=AT + timedelta(minutes=1))

    def test_permission_denied_does_not_fallback_or_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("v5_offline.live._append", side_effect=PermissionError(13, "denied")):
                result = decide(request("unlogged"), d, now=AT)
            self.assertFalse(result["telemetry_recorded"])
            self.assertEqual(result["error_code"], "ROUTING_STATE_UNWRITABLE")
            self.assertTrue(result["routing_state_unwritable"])
            self.assertFalse(result["fallback_used"])
            self.assertIsNone(result["v5_live_decision"])
            self.assertIsNone(result["dispatch_route"])
            self.assertFalse(Path(d, "live_decisions.jsonl").exists())
            with self.assertRaises((FileNotFoundError, ValueError)):
                observe({"task_id": "unlogged"}, d, now=AT + timedelta(minutes=1))

    def test_permission_denied_while_logging_other_failure_stops_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            from v5_offline import live
            def fail_both(path, value):
                if value.get("schema_version"):
                    raise PermissionError(13, "denied", str(path))
                raise OSError(5, "io failure", str(path))
            with patch("v5_offline.live._append", side_effect=fail_both):
                result = live.decide(request("fallback_denied"), d, now=AT)
            self.assertEqual(result["error_code"], "ROUTING_STATE_UNWRITABLE")
            self.assertFalse(result["fallback_used"])
            self.assertFalse(result["telemetry_recorded"])
            self.assertIsNone(result["dispatch_route"])
            with self.assertRaises((FileNotFoundError, ValueError)):
                observe({"task_id": "fallback_denied"}, d, now=AT + timedelta(minutes=1))

    def test_all_eight_auto_routes_preserve_tiers_and_efforts(self):
        cases = (
            ({}, "gpt-6-luna", "low"),
            ({"uncertainty": "medium"}, "gpt-6-luna", "medium"),
            ({"dependencies": "coupled"}, "gpt-6-luna", "high"),
            ({"volume": "large", "dependencies": "coupled", "failcost": "high",
              "verification": "strong"}, "gpt-6-luna", "xhigh"),
            ({"phase": "design"}, "gpt-6.2-sol", "low"),
            ({"phase": "design", "uncertainty": "medium"}, "gpt-6.2-sol", "medium"),
            ({"rule_status": "DEFINE", "consequential_choice_unresolved": True,
              "uncertainty": "high"}, "gpt-6.2-sol", "high"),
            ({"rule_status": "DEFINE", "consequential_choice_unresolved": True,
              "uncertainty": "high", "failcost": "high", "scope": "cross-system"},
             "gpt-6.2-sol", "xhigh"),
        )
        for index, (updates, family, effort) in enumerate(cases):
            with self.subTest(family=family, effort=effort), tempfile.TemporaryDirectory() as d:
                result = decide(request(f"route_{index}", **updates), d, now=AT)
                self.assertEqual(result["v5_live_decision"]["route"],
                                 {"family": family, "effort": effort})
                self.assertFalse(result["fallback_used"])
                self.assertNotIn("INVALID_AUTOMATIC_ROUTE_RECOMMEND_ROLLBACK", audit(d, checkpoint=5)["flags"])

    def test_capability_escalation_dispatches_sol61(self):
        with tempfile.TemporaryDirectory() as d:
            candidate = request("capability", failure="capability",
                                capability_evidence_ref="test-evidence")
            result = decide(candidate, d, now=AT)
            self.assertEqual(result["v5_live_decision"]["route"],
                             {"family": "gpt-6.2-sol", "effort": "xhigh"})
            self.assertIn("ONE_CAPABILITY_ESCALATION",
                          result["v5_live_decision"]["adjustments"])

    def test_legacy_sol_history_is_readable_but_excluded_while_luna_remains_eligible(self):
        old = history("old_sol", available_at=AT - timedelta(hours=1))
        data = old.to_dict()
        data["initial_route"] = {"family": "gpt-6-sol", "effort": "medium"}
        data["final_successful_route"] = data["initial_route"]
        data["actual_model"] = "gpt-6-sol"
        data["router_version"] = "v5_prequential_history_live"
        legacy = OutcomeRecord.from_dict(data)
        self.assertIsInstance(legacy.initial_route, HistoricalRoute)
        with tempfile.TemporaryDirectory() as d:
            registry = OutcomeRegistry(Path(d, "outcome_registry.jsonl"))
            registry.append(legacy)
            first = decide(request("after_legacy"), d, now=AT)
            self.assertEqual(first["history_records_used"], 0)
            registry.append(history("old_luna", available_at=AT - timedelta(hours=1),
                                    final="verified_failure"))
            second = decide(request("after_luna"), d, now=AT)
            self.assertEqual(second["history_records_used"], 1)
            self.assertEqual(second["v5_live_decision"]["route"]["family"], "gpt-6.2-sol")

    def test_observe_exact_sol61_and_reject_legacy_route_and_generation_drift(self):
        with tempfile.TemporaryDirectory() as d:
            candidate = request("sol_observed", rule_status="DEFINE",
                                consequential_choice_unresolved=True, uncertainty="high")
            candidate["v4_1"] = {"rule_status": "DEFINE", "high": True,
                                   "reason_codes": ["FROZEN_TEST"]}
            decide(candidate, d, now=AT)
            evidence = Path(d, "check.txt")
            evidence.write_text("verified", encoding="utf-8")
            obs = {"task_id": "sol_observed", "final_outcome": "verified_success",
                   "verification_evidence_file": str(evidence),
                   "verification_evidence_source_ref": "test-run",
                   "actual_model": "gpt-6.2-sol", "actual_effort": "high",
                   "model_version": "gpt-6-2026-09", "model_generation": "gpt-6",
                   "retry_count": 0, "rework_count": 0,
                   "manual_override": False, "escalation": False}
            with self.assertRaisesRegex(ValueError, "actual route drift"):
                observe({**obs, "actual_model": "gpt-6-sol"}, d, now=AT + timedelta(minutes=1))
            with self.assertRaisesRegex(ValueError, "model generation"):
                observe({**obs, "model_generation": "gpt-6.1"}, d,
                        now=AT + timedelta(minutes=1))
            with self.assertRaisesRegex(ValueError, "model version"):
                observe({**obs, "model_version": "wrong"}, d,
                        now=AT + timedelta(minutes=1))
            self.assertTrue(observe(obs, d, now=AT + timedelta(minutes=1))["recorded"])

    def test_audit_ignores_archived_legacy_epoch(self):
        with tempfile.TemporaryDirectory() as d:
            fallback("legacy", "DEFINE", True, "BRIDGE_INVOCATION_FAILED", d, now=AT)
            path = Path(d, "live_decisions.jsonl")
            legacy = json.loads(path.read_text(encoding="utf-8"))
            legacy["router_version"] = "v5_prequential_history_live"
            legacy["v5_live_decision"]["route"]["family"] = "gpt-6-sol"
            path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
            current = decide(request("current"), d, now=AT + timedelta(minutes=1))
            self.assertFalse(current["fallback_used"])
            report = audit(d, checkpoint=5)
            self.assertEqual(report["counts"]["decision_count"], 1)
            self.assertNotIn("INVALID_AUTOMATIC_ROUTE_RECOMMEND_ROLLBACK", report["flags"])

    def test_observe_rejects_fallback_and_automatic_astra(self):
        with tempfile.TemporaryDirectory() as d:
            fallback("fallback_task", "DEFINE", True, "BRIDGE_INVOCATION_FAILED", d, now=AT)
            evidence = Path(d, "check.txt")
            evidence.write_text("verified", encoding="utf-8")
            obs = {"task_id": "fallback_task", "final_outcome": "verified_success",
                   "verification_evidence_file": str(evidence),
                   "verification_evidence_source_ref": "test-run",
                   "actual_model": "gpt-6.2-sol", "actual_effort": "high",
                   "model_version": "gpt-6-2026-09", "retry_count": 0,
                   "rework_count": 0, "manual_override": False, "escalation": False}
            with self.assertRaisesRegex(ValueError, "only executed V5"):
                observe(obs, d, now=AT + timedelta(minutes=1))
            candidate = request("astra_forged")
            decide(candidate, d, now=AT + timedelta(minutes=2))
            path = Path(d, "live_decisions.jsonl")
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            rows[-1]["v5_live_decision"]["route"] = {"family": "gpt-6-astra", "effort": "high"}
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-automatic route"):
                observe({**obs, "task_id": "astra_forged", "actual_model": "gpt-6-astra"},
                        d, now=AT + timedelta(minutes=3))

    def run_validate_cli(self, candidate, root):
        request_path = Path(root, "request.json")
        request_path.write_text(json.dumps(candidate), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["validate", str(request_path), "--state-dir", str(Path(root, "state"))])
        return code, json.loads(output.getvalue())

    def test_validate_cli_exit_codes_and_json_for_valid_and_invalid_requests(self):
        cases = (
            (request(), 0, True),
            (request(scope="bounded"), 2, False),
            ({**request(), "v4_1": {"rule_status": "BROKEN", "high": False,
                                    "reason_codes": ["TEST"]}}, 2, False),
            (request(rule_status="DEFINE"), 2, False),
            (request(mutation="local_mutation"), 0, True),
        )
        for candidate, expected_code, expected_valid in cases:
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as d:
                code, result = self.run_validate_cli(candidate, d)
                self.assertEqual(code, expected_code)
                self.assertEqual(result["valid"], expected_valid)
                self.assertIn("errors", result)
                self.assertIn("v4_1_errors", result)
                self.assertIn("failure_stage", result)
                self.assertIn("normalizations_applied", result)
                self.assertIn("normalized_request", result)
                self.assertFalse(Path(d, "state", "live_decisions.jsonl").exists())

    def test_validate_cli_invalid_v41_and_define_missing_flag_exit_two(self):
        invalid_v41 = {**request(), "v4_1": {"rule_status": "KNOWN", "high": "false",
                                              "reason_codes": ["TEST"]}}
        define_missing = request(rule_status="DEFINE")
        for candidate, expected_error in ((invalid_v41, "v4_1.high"),
                                          (define_missing, "unresolved consequential choice")):
            with self.subTest(expected_error=expected_error), tempfile.TemporaryDirectory() as d:
                code, result = self.run_validate_cli(candidate, d)
                self.assertEqual(code, 2)
                self.assertFalse(result["valid"])
                self.assertTrue(any(expected_error in error["field"] or
                                    expected_error in error.get("reason", "")
                                    for error in result["errors"]))

    def test_v41_adjudication_is_fully_preflight_validated(self):
        canonical = request()
        self.assertTrue(validate_request(canonical)["valid"])
        cases = (
            ({}, "v4_1"),
            ({"rule_status": "BROKEN", "high": False, "reason_codes": ["X"]}, "v4_1.rule_status"),
            ({"rule_status": "KNOWN", "high": "false", "reason_codes": ["X"]}, "v4_1.high"),
            ({"rule_status": "KNOWN", "high": False, "reason_codes": []}, "v4_1.reason_codes"),
            ({"rule_status": "KNOWN", "high": False, "reason_codes": [""]}, "v4_1.reason_codes[0]"),
            ({"rule_status": "KNOWN", "high": False, "reason_codes": [None]}, "v4_1.reason_codes[0]"),
        )
        for bad_v41, expected_field in cases:
            with self.subTest(bad_v41=bad_v41):
                candidate = dict(canonical)
                if bad_v41:
                    candidate["v4_1"] = bad_v41
                else:
                    candidate.pop("v4_1")
                checked = validate_request(candidate)
                self.assertFalse(checked["valid"])
                self.assertIn(expected_field, [error["field"] for error in checked["errors"]])

    def test_invalid_v41_is_rejected_before_decide_routing(self):
        for bad_v41 in (None,
                        {"rule_status": "broken", "high": False, "reason_codes": ["X"]},
                        {"rule_status": "KNOWN", "high": "false", "reason_codes": ["X"]},
                        {"rule_status": "KNOWN", "high": False, "reason_codes": []},
                        {"rule_status": "KNOWN", "high": False, "reason_codes": [""]},
                        {"rule_status": "KNOWN", "high": False, "reason_codes": [1]}):
            with self.subTest(bad_v41=bad_v41), tempfile.TemporaryDirectory() as d:
                candidate = request()
                candidate["v4_1"] = bad_v41
                self.assertFalse(validate_request(candidate)["valid"])
                with self.assertRaisesRegex(ValueError, "V4.1 preflight validation failed"):
                    decide(candidate, d, now=AT)

    def test_valid_preflight_passes_v41_and_capsule_construction(self):
        candidate = request()
        self.assertTrue(validate_request(candidate)["valid"])
        with tempfile.TemporaryDirectory() as d:
            result = decide(candidate, d, now=AT)
        self.assertFalse(result["fallback_used"])
        self.assertIsNone(result["failure_stage"])

    def test_canonical_capsule_validates_without_changes(self):
        original = request()
        checked = validate_request(original)
        self.assertTrue(checked["valid"])
        self.assertEqual(checked["request"], original)
        self.assertEqual(checked["normalizations_applied"], [])

    def test_model_version_accepts_gpt6_identifiers_and_rejects_router_markers(self):
        for accepted in ("gpt-6", "gpt-6-luna", "gpt-6.2-sol", "gpt-6-2026-09"):
            with self.subTest(model_version=accepted):
                self.assertTrue(validate_request(request(model_version=accepted))["valid"])
        checked = validate_request(request(model_version=ROUTER_VERSION))
        self.assertFalse(checked["valid"])
        self.assertIn("model_version", [error["field"] for error in checked["errors"]])

    def test_canonical_builder_preserves_required_fields_inside_capsule(self):
        candidate = request("canonical_builder")
        checked = validate_request(candidate)
        self.assertTrue(checked["valid"])
        self.assertTrue({"task_id", "task_family", "model_version"}.issubset(checked["request"]["capsule"]))
        self.assertNotIn("task_id", {key for key in checked["request"] if key != "capsule"})

    def test_verification_coverage_requires_explicit_boolean(self):
        self.assertTrue(validate_request(request(verification_covers_failure_modes=True))["valid"])
        checked = validate_request(request(verification_covers_failure_modes="true"))
        self.assertFalse(checked["valid"])
        self.assertIn("verification_covers_failure_modes", [error["field"] for error in checked["errors"]])

    def test_deterministic_enum_syntax_aliases_reach_v5(self):
        for field, alias, canonical in (("scope", "multi_component", "multi-component"),
                                        ("scope", "cross_system", "cross-system"),
                                        ("mutation", "local_mutation", "local-mutation")):
            with self.subTest(field=field, alias=alias), tempfile.TemporaryDirectory() as d:
                checked = validate_request(request(**{field: alias}))
                self.assertTrue(checked["valid"])
                self.assertEqual(checked["request"]["capsule"][field], canonical)
                result = decide(request(**{field: alias}), d, now=AT)
                self.assertFalse(result["fallback_used"])
                self.assertEqual(result["request_validation_status"], "valid")
                self.assertEqual(result["normalizations_applied"][0]["to"], canonical)
                self.assertNotEqual(result["v5_live_decision"]["route"]["family"], "gpt-6-astra")

    def test_scope_value_from_other_axis_is_rejected_with_allowed_values(self):
        checked = validate_request(request(scope="bounded"))
        self.assertFalse(checked["valid"])
        self.assertEqual(checked["errors"][0], {
            "field": "scope", "value": "bounded",
            "allowed": ["local", "multi-component", "cross-system"]})
        self.assertFalse(validate_request(request(scope="large"))["valid"])

    def test_only_one_explicit_local_repair_is_allowed(self):
        bad = request(scope="bounded")
        repaired = repair_capsule_field(bad, "scope", "local")
        self.assertTrue(validate_request(repaired)["valid"])
        self.assertTrue(repaired["local_repair_used"])
        with self.assertRaisesRegex(ValueError, "only one"):
            repair_capsule_field(repaired, "scope", "cross-system")

    def test_repaired_valid_request_routes_and_records_repair(self):
        repaired = repair_capsule_field(request(scope="bounded"), "scope", "local")
        with tempfile.TemporaryDirectory() as d:
            result = decide(repaired, d, now=AT)
        self.assertFalse(result["fallback_used"])
        self.assertTrue(result["local_repair_used"])
        self.assertEqual(result["request_validation_status"], "valid")

    def test_unrepaired_invalid_request_uses_v41_and_explicit_telemetry(self):
        with tempfile.TemporaryDirectory() as d:
            result = decide(request(scope="bounded"), d, now=AT)
            row = json.loads(Path(d, "live_decisions.jsonl").read_text().splitlines()[-1])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["fallback_reason"], "INVALID_CAPSULE")
        self.assertEqual(result["v5_live_decision"], result["v4_1_shadow_decision"])
        self.assertEqual(row["request_validation_status"], "invalid")
        self.assertEqual(row["validation_errors"][0]["field"], "scope")
        self.assertEqual(row["validation_errors"][0]["value"], "bounded")
        self.assertEqual(row["failure_stage"], "PREFLIGHT_VALIDATION")

    def test_point_zero_constructor_failure_class_is_rejected_in_preflight(self):
        # Production failure class: enum-valid DEFINE capsule omitted the required unresolved-choice flag.
        case = request("jp_point_zero_20260929_01", task_family="project_clean_rebuild",
                       phase="design", rule_status="DEFINE", scope="cross-system",
                       uncertainty="high", failcost="high", volume="large", dependencies="coupled",
                       spec_completeness="medium", repo_familiarity="medium")
        checked = validate_request(case)
        self.assertFalse(checked["valid"])
        self.assertEqual(checked["errors"][0]["field"], "capsule")
        self.assertIn("DEFINE requires an unresolved consequential choice",
                      checked["errors"][0]["reason"])
        with tempfile.TemporaryDirectory() as d:
            result = decide(case, d, now=AT)
            row = json.loads(Path(d, "live_decisions.jsonl").read_text().splitlines()[-1])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["fallback_reason"], "INVALID_CAPSULE")
        self.assertEqual(row["failure_stage"], "PREFLIGHT_VALIDATION")

    def test_valid_preflight_cannot_fallback_at_capsule_validation_or_construction(self):
        case = request()
        self.assertTrue(validate_request(case)["valid"])
        with tempfile.TemporaryDirectory() as d:
            result = decide(case, d, now=AT)
            row = json.loads(Path(d, "live_decisions.jsonl").read_text().splitlines()[-1])
        self.assertNotEqual(row["failure_stage"], "PREFLIGHT_VALIDATION")
        self.assertNotEqual(row["failure_stage"], "CAPSULE_CONSTRUCTION")
        self.assertNotEqual(result["fallback_reason"], "INVALID_CAPSULE_AFTER_REPAIR")
        self.assertNotEqual(result["fallback_reason"], "INVALID_CAPSULE")

    def test_explicit_bridge_failure_fallback_is_logged(self):
        with tempfile.TemporaryDirectory() as d:
            result = fallback("bridge_failed", "DEFINE", True, "BRIDGE_INVOCATION_FAILED", d, now=AT)
            self.assertTrue(result["fallback_used"])
            self.assertEqual(result["v5_live_decision"]["route"],
                             {"family": "gpt-6.2-sol", "effort": "high"})
            row = json.loads(Path(d, "live_decisions.jsonl").read_text())
            self.assertIsNone(row["capsule"])
            self.assertEqual(row["fallback_reason"], "BRIDGE_INVOCATION_FAILED")

    def test_explicit_fallback_can_preserve_preflight_capsule_and_errors(self):
        diagnostic_request = request("invalid_diagnostic", scope="bounded")
        with tempfile.TemporaryDirectory() as d:
            result = fallback("invalid_diagnostic", "KNOWN", False, "INVALID_CAPSULE", d,
                              now=AT, diagnostics=diagnostic_request)
            row = json.loads(Path(d, "live_decisions.jsonl").read_text())
        self.assertEqual(result["request_validation_status"], "invalid")
        self.assertEqual(row["capsule"]["scope"], "bounded")
        self.assertEqual(row["validation_errors"][0]["field"], "scope")
        self.assertEqual(row["failure_stage"], "PREFLIGHT_VALIDATION")

    def test_divergence_is_not_fallback_and_telemetry_has_no_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            result = decide(request(scope="cross-system", dependencies="coupled",
                                    mutation="stateful-or-high-blast-radius", spec_completeness="low"), d, now=AT)
            self.assertEqual(result["difference"]["classification"], "DIVERGENT_ROUTE")
            self.assertFalse(result["fallback_used"])
            self.assertEqual(result["v5_live_decision"]["route"]["family"], "gpt-6.2-sol")
            self.assertNotIn("gpt-6-astra", Path(d, "live_decisions.jsonl").read_text())

    def test_registry_failure_falls_back_to_frozen_v41(self):
        class BrokenRegistry:
            def read(self):
                raise ValueError("corrupt record")
        with tempfile.TemporaryDirectory() as d:
            result = decide(request(), d, now=AT, registry=BrokenRegistry())
            self.assertTrue(result["fallback_used"])
            self.assertEqual(result["v5_live_decision"], result["v4_1_shadow_decision"])
            self.assertEqual(result["fallback_reason"], "REGISTRY_READ_FAILURE")
            self.assertIn("ValueError", result["fallback_error"])

    def test_prequential_availability_current_task_version_and_ttl(self):
        with tempfile.TemporaryDirectory() as d:
            registry = OutcomeRegistry(Path(d) / "outcome_registry.jsonl")
            registry.append_many([
                history("valid_prior", available_at=AT - timedelta(hours=1), final="verified_failure"),
                history("future", available_at=AT + timedelta(seconds=1), final="verified_failure"),
                history("live_task", available_at=AT - timedelta(hours=1), final="verified_failure"),
                history("stale", available_at=AT - timedelta(days=100), final="verified_failure"),
            ])
            result = decide(request(), d, now=AT)
            self.assertEqual(result["history_records_used"], 1)
            self.assertEqual(result["v5_live_decision"]["route"]["family"], "gpt-6.2-sol")
            row = json.loads(Path(d, "live_decisions.jsonl").read_text())
            self.assertEqual([x["task_id"] for x in row["history_records_used"]], ["valid_prior"])

    def test_invalid_v5_decision_is_internal_error_without_dispatch_and_auto_astra_is_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("v5_offline.live.route_v5", return_value=object()):
                result = decide(request(), d, now=AT)
            self.assertFalse(result["fallback_used"])
            self.assertEqual(result["error_code"], "ROUTER_INTERNAL_ERROR")
            self.assertIsNone(result["dispatch_route"])
            row = json.loads(Path(d, "live_decisions.jsonl").read_text().splitlines()[-1])
            self.assertEqual(row["failure_stage"], "DECISION_VALIDATION")
            self.assertIsNone(row["v5_live_decision"])
            malformed = request("malformed", scope="unsupported")
            fallback = decide(malformed, d, now=AT)
            self.assertTrue(fallback["fallback_used"])
            malformed_row = json.loads(Path(d, "live_decisions.jsonl").read_text().splitlines()[-1])
            self.assertEqual(malformed_row["capsule"]["scope"], "unsupported")
            self.assertEqual(malformed_row["validation_errors"][0]["field"], "scope")
            with self.assertRaisesRegex(ValueError, "manual model override"):
                decide(request("other", manual_override={"family": "gpt-6-astra", "effort": "high"}), d, now=AT)

    def test_observe_stamps_evidence_then_future_decision_can_use_it(self):
        with tempfile.TemporaryDirectory() as d:
            decide(request("first"), d, now=AT)
            evidence = Path(d, "check.txt")
            evidence.write_text("test passed", encoding="utf-8")
            obs = {"task_id": "first", "final_outcome": "verified_failure",
                   "verification_evidence_file": str(evidence), "verification_evidence_source_ref": "test-run-1",
                   "actual_model": "gpt-6-luna", "actual_effort": "low", "model_version": "gpt-6-2026-09",
                   "retry_count": 0, "rework_count": 0, "manual_override": False,
                   "escalation": False, "failure_type": "capability"}
            observe(obs, d, now=AT + timedelta(minutes=10))
            before = decide(request("second"), d, now=AT + timedelta(minutes=5))
            after = decide(request("third"), d, now=AT + timedelta(minutes=11))
            self.assertEqual(before["history_records_used"], 0)
            self.assertEqual(after["history_records_used"], 1)
            self.assertEqual(after["v5_live_decision"]["route"]["family"], "gpt-6.2-sol")
            self.assertIn("CHECKPOINT_NOT_YET_REACHED", audit(d, checkpoint=5)["flags"])
            self.assertIn("route_distribution", audit(d, checkpoint=15)["early_evidence"])


if __name__ == "__main__":
    unittest.main()
