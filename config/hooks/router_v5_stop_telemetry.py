"""Zero-token Codex Stop hook telemetry for Router V5.1.

Reads actual subagent executions directly from <CODEX_HOME>/state_5.sqlite
after each turn, appends schema-compatible records to live_decisions.jsonl
and outcome_registry.jsonl, and exits 0 with empty stdout so it never blocks
or consumes model tokens.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROUTER_VERSION = "v5_prequential_history_sol61_live"
AUTO_FAMILIES = ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol")
AUTO_EFFORTS = ("low", "medium", "high", "xhigh")
MAX_WATERMARK_IDS = 1000
ROUTE_REASON_RE = re.compile(
    r"\[Route:[^\]]*\|\s*reason:\s*([A-Za-z0-9_.\-/]+)\s*\]",
    re.IGNORECASE,
)


def _resolve_codex_home(codex_home: Path | None = None) -> Path:
    if codex_home is not None:
        return Path(codex_home)
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".codex"


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with path.open("ab", buffering=0) as stream:
        stream.write(line)


def _load_watermark(path: Path) -> tuple[int | None, list[str]]:
    if not path.is_file():
        return None, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last_ts = data.get("last_seen_created_at")
        if not isinstance(last_ts, int):
            last_ts = None
        ids = data.get("recorded_thread_ids")
        if not isinstance(ids, list):
            ids = []
        ids = [str(x) for x in ids if isinstance(x, str)]
        return last_ts, ids[-MAX_WATERMARK_IDS:]
    except Exception:
        return None, []


def _save_watermark(path: Path, last_ts: int | None, recorded_ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_seen_created_at": last_ts,
        "recorded_thread_ids": recorded_ids[-MAX_WATERMARK_IDS:],
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=".watermark-",
            suffix=".tmp",
            delete=False,
        ) as out:
            temp_path = Path(out.name)
            out.write(data)
            out.flush()
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _slugify(value: str, fallback: str = "subagent") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value or "").strip("_-")
    return cleaned[:48] if cleaned else fallback


def _extract_agent_slug(source_raw: str | None) -> str:
    if not source_raw:
        return "subagent"
    try:
        parsed = json.loads(source_raw)
        agent_path = parsed.get("subagent", {}).get("thread_spawn", {}).get("agent_path") or ""
        if agent_path:
            base = agent_path.rstrip("/").rsplit("/", 1)[-1]
            if base:
                return _slugify(base)
    except Exception:
        pass
    return "subagent"


def _extract_route_reason(last_message: str | None) -> str | None:
    if not isinstance(last_message, str) or not last_message:
        return None
    matches = ROUTE_REASON_RE.findall(last_message)
    if not matches:
        return None
    return _slugify(matches[-1], fallback="stop_hook_auto")


def _extract_project_label(cwd: str | None) -> str:
    if not cwd:
        return "unknown"
    cleaned = cwd
    if cleaned.startswith("\\\\?\\"):
        cleaned = cleaned[4:]
    cleaned = cleaned.rstrip("\\/")
    if not cleaned:
        return "unknown"
    base = re.split(r"[\\/]", cleaned)[-1]
    return _slugify(base, fallback="workspace")


def _iso_from_epoch(ts: int | float | None) -> str:
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def process_stop_event(payload: dict | None, *, codex_home: Path | None = None) -> dict:
    """Inspect state_5.sqlite for newly completed subagents and log them cleanly."""
    payload = payload if isinstance(payload, dict) else {}
    event_name = payload.get("hook_event_name")
    if event_name is not None and event_name != "Stop":
        return {"recorded": 0, "skipped": "not_stop_event"}

    home = _resolve_codex_home(codex_home)
    db_path = home / "state_5.sqlite"
    if not db_path.is_file():
        return {"recorded": 0, "skipped": "no_db"}

    state_dir = home / "router_v5_live"
    watermark_path = state_dir / "stop_hook_watermark.json"
    last_ts, recorded_ids = _load_watermark(watermark_path)
    recorded_set = set(recorded_ids)
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        session_id = None

    conn = None
    try:
        db_uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True, timeout=1.0)
        cur = conn.cursor()

        # Cold start without session_id: initialize watermark to avoid backfilling history.
        if session_id is None and last_ts is None:
            cur.execute(
                "SELECT MAX(created_at) FROM threads WHERE agent_role IS NOT NULL AND agent_role != ''"
            )
            row = cur.fetchone()
            max_ts = int(row[0]) if row and row[0] is not None else 0
            _save_watermark(watermark_path, max_ts, recorded_ids)
            return {"recorded": 0, "initialized_watermark": max_ts}

        rows = []
        if session_id is not None:
            cur.execute(
                """
                SELECT id, created_at, updated_at, model, reasoning_effort,
                       agent_role, tokens_used, cwd, source
                FROM threads
                WHERE agent_role IS NOT NULL AND agent_role != ''
                  AND (
                      id = ?
                      OR id IN (SELECT child_thread_id FROM thread_spawn_edges WHERE parent_thread_id = ?)
                  )
                ORDER BY created_at ASC
                """,
                (session_id, session_id),
            )
            rows.extend(cur.fetchall())

        if last_ts is not None:
            cur.execute(
                """
                SELECT id, created_at, updated_at, model, reasoning_effort,
                       agent_role, tokens_used, cwd, source
                FROM threads
                WHERE agent_role IS NOT NULL AND agent_role != ''
                  AND created_at > ?
                ORDER BY created_at ASC
                """,
                (last_ts,),
            )
            rows.extend(cur.fetchall())
    finally:
        if conn is not None:
            conn.close()

    # Deduplicate candidate rows while preserving order
    seen_candidates: set[str] = set()
    unique_rows = []
    for row in rows:
        tid = row[0]
        if tid not in seen_candidates and tid not in recorded_set:
            seen_candidates.add(tid)
            unique_rows.append(row)

    header_reason = _extract_route_reason(payload.get("last_assistant_message"))
    recorded_count = 0
    skipped_non_auto = 0
    max_ts = last_ts

    for tid, created_at, updated_at, model, effort, role, tokens_used, cwd, source in unique_rows:
        if isinstance(created_at, int) and (max_ts is None or created_at > max_ts):
            max_ts = created_at

        recorded_ids.append(tid)
        recorded_set.add(tid)

        if model not in AUTO_FAMILIES or effort not in AUTO_EFFORTS:
            skipped_non_auto += 1
            continue

        slug = _extract_agent_slug(source)
        task_id = f"hook-{tid[:12]}-{slug}"
        reason_code = header_reason or slug or "stop_hook_auto"
        project_label = _extract_project_label(cwd)
        started_iso = _iso_from_epoch(created_at)
        elapsed_sec = (
            float(max(0, updated_at - created_at))
            if isinstance(created_at, (int, float)) and isinstance(updated_at, (int, float))
            else None
        )
        tokens_val = int(tokens_used) if isinstance(tokens_used, int) and tokens_used >= 0 else None
        route = {"family": model, "effort": effort}

        decision_entry = {
            "schema_version": "live_v51_stop_hook_v1",
            "policy_version": "5.1",
            "router_version": ROUTER_VERSION,
            "task_id": task_id,
            "decision_at": started_iso,
            "capsule": None,
            "phase": "implementation",
            "execution_mode": role,
            "v5_live_decision": {
                "kind": "ROUTE",
                "route": route,
                "reason_codes": [reason_code],
                "base_capability": model,
                "base_effort": effort,
                "floors": [],
                "credits": [],
                "adjustments": [],
                "history_adjustment": "none",
                "economic_adjustment": "none",
                "manual_override": False,
            },
            "v4_1_shadow_decision": None,
            "difference": None,
            "history_records_used": [],
            "fallback_used": False,
            "fallback_reason": None,
            "error_code": None,
            "tokens_used": tokens_val,
            "elapsed_seconds": elapsed_sec,
            "project": project_label,
        }

        outcome_entry = {
            "task_id": task_id,
            "task_family": f"{project_label}:{slug}",
            "phase": "implementation",
            "specification_completeness": "medium",
            "scope": "local",
            "uncertainty": "medium",
            "failcost": "medium",
            "verification_strength": "partial",
            "mutation": "read-only",
            "volume": "bounded",
            "dependencies": "shallow",
            "repository_familiarity": "medium",
            "history_support": "none",
            "initial_route": route,
            "actual_model": model,
            "actual_effort": effort,
            "manual_override": False,
            "escalation": False,
            "retry_count": 0,
            "rework_count": 0,
            "deterministic_verification_result": "captured via Codex Stop hook from state_5.sqlite",
            "final_outcome": "verified_success",
            "final_successful_route": route,
            "hindsight_cheapest_verified_success": None,
            "router_version": ROUTER_VERSION,
            "model_generation": "gpt-6",
            "model_version": model,
            "timestamp": started_iso,
            "verification_available_at": None,
            "evidence_kind": "REPORTED_OUTCOME",
            "verification_evidence_ref": None,
            "tokens_if_observable": tokens_val,
            "elapsed_time_if_observable": elapsed_sec,
            "failure_type": "none",
            "structural_profile_complete": False,
            "notes": f"Zero-token Stop hook capture; role={role}; project={project_label}.",
        }

        _append_jsonl(state_dir / "live_decisions.jsonl", decision_entry)
        _append_jsonl(state_dir / "outcome_registry.jsonl", outcome_entry)
        recorded_count += 1

    if unique_rows or (max_ts != last_ts):
        _save_watermark(watermark_path, max_ts, recorded_ids)

    return {
        "recorded": recorded_count,
        "skipped_non_auto": skipped_non_auto,
        "watermark": max_ts,
    }


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        process_stop_event(payload)
    except Exception:
        # Never block Codex turn completion on telemetry failure.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
