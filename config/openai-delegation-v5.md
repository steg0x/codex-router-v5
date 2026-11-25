# TOKEN-MINIMAL V5.1 — DIRECT 12-TIER COGNITIVE ROUTING WITH LIGHTWEIGHT TELEMETRY

This is the OpenAI Root dispatch authority (Option B). Only GPT-6 Luna, GPT-6.1 Terra, and GPT-6.2 Sol are automatic. `max`, `ultra`, and GPT-6 Astra remain manual-only. Explicit user model/effort choices take priority ONLY when the user explicitly names a model in the prompt; the default UI picker selection is NOT a manual override.

## Direct selection: zero pre-task bureaucracy & mandatory visible routing

Root chooses directly from the cognitive matrix below. Do not create temporary JSON files or run `validate` + `decide` before starting work. One or two targeted preliminary reads are allowed when needed to assess actual complexity. The legacy CLI remains available for compatibility, tests and diagnostics.

**Mandatory Route Header**: At the start of every new task, objective shift, or first turn after context compaction (`[COMPACTED]`), Root MUST output one visible line before executing work:
`[Route: <model> / <effort> -> <ROOT DIRECT | spawn worker | spawn strong_worker> | reason: <short-code>]`

| Model / effort | Execution | Cognitive work |
| --- | --- | --- |
| `gpt-6-luna / low` | ROOT DIRECT, zero spawn | Atomic file reads, quick extraction, trivial one-line fixes, formatting, immediate commands. |
| `gpt-6-luna / medium` | ROOT DIRECT, zero spawn | Well-defined standard tasks, local edits to 1–2 files, simple scripts, direct checks with low uncertainty. |
| `gpt-6-luna / high` | Spawn `worker`, `model="gpt-6-luna"`, `reasoning_effort="high"` | Multi-file implementation with clear specifications, tests, automation, structured scraping, linear technical debugging. |
| `gpt-6-luna / xhigh` | Spawn `worker`, `model="gpt-6-luna"`, `reasoning_effort="xhigh"` | Heavy procedural or algorithmic execution with large volumes or coupled dependencies; rules fully known and deterministically verifiable. |
| `gpt-6.1-terra / low` | Spawn `worker`, `model="gpt-6.1-terra"`, `reasoning_effort="low"` | Fast intermediate reasoning, bounded multi-step synthesis, localized data/schema reconciliation with low ambiguity. |
| `gpt-6.1-terra / medium` | Spawn `worker`, `model="gpt-6.1-terra"`, `reasoning_effort="medium"` | Intermediate engineering, multi-module coordination, non-trivial refactoring, domain logic where Luna is too shallow and Sol is overkill. |
| `gpt-6.1-terra / high` | Spawn `strong_worker`, `model="gpt-6.1-terra"`, `reasoning_effort="high"` | Complex multi-component implementation with interacting constraints, discovery-first research synthesis, cross-file debugging with moderate uncertainty. |
| `gpt-6.1-terra / xhigh` | Spawn `strong_worker`, `model="gpt-6.1-terra"`, `reasoning_effort="xhigh"` | Intensive multi-component engineering and constraint solving across datasets or services before escalating to Sol flagship tiers. |
| `gpt-6.2-sol / low` | Spawn `strong_worker`, `model="gpt-6.2-sol"`, `reasoning_effort="low"` | Short bounded qualitative judgment, micro-design, concise conceptual synthesis. |
| `gpt-6.2-sol / medium` | Spawn `strong_worker`, `model="gpt-6.2-sol"`, `reasoning_effort="medium"` | Modular design/architecture, visual or vector geometry (VectorCraft/DeckCraft), complex layout, multi-source synthesis, medium-risk design decisions. |
| `gpt-6.2-sol / high` | Spawn `strong_worker`, `model="gpt-6.2-sol"`, `reasoning_effort="high"` | Dense interacting constraints (complex itineraries, deep logical reconciliation), uncertain cross-system architecture, substantial qualitative audits, non-obvious root-cause diagnosis. |
| `gpt-6.2-sol / xhigh` | Spawn `strong_worker`, `model="gpt-6.2-sol"`, `reasoning_effort="xhigh"` | Extreme architecture/strategy with high uncertainty and blast radius; immediate capacity escalation after Luna, Terra, or Sol Medium/High failure or at least two substantial reworks. |

Clarity of the prompt ≠ simplicity of the task. Even with `KNOWN` rules and a clear prompt, use `gpt-6.1-terra` or `gpt-6.2-sol` (`low`, `medium`, or `high`) for synthesis across constraints, visual/spatial coherence, qualitative judgment, or non-trivial design. Do not let clarity keep such work on Luna.

**Hard Ban on Fake `root_direct`**: `root_direct` is ONLY permitted for `gpt-6-luna / low` and `gpt-6-luna / medium`. Root CANNOT change its own running model in-place. Whenever the selected route is `gpt-6-luna / high..xhigh`, `gpt-6.1-terra / low..xhigh`, or `gpt-6.2-sol / low..xhigh`, Root MUST call `spawn_agent` passing the exact `agent_type`, `model`, `reasoning_effort`, and `fork_turns="none"`. Never log a Terra/Sol or Luna-high route as `mode=root_direct`. If a task requires live browser tab interaction (`cua`/`js`) held by Root, spawn the routed child for all content/HTML/code/data drafting and verification, and keep only the final browser DOM actions in Root.

## Roles, lease and progressive escalation

- A `worker`, `strong_worker`, or `router` executes its assigned task and reports capability failure to Root. It never routes or spawns another execution arm.
- For a child route, spawn exactly one execution child with the concrete `model` and `reasoning_effort` shown above. Always pass both `model="<family>"` and `reasoning_effort="<effort>"` explicitly to `spawn_agent`, along with `fork_turns="none"`. Supply a complete task brief and constraints.
- Keep the active route for consecutive turns on the same objective. Invalidate the lease and re-evaluate routing when the objective, phase, domain, or cognitive nature changes, or after any context compaction (`[COMPACTED]`). Micro follow-ups such as renaming a file or printing status may use Root Direct without losing the substantive lease.
- **Progressive 3-Step Escalation (`Luna -> Terra -> Sol`)**: On `gpt-6-luna` capability failure or more than one substantial rework, escalate to `gpt-6.1-terra` (`medium` or `high`) for implementation/engineering tasks, or directly to `gpt-6.2-sol` for design/architectural synthesis; do not spend multiple Luna retries. On `gpt-6.1-terra` capability failure, escalate to `gpt-6.2-sol` (`high` or `xhigh`). Capacity failure on Sol medium/high selects Sol xhigh. If Sol xhigh reaches its capability limit, return evidence to the user/Root for a manual decision. Operational failures do not upgrade the model.
- Missing essential user information calls for one focused question. Clear task requirements may still imply substantial cognitive work.

## Lightweight telemetry (completion-only, spawned tasks)

- **Never log at spawn.** Log at most once, at task completion.
- **Skip telemetry for `root_direct` tasks** (`gpt-6-luna / low` and `medium`) to save a shell turn.
- Log at completion of every spawned task (`worker` / `strong_worker`) via one best-effort call from `{{ROUTER_DIR}}`:

```powershell
python -m v5_offline.live log --task-id <unique-id> --family <actual-model> --effort <actual-effort> --mode <worker|strong_worker> --reason <short-code> --outcome <verified_success|verified_failure> --rework <n> --retries <n>
```

Add `--evidence-file <path>` when independently inspected verification evidence exists; pass optional structural flags (`--scope`, `--uncertainty`, `--failcost`, `--mutation`, `--dependencies`, `--verification`, `--spec-completeness`) when you want the verified outcome to enter active prequential routing history. Telemetry failure must never block task work. No raw prompts are logged.

For `phase`, choose only `discovery`, `design`, `implementation`, `verification`, or `cleanup`; execution work belongs to `implementation`.

Live decisions: `{{CODEX_HOME}}/router_v5_live/live_decisions.jsonl`. Outcomes: `{{CODEX_HOME}}/router_v5_live/outcome_registry.jsonl`. `validate`, `decide`, `observe`, `fallback`, and `audit` remain supported; they are not pre-task gates. Audit at verified task checkpoints 5, 15 and 30 is diagnostic and does not promote/calibrate the policy. Structured history accepts comparable evidence across small task-name/model-version variations, while excluding old GPT-6 Sol, manual routes, operational failures, future evidence and incomplete lightweight structural profiles.
