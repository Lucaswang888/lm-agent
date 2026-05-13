"""API-level code slicing for migration tasks."""

from __future__ import annotations

import ast
from pathlib import Path

from minisweagent.migration.discovery import parse_python_file
from minisweagent.migration.pig_models import ApiChange, ApiOccurrence, CodeSlice


def build_code_slices(
    project: Path,
    occurrences: list[ApiOccurrence],
    api_changes: list[ApiChange],
    *,
    radius: int = 8,
    max_slices: int = 80,
) -> list[CodeSlice]:
    """Build compact migration slices around discovered occurrences and benchmark hints."""
    slices: list[CodeSlice] = []
    by_file = _changes_by_file(api_changes)
    for occurrence in occurrences[:max_slices]:
        path = project / occurrence.file_path
        if not path.exists() or not path.is_file():
            continue
        slices.append(
            _slice_for_occurrence(
                project=project,
                path=path,
                occurrence=occurrence,
                related_changes=by_file.get(occurrence.file_path, []),
                radius=radius,
            )
        )

    seen_files = {slice_.file_path for slice_ in slices}
    for change in api_changes:
        if len(slices) >= max_slices:
            break
        if change.file_path in seen_files:
            continue
        path = project / change.file_path
        if not path.exists() or not path.is_file():
            continue
        slices.append(_slice_for_change(project, path, change, radius=radius))
        seen_files.add(change.file_path)
    return _dedupe_slices(slices)[:max_slices]


def _slice_for_occurrence(
    *,
    project: Path,
    path: Path,
    occurrence: ApiOccurrence,
    related_changes: list[ApiChange],
    radius: int,
) -> CodeSlice:
    lines = path.read_text(errors="replace").splitlines()
    start, end = _enclosing_node_span(path, occurrence.line)
    if start is None or end is None or end - start > max(radius * 4, 40):
        start = max(1, occurrence.line - radius)
        end = min(len(lines), occurrence.line + radius)
    return CodeSlice(
        file_path=str(path.relative_to(project)),
        start_line=start,
        end_line=end,
        reason=f"{occurrence.kind} occurrence of {occurrence.qualified_name}",
        code=_numbered_lines(lines, start, end),
        occurrence=occurrence,
        related_changes=tuple(related_changes),
    )


def _slice_for_change(project: Path, path: Path, change: ApiChange, *, radius: int) -> CodeSlice:
    lines = path.read_text(errors="replace").splitlines()
    line = _first_line_number(change.line) or 1
    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    return CodeSlice(
        file_path=str(path.relative_to(project)),
        start_line=start,
        end_line=end,
        reason=f"PyMigBench checklist item {change.line}",
        code=_numbered_lines(lines, start, end),
        related_changes=(change,),
    )


def _changes_by_file(api_changes: list[ApiChange]) -> dict[str, list[ApiChange]]:
    by_file: dict[str, list[ApiChange]] = {}
    for change in api_changes:
        by_file.setdefault(change.file_path, []).append(change)
    return by_file


def _enclosing_node_span(path: Path, line: int) -> tuple[int | None, int | None]:
    tree = parse_python_file(path)
    if tree is None:
        return None, None
    best: tuple[int, int] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if not start or not end or not (start <= line <= end):
            continue
        if best is None or (end - start) < (best[1] - best[0]):
            best = (start, end)
    return best if best else (None, None)


def _first_line_number(value: str) -> int | None:
    if not value:
        return None
    digits = []
    for char in value:
        if char.isdigit():
            digits.append(char)
        elif digits:
            break
    return int("".join(digits)) if digits else None


def _numbered_lines(lines: list[str], start: int, end: int) -> str:
    return "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))


def _dedupe_slices(slices: list[CodeSlice]) -> list[CodeSlice]:
    seen: set[tuple[str, int, int, str]] = set()
    unique: list[CodeSlice] = []
    for slice_ in slices:
        key = (slice_.file_path, slice_.start_line, slice_.end_line, slice_.reason)
        if key in seen:
            continue
        seen.add(key)
        unique.append(slice_)
    return unique
