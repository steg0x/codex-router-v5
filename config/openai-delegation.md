# TOKEN-MINIMAL V4.1 - CONDITIONAL FEATURE ROUTER

## 1. MANUAL OVERRIDE & RECURSION GUARD
- Explicit user model/effort choice takes immediate priority; do not route. Astra is MANUAL ONLY.
- **Router recursion guard**: If current role is outer, do not route or delegate; execute only router instructions and return the 3-field verdict.
- **Execution role exception**: If current role is worker or strong_worker, do NOT apply root dispatch logic. Do NOT call the router. Do NOT spawn another execution worker. Execute the assigned task directly. (For capability failure, return evidence to parent; do not self-escalate).

## 2. DISPATCH PROTOCOL
- **Pre-Router Task-Spec Hydration**: Root may read ONLY the explicit task specification (prompt, brief, explicitly attached file) BEFORE calling the router. Do NOT start solving, exploring the repository, or tracing code before routing.
- **Atomic Fast Path**: If manifestly atomic (single bounded operation, explicit instruction, predetermined criteria, no design/strategy, immediate verification: read file, simple edit, rename, run script, extract fields), execute directly on root gpt-6-luna / medium. Do NOT spawn router or worker. In doubt, call router.
- **New Non-Trivial Task**: The root's FIRST action must be:
  spawn_agent(agent_type="router", model="gpt-6-luna", reasoning_effort="medium", fork_turns="none", message=<capsule>)
  Pass only objective, constraints, known rules, essential context.
- **Deterministic Mapping**:
  - RULE_STATUS: MISSING -> **CLARIFY** (Ask 1 focused question; no execution child. Do NOT reinterpret as KNOWN).
  - KNOWN + STANDARD -> **ROOT DIRECT** (gpt-6-luna / medium; no execution child).
  - KNOWN + HIGH -> **SPAWN WORKER** (gpt-6-luna / high, fork_turns="none").
  - DEFINE + STANDARD -> **SPAWN STRONG_WORKER** (gpt-6.2-sol / medium, fork_turns="none").
  - DEFINE + HIGH -> **SPAWN STRONG_WORKER** (gpt-6.2-sol / high, fork_turns="none").
  - *Malformed fallback*: Default safely to worker (gpt-6-luna / high).
- **Execution Guard**: NONE (normal); VALIDATE_FIRST (dry-run/schema check); CHECKPOINT (snapshot before batch writes); USER_CONFIRM (confirm only if unapproved destruction, publish, or paid API spend). Operational risk NEVER upgrades the model.

## 3. ROUTE LEASE & HYSTERESIS
- **Reuse active route**: Reuse previous route if prompt continues same objective, rubric, or batch. Do not re-route every turn.
- **Atomic continuation override**: If a follow-up during a complex task is atomic ("rinomina file"), execute on root without spawning Sol/router, preserving active lease.
- **Invalidate lease**: Re-route if objective changes, cognitive nature shifts to DEFINE, domain changes, or lease lost after compaction. Do not oscillate models.

## 4. FAILURE & HARD CAPS
- **Operational failure** (syntax, path, timeout): DO NOT upgrade model. Max 1 local repair.
- **Capability failure** (reasoning deficit, constraint violation): Max ONE step escalation (Luna Med -> Luna High -> Sol Med -> Sol High -> STOP).
- **Hard caps**: Max 1 successful router call per phase. Max 1 active execution child. Max 1 capability escalation. Zero automatic reviewers. Zero nested delegation.
