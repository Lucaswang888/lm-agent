#!/usr/bin/env python3

"""Agent-callable PIG-style helper tools for library migration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from minisweagent.migration.checker import CheckContext, run_default_pipeline
from minisweagent.migration.context import api_changes_from_pymigbench, build_pig_context
from minisweagent.migration.verification import verify_project_migration

app = typer.Typer(rich_markup_mode="rich", add_completion=False)


def _load_pymigbench_yaml(path: Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise typer.BadParameter(f"Expected a mapping in PyMigBench YAML file: {path}")
    return data


def _build_context(
    *,
    project: Path,
    source: str,
    target: str,
    scopes: list[str],
    pymigbench_yaml: Path | None,
    max_candidates: int,
    max_slices: int,
    slice_radius: int,
    introspect_target: bool,
):
    if not project.exists() or not project.is_dir():
        raise typer.BadParameter(f"Project root does not exist or is not a directory: {project}")
    pymigbench_data = _load_pymigbench_yaml(pymigbench_yaml)
    return build_pig_context(
        project=project.resolve(),
        source=source,
        target=target,
        scopes=scopes,
        pymigbench_data=pymigbench_data,
        max_candidates=max_candidates,
        max_slices=max_slices,
        slice_radius=slice_radius,
        introspect_target=introspect_target,
    )


@app.command()
def context(
    project: Annotated[Path, typer.Option("--project", "-p", help="Project root to inspect.")],
    source: Annotated[str, typer.Option("--source", help="Source library/package.")],
    target: Annotated[str, typer.Option("--target", help="Target library/package.")],
    pymigbench_yaml: Annotated[Path | None, typer.Option("--pymigbench-yaml", help="Optional benchmark ground truth YAML.")] = None,
    scope: Annotated[list[str], typer.Option("--scope", "-s", help="Priority scope. Repeatable.")] = [],
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write full PIG context JSON to this path.")] = None,
    max_candidates: Annotated[int, typer.Option("--max-candidates", min=1)] = 8,
    max_slices: Annotated[int, typer.Option("--max-slices", min=1)] = 80,
    slice_radius: Annotated[int, typer.Option("--slice-radius", min=1)] = 8,
    introspect_target: Annotated[bool, typer.Option("--introspect-target")] = False,
) -> None:
    """Build the full PIG-style context JSON."""
    pig_context = _build_context(
        project=project,
        source=source,
        target=target,
        scopes=scope,
        pymigbench_yaml=pymigbench_yaml,
        max_candidates=max_candidates,
        max_slices=max_slices,
        slice_radius=slice_radius,
        introspect_target=introspect_target,
    )
    payload = pig_context.to_dict()
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def discover(
    project: Annotated[Path, typer.Option("--project", "-p", help="Project root to inspect.")],
    source: Annotated[str, typer.Option("--source", help="Source library/package.")],
    target: Annotated[str, typer.Option("--target", help="Target library/package.")],
    pymigbench_yaml: Annotated[Path | None, typer.Option("--pymigbench-yaml", help="Optional benchmark ground truth YAML.")] = None,
    scope: Annotated[list[str], typer.Option("--scope", "-s", help="Priority scope. Repeatable.")] = [],
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write discovery JSON to this path.")] = None,
    output_format: Annotated[str, typer.Option("--format", help="json or jsonl.")] = "json",
) -> None:
    """Find source-library API occurrences in the current project."""
    pig_context = _build_context(
        project=project,
        source=source,
        target=target,
        scopes=scope,
        pymigbench_yaml=pymigbench_yaml,
        max_candidates=1,
        max_slices=1,
        slice_radius=1,
        introspect_target=False,
    )
    payload = {
        "source": pig_context.source,
        "target": pig_context.target,
        "occurrence_count": len(pig_context.occurrences),
        "inspected_files": pig_context.inspected_files,
        "warnings": pig_context.warnings,
        "occurrences": pig_context.to_dict()["occurrences"],
    }
    text = _format_json_payload(payload, output_format)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text)
    typer.echo(text)


@app.command()
def slices(
    project: Annotated[Path, typer.Option("--project", "-p", help="Project root to inspect.")],
    source: Annotated[str, typer.Option("--source", help="Source library/package.")],
    target: Annotated[str, typer.Option("--target", help="Target library/package.")],
    pymigbench_yaml: Annotated[Path | None, typer.Option("--pymigbench-yaml", help="Optional benchmark ground truth YAML.")] = None,
    scope: Annotated[list[str], typer.Option("--scope", "-s", help="Priority scope. Repeatable.")] = [],
    max_slices: Annotated[int, typer.Option("--max-slices", min=1)] = 12,
    slice_radius: Annotated[int, typer.Option("--slice-radius", min=1)] = 8,
    output_format: Annotated[str, typer.Option("--format", help="json or markdown.")] = "markdown",
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write slices output to this path.")] = None,
) -> None:
    """Return migration-relevant code slices around source API usage."""
    pig_context = _build_context(
        project=project,
        source=source,
        target=target,
        scopes=scope,
        pymigbench_yaml=pymigbench_yaml,
        max_candidates=1,
        max_slices=max_slices,
        slice_radius=slice_radius,
        introspect_target=False,
    )
    unique_slices = _compact_slices(pig_context.to_dict()["slices"])
    if output_format == "json":
        text = json.dumps({"slices": unique_slices}, indent=2, sort_keys=True)
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text)
        typer.echo(text)
        return
    if output_format == "jsonl":
        text = "\n".join(json.dumps(code_slice, sort_keys=True) for code_slice in unique_slices)
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text)
        typer.echo(text)
        return
    if output_format != "markdown":
        raise typer.BadParameter("--format must be json, jsonl, or markdown")
    lines: list[str] = []
    for index, code_slice in enumerate(unique_slices, 1):
        lines.extend(
            [
                f"### Slice {index}: {code_slice['file_path']}:{code_slice['start_line']}-{code_slice['end_line']}",
                f"Reason: {'; '.join(code_slice['reasons'])}",
                "```python",
                code_slice["code"],
                "```",
            ]
        )
    text = "\n".join(lines)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text)
    typer.echo(text)


@app.command()
def candidates(
    project: Annotated[Path, typer.Option("--project", "-p", help="Project root to inspect.")],
    source: Annotated[str, typer.Option("--source", help="Source library/package.")],
    target: Annotated[str, typer.Option("--target", help="Target library/package.")],
    pymigbench_yaml: Annotated[Path | None, typer.Option("--pymigbench-yaml", help="Optional benchmark ground truth YAML.")] = None,
    scope: Annotated[list[str], typer.Option("--scope", "-s", help="Priority scope. Repeatable.")] = [],
    max_candidates: Annotated[int, typer.Option("--max-candidates", min=1)] = 12,
    introspect_target: Annotated[bool, typer.Option("--introspect-target")] = False,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write candidate JSON to this path.")] = None,
    output_format: Annotated[str, typer.Option("--format", help="json or jsonl.")] = "json",
) -> None:
    """Rank likely target APIs for the migration."""
    pig_context = _build_context(
        project=project,
        source=source,
        target=target,
        scopes=scope,
        pymigbench_yaml=pymigbench_yaml,
        max_candidates=max_candidates,
        max_slices=1,
        slice_radius=1,
        introspect_target=introspect_target,
    )
    payload = {"candidates": pig_context.to_dict()["candidates"]}
    text = _format_json_payload(payload, output_format)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text)
    typer.echo(text)


@app.command()
def check(
    project: Annotated[Path, typer.Option("--project", "-p", help="Project root to verify.")],
    source: Annotated[str, typer.Option("--source", help="Source library/package.")],
    target: Annotated[str, typer.Option("--target", help="Target library/package.")],
    layers: Annotated[str, typer.Option("--layers", help="Comma-separated checker layers, e.g. L0,L1,L2,L3.")] = "L0,L1,L2,L3",
    pymigbench_yaml: Annotated[Path | None, typer.Option("--pymigbench-yaml", help="Optional benchmark ground truth YAML.")] = None,
    scope: Annotated[list[str], typer.Option("--scope", "-s", help="Priority scope. Repeatable.")] = [],
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write CheckerReport JSON to this path.")] = None,
    enable_l4: Annotated[bool, typer.Option("--enable-l4", help="Enable optional L4 behaviour-diff hook.")] = False,
    test_command: Annotated[list[str], typer.Option("--test-command", help="Validation command to run in L3. Repeatable.")] = [],
) -> None:
    """Run the multi-layer checker pipeline and return CheckerReport JSON."""
    pymigbench_data = _load_pymigbench_yaml(pymigbench_yaml)
    layer_names = tuple(layer.strip().upper() for layer in layers.split(",") if layer.strip())
    ctx = CheckContext(
        project=project.resolve(),
        source=source,
        target=target,
        scopes=tuple(scope),
        api_changes=tuple(api_changes_from_pymigbench(pymigbench_data)),
        layers_to_run=layer_names,
        enable_l4=enable_l4,
        test_commands=tuple(test_command),
    )
    report = run_default_pipeline(ctx)
    payload = report.to_dict()
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    if not report.passed:
        raise typer.Exit(1)


@app.command()
def verify(
    project: Annotated[Path, typer.Option("--project", "-p", help="Project root to verify.")],
    source: Annotated[str, typer.Option("--source", help="Source library/package.")],
    target: Annotated[str, typer.Option("--target", help="Target library/package.")],
    pymigbench_yaml: Annotated[Path | None, typer.Option("--pymigbench-yaml", help="Optional benchmark ground truth YAML.")] = None,
    scope: Annotated[list[str], typer.Option("--scope", "-s", help="Priority scope. Repeatable.")] = [],
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write verification JSON to this path.")] = None,
) -> None:
    """Run static/API migration verification after edits."""
    pymigbench_data = _load_pymigbench_yaml(pymigbench_yaml)
    report = verify_project_migration(
        project=project.resolve(),
        source=source,
        target=target,
        scopes=scope,
        api_changes=api_changes_from_pymigbench(pymigbench_data),
    )
    payload = report.to_dict()
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    if not report.passed:
        raise typer.Exit(1)


def _compact_slices(slices_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    index_by_key: dict[tuple[str, int, int, str], int] = {}
    for code_slice in slices_data:
        key = (
            code_slice["file_path"],
            code_slice["start_line"],
            code_slice["end_line"],
            code_slice["code"],
        )
        reason = code_slice["reason"]
        if key in index_by_key:
            reasons = ordered[index_by_key[key]]["reasons"]
            if reason not in reasons:
                reasons.append(reason)
            continue
        compact = dict(code_slice)
        compact["reasons"] = [reason]
        compact.pop("reason", None)
        index_by_key[key] = len(ordered)
        ordered.append(compact)
    return ordered


def _format_json_payload(payload: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(payload, indent=2, sort_keys=True)
    if output_format == "jsonl":
        values = next(iter(payload.values()), [])
        if not isinstance(values, list):
            return json.dumps(payload, sort_keys=True)
        return "\n".join(json.dumps(item, sort_keys=True) for item in values)
    raise typer.BadParameter("--format must be json or jsonl")


if __name__ == "__main__":
    app()
