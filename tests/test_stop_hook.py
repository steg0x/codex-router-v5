import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from v5_offline.live import audit
from v5_offline.registry import OutcomeRegistry

HOOK_PATH = Path(__file__).parents[1] / "config" / "hooks" / "router_v5_stop_telemetry.py"


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("router_v5_stop_telemetry", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _create_mock_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            created_at INTEGER,
            updated_at INTEGER,
            model TEXT,
            reasoning_effort TEXT,
            agent_role TEXT,
            tokens_used INTEGER,
            cwd TEXT,
            source TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE thread_spawn_edges (
            parent_thread_id TEXT,
            child_thread_id TEXT,
            status TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _insert_thread(
    db_path: Path,
    *,
    tid: str,
    created_at: int,
    updated_at: int,
    model: str,
    effort: str,
    role: str | None,
    tokens: int,
    cwd: str,
    parent_id: str | None = None,
    agent_path: str | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    source = (
        json.dumps(
            {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": parent_id,
                        "depth": 1,
                        "agent_path": agent_path or "/root/task_alpha",
                        "agent_role": role,
                    }
                }
            }
        )
        if parent_id
        else "vscode"
    )
    cur.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, created_at, updated_at, model, effort, role, tokens, cwd, source),
    )
    if parent_id:
        cur.execute(
            "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
            (parent_id, tid, "closed"),
        )
    conn.commit()
    conn.close()


class StopHookTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.hook = _load_hook_module()

    def test_records_spawned_subagents_once_with_real_tokens_and_passes_audit(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            db = home / "state_5.sqlite"
            _create_mock_db(db)

            _insert_thread(
                db,
                tid="root-0001",
                created_at=1795600000,
                updated_at=1795600120,
                model="gpt-6-luna",
                effort="medium",
                role=None,
                tokens=5000,
                cwd=r"C:\work\my_project",
            )
            _insert_thread(
                db,
                tid="child-terra-0001",
                created_at=1795600010,
                updated_at=1795600055,
                model="gpt-6.1-terra",
                effort="high",
                role="strong_worker",
                tokens=420000,
                cwd=r"C:\work\my_project",
                parent_id="root-0001",
                agent_path="/root/refactor_pipeline",
            )
            _insert_thread(
                db,
                tid="child-sol62-0002",
                created_at=1795600060,
                updated_at=1795600110,
                model="gpt-6.2-sol",
                effort="medium",
                role="strong_worker",
                tokens=780000,
                cwd=r"\\?\C:\work\my_project",
                parent_id="root-0001",
                agent_path="/root/design_svg",
            )
            # Manual Astra and legacy gpt-6-sol must be skipped without breaking audit
            _insert_thread(
                db,
                tid="child-astra-0003",
                created_at=1795600115,
                updated_at=1795600119,
                model="gpt-6-astra",
                effort="high",
                role="strong_worker",
                tokens=100000,
                cwd=r"C:\work\my_project",
                parent_id="root-0001",
                agent_path="/root/manual_astra",
            )

            payload = {
                "session_id": "root-0001",
                "hook_event_name": "Stop",
                "model": "gpt-6-luna",
                "last_assistant_message": "[Route: gpt-6.2-sol / medium -> spawn strong_worker | reason: svg_layout]\nDone.",
            }
            first = self.hook.process_stop_event(payload, codex_home=home)
            self.assertEqual(first["recorded"], 2)
            self.assertEqual(first["skipped_non_auto"], 1)

            # Re-running on same parent OR child session_id must NOT duplicate
            second = self.hook.process_stop_event(payload, codex_home=home)
            self.assertEqual(second["recorded"], 0)
            third = self.hook.process_stop_event(
                {"session_id": "child-terra-0001", "hook_event_name": "Stop"},
                codex_home=home,
            )
            self.assertEqual(third["recorded"], 0)

            state_dir = home / "router_v5_live"
            decisions = [
                json.loads(line)
                for line in (state_dir / "live_decisions.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(decisions), 2)
            self.assertEqual(decisions[0]["tokens_used"], 420000)
            self.assertEqual(decisions[0]["elapsed_seconds"], 45.0)
            self.assertEqual(decisions[1]["v5_live_decision"]["route"], {"family": "gpt-6.2-sol", "effort": "medium"})
            self.assertEqual(decisions[1]["v5_live_decision"]["reason_codes"], ["svg_layout"])

            records = OutcomeRegistry(state_dir / "outcome_registry.jsonl").read()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0].tokens_if_observable, 420000)
            self.assertEqual(records[1].elapsed_time_if_observable, 50.0)
            self.assertFalse(records[0].structural_profile_complete)

            report = audit(state_dir, checkpoint=5)
            self.assertEqual(report["counts"]["decision_count"], 2)
            self.assertNotIn("INVALID_AUTOMATIC_ROUTE_RECOMMEND_ROLLBACK", report["flags"])
            self.assertNotIn("AUTOMATIC_ASTRA_RECOMMEND_ROLLBACK", report["flags"])

    def test_cold_start_without_session_id_initializes_watermark_without_backfill(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            db = home / "state_5.sqlite"
            _create_mock_db(db)
            _insert_thread(
                db,
                tid="old-child-0001",
                created_at=1795500000,
                updated_at=1795500030,
                model="gpt-6.1-terra",
                effort="medium",
                role="worker",
                tokens=12345,
                cwd=r"C:\work\old",
                parent_id="old-root",
            )
            init_res = self.hook.process_stop_event({}, codex_home=home)
            self.assertEqual(init_res["recorded"], 0)
            self.assertEqual(init_res["initialized_watermark"], 1795500000)
            self.assertFalse((home / "router_v5_live" / "live_decisions.jsonl").exists())

            # A newer subagent after watermark is captured
            _insert_thread(
                db,
                tid="new-child-0002",
                created_at=1795500100,
                updated_at=1795500120,
                model="gpt-6-luna",
                effort="high",
                role="worker",
                tokens=22222,
                cwd=r"C:\work\new",
                parent_id="new-root",
            )
            next_res = self.hook.process_stop_event({"hook_event_name": "Stop"}, codex_home=home)
            self.assertEqual(next_res["recorded"], 1)

    def test_main_never_prints_stdout_or_fails_on_bad_stdin(self):
        for raw_stdin in ("{bad json", "", json.dumps({"hook_event_name": "PreToolUse"})):
            with self.subTest(raw_stdin=raw_stdin):
                buf = io.StringIO()
                with patch("sys.stdin", io.StringIO(raw_stdin)), redirect_stdout(buf):
                    code = self.hook.main()
                self.assertEqual(code, 0)
                self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
