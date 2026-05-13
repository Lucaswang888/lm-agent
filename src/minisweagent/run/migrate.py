#!/usr/bin/env python3

"""Run mini-SWE-agent as a structured Python library migration agent."""

import json
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
    pig_report: Path | None = None,
    strict_static_check: bool = False,
    strict_report: Path | None = None,
) -> str:
    """Build the user-facing migration task sent to the agent."""
    hints = _format_pymigbench_hints(pymigbench_data or {})
    examples = _format_pymigbench_examples(pymigbench_examples or [])
    pig_section = f"\n{pig_context}\n" if pig_context else ""
    strict_lines = ""
    if strict_static_check:
        report_line = f" The harness will write the JSON report to `{strict_report}`." if strict_report else ""
        strict_lines = (
            "\nStrict API-level verification is enabled after the agent exits."
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
- Treat every PyMigBench file and code_change entry as a mandatory migration checklist item.
- When an API checklist item includes exact source/target patterns, remove the source pattern and make the target pattern appear unless you have an explicit compatibility reason.
- Before finishing, inspect every PyMigBench file listed above and confirm all listed target APIs/patterns are present and relevant source APIs/patterns are gone.
- Always inspect common dependency declaration files such as pyproject.toml, setup.py, setup.cfg, requirements*.txt, tox.ini, environment.yml, and lock files when present.
- Preserve existing behavior unless the source and target libraries require an intentional compatibility adjustment.
- Run the provided validation commands. If no validation command was provided, discover and run the smallest relevant project tests or static checks available.
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
    pig_helper_commands: dict[str, str] = {}
    if normalized_strategy == "pig-advisory":
        pig_context = build_pig_context(
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
            pig_context,
            report_path=pig_report,
            helper_commands=pig_helper_commands,
        )
        if pig_report:
            pig_report.parent.mkdir(parents=True, exist_ok=True)
            pig_report.write_text(json.dumps(pig_context.to_dict(), indent=2, sort_keys=True))
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
    task = build_migration_task(
        project=project,
        source=source,
        target=target,
        scopes=scopes,
        test_commands=test_command,
        notes=note,
        pymigbench_data=pymigbench_data,
        pymigbench_examples=pymigbench_examples,
        strategy=normalized_strategy,
        pig_context=pig_context_markdown,
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
            "test_command": test_command,
            "pymigbench_yaml": str(pymigbench_yaml) if pymigbench_yaml else UNSET,
            "pymigbench_dataset": str(pymigbench_dataset) if pymigbench_dataset else UNSET,
            "examples": examples,
            "strategy": normalized_strategy,
            "pig_report": str(pig_report) if pig_report else UNSET,
            "pig_introspect_target": pig_introspect_target,
            "strict_static_check": strict_static_check,
            "strict_report": str(strict_report) if strict_report else UNSET,
        },
    })
    config = recursive_merge(*configs)

    model = get_model(config=config.get("model", {}))
    env = get_environment(config.get("environment", {}), default_type="local")
    agent = get_agent(model, env, config.get("agent", {}), default_type="interactive")
    agent.run(task)
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
        if not report.passed:
            raise typer.Exit(1)
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
    if strict_report:
        verify.extend(["--output", str(strict_report)])
    return {
        "discover source API usage": _render_shell_command(base + ["discover"] + common, pythonpath=pythonpath),
        "retrieve focused code slices": _render_shell_command(
            base + ["slices"] + common + ["--max-slices", "12"],
            pythonpath=pythonpath,
        ),
        "rank target API candidates": _render_shell_command(base + ["candidates"] + common, pythonpath=pythonpath),
        "run static/API verifier": _render_shell_command(verify, pythonpath=pythonpath),
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
