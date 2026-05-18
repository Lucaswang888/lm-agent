# CheckerReport v2 Schema

`mini-migrate-tools check` emits a deterministic `CheckerReport` JSON document. It is designed for the migration agent to consume after every edit batch.

Top-level fields:

- `verifier_version`: schema version. Current value is `v2.0`.
- `summary`: compact status with `passed`, `layer_failed`, `blocker_count`, `warning_count`, and `elapsed_seconds`.
- `layers`: ordered per-layer results for L0 through L4.
- `failures`: flattened actionable issues. Each failure has `id`, `layer`, `category`, `severity`, `file`, `line`, `evidence`, `diagnosis`, `suggested_fix`, and `rule_ref`.
- `passes`: checks that passed, so the agent avoids undoing correct work.
- `coverage`: dynamic validation coverage summary.
- `escalation`: next action hint and whether human review is required.

Layer meanings:

- `L0`: environment and scan-surface probe. Includes `.py`, Python shebang scripts in `bin/`, `scripts/`, `tools/`, and console-script modules from project metadata.
- `L1`: AST/static checker. Reports source-library residue, syntax errors, target import-path mistakes, framework-specific semantic rules, and PyMigBench API checklist failures.
- `L2`: isolated import-smoke checker for safe importable modules.
- `L3`: project validation command runner.
- `L4`: optional behaviour-diff hook, disabled by default.

Failure categories follow the PIG taxonomy where possible:

- `INC`: incorrect target import path or API path.
- `SEM`: semantic/runtime behaviour regression.
- `MIG`: migration incomplete; source-library residue remains.
- `MIN`: minimally invalid Python, undefined names, or import-time errors.
- `ENV`: environment or dependency metadata issue.
- `COV`: coverage/test-oracle gap.

Agent contract:

- Fix all `severity=blocker` failures before completion.
- Inspect `suggested_fix.edit_targets` before editing.
- For `suggested_fix.kind=rename_import`, prefer `candidates[0]` unless file evidence contradicts it.
- Do not claim completion while `summary.passed == false` or `summary.layer_failed != null`.
