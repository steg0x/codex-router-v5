import re
import unittest
from pathlib import Path

from v5_offline.core import Phase


class DispatchPolicyTests(unittest.TestCase):
    def test_v51_matrix_allows_direct_work_and_all_eight_tiers(self):
        policy = (Path(__file__).parents[1] / "config" / "openai-delegation-v5.md").read_text(encoding="utf-8")
        self.assertIn("zero pre-task bureaucracy", policy)
        self.assertIn("Do not create temporary JSON files", policy)
        self.assertIn("Clarity of the prompt ≠ simplicity", policy)
        for model in ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol"):
            for effort in ("low", "medium", "high", "xhigh"):
                self.assertIn(f"`{model} / {effort}`", policy)
        self.assertIn("ROOT DIRECT, zero spawn", policy)
        self.assertIn("max`, `ultra`, and GPT-6 Astra remain manual-only", policy)

    def test_phase_instruction_lists_only_taskcapsule_phase_enum_values(self):
        policy = (Path(__file__).parents[1] / "config" / "openai-delegation-v5.md").read_text(
            encoding="utf-8"
        )
        match = re.search(r"For `phase`, choose only ([^;]+);", policy)

        self.assertIsNotNone(match)
        listed_values = re.findall(r"`([^`]+)`", match.group(1))
        self.assertEqual(listed_values, [phase.value for phase in Phase])
        self.assertIn("execution work belongs to `implementation`", policy)


if __name__ == "__main__":
    unittest.main()
