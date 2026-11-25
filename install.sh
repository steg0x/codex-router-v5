#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_HOME="${HOME}/.codex"
POLICY_ONLY=0
NO_TELEMETRY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --codex-home)
      if [[ $# -lt 2 ]]; then
        echo "Error: --codex-home requires a path argument" >&2
        exit 1
      fi
      CODEX_HOME="$2"
      shift 2
      ;;
    --policy-only)
      POLICY_ONLY=1
      shift
      ;;
    --no-telemetry)
      NO_TELEMETRY=1
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--codex-home PATH] [--policy-only] [--no-telemetry]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is required to run install.sh cleanly." >&2
  if [[ "$NO_TELEMETRY" -eq 0 ]]; then
    echo "Tip: install Python 3 or re-run after installing python3." >&2
  fi
  exit 1
fi

export REPO_DIR CODEX_HOME POLICY_ONLY NO_TELEMETRY

python3 - <<'PY'
import os
import re
import shutil
from pathlib import Path

repo_dir = Path(os.environ["REPO_DIR"])
codex_home = Path(os.environ["CODEX_HOME"]).expanduser()
policy_only = os.environ["POLICY_ONLY"] == "1"
no_telemetry = os.environ["NO_TELEMETRY"] == "1"

agents_dir = codex_home / "agents"
agents_dir.mkdir(parents=True, exist_ok=True)

copied = []
for name in ("worker.toml", "strong-worker.toml", "reviewer.toml"):
    src = repo_dir / "config" / "agents" / name
    if src.is_file():
        shutil.copy2(src, agents_dir / name)
        copied.append(name)

legacy_router = agents_dir / "router.toml"
if legacy_router.exists():
    legacy_router.unlink()

policy_src = repo_dir / "config" / "openai-delegation-v5.md"
policy_text = policy_src.read_text(encoding="utf-8")
policy_text = policy_text.replace("{{ROUTER_DIR}}", str(repo_dir))
policy_text = policy_text.replace("{{CODEX_HOME}}", str(codex_home))

if no_telemetry:
    marker = "## Lightweight telemetry"
    idx = policy_text.find(marker)
    if idx != -1:
        policy_text = policy_text[:idx].rstrip()

policy_text = policy_text.rstrip()
block = f"<!-- CODEX-TOKEN-MINIMAL START -->\n{policy_text}\n<!-- CODEX-TOKEN-MINIMAL END -->"

agents_md = codex_home / "AGENTS.md"
existing = agents_md.read_text(encoding="utf-8") if agents_md.is_file() else ""
pattern = re.compile(r"<!-- CODEX-TOKEN-MINIMAL START -->.*?<!-- CODEX-TOKEN-MINIMAL END -->", re.DOTALL)
if pattern.search(existing):
    new_agents_md = pattern.sub(lambda _: block, existing)
elif not existing.strip():
    new_agents_md = block
else:
    new_agents_md = existing.rstrip() + "\n\n" + block

agents_md.write_text(new_agents_md.rstrip() + "\n", encoding="utf-8")

def set_key_in_block(text: str, key: str, line: str) -> str:
    pat = re.compile(rf"(?m)^\s*{re.escape(key)}\s*=.*$")
    if pat.search(text):
        return pat.sub(lambda _: line, text, count=1)
    stripped = text.rstrip("\r\n")
    return (stripped + "\n" + line + "\n") if stripped.strip() else (line + "\n")

def set_section_keys(text: str, section: str, key_lines: dict[str, str]) -> str:
    pat = re.compile(rf"(?ms)^(\[{re.escape(section)}\]\s*\n)(.*?)(?=^\[|\Z)")
    m = pat.search(text)
    if m:
        header, body = m.group(1), m.group(2)
        for k, v in key_lines.items():
            body = set_key_in_block(body, k, v)
        repl = header + body.rstrip("\r\n") + "\n\n"
        return text[:m.start()] + repl + text[m.end():]
    lines = [f"[{section}]", *key_lines.values()]
    return text.rstrip("\r\n") + "\n\n" + "\n".join(lines) + "\n"

config_patched = False
if not policy_only:
    cfg_path = codex_home / "config.toml"
    cfg_text = ""
    if cfg_path.is_file():
        shutil.copy2(cfg_path, codex_home / "config.toml.bak")
        cfg_text = cfg_path.read_text(encoding="utf-8")

    sec = re.search(r"(?m)^\[", cfg_text)
    if sec:
        preamble, rest = cfg_text[:sec.start()], cfg_text[sec.start():]
    else:
        preamble, rest = cfg_text, ""

    preamble = set_key_in_block(preamble, "model_provider", 'model_provider = "openai"')
    preamble = set_key_in_block(preamble, "model", 'model = "gpt-6-luna"')
    preamble = set_key_in_block(preamble, "model_reasoning_effort", 'model_reasoning_effort = "medium"')

    combined = preamble.rstrip("\r\n") + ("\n\n" + rest.lstrip("\r\n") if rest.strip() else "\n")
    combined = set_section_keys(combined, "features", {"multi_agent": "multi_agent = true"})
    combined = set_section_keys(combined, "agents", {
        "default_subagent_model": 'default_subagent_model = "gpt-6-luna"',
        "default_subagent_reasoning_effort": 'default_subagent_reasoning_effort = "medium"',
    })
    cfg_path.write_text(combined.lstrip("\r\n").rstrip("\r\n") + "\n", encoding="utf-8")
    config_patched = True

print("\nCodex Router V5.1 installato con successo!")
print(f"  Codex Home   : {codex_home}")
print(f"  Sub-agenti   : {', '.join(copied)}")
print(f"  AGENTS.md    : aggiornato ({'senza telemetria' if no_telemetry else 'con telemetria'})")
print(f"  config.toml  : {'aggiornato (backup .bak salvato se preesistente)' if config_patched else 'saltato (--policy-only)'}")
print("\nProssimo passo: riavvia Codex e apri una NUOVA chat (consigliato: permessi 'Accesso completo').")
PY
