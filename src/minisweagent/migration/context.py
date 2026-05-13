"""PIG-style migration context assembly and rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from minisweagent.migration.candidates import rank_candidate_apis
from minisweagent.migration.discovery import discover_api_occurrences, iter_python_files
from minisweagent.migration.pig_models import ApiChange, CandidateApi, CodeSlice, PigContext
from minisweagent.migration.slicing import build_code_slices


def api_changes_from_pymigbench(data: dict[str, Any] | None) -> list[ApiChange]:
    """Extract API-level migration facts from a PyMigBench record."""
    if not data:
        return []
    changes: list[ApiChange] = []
    for file_record in data.get("files", []) or []:
        path = file_record.get("path") or "unknown"
        for change in file_record.get("code_changes", []) or []:
            changes.append(
                ApiChange(
                    file_path=path,
                    line=str(change.get("line", "unknown")),
                    source_apis=tuple(change.get("source_apis", []) or ()),
                    target_apis=tuple(change.get("target_apis", []) or ()),
                    properties=tuple(change.get("properties", []) or ()),
                    source_program_elements=tuple(change.get("source_program_elements", []) or ()),
                    target_program_elements=tuple(change.get("target_program_elements", []) or ()),
                    cardinality=change.get("cardinality"),
                    source_snippet=change.get("source_snippet"),
                    target_snippet=change.get("target_snippet"),
                    source_removed_required=bool(change.get("source_removed_required", True)),
                    target_required=bool(change.get("target_required", True)),
                )
            )
    return changes


def build_pig_context(
    *,
    project: Path,
    source: str,
    target: str,
    scopes: list[str],
    pymigbench_data: dict[str, Any] | None = None,
    pymigbench_examples: list[dict[str, Any]] | None = None,
    max_candidates: int = 8,
    max_slices: int = 80,
    slice_radius: int = 8,
    introspect_target: bool = False,
) -> PigContext:
    """Build the PIG-inspired retrieval, slicing, and candidate context."""
    api_changes = api_changes_from_pymigbench(pymigbench_data)
    occurrences, warnings = discover_api_occurrences(project, source, scopes, api_changes)
    slices = build_code_slices(
        project,
        occurrences,
        api_changes,
        radius=slice_radius,
        max_slices=max_slices,
    )
    candidates = rank_candidate_apis(
        source=source,
        target=target,
        api_changes=api_changes,
        examples=pymigbench_examples or [],
        max_candidates=max_candidates,
        introspect_target=introspect_target,
    )
    inspected_files = [str(path.relative_to(project)) for path in iter_python_files(project, scopes)]
    return PigContext(
        project=str(project.resolve()),
        source=source,
        target=target,
        strategy="pig-advisory",
        api_changes=tuple(api_changes),
        occurrences=tuple(occurrences),
        slices=tuple(slices),
        candidates=tuple(candidates),
        inspected_files=tuple(inspected_files),
        warnings=tuple(warnings),
        metadata={
            "pymigbench_repo": (pymigbench_data or {}).get("repo"),
            "pymigbench_commit": (pymigbench_data or {}).get("commit"),
            "example_count": len(pymigbench_examples or []),
            "introspect_target": introspect_target,
        },
    )


def render_pig_context(context: PigContext) -> str:
    """Render a short PIG summary for the migration agent prompt."""
    return render_pig_prompt_summary(context)


def render_pig_prompt_summary(
    context: PigContext,
    *,
    report_path: Path | None = None,
    helper_commands: dict[str, str] | None = None,
) -> str:
    """Render concise PIG guidance while leaving details to callable tools."""
    lines = [
        "## PIG-style migration support",
        "",
        "Use PIG as an automated helper workflow, not as a full prompt dump. "
        "Call the helper commands below when you need API discovery, code slices, "
        "candidate APIs, or static/API verification.",
        "",
        "Initial PIG summary:",
        f"- Source library: `{context.source}`",
        f"- Target library: `{context.target}`",
        f"- API checklist items: {len(context.api_changes)}",
        f"- Source API occurrences found locally: {len(context.occurrences)}",
        f"- Code slices prepared: {len(context.slices)}",
        f"- Python files inspected for discovery: {len(context.inspected_files)}",
    ]
    if context.warnings:
        lines.append("- Discovery warnings:")
        lines.extend(f"  - {warning}" for warning in context.warnings[:5])

    if report_path:
        lines.extend(["", f"Full machine-readable PIG report: `{report_path}`"])

    lines.extend(["", "Use these PIG helper tools during the migration:"])
    if helper_commands:
        for label, command in helper_commands.items():
            lines.append(f"- {label}: `{command}`")
    else:
        lines.append("- No helper command path was provided; inspect the project manually and use the JSON report if present.")

    lines.extend(
        [
            "",
            "Recommended workflow:",
            "1. Run discovery before editing to locate source-library usage.",
            "2. Request code slices only for the files or APIs you are about to edit.",
            "3. Check target API candidates before choosing replacements.",
            "4. After edits and tests, run the verifier and fix reported API/static failures.",
            "",
            "Prompt policy:",
            "- Do not rely on this prompt as the only PIG integration surface.",
            "- Use the helper tools to perform PIG-style steps on demand.",
            "- Keep project tests as the primary validation when they exist.",
        ]
    )
    return "\n".join(lines)


def _render_candidates(candidates: list[CandidateApi]) -> list[str]:
    if not candidates:
        return ["- No candidates could be ranked automatically; inspect target docs or installed symbols."]
    lines: list[str] = []
    for candidate in candidates:
        reason = "; ".join(candidate.reasons[:3]) or "ranked candidate"
        lines.append(f"- `{candidate.api}` score={candidate.score}: {reason}")
    return lines


def _compact_prompt_slices(slices: list[CodeSlice]) -> list[tuple[CodeSlice, tuple[str, ...]]]:
    """Deduplicate identical rendered slices while preserving occurrence reasons."""
    ordered: list[tuple[CodeSlice, list[str]]] = []
    index_by_key: dict[tuple[str, int, int, str], int] = {}
    for code_slice in slices:
        key = (code_slice.file_path, code_slice.start_line, code_slice.end_line, code_slice.code)
        reason = code_slice.reason
        if key in index_by_key:
            reasons = ordered[index_by_key[key]][1]
            if reason not in reasons:
                reasons.append(reason)
            continue
        index_by_key[key] = len(ordered)
        ordered.append((code_slice, [reason]))
    return [(code_slice, tuple(reasons)) for code_slice, reasons in ordered]


def _single_line(value: str) -> str:
    return " ".join(value.strip().split())
