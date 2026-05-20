#!/usr/bin/env python3

"""Run mini-SWE-agent as a structured Python library migration agent."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console

from minisweagent import global_config_dir
from minisweagent.agents import get_agent
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.migration.context import api_changes_from_pymigbench, build_pig_context, render_pig_prompt_summary
from minisweagent.migration.pig_models import PigContext
from minisweagent.migration.verification import VerificationReport, verify_project_migration
from minisweagent.models import get_model
from minisweagent.run.utilities.config import configure_if_first_time
from minisweagent.utils.serialize import UNSET, recursive_merge

DEFAULT_CONFIG_FILES = [
    str(builtin_config_dir / "mini.yaml"),
    str(builtin_config_dir / "migration.yaml"),
]
DEFAULT_OUTPUT_FILE = global_config_dir / "last_migration_run.traj.json"

_CONFIG_SPEC_HELP_TEXT = """Path to config files, filenames, or key-value pairs.

The default migration run loads mini.yaml and migration.yaml. If you set this
option, include both defaults explicitly when you still want the migration prompt.
"""

console = Console(highlight=False)
app = typer.Typer(rich_markup_mode="rich", add_completion=False)


def load_pymigbench_yaml(path: Path) -> dict[str, Any]:
    """Load one PyMigBench migration YAML file."""
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        msg = f"Expected a mapping in PyMigBench YAML file: {path}"
        raise ValueError(msg)
    return data


def _format_list(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values) if values else "- None provided"


def _format_pymigbench_hints(data: dict[str, Any]) -> str:
    """Render compact migration hints from a PyMigBench migration record."""
    if not data:
        return "No PyMigBench migration record was provided."

    lines = [
        "PyMigBench migration record:",
        f"- Repository: {data.get('repo', 'unknown')}",
        f"- Commit: {data.get('commit', 'unknown')}",
        f"- Domain: {data.get('domain', 'unknown')}",
        f"- Reference commit URL: {data.get('commit_url', 'unknown')} "
        "(context only; do not clone or replace the project)",
        "- Migration-related files and API changes:",
    ]
    for file_record in data.get("files", []) or []:
        path = file_record.get("path", "unknown")
        lines.append(f"  - {path}")
        for change in file_record.get("code_changes", []) or []:
            source_apis = ", ".join(change.get("source_apis", []) or ["unknown"])
            target_apis = ", ".join(change.get("target_apis", []) or ["unknown"])
            properties = ", ".join(change.get("properties", []) or ["none"])
            lines.append(
                f"    - line {change.get('line', 'unknown')}: {source_apis} -> {target_apis}; "
                f"properties: {properties}"
            )
    return "\n".join(lines)


def _record_terms(data: dict[str, Any]) -> set[str]:
    terms = {data.get("source", ""), data.get("target", "")}
    for file_record in data.get("files", []) or []:
        terms.add(file_record.get("path", ""))
        for change in file_record.get("code_changes", []) or []:
            terms.update(change.get("source_apis", []) or [])
            terms.update(change.get("target_apis", []) or [])
            terms.update(change.get("properties", []) or [])
    return {term for term in terms if term}


def _score_pymigbench_example(current: dict[str, Any], candidate: dict[str, Any]) -> int:
    score = 0
    if candidate.get("source") == current.get("source") and candidate.get("target") == current.get("target"):
        score += 100
    elif candidate.get("source") == current.get("source") or candidate.get("target") == current.get("target"):
        score += 20
    score += len(_record_terms(current) & _record_terms(candidate))
    score += min(len(candidate.get("files", []) or []), 5)
    return score


def get_pymigbench_examples(
    *,
    current_data: dict[str, Any],
    dataset_dir: Path | None,
    current_yaml: Path | None = None,
    count: int = 0,
) -> list[dict[str, Any]]:
    """Return similar PyMigBench records for retrieval-augmented migration context."""
    if not current_data or not dataset_dir or count <= 0 or not dataset_dir.exists():
        return []

    current_commit = current_data.get("commit")
    current_yaml_resolved = current_yaml.resolve() if current_yaml else None
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for path in sorted(dataset_dir.glob("*.yaml")):
        if current_yaml_resolved and path.resolve() == current_yaml_resolved:
            continue
        try:
            candidate = load_pymigbench_yaml(path)
        except ValueError:
            continue
        if candidate.get("commit") == current_commit and candidate.get("repo") == current_data.get("repo"):
            continue
        score = _score_pymigbench_example(current_data, candidate)
        if score <= 0:
            continue
        candidate["_example_yaml"] = path.name
        candidates.append((score, path.name, candidate))
    return [candidate for _, _, candidate in sorted(candidates, key=lambda item: (-item[0], item[1]))[:count]]


def _format_pymigbench_examples(examples: list[dict[str, Any]]) -> str:
    if not examples:
        return "No historical PyMigBench examples were provided."

    lines = ["Historical PyMigBench migration examples to imitate:"]
    for index, example in enumerate(examples, 1):
        lines.extend(
            [
                f"Example {index}: {example.get('source', 'unknown')} -> {example.get('target', 'unknown')}",
                f"- YAML: {example.get('_example_yaml', 'unknown')}",
                f"- Repository: {example.get('repo', 'unknown')}",
                f"- Commit: {example.get('commit', 'unknown')}",
                "- Patterns:",
            ]
        )
        pattern_count = 0
        for file_record in example.get("files", []) or []:
            path = file_record.get("path", "unknown")
            for change in file_record.get("code_changes", []) or []:
                source_apis = ", ".join(change.get("source_apis", []) or ["unknown"])
                target_apis = ", ".join(change.get("target_apis", []) or ["unknown"])
                properties = ", ".join(change.get("properties", []) or ["none"])
                lines.append(f"  - {path}: {source_apis} -> {target_apis}; properties: {properties}")
                pattern_count += 1
                if pattern_count >= 12:
                    break
            if pattern_count >= 12:
                lines.append("  - ...")
                break
    return "\n".join(lines)


def build_migration_task(
    *,
    project: Path,
    source: str,
    target: str,
    scopes: list[str],
    test_commands: list[str],
    notes: list[str],
    pymigbench_data: dict[str, Any] | None = None,
    pymigbench_examples: list[dict[str, Any]] | None = None,
    strategy: str = "default",
    pig_context: str | None = None,
    agent_automation_context: str | None = None,
    pig_report: Path | None = None,
    strict_static_check: bool = False,
    strict_report: Path | None = None,
) -> str:
    """Build the user-facing migration task sent to the agent."""
    hints = _format_pymigbench_hints(pymigbench_data or {})
    examples = _format_pymigbench_examples(pymigbench_examples or [])
    pig_section = f"\n{pig_context}\n" if pig_context else ""
    automation_section = f"\n{agent_automation_context}\n" if agent_automation_context else ""
    strict_lines = ""
    if strict_static_check:
        report_line = f" The harness will write the JSON report to `{strict_report}`." if strict_report else ""
        strict_lines = (
            "\nStrict API-level verification and one automatic repair pass are enabled after the agent exits."
            f"{report_line} Use every PIG/PyMigBench checklist item as a hard requirement, not as optional context.\n"
        )
    compatibility_notes = _format_compatibility_notes(source, target)
    return f"""Migrate this Python project from `{source}` to `{target}`.

Project root:
{project}

Project boundary:
- Work only in the project root shown above.
- Do not clone, replace, reset, or re-create the project.
- Treat PyMigBench URLs and commit metadata as reference context only.

Priority scope:
{_format_list(scopes)}

Validation commands to run after the migration:
{_format_list(test_commands)}

Additional user notes:
{_format_list(notes)}

Migration strategy:
- {strategy}

Compatibility notes:
{compatibility_notes}

{hints}

{examples}
{pig_section}
{automation_section}
PIG automation notes:
- PIG-style steps are available as agent-callable helper commands, not only as prompt text.
- Use discovery, slices, candidates, and verification during the migration when the project evidence is unclear.
- YAML, when provided, is benchmark/evaluation ground truth; it is not the normal user input.
{f"- Full PIG report path: `{pig_report}`" if pig_report else "- No full PIG report path was provided."}
{strict_lines}

Required outcome:
- Replace usage of the source library with the target library.
- Update imports, API calls, dependency declarations, tests, documentation snippets, and configuration only when they are relevant to the migration.
- Treat the priority scope as the starting point, not a hard file limit.
- Use the PIG helper tools as needed for API discovery, code slicing, target candidate lookup, and static/API verification.
- The precomputed migration plan is the first pass. Expand it with helper tools when it misses files or APIs.
- Treat every PyMigBench file and code_change entry as a mandatory migration checklist item.
- When an API checklist item includes exact source/target patterns, remove the source pattern and make the target pattern appear unless you have an explicit compatibility reason.
- Before finishing, inspect every PyMigBench file listed above and confirm all listed target APIs/patterns are present and relevant source APIs/patterns are gone.
- Always inspect common dependency declaration files such as pyproject.toml, setup.py, setup.cfg, requirements*.txt, tox.ini, environment.yml, and lock files when present.
- Preserve existing behavior unless the source and target libraries require an intentional compatibility adjustment.
- Run the provided validation commands. If no validation command was provided, discover and run the smallest relevant project tests or static checks available.
- Avoid broad chained rewrite commands over the whole repository. Inspect the files in the migration plan and make focused edits.
- Do not make unrelated refactors or formatting-only changes.
- Finish only after verification by issuing exactly this command on its own:
  echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
"""


def _format_compatibility_notes(source: str, target: str) -> str:
    if source.lower() == "flask" and target.lower() == "quart":
        return "\n".join(
            [
                "- Quart route handlers should be async functions.",
                "- Await Quart request body helpers such as request.get_json(...).",
                "- Do not await request.args; in Quart it is already a synchronous dict-like object.",
            ]
        )
    return "- No target-specific notes."


def _derive_scopes(scopes: list[str], pymigbench_data: dict[str, Any] | None) -> list[str]:
    if scopes or not pymigbench_data:
        return scopes
    return [file_record["path"] for file_record in pymigbench_data.get("files", []) or [] if file_record.get("path")]


def _discover_validation_commands(project: Path) -> list[str]:
    """Find small project-native validation commands for the agent to run."""
    commands: list[str] = []
    has_tests_dir = any((project / name).is_dir() for name in ("tests", "test"))
    has_pytest_config = any((project / name).exists() for name in ("pytest.ini", "tox.ini", "setup.cfg"))
    pyproject = project / "pyproject.toml"
    if pyproject.exists() and "pytest" in pyproject.read_text(errors="replace").lower():
        has_pytest_config = True
    if has_tests_dir or has_pytest_config:
        commands.append("python -m pytest")
    if not commands and (project / "manage.py").exists():
        commands.append("python manage.py test")
    return commands


def _discover_ecosystem_validation_commands(project: Path) -> list[str]:
    """Find ecosystem-native install/build gates that prove dependency migrations are reproducible."""
    commands: list[str] = []
    package_json = project / "package.json"
    if package_json.exists() and package_json.is_file():
        if (project / "package-lock.json").exists():
            commands.append("npm ci --legacy-peer-deps --ignore-scripts")
        else:
            commands.append("npm install --legacy-peer-deps --ignore-scripts")
        scripts = _package_json_scripts(package_json)
        if "build" in scripts:
            commands.append("npm run build")
        return commands
    return commands


def _package_json_scripts(package_json: Path) -> dict[str, str]:
    try:
        data = json.loads(package_json.read_text(errors="replace"))
    except json.JSONDecodeError:
        return {}
    scripts = data.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def _run_project_validation_commands(
    project: Path,
    commands: list[str],
    *,
    timeout_seconds: int = 300,
) -> list[dict[str, object]]:
    """Run validation commands and return compact evidence for the cross-review gate."""
    reports: list[dict[str, object]] = []
    for command in _dedupe_strings(commands):
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=project,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
            output = result.stdout + result.stderr
            reports.append(
                {
                    "command": command,
                    "returncode": result.returncode,
                    "passed": result.returncode == 0,
                    "output_tail": "\n".join(output.splitlines()[-80:]),
                }
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            reports.append(
                {
                    "command": command,
                    "returncode": None,
                    "passed": False,
                    "output_tail": "\n".join(output.splitlines()[-80:]),
                    "error": f"timed out after {timeout_seconds}s",
                }
            )
    return reports


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _dependency_files_with_mentions(project: Path, source: str, target: str) -> list[str]:
    dependency_names = (
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "tox.ini",
        "environment.yml",
        "environment.yaml",
        "Pipfile",
    )
    findings: list[str] = []
    for name in dependency_names:
        path = project / name
        if not path.exists() or not path.is_file():
            continue
        text = path.read_text(errors="replace")
        text_lower = text.lower()
        mentions: list[str] = []
        if source and source.lower() in text_lower:
            mentions.append(f"source `{source}`")
        if target and target.lower() in text_lower:
            mentions.append(f"target `{target}`")
        if mentions:
            findings.append(f"{name}: {', '.join(mentions)}")
    return findings


def _render_agent_automation_context(
    *,
    context: PigContext | None,
    project: Path,
    source: str,
    target: str,
    test_commands: list[str],
    discovered_test_commands: list[str],
) -> str:
    """Render the pre-agent automation plan that makes PIG outputs actionable."""
    lines = [
        "## Agent-owned automated migration plan",
        "",
        "The harness already performed the first PIG-style pass before this task reached you: "
        "source API discovery, focused code slicing, target API candidate ranking, and validation command discovery. "
        "You own the final decisions: inspect this evidence, edit the project, run checks, and repair failures.",
        "",
        f"Migration request: `{source}` -> `{target}`",
        f"Project root: `{project}`",
    ]
    if context:
        occurrence_count = len(context.occurrences)
        file_count = len({occurrence.file_path for occurrence in context.occurrences})
        complexity = "complex/project-level" if file_count > 3 or occurrence_count > 12 else "focused/API-level"
        lines.extend(
            [
                f"Initial classification: {complexity}",
                f"Discovered source occurrences: {occurrence_count} across {file_count} file(s)",
            ]
        )
        grouped: dict[str, list[str]] = {}
        for occurrence in context.occurrences:
            grouped.setdefault(occurrence.file_path, []).append(
                f"line {occurrence.line}: {occurrence.qualified_name or occurrence.api} | {occurrence.source_line.strip()}"
            )
        lines.extend(["", "Files to inspect/edit first:"])
        if grouped:
            for file_path, entries in list(grouped.items())[:12]:
                lines.append(f"- `{file_path}`")
                for entry in entries[:5]:
                    lines.append(f"  - {entry}")
                if len(entries) > 5:
                    lines.append(f"  - ... {len(entries) - 5} more occurrence(s)")
        else:
            lines.append("- No direct source occurrences were discovered; inspect imports, dependency files, and project configuration.")

        dependency_findings = _dependency_files_with_mentions(project, source, target)
        lines.extend(["", "Dependency/config files that mention the migration libraries:"])
        lines.extend(f"- {finding}" for finding in dependency_findings[:12]) if dependency_findings else lines.append(
            "- No dependency/config file mentioned the source or target library."
        )

        lines.extend(["", "Top target API candidates from the automated matcher:"])
        if context.candidates:
            for candidate in context.candidates[:8]:
                reason = "; ".join(candidate.reasons[:3]) or "ranked candidate"
                lines.append(f"- `{candidate.api}` score={candidate.score}: {reason}")
        else:
            lines.append("- No target API candidates were ranked; infer replacements from target docs or installed API shape.")

        lines.extend(["", "Focused code slices to review before editing:"])
        if context.slices:
            for code_slice in _dedupe_slices(context.slices)[:8]:
                lines.extend(
                    [
                        f"### `{code_slice.file_path}` lines {code_slice.start_line}-{code_slice.end_line}",
                        f"Reason: {code_slice.reason}",
                        "```python",
                        code_slice.code.rstrip(),
                        "```",
                    ]
                )
        else:
            lines.append("- No code slices were prepared; run the helper slicing command if discovery looked incomplete.")
        if context.warnings:
            lines.extend(["", "Discovery warnings to resolve:"])
            lines.extend(f"- {warning}" for warning in context.warnings[:8])

    lines.extend(["", "Validation plan:"])
    if test_commands:
        origin = "provided by user/UI"
        if discovered_test_commands and test_commands == discovered_test_commands:
            origin = "auto-discovered from the project"
        lines.append(f"- Validation commands ({origin}):")
        lines.extend(f"  - `{command}`" for command in test_commands)
    else:
        lines.append("- No project-native test command was discovered. Run at least syntax/static checks and the migration verifier.")
    lines.extend(
        [
            "",
            "Agent decision rules:",
            "- Use the source occurrences and slices as the edit map; do not treat them as optional background.",
            "- If a source occurrence remains after edits, run the verifier and repair that exact file.",
            "- Prefer project-native tests when available; otherwise run the static/API verifier and syntax checks.",
            "- Keep edits inside the project root and avoid unrelated refactors.",
        ]
    )
    return "\n".join(lines)


def _dedupe_slices(slices: tuple[Any, ...]) -> list[Any]:
    unique: list[Any] = []
    seen: set[tuple[str, int, int, str]] = set()
    for code_slice in slices:
        key = (code_slice.file_path, code_slice.start_line, code_slice.end_line, code_slice.code)
        if key in seen:
            continue
        seen.add(key)
        unique.append(code_slice)
    return unique


def _build_repair_task(
    *,
    project: Path,
    source: str,
    target: str,
    report: VerificationReport,
    strict_report: Path | None,
    test_commands: list[str],
) -> str:
    """Build a focused repair request after strict verification fails."""
    report_path = f"`{strict_report}`" if strict_report else "the in-memory strict verification report"
    checker_report = report.checker_report or {}
    checker_failures = checker_report.get("failures") if isinstance(checker_report, dict) else None
    blockers = [
        failure
        for failure in checker_failures or []
        if isinstance(failure, dict) and failure.get("severity") == "blocker"
    ]
    failures = {
        "syntax_errors": report.syntax_errors,
        "source_residue": report.source_residue,
        "api_check_failures": report.api_check_failures,
        "dependency_findings": report.dependency_findings,
        "target_evidence": report.target_evidence,
        "checker_blockers": blockers,
    }
    return f"""Continue the same `{source}` -> `{target}` migration in this project and repair the strict verification failures.

Project root:
{project}

Strict verification report source:
{report_path}

Verifier summary:
```json
{json.dumps(failures, indent=2, sort_keys=True)}
```

Repair requirements:
- Fix every blocker first. If `checker_blockers[].suggested_fix.edit_targets` is present, inspect those locations before editing.
- If `suggested_fix.kind=rename_import`, try the first candidate unless file evidence contradicts it.
- Quote the `rule_ref` in your reasoning when applying a checker-suggested fix.
- Inspect dependency/config findings; replace source dependency declarations with target declarations when that is part of this migration.
- Preserve already-correct target-library changes.
- Avoid broad chained rewrite commands. Use focused file inspection and targeted edits.
- Run these validation commands after the repair:
{_format_list(test_commands)}
- Run the migration verifier again if a helper command is available.
- Finish only after verification by issuing exactly this command on its own:
  echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
"""


def _build_validation_repair_task(
    *,
    project: Path,
    source: str,
    target: str,
    report: VerificationReport,
    strict_report: Path | None,
    validation_reports: list[dict[str, object]],
    repair_attempt: int,
    test_commands: list[str],
) -> str:
    """Build a repair request from deterministic checker and project validation evidence."""
    report_path = f"`{strict_report}`" if strict_report else "the in-memory strict verification report"
    evidence = {
        "repair_attempt": repair_attempt,
        "strict_report_path": str(strict_report) if strict_report else None,
        "strict_report": report.to_dict(),
        "project_validation": validation_reports,
    }
    return f"""Continue the same `{source}` -> `{target}` migration and repair deterministic validation failures before cross-review.

Project root:
{project}

Repair attempt: {repair_attempt}
Strict verification report source: {report_path}

Evidence:
```json
{json.dumps(evidence, indent=2, sort_keys=True)}
```

Repair requirements:
- Fix strict checker blockers, source residue, dependency findings, and failed project validation commands.
- If this is an npm/TypeScript project, keep `package.json` and lockfiles consistent; regenerate lockfiles when dependency declarations change.
- Preserve already-correct target-library changes.
- Run these validation commands after the repair:
{_format_list(test_commands)}
- Run the migration verifier again if a helper command is available.
- Finish only after verification by issuing exactly this command on its own:
  echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
"""


def _build_cross_review_repair_task(
    *,
    project: Path,
    source: str,
    target: str,
    cross_review_report: dict[str, object],
    validation_reports: list[dict[str, object]],
    repair_attempt: int,
    test_commands: list[str],
) -> str:
    """Build a repair request from independent cross-review findings."""
    evidence = {
        "cross_review_report": cross_review_report,
        "project_validation": validation_reports,
    }
    return f"""Repair the library migration using the independent cross-review findings.

Project root:
{project}

Migration:
{source} -> {target}

Repair attempt: {repair_attempt}

Cross-review evidence:
```json
{json.dumps(evidence, indent=2, sort_keys=True)}
```

Repair requirements:
- Treat `blockers`, `missing_checks`, and `rationale` as concrete repair inputs.
- If the reviewer found manifest/lockfile inconsistency, update dependency manifests and lockfiles together.
- If the reviewer found checker coverage gaps, add or run ecosystem-native validation that proves this migration.
- Preserve already-correct target-library changes.
- Run these validation commands after the repair:
{_format_list(test_commands)}
- Finish only after verification by issuing exactly this command on its own:
  echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
"""


def _build_cross_review_task(
    *,
    project: Path,
    source: str,
    target: str,
    strict_report: Path | None,
    pig_report: Path | None,
    verification_report: VerificationReport,
    validation_reports: list[dict[str, object]],
) -> str:
    """Build the second-agent read-only review task."""
    evidence = {
        "strict_report_path": str(strict_report) if strict_report else None,
        "pig_report_path": str(pig_report) if pig_report else None,
        "strict_report": verification_report.to_dict(),
        "project_validation": validation_reports,
    }
    return f"""Cross-review this completed library migration.

Migration: {source} -> {target}
Project root: {project}

Do not edit files. Do not run destructive commands. Your job is independent verification only.

Review questions:
1. What ecosystem is this project using?
2. Did the migration update code, dependency manifests, and lockfiles consistently?
3. Did the strict checker inspect relevant files for this ecosystem?
4. Do the project-native validation command results prove the migration is reproducible?
5. Is there any false-positive risk in the current "passed" status?

Hard verdict rules:
- If any project_validation entry has passed=false, verdict must be "fail".
- If package/dependency manifests and lockfiles are inconsistent, verdict must be "fail".
- If the strict checker inspected 0 relevant files for this ecosystem, mention that as a risk.
- Submit only a JSON object with keys:
  verdict ("pass" or "fail"), confidence, ecosystem, blockers, missing_checks, rationale.
- Your final bash command must print `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` on the first line and the JSON object on the following lines.
  Example:
  `printf '%s\n%s\n' 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' '{{"verdict":"fail","confidence":"high","ecosystem":"npm","blockers":[],"missing_checks":[],"rationale":""}}'`

Evidence:
```json
{json.dumps(evidence, indent=2, sort_keys=True)}
```

Current git diff:
```diff
{_git_diff(project)}
```
"""


def _run_cross_review_agent(
    *,
    model: Any,
    env: Any,
    agent_config: dict[str, Any],
    project: Path,
    source: str,
    target: str,
    strict_report: Path | None,
    pig_report: Path | None,
    verification_report: VerificationReport,
    validation_reports: list[dict[str, object]],
) -> dict[str, object]:
    task = _build_cross_review_task(
        project=project,
        source=source,
        target=target,
        strict_report=strict_report,
        pig_report=pig_report,
        verification_report=verification_report,
        validation_reports=validation_reports,
    )
    verifier_agent = get_agent(model, env, _cross_review_agent_config(agent_config), default_type="interactive")
    result = verifier_agent.run(task)
    return _parse_cross_review_submission(str(result.get("submission", "")))


def _cross_review_agent_config(agent_config: dict[str, Any]) -> dict[str, Any]:
    config = dict(agent_config)
    config["system_template"] = (
        "You are an independent migration verifier reviewing another agent's completed work. "
        "Be skeptical, evidence-driven, and read-only. You may run non-destructive inspection "
        "or validation commands, but you must not edit files. If required evidence is missing "
        "or any hard validation command failed, return verdict fail. Submit your verdict by "
        "printing COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT on the first line and the JSON verdict "
        "on the following lines."
    )
    config["instance_template"] = "{{task}}"
    config["confirm_exit"] = False
    config.pop("output_path", None)
    return config


def _parse_cross_review_submission(submission: str) -> dict[str, object]:
    text = submission.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            verdict = str(data.get("verdict", "")).lower()
            data["verdict"] = verdict
            data["passed"] = verdict == "pass"
            return data
        except json.JSONDecodeError:
            pass
    lowered = text.lower()
    if "verdict: pass" in lowered:
        return {"verdict": "pass", "passed": True, "raw_submission": submission}
    if "verdict: fail" in lowered:
        return {"verdict": "fail", "passed": False, "raw_submission": submission}
    return {
        "verdict": "fail",
        "passed": False,
        "raw_submission": submission,
        "blockers": ["Verifier agent did not return a parseable verdict."],
    }


def _git_diff(project: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "--", "."],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        return f"Could not read git diff: {exc}"
    diff = result.stdout or result.stderr
    if len(diff) > 60000:
        return diff[:60000] + "\n... diff truncated ..."
    return diff


def _working_tree_snapshot(project: Path) -> str:
    try:
        diff = subprocess.run(
            ["git", "diff", "--", "."],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        return f"unavailable:{exc}"
    return f"{status.stdout}\n{diff.stdout}"


def _has_failed_validation(validation_reports: list[dict[str, object]]) -> bool:
    return any(report.get("passed") is False for report in validation_reports)


def _option_enabled(value: object) -> bool:
    return value if isinstance(value, bool) else False


def _print_cross_review_stats(stats: dict[str, object]) -> None:
    console.print("Cross-review collaboration stats:")
    console.print(json.dumps(stats, indent=2, sort_keys=True))


# fmt: off
@app.command()
def main(
    project: Path = typer.Option(Path.cwd(), "-p", "--project", help="Project root to migrate."),
    source: str | None = typer.Option(None, "--source", help="Source library/package to migrate away from."),
    target: str | None = typer.Option(None, "--target", help="Target library/package to migrate to."),
    pymigbench_yaml: Path | None = typer.Option(None, "--pymigbench-yaml", help="Optional PyMigBench migration YAML file to derive source/target/scope/API hints."),
    pymigbench_dataset: Path | None = typer.Option(None, "--pymigbench-dataset", help="Directory of PyMigBench YAML files used to retrieve historical examples."),
    examples: int = typer.Option(0, "--examples", min=0, help="Number of similar PyMigBench historical examples to include in the migration task."),
    strategy: str = typer.Option("pig", "--strategy", help="Migration strategy: default, pig, or pig-advisory."),
    pig_report: Path | None = typer.Option(None, "--pig-report", help="Write the generated PIG context JSON to this path."),
    pig_max_candidates: int = typer.Option(8, "--pig-max-candidates", min=1, help="Maximum target API candidates to include."),
    pig_max_slices: int = typer.Option(80, "--pig-max-slices", min=1, help="Maximum PIG code slices to build."),
    pig_slice_radius: int = typer.Option(8, "--pig-slice-radius", min=1, help="Fallback line radius for PIG code slices."),
    pig_introspect_target: bool = typer.Option(False, "--pig-introspect-target", help="Import the target package locally to add public symbols to candidate ranking."),
    verify_only: bool = typer.Option(False, "--verify-only", help="Run migration static/API checks and exit without running the agent."),
    strict_static_check: bool = typer.Option(False, "--strict-static-check", help="Run strict static/API checks after the agent exits."),
    strict_report: Path | None = typer.Option(None, "--strict-report", help="Write static/API verification JSON to this path."),
    auto_repair_attempts: int = typer.Option(1, "--auto-repair-attempts", min=0, help="Automatic repair passes to run when strict verification fails."),
    cross_review: bool = typer.Option(False, "--cross-review/--no-cross-review", help="Run an independent read-only verifier agent and ecosystem validation gate after migration."),
    discover_tests: bool = typer.Option(True, "--discover-tests/--no-discover-tests", help="Auto-discover project-native validation commands when none are provided."),
    scope: list[str] = typer.Option([], "-s", "--scope", help="File, directory, or symbol scope to prioritize. Repeatable."),
    test_command: list[str] = typer.Option([], "-T", "--test-command", help="Validation command to run after migration. Repeatable."),
    note: list[str] = typer.Option([], "--note", help="Extra migration requirement or constraint. Repeatable."),
    model_name: str | None = typer.Option(None, "-m", "--model", help="Model to use."),
    model_class: str | None = typer.Option(None, "--model-class", help="Model class to use."),
    agent_class: str | None = typer.Option(None, "--agent-class", help="Agent class to use.", rich_help_panel="Advanced"),
    environment_class: str | None = typer.Option(None, "--environment-class", help="Environment class to use.", rich_help_panel="Advanced"),
    yolo: bool = typer.Option(False, "-y", "--yolo", help="Run without confirmation."),
    cost_limit: float | None = typer.Option(None, "-l", "--cost-limit", help="Cost limit. Set to 0 to disable."),
    config_spec: list[str] = typer.Option(DEFAULT_CONFIG_FILES, "-c", "--config", help=_CONFIG_SPEC_HELP_TEXT),
    output: Path | None = typer.Option(DEFAULT_OUTPUT_FILE, "-o", "--output", help="Output trajectory file."),
    exit_immediately: bool = typer.Option(False, "--exit-immediately", help="Exit immediately when the agent wants to finish instead of prompting.", rich_help_panel="Advanced"),
    print_task: bool = typer.Option(False, "--print-task", help="Print the generated migration task and exit without running the agent."),
) -> Any:
    # fmt: on
    """Run a structured library migration task."""
    configure_if_first_time()

    pymigbench_data = load_pymigbench_yaml(pymigbench_yaml) if pymigbench_yaml else None
    source = source or (pymigbench_data or {}).get("source")
    target = target or (pymigbench_data or {}).get("target")
    if not source or not target:
        raise typer.BadParameter("Provide --source and --target, or pass --pymigbench-yaml with source/target fields.")
    if not project.exists() or not project.is_dir():
        raise typer.BadParameter(f"Project root does not exist or is not a directory: {project}")
    project = project.resolve()
    if pig_report:
        pig_report = pig_report.resolve()
    if strict_report:
        strict_report = strict_report.resolve()

    scopes = _derive_scopes(scope, pymigbench_data)
    pymigbench_examples = get_pymigbench_examples(
        current_data=pymigbench_data or {},
        dataset_dir=pymigbench_dataset or (pymigbench_yaml.parent if pymigbench_yaml else None),
        current_yaml=pymigbench_yaml,
        count=examples,
    )
    normalized_strategy = strategy.lower()
    if normalized_strategy == "pig":
        normalized_strategy = "pig-advisory"
    if normalized_strategy not in {"default", "pig-advisory"}:
        raise typer.BadParameter("Strategy must be one of: default, pig, pig-advisory.")
    api_changes = api_changes_from_pymigbench(pymigbench_data)
    pig_context_markdown = None
    pig_context_obj: PigContext | None = None
    pig_helper_commands: dict[str, str] = {}
    if normalized_strategy == "pig-advisory":
        pig_context_obj = build_pig_context(
            project=project,
            source=source,
            target=target,
            scopes=scopes,
            pymigbench_data=pymigbench_data,
            pymigbench_examples=pymigbench_examples,
            max_candidates=pig_max_candidates,
            max_slices=pig_max_slices,
            slice_radius=pig_slice_radius,
            introspect_target=pig_introspect_target,
        )
        pig_helper_commands = _build_pig_helper_commands(
            project=project,
            source=source,
            target=target,
            scopes=scopes,
            pymigbench_yaml=pymigbench_yaml,
            strict_report=strict_report,
        )
        pig_context_markdown = render_pig_prompt_summary(
            pig_context_obj,
            report_path=pig_report,
            helper_commands=pig_helper_commands,
        )
        if pig_report:
            pig_report.parent.mkdir(parents=True, exist_ok=True)
            pig_report.write_text(json.dumps(pig_context_obj.to_dict(), indent=2, sort_keys=True))
    if verify_only:
        report = verify_project_migration(
            project=project.resolve(),
            source=source,
            target=target,
            scopes=scopes,
            api_changes=api_changes,
        )
        _emit_verification_report(report, strict_report)
        _print_verification_summary(report)
        if not report.passed:
            raise typer.Exit(1)
        return report
    discovered_test_commands = _discover_validation_commands(project) if discover_tests and not test_command else []
    effective_test_commands = test_command or discovered_test_commands
    agent_automation_context = _render_agent_automation_context(
        context=pig_context_obj,
        project=project,
        source=source,
        target=target,
        test_commands=effective_test_commands,
        discovered_test_commands=discovered_test_commands,
    )
    task = build_migration_task(
        project=project,
        source=source,
        target=target,
        scopes=scopes,
        test_commands=effective_test_commands,
        notes=note,
        pymigbench_data=pymigbench_data,
        pymigbench_examples=pymigbench_examples,
        strategy=normalized_strategy,
        pig_context=pig_context_markdown,
        agent_automation_context=agent_automation_context,
        pig_report=pig_report,
        strict_static_check=strict_static_check,
        strict_report=strict_report,
    )
    if print_task:
        return console.print(task)

    console.print(f"Building migration agent config from specs: [bold green]{config_spec}[/bold green]")
    configs = [get_config_from_spec(spec) for spec in config_spec]
    configs.append({
        "agent": {
            "agent_class": agent_class or UNSET,
            "mode": "yolo" if yolo else UNSET,
            "cost_limit": cost_limit if cost_limit is not None else UNSET,
            "confirm_exit": False if exit_immediately or yolo else UNSET,
            "output_path": output or UNSET,
        },
        "model": {
            "model_class": model_class or UNSET,
            "model_name": model_name or UNSET,
        },
        "environment": {
            "environment_class": environment_class or UNSET,
            "cwd": str(project.resolve()),
        },
        "migration": {
            "source": source,
            "target": target,
            "scope": scopes,
            "test_command": effective_test_commands,
            "pymigbench_yaml": str(pymigbench_yaml) if pymigbench_yaml else UNSET,
            "pymigbench_dataset": str(pymigbench_dataset) if pymigbench_dataset else UNSET,
            "examples": examples,
            "strategy": normalized_strategy,
            "pig_report": str(pig_report) if pig_report else UNSET,
            "pig_introspect_target": pig_introspect_target,
            "strict_static_check": strict_static_check,
            "strict_report": str(strict_report) if strict_report else UNSET,
            "auto_repair_attempts": auto_repair_attempts,
            "cross_review": _option_enabled(cross_review),
            "discover_tests": discover_tests,
        },
    })
    config = recursive_merge(*configs)

    model = get_model(config=config.get("model", {}))
    env = get_environment(config.get("environment", {}), default_type="local")
    agent = get_agent(model, env, config.get("agent", {}), default_type="interactive")
    agent.run(task)
    report: VerificationReport | None = None
    if strict_static_check:
        report = verify_project_migration(
            project=project,
            source=source,
            target=target,
            scopes=scopes,
            api_changes=api_changes,
        )
        _emit_verification_report(report, strict_report)
        _print_verification_summary(report)
        for attempt in range(1, auto_repair_attempts + 1):
            if report.passed:
                break
            console.print(f"Strict verification failed; starting automatic repair pass {attempt}/{auto_repair_attempts}.")
            repair_task = _build_repair_task(
                project=project,
                source=source,
                target=target,
                report=report,
                strict_report=strict_report,
                test_commands=effective_test_commands,
            )
            agent.run(repair_task)
            report = verify_project_migration(
                project=project,
                source=source,
                target=target,
                scopes=scopes,
                api_changes=api_changes,
            )
            _emit_verification_report(report, strict_report)
            _print_verification_summary(report)
        if not report.passed:
            raise typer.Exit(1)
    if _option_enabled(cross_review):
        validation_commands = _dedupe_strings(effective_test_commands + _discover_ecosystem_validation_commands(project))
        collaboration_stats: dict[str, object] = {
            "pre_review_repair_attempts": 0,
            "cross_review_iterations": 0,
            "cross_review_repair_attempts": 0,
            "stop_reason": None,
        }
        while True:
            if report is None:
                report = verify_project_migration(
                    project=project,
                    source=source,
                    target=target,
                    scopes=scopes,
                    api_changes=api_changes,
                )
                _emit_verification_report(report, strict_report)
                _print_verification_summary(report)
            validation_reports = _run_project_validation_commands(project, validation_commands)
            if _has_failed_validation(validation_reports) or not report.passed:
                collaboration_stats["pre_review_repair_attempts"] = int(collaboration_stats["pre_review_repair_attempts"]) + 1
                before_repair = _working_tree_snapshot(project)
                repair_task = _build_validation_repair_task(
                    project=project,
                    source=source,
                    target=target,
                    report=report,
                    strict_report=strict_report,
                    validation_reports=validation_reports,
                    repair_attempt=int(collaboration_stats["pre_review_repair_attempts"]),
                    test_commands=validation_commands,
                )
                agent.run(repair_task)
                after_repair = _working_tree_snapshot(project)
                if after_repair == before_repair:
                    collaboration_stats["stop_reason"] = "pre_review_repair_made_no_changes"
                    _print_cross_review_stats(collaboration_stats)
                    raise typer.Exit(1)
                report = None
                continue
            collaboration_stats["cross_review_iterations"] = int(collaboration_stats["cross_review_iterations"]) + 1
            cross_review_report = _run_cross_review_agent(
                model=model,
                env=env,
                agent_config=config.get("agent", {}),
                project=project,
                source=source,
                target=target,
                strict_report=strict_report,
                pig_report=pig_report,
                verification_report=report,
                validation_reports=validation_reports,
            )
            console.print("Cross-review verifier report:")
            console.print(json.dumps(cross_review_report, indent=2, sort_keys=True))
            if cross_review_report.get("passed"):
                collaboration_stats["stop_reason"] = "cross_review_passed"
                _print_cross_review_stats(collaboration_stats)
                break
            collaboration_stats["cross_review_repair_attempts"] = int(
                collaboration_stats["cross_review_repair_attempts"]
            ) + 1
            before_repair = _working_tree_snapshot(project)
            repair_task = _build_cross_review_repair_task(
                project=project,
                source=source,
                target=target,
                cross_review_report=cross_review_report,
                validation_reports=validation_reports,
                repair_attempt=int(collaboration_stats["cross_review_repair_attempts"]),
                test_commands=validation_commands,
            )
            agent.run(repair_task)
            after_repair = _working_tree_snapshot(project)
            if after_repair == before_repair:
                collaboration_stats["stop_reason"] = "cross_review_repair_made_no_changes"
                _print_cross_review_stats(collaboration_stats)
                raise typer.Exit(1)
            report = None
    if (output_path := config.get("agent", {}).get("output_path")):
        console.print(f"Saved trajectory to [bold green]'{output_path}'[/bold green]")
    return agent


def _build_pig_helper_commands(
    *,
    project: Path,
    source: str,
    target: str,
    scopes: list[str],
    pymigbench_yaml: Path | None,
    strict_report: Path | None,
) -> dict[str, str]:
    base = [
        sys.executable,
        "-m",
        "minisweagent.run.migration_tools",
    ]
    pythonpath = Path(__file__).resolve().parents[2]
    common = [
        "--project",
        str(project),
        "--source",
        source,
        "--target",
        target,
    ]
    if pymigbench_yaml:
        common.extend(["--pymigbench-yaml", str(pymigbench_yaml)])
    for scope in scopes:
        common.extend(["--scope", scope])
    verify = base + ["verify"] + common
    check = base + ["check"] + common + ["--layers", "L0,L1,L2,L3"]
    if strict_report:
        verify.extend(["--output", str(strict_report)])
        check.extend(["--output", str(strict_report)])
    return {
        "discover source API usage": _render_shell_command(base + ["discover"] + common, pythonpath=pythonpath),
        "retrieve focused code slices": _render_shell_command(
            base + ["slices"] + common + ["--max-slices", "12"],
            pythonpath=pythonpath,
        ),
        "rank target API candidates": _render_shell_command(base + ["candidates"] + common, pythonpath=pythonpath),
        "run static/API verifier": _render_shell_command(verify, pythonpath=pythonpath),
        "run automated checker": _render_shell_command(check, pythonpath=pythonpath),
    }


def _render_shell_command(args: list[str], *, pythonpath: Path | None = None) -> str:
    command = " ".join(_shell_quote(arg) for arg in args)
    env_parts = ["MSWEA_SILENT_STARTUP=1"]
    if not pythonpath:
        return f"{' '.join(env_parts)} {command}"
    env_parts.append(f"PYTHONPATH={_shell_quote(str(pythonpath))}")
    return f"{' '.join(env_parts)} {command}"


def _shell_quote(value: str) -> str:
    if not value:
        return "''"
    safe_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@%+=:,./-")
    if all(char in safe_chars for char in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _emit_verification_report(report: VerificationReport, output: Path | None) -> None:
    if not output:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))


def _print_verification_summary(report: VerificationReport) -> None:
    status = "passed" if report.passed else "failed"
    console.print(f"Migration static/API verification {status}.")
    console.print(
        json.dumps(
            {
                "syntax_errors": len(report.syntax_errors),
                "source_residue": len(report.source_residue),
                "target_evidence": len(report.target_evidence),
                "api_checks": len(report.api_checks),
                "api_check_failures": len(report.api_check_failures),
                "dependency_findings": len(report.dependency_findings),
            },
            indent=2,
            sort_keys=True,
        )
    )
    for failure in report.api_check_failures[:20]:
        console.print(f"- {failure}")


if __name__ == "__main__":
    app()
