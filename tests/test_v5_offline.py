import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from v5_offline.core import (AUTO_ROUTES, DecisionKind, Dependencies, Effort, Failure,
                             Family, HistorySupport, Lease, Level, Mutation, Phase,
                             Profile, Role, Route, RuleStatus, Scope, TaskCapsule,
                             Verification, Volume, route_v4_1, route_v5)
from v5_offline.registry import HistoricalRoute, OutcomeRecord, OutcomeRegistry
from v5_offline.arena import Arena, evaluate_promotion, evaluate_promotion_views, metrics, pareto_frontier
from v5_offline.backfill import main as backfill_main


def capsule(**updates):
    data = dict(task_id="t1", task_family="bounded_work", phase=Phase.IMPLEMENTATION,
                rule_status=RuleStatus.KNOWN, scope=Scope.LOCAL, uncertainty=Level.LOW,
                failcost=Level.LOW, verification=Verification.PARTIAL,
                mutation=Mutation.LOCAL, volume=Volume.BOUNDED,
                dependencies=Dependencies.SHALLOW, spec_completeness=Level.HIGH,
                repo_familiarity=Level.MEDIUM)
    data.update(updates)
    return TaskCapsule(**data)


def record(c, **updates):
    data = dict(task_id=c.task_id, task_family=c.task_family, phase=c.phase,
                specification_completeness=c.spec_completeness, scope=c.scope,
                uncertainty=c.uncertainty, failcost=c.failcost,
                verification_strength=c.verification, mutation=c.mutation,
                volume=c.volume, dependencies=c.dependencies,
                repository_familiarity=c.repo_familiarity, history_support=c.history_support,
                initial_route=Route(Family.LUNA, Effort.LOW), actual_model="gpt-6-luna",
                actual_effort="low", manual_override=False, escalation=False,
                retry_count=0, rework_count=0,
                deterministic_verification_result="pass", final_outcome="verified_success",
                final_successful_route=Route(Family.LUNA, Effort.LOW),
                hindsight_cheapest_verified_success=Route(Family.LUNA, Effort.LOW),
                router_version="v5_candidate", model_generation="gpt-6",
                model_version="gpt-6", timestamp=datetime.now(timezone.utc),
                evidence_kind="REAL_VERIFIED", cost_if_observable=1.0,
                verification_evidence_ref="test://evidence")
    data.update(updates)
    return OutcomeRecord(**data)


class CapsuleTests(unittest.TestCase):
    def test_invalid_capsule_rejected(self):
        with self.assertRaises(ValueError):
            capsule(scope="unknown")
        with self.assertRaises(ValueError):
            capsule(rule_status=RuleStatus.MISSING)
        with self.assertRaises(ValueError):
            capsule(rule_status=RuleStatus.DEFINE)
        with self.assertRaises(ValueError):
            capsule(model_generation="gpt-5.6")
        with self.assertRaises(ValueError):
            capsule(manual_override={"family": "gpt-6-astra", "effort": "high"})
        with self.assertRaises(ValueError):
            capsule(failure=Failure.CAPABILITY)
        with self.assertRaises(ValueError):
            capsule(lease=Lease(Phase.IMPLEMENTATION, Route(Family.ASTRA, Effort.HIGH), "t1"))

    def test_route_gap_unknown_cause_and_complete_cross_component(self):
        c = capsule(task_family="first_occurrence_no_exact_rule", uncertainty=Level.HIGH,
                    scope=Scope.MULTI_COMPONENT, dependencies=Dependencies.COUPLED)
        d = route_v5(c)
        self.assertEqual(d.rule_status, RuleStatus.KNOWN)
        self.assertEqual(d.route, Route(Family.SOL, Effort.MEDIUM))

    def test_missing_consequential_user_information(self):
        c = capsule(rule_status=RuleStatus.MISSING, essential_information_unavailable=True)
        self.assertEqual(route_v5(c).kind, DecisionKind.CLARIFY)

    def test_capability_and_effort_are_independent(self):
        self.assertEqual(route_v5(capsule(uncertainty=Level.HIGH)).route, Route(Family.SOL, Effort.MEDIUM))
        self.assertEqual(route_v5(capsule(volume=Volume.LARGE)).route, Route(Family.LUNA, Effort.MEDIUM))
        self.assertEqual(route_v5(capsule(rule_status=RuleStatus.DEFINE,
                     consequential_choice_unresolved=True)).route, Route(Family.SOL, Effort.LOW))

    def test_manual_astra_only(self):
        override = Route(Family.ASTRA, Effort.HIGH)
        self.assertEqual(route_v5(capsule(manual_override=override)).route, override)
        for profile in Profile:
            for status in (RuleStatus.KNOWN, RuleStatus.DEFINE):
                c = capsule(profile=profile, rule_status=status,
                            consequential_choice_unresolved=status is RuleStatus.DEFINE,
                            failcost=Level.HIGH, verification=Verification.WEAK)
                self.assertNotEqual(route_v5(c).final_capability, Family.ASTRA)

    def test_nonroot_and_lease(self):
        for role in (Role.WORKER, Role.STRONG_WORKER, Role.ROUTER):
            self.assertEqual(route_v5(capsule(role=role)).kind, DecisionKind.RETURN_TO_ROOT)
            self.assertEqual(route_v5(capsule(role=role, failure=Failure.CAPABILITY,
                                              capability_evidence_ref="test://ceiling")).kind,
                             DecisionKind.RETURN_TO_ROOT)
        lease = Lease(Phase.IMPLEMENTATION, Route(Family.LUNA, Effort.HIGH), "t1")
        self.assertEqual(route_v5(capsule(lease=lease)).kind, DecisionKind.KEEP_LEASE)
        self.assertEqual(route_v5(capsule(lease=lease, failure=Failure.OPERATIONAL)).route, Route(Family.LUNA, Effort.HIGH))
        old_phase_lease = Lease(Phase.DESIGN, Route(Family.SOL, Effort.HIGH), "t1")
        self.assertEqual(route_v5(capsule(lease=old_phase_lease, failure=Failure.OPERATIONAL)).kind,
                         DecisionKind.ROUTE)

    def test_escalation_bounded_and_operational_failure(self):
        c = capsule(failure=Failure.CAPABILITY, capability_evidence_ref="test://ceiling")
        self.assertEqual(route_v5(c).route, Route(Family.SOL, Effort.XHIGH))
        self.assertEqual(route_v5(replace(c, capability_escalations=1)).kind,
                         DecisionKind.MANUAL_ESCALATION_REQUIRED)
        self.assertEqual(route_v5(capsule(rule_status=RuleStatus.DEFINE,
                         consequential_choice_unresolved=True, failcost=Level.HIGH,
                         failure=Failure.CAPABILITY,
                         capability_evidence_ref="test://ceiling")).route,
                         Route(Family.SOL, Effort.XHIGH))
        self.assertEqual(route_v5(capsule(failure=Failure.OPERATIONAL)).route, Route(Family.LUNA, Effort.LOW))

    def test_verification_and_cost_floor(self):
        c = capsule(uncertainty=Level.HIGH, verification=Verification.STRONG,
                    verification_covers_failure_modes=True)
        self.assertEqual(route_v5(c).route, Route(Family.LUNA, Effort.LOW))
        c = replace(c, failcost=Level.HIGH, profile=Profile.COST_FIRST)
        self.assertEqual(route_v5(c).route, Route(Family.SOL, Effort.MEDIUM))
        c = replace(c, mutation=Mutation.STATEFUL)
        self.assertEqual(route_v5(c).route, Route(Family.SOL, Effort.MEDIUM))

    def test_phase_transition_and_stability(self):
        c = capsule(phase=Phase.DESIGN, rule_status=RuleStatus.DEFINE,
                    consequential_choice_unresolved=True, uncertainty=Level.HIGH)
        self.assertEqual(route_v5(c).route, route_v5(c).route)
        implementation = replace(c, phase=Phase.IMPLEMENTATION, rule_status=RuleStatus.KNOWN,
                                 consequential_choice_unresolved=False)
        verification = replace(implementation, phase=Phase.VERIFICATION,
                               uncertainty=Level.LOW, dependencies=Dependencies.SHALLOW)
        self.assertEqual(route_v5(c).route, Route(Family.SOL, Effort.HIGH))
        self.assertEqual(route_v5(implementation).route, Route(Family.SOL, Effort.MEDIUM))
        self.assertEqual(route_v5(verification).route, Route(Family.LUNA, Effort.LOW))

    def test_v51_capability_failure_escalates_to_sol_xhigh(self):
        high = capsule(uncertainty=Level.HIGH, failure=Failure.CAPABILITY,
                       capability_evidence_ref="test://capability")
        self.assertEqual(route_v5(high).route, Route(Family.SOL, Effort.XHIGH))
        medium = replace(high, uncertainty=Level.LOW)
        self.assertEqual(route_v5(medium).route, Route(Family.SOL, Effort.XHIGH))
        sol_lease = Lease(Phase.IMPLEMENTATION, Route(Family.SOL, Effort.MEDIUM), "t1")
        self.assertEqual(route_v5(replace(medium, lease=sol_lease)).route,
                         Route(Family.SOL, Effort.XHIGH))

    def test_v51_effort_adjustments_never_change_family(self):
        quality = capsule(uncertainty=Level.HIGH, verification=Verification.WEAK,
                          profile=Profile.QUALITY_FIRST)
        self.assertEqual(route_v5(quality).route, Route(Family.SOL, Effort.HIGH))
        sol_medium = capsule(rule_status=RuleStatus.DEFINE,
                             consequential_choice_unresolved=True,
                             verification=Verification.STRONG,
                             verification_covers_failure_modes=True,
                             profile=Profile.COST_FIRST)
        self.assertEqual(route_v5(sol_medium).route, Route(Family.SOL, Effort.LOW))
        self.assertEqual(route_v5(sol_medium).credits, ())

    def test_v51_spec_completeness_and_telemetry_only_dimensions(self):
        c = capsule(scope=Scope.CROSS_SYSTEM, failcost=Level.HIGH,
                    spec_completeness=Level.LOW)
        self.assertEqual(route_v5(c).route, Route(Family.SOL, Effort.MEDIUM))
        self.assertIn("LOW_SPEC_AUTONOMY", route_v5(c).floors)
        complete = replace(c, spec_completeness=Level.HIGH)
        self.assertEqual(route_v5(complete).route, Route(Family.SOL, Effort.MEDIUM))
        for familiarity in Level:
            for volume in Volume:
                self.assertEqual(route_v5(replace(complete, repo_familiarity=familiarity,
                                                  volume=volume)).route,
                                 Route(Family.SOL, Effort.MEDIUM))

    def test_v51_missing_precedes_lease_and_manual_role_boundary(self):
        lease = Lease(Phase.IMPLEMENTATION, Route(Family.LUNA, Effort.HIGH), "t1")
        missing = capsule(rule_status=RuleStatus.MISSING,
                          essential_information_unavailable=True, lease=lease)
        self.assertEqual(route_v5(missing).kind, DecisionKind.CLARIFY)
        self.assertEqual(route_v5(replace(missing, manual_override=Route(Family.ASTRA, Effort.HIGH))).kind,
                         DecisionKind.CLARIFY)
        root_manual = capsule(manual_override=Route(Family.ASTRA, Effort.HIGH))
        self.assertEqual(route_v5(root_manual).route.family, Family.ASTRA)
        self.assertEqual(route_v5(replace(root_manual, role=Role.WORKER)).kind,
                         DecisionKind.RETURN_TO_ROOT)

    def test_semantic_label_alone_does_not_change_route(self):
        baseline = route_v5(capsule()).route
        for label in ("architecture", "database", "migration", "security", "refactor", "benchmark"):
            self.assertEqual(route_v5(capsule(task_family=label)).route, baseline)

    def test_v41_frozen_mapping(self):
        expected = {("KNOWN", False): Route(Family.LUNA, Effort.MEDIUM), ("KNOWN", True): Route(Family.LUNA, Effort.HIGH),
                    ("DEFINE", False): Route(Family.SOL, Effort.MEDIUM), ("DEFINE", True): Route(Family.SOL, Effort.HIGH)}
        for (status, high), route in expected.items():
            self.assertEqual(route_v4_1(status, high).route, route)
        self.assertEqual(route_v4_1("MISSING", False).kind, DecisionKind.CLARIFY)

    def test_combinatorial_no_automatic_astra(self):
        for status in (RuleStatus.KNOWN, RuleStatus.DEFINE):
            for scope in Scope:
                for uncertainty in Level:
                    for failcost in Level:
                        for profile in Profile:
                            c = capsule(rule_status=status, scope=scope, uncertainty=uncertainty,
                                        failcost=failcost, profile=profile,
                                        consequential_choice_unresolved=status is RuleStatus.DEFINE)
                            d = route_v5(c)
                            self.assertIn(d.route, AUTO_ROUTES)
                            if failcost is Level.HIGH:
                                self.assertIn(d.route.effort, (Effort.MEDIUM, Effort.HIGH, Effort.XHIGH))
                            if status is RuleStatus.DEFINE:
                                self.assertEqual(d.route.family, Family.SOL)


class RegistryArenaTests(unittest.TestCase):
    def test_registry_round_trip_privacy_and_generation(self):
        c = capsule()
        r = record(c)
        with tempfile.TemporaryDirectory() as directory:
            registry = OutcomeRegistry(Path(directory) / "outcomes.jsonl")
            registry.append(r)
            self.assertEqual(registry.read(), [r])
            self.assertEqual(len(registry.verified_for(c)), 1)
            self.assertNotIn("raw_prompt", registry.path.read_text(encoding="utf-8"))
        old = replace(r, model_generation="gpt-5.6", model_version="gpt-5.6",
                      actual_model="gpt-5.6-luna",
                      initial_route=HistoricalRoute("gpt-5.6-luna", "medium"),
                      final_successful_route=HistoricalRoute("gpt-5.6-luna", "medium"),
                      hindsight_cheapest_verified_success=HistoricalRoute("gpt-5.6-luna", "medium"))
        self.assertFalse(old.valid_for(c))
        self.assertEqual(OutcomeRecord.from_dict(old.to_dict()), old)
        stale = replace(r, timestamp=datetime.now(timezone.utc) - timedelta(days=91))
        self.assertFalse(stale.valid_for(c))
        synthetic = replace(r, evidence_kind="SYNTHETIC_FIXTURE")
        self.assertFalse(synthetic.valid_for(c))
        manual_astra = replace(r, initial_route=Route(Family.ASTRA, Effort.HIGH),
                               actual_model="gpt-6-astra", manual_override=True)
        self.assertFalse(manual_astra.valid_for(c))

    def test_history_one_step_and_not_astra(self):
        c = capsule(uncertainty=Level.MEDIUM, dependencies=Dependencies.COUPLED)
        positive = record(c, task_id="prior")
        self.assertEqual(route_v5(c, [positive]).route, Route(Family.LUNA, Effort.MEDIUM))
        negative = replace(positive, final_outcome="verified_failure", final_successful_route=None,
                           failure_type=Failure.CAPABILITY)
        self.assertEqual(route_v5(c, [negative]).route, Route(Family.SOL, Effort.HIGH))
        self.assertNotEqual(route_v5(c, [negative]).final_capability, Family.ASTRA)
        operational = replace(negative, failure_type=Failure.OPERATIONAL)
        self.assertEqual(route_v5(c, [operational]).route, Route(Family.LUNA, Effort.HIGH))

    def test_replay_history_excludes_self_and_future(self):
        c = capsule(uncertainty=Level.MEDIUM, dependencies=Dependencies.COUPLED)
        now = datetime.now(timezone.utc)
        same = record(c, timestamp=now)
        future = record(c, task_id="later", timestamp=now + timedelta(days=1))
        self.assertEqual(route_v5(replace(c, as_of=now), [same, future]).route, Route(Family.LUNA, Effort.HIGH))

    def test_v41_replay_adjudication_independent(self):
        c = capsule(v4_1_rule_status=RuleStatus.MISSING, v4_1_high=False)
        rows = Arena().replay([c], [])
        self.assertEqual(next(r for r in rows if r.strategy == "v4_1").decision.kind, DecisionKind.CLARIFY)
        self.assertEqual(next(r for r in rows if r.strategy == "v5_candidate").decision.kind, DecisionKind.ROUTE)

    def test_arena_strategies_and_unknown_counterfactuals(self):
        c = capsule()
        arena = Arena()
        rows = arena.replay([c], [record(c)])
        self.assertEqual(len(rows), 9)
        self.assertEqual(set(r.strategy for r in rows), set(arena.strategies))
        self.assertEqual(next(r for r in rows if r.strategy == "v4_1").decision.kind,
                         DecisionKind.UNOBSERVED)
        self.assertFalse(next(r for r in rows if r.strategy == "always_gpt_6_sol_high").observed)
        report = metrics(rows)
        self.assertEqual(report["v5_candidate"]["verified_success"], 1)
        self.assertEqual(report["always_gpt_6_sol_high"]["verified_success_rate"], None)
        self.assertEqual(evaluate_promotion(rows, [record(c)],
                                            candidate_strategy="v5_no_history").status, "INSUFFICIENT_EVIDENCE")

    def test_arena_prequential_history_uses_frozen_decision_time(self):
        decision_at = datetime.now(timezone.utc) - timedelta(hours=2)
        c = capsule(uncertainty=Level.MEDIUM, dependencies=Dependencies.COUPLED, as_of=decision_at)
        prior = record(c, task_id="prior", timestamp=decision_at - timedelta(minutes=2),
                       verification_available_at=decision_at - timedelta(minutes=1))
        between_decision_and_completion = record(c, task_id="leak",
                                                 timestamp=decision_at - timedelta(minutes=1),
                                                 verification_available_at=decision_at + timedelta(minutes=5))
        current = record(c, timestamp=decision_at + timedelta(minutes=10))
        rows = Arena().replay([c], [prior, between_decision_and_completion, current])
        decisions = {r.strategy: r.decision for r in rows}
        self.assertEqual(decisions["v5_no_history"].route, Route(Family.LUNA, Effort.HIGH))
        self.assertEqual(decisions["v5_candidate"].route, Route(Family.LUNA, Effort.HIGH))
        self.assertEqual(decisions["v5_prequential_history"].route, Route(Family.LUNA, Effort.MEDIUM))

        no_prior = Arena().replay([c], [between_decision_and_completion, current])
        self.assertEqual(next(r.decision.route for r in no_prior
                              if r.strategy == "v5_prequential_history"), Route(Family.LUNA, Effort.HIGH))
        missing_timestamp = Arena().replay([replace(c, as_of=None)], [prior])
        self.assertEqual(next(r.decision.kind for r in missing_timestamp
                              if r.strategy == "v5_prequential_history"), DecisionKind.UNOBSERVED)
        legacy_without_availability = replace(prior, verification_available_at=None)
        legacy_rows = Arena().replay([c], [legacy_without_availability, current])
        self.assertEqual(next(r.decision.route for r in legacy_rows
                              if r.strategy == "v5_prequential_history"), Route(Family.LUNA, Effort.HIGH))

    def test_prospective_paired_arm_identity_cannot_be_swapped(self):
        c = capsule(scope=Scope.CROSS_SYSTEM, dependencies=Dependencies.COUPLED,
                    mutation=Mutation.STATEFUL, spec_completeness=Level.LOW,
                    v4_1_rule_status=RuleStatus.KNOWN, v4_1_high=False,
                    as_of=datetime.now(timezone.utc) - timedelta(minutes=3))
        common = dict(comparison_protocol="CONTROLLED_PAIRED_REPLAY",
                      comparison_group_id="group", input_snapshot_ref="sha256:fixture")
        wrong_old = record(c, initial_route=Route(Family.LUNA, Effort.MEDIUM),
                           final_successful_route=Route(Family.LUNA, Effort.MEDIUM),
                           router_version="v5_controlled_replay", **common)
        wrong_new = record(c, initial_route=Route(Family.SOL, Effort.HIGH), actual_model="gpt-6-sol",
                           actual_effort="high", final_successful_route=Route(Family.SOL, Effort.HIGH),
                           router_version="v4_1_live", **common)
        rows = Arena().replay([c], [wrong_old, wrong_new])
        self.assertFalse(next(r for r in rows if r.strategy == "v4_1").observed)
        self.assertFalse(next(r for r in rows if r.strategy == "v5_candidate").observed)

    def test_synthetic_cannot_promote(self):
        records = [replace(record(capsule(task_id=str(i))), evidence_kind="SYNTHETIC_FIXTURE")
                   for i in range(40)]
        capsules = [capsule(task_id=str(i)) for i in range(40)]
        result = evaluate_promotion(Arena().replay(capsules, records), records,
                                    candidate_strategy="v5_no_history")
        self.assertEqual(result.status, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result.real_verified_task_count, 0)

    def test_backfill_dry_run_then_commit(self):
        import json
        c = capsule()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.jsonl"
            target = Path(directory) / "registry.jsonl"
            source.write_text(json.dumps(record(c).to_dict()) + "\n", encoding="utf-8")
            self.assertEqual(backfill_main([str(source), str(target)]), 0)
            self.assertFalse(target.exists())
            self.assertEqual(backfill_main([str(source), str(target), "--commit"]), 0)
            self.assertEqual(len(OutcomeRegistry(target).read()), 1)
            with self.assertRaises(ValueError):
                backfill_main([str(source), str(target), "--commit"])

    def test_v51_under_routing_uses_independent_axes(self):
        c = capsule(uncertainty=Level.HIGH)
        # Luna High has more effort but less capability than Sol Medium.
        r = record(c, task_id="prior", initial_route=Route(Family.LUNA, Effort.HIGH),
                   actual_effort="high", hindsight_cheapest_verified_success=Route(Family.SOL, Effort.MEDIUM))
        rows = Arena().replay([c], [replace(r, task_id=c.task_id)])
        fixed = next(row for row in rows if row.strategy == "always_gpt_6_luna_high")
        self.assertEqual(metrics([fixed])[fixed.strategy]["under_routing_rate"], 1.0)

    def test_v51_arena_rejects_mismatched_task_structure(self):
        c = capsule()
        wrong = record(c, scope=Scope.CROSS_SYSTEM)
        rows = Arena().replay([c], [wrong])
        self.assertFalse(next(row for row in rows if row.strategy == "v5_candidate").observed)

    def test_v51_arena_rejects_ambiguous_snapshots_and_duplicate_route(self):
        c = capsule()
        first = record(c, comparison_protocol="CONTROLLED_PAIRED_REPLAY",
                       comparison_group_id="g1", input_snapshot_ref="snapshot://one")
        second = replace(first, comparison_group_id="g2",
                         input_snapshot_ref="snapshot://two")
        rows = Arena().replay([c], [first, second])
        self.assertFalse(next(row for row in rows if row.strategy == "v5_candidate").observed)
        duplicate = Arena().replay([c], [record(c), record(c)])
        self.assertFalse(next(row for row in duplicate if row.strategy == "v5_candidate").observed)

    def test_v51_paired_counterfactual_gate_and_zero_baselines(self):
        capsules = []
        paired_records = []
        unpaired_records = []
        for i in range(30):
            c = capsule(task_id=f"pair-{i}", v4_1_rule_status=RuleStatus.KNOWN,
                        v4_1_high=True)
            capsules.append(c)
            common = dict(comparison_protocol="CONTROLLED_PAIRED_REPLAY",
                          comparison_group_id=f"group-{i}",
                          input_snapshot_ref=f"snapshot://{i}")
            new = record(c, cost_if_observable=1.0, **common)
            old = record(c, initial_route=Route(Family.LUNA, Effort.HIGH), actual_effort="high",
                         final_successful_route=Route(Family.LUNA, Effort.HIGH), cost_if_observable=2.0,
                         **common)
            paired_records.extend((new, old))
            unpaired_records.extend((replace(new, comparison_protocol=None,
                                             comparison_group_id=None, input_snapshot_ref=None),
                                     replace(old, comparison_protocol=None,
                                             comparison_group_id=None, input_snapshot_ref=None)))
        arena = Arena()
        missing_pair = evaluate_promotion(arena.replay(capsules, unpaired_records), unpaired_records,
                                          candidate_strategy="v5_no_history")
        self.assertEqual(missing_pair.status, "INSUFFICIENT_EVIDENCE")
        self.assertIn("CONTROLLED_PAIRED_REPLAY_MISSING", missing_pair.reason_codes)
        valid_pair = evaluate_promotion(arena.replay(capsules, paired_records), paired_records,
                                        candidate_strategy="v5_no_history")
        self.assertEqual(valid_pair.status, "PASS")
        one_override = list(paired_records)
        one_override[0] = replace(one_override[0], manual_override=True)
        self.assertEqual(evaluate_promotion(arena.replay(capsules, one_override),
                                            one_override, candidate_strategy="v5_no_history").status, "FAIL")
        self.assertEqual(evaluate_promotion(arena.replay(capsules, one_override),
                                            one_override, candidate_strategy="v5_no_history",
                                            manual_override_tolerance=0.04).status, "PASS")

    def test_promotion_views_use_their_own_candidate_route_and_pair(self):
        decision_at = datetime.now(timezone.utc) - timedelta(hours=2)
        capsules = [capsule(task_id=f"history-sensitive-{i}", rule_status=RuleStatus.DEFINE,
                            consequential_choice_unresolved=True, uncertainty=Level.HIGH,
                            v4_1_rule_status=RuleStatus.KNOWN, v4_1_high=True,
                            as_of=decision_at) for i in range(30)]
        prior = record(capsules[0], task_id="independent-prior",
                       timestamp=decision_at - timedelta(minutes=2),
                       verification_available_at=decision_at - timedelta(minutes=1))

        def paired_for(candidate_route):
            records = []
            for i, c in enumerate(capsules):
                pair = dict(comparison_protocol="CONTROLLED_PAIRED_REPLAY",
                            comparison_group_id=f"group-{i}", input_snapshot_ref=f"sha256:fixture-{i}")
                old = record(c, initial_route=Route(Family.LUNA, Effort.HIGH), actual_effort="high",
                             final_successful_route=Route(Family.LUNA, Effort.HIGH), cost_if_observable=2.0,
                             router_version="v4_1_live", **pair)
                new = record(c, initial_route=candidate_route, actual_model="gpt-6-sol",
                             actual_effort=candidate_route.effort.value,
                             final_successful_route=candidate_route, cost_if_observable=1.0,
                             router_version="v5_controlled_replay", **pair)
                records.extend((old, new))
            return records

        structural_pair = paired_for(Route(Family.SOL, Effort.HIGH))  # Sol High
        structural_rows = Arena().replay(capsules, [prior, *structural_pair])
        structural_views = evaluate_promotion_views(structural_rows, structural_pair)
        self.assertEqual(structural_views["structural"].status, "PASS")
        self.assertEqual(structural_views["structural"].candidate_strategy, "v5_no_history")
        self.assertEqual(structural_views["full_v5"].status, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(structural_views["full_v5"].candidate_strategy, "v5_prequential_history")
        self.assertIn("COUNTERFACTUAL_OUTCOMES_UNOBSERVED", structural_views["full_v5"].reason_codes)
        self.assertEqual(evaluate_promotion(structural_rows, structural_pair,
                                            candidate_strategy="v5_candidate").status, "PASS")

        prequential_pair = paired_for(Route(Family.SOL, Effort.MEDIUM))  # Sol Medium after prior verified success
        prequential_rows = Arena().replay(capsules, [prior, *prequential_pair])
        prequential_views = evaluate_promotion_views(prequential_rows, prequential_pair)
        self.assertEqual(prequential_views["structural"].status, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(prequential_views["full_v5"].status, "PASS")

    def test_promotion_requires_explicit_candidate_strategy(self):
        with self.assertRaises(TypeError):
            evaluate_promotion([], [])
        with self.assertRaisesRegex(ValueError, "candidate_strategy"):
            evaluate_promotion([], [], candidate_strategy="unknown")


if __name__ == "__main__":
    unittest.main()
