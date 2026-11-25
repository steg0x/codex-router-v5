"""Conservative historical import: validate provenance, preserve model identity, dry-run by default."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .registry import OutcomeRecord, OutcomeRegistry


def load_jsonl(path: str | Path) -> list[OutcomeRecord]:
    rows = []
    with Path(path).open(encoding="utf-8") as source:
        for line_no, line in enumerate(source, 1):
            if line.strip():
                try:
                    rows.append(OutcomeRecord.from_dict(json.loads(line)))
                except (TypeError, ValueError, KeyError) as exc:
                    raise ValueError(f"source line {line_no}: {exc}") from exc
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or append verified outcome JSONL; dry-run default")
    parser.add_argument("source", help="JSONL conforming to OutcomeRecord schema")
    parser.add_argument("destination", help="Outcome Registry JSONL path")
    parser.add_argument("--commit", action="store_true", help="append validated records")
    args = parser.parse_args(argv)
    rows = load_jsonl(args.source)
    real = sum(r.evidence_kind == "REAL_VERIFIED" for r in rows)
    synthetic = len(rows) - real
    if args.commit:
        registry = OutcomeRegistry(args.destination)
        existing = {(r.task_id, r.timestamp, r.actual_model) for r in registry.read()}
        for row in rows:
            key = (row.task_id, row.timestamp, row.actual_model)
            if key in existing:
                raise ValueError(f"duplicate outcome: {row.task_id}")
            existing.add(key)
        registry.append_many(rows)
    print(json.dumps({"validated": len(rows), "real_verified": real,
                      "synthetic_fixture": synthetic, "committed": args.commit}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
