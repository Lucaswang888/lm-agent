"""Data models for the PIG-style migration planner."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ApiChange:
    """One source API to target API migration fact."""

    file_path: str
    line: str
    source_apis: tuple[str, ...] = ()
    target_apis: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()
    source_program_elements: tuple[str, ...] = ()
    target_program_elements: tuple[str, ...] = ()
    cardinality: str | None = None
    source_snippet: str | None = None
    target_snippet: str | None = None
    source_removed_required: bool = True
    target_required: bool = True
    origin: str = "pymigbench"


@dataclass(frozen=True)
class ApiOccurrence:
    """A concrete source API occurrence found in a project."""

    file_path: str
    line: int
    column: int
    api: str
    qualified_name: str
    kind: str
    source_line: str
    enclosing_scope: str | None = None


@dataclass(frozen=True)
class CodeSlice:
    """A small migration-relevant code slice around an API occurrence."""

    file_path: str
    start_line: int
    end_line: int
    reason: str
    code: str
    occurrence: ApiOccurrence | None = None
    related_changes: tuple[ApiChange, ...] = ()


@dataclass(frozen=True)
class CandidateApi:
    """A target-library API candidate ranked for migration."""

    api: str
    score: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PigContext:
    """The complete PIG-style pre-migration context handed to the agent."""

    project: str
    source: str
    target: str
    strategy: str
    api_changes: tuple[ApiChange, ...] = ()
    occurrences: tuple[ApiOccurrence, ...] = ()
    slices: tuple[CodeSlice, ...] = ()
    candidates: tuple[CandidateApi, ...] = ()
    inspected_files: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)
