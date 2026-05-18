"""Data models for the multi-layer migration checker."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Category = Literal["INC", "SEM", "MIG", "MIN", "ENV", "COV"]
Severity = Literal["blocker", "warning", "info"]
LayerName = Literal["L0", "L1", "L2", "L3", "L4"]


@dataclass(frozen=True)
class SuggestedFix:
    """Machine-actionable repair hint returned to the migration agent."""

    kind: str
    edit_targets: tuple[str, ...] = ()
    from_pattern: str | None = None
    to_pattern: str | None = None
    candidates: tuple[str, ...] = ()
    similarity_score: float | None = None
    hint: str | None = None


@dataclass(frozen=True)
class Failure:
    """One actionable checker failure."""

    id: str
    layer: LayerName
    category: Category
    severity: Severity
    file: str | None = None
    line: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    diagnosis: str = ""
    suggested_fix: SuggestedFix | None = None
    rule_ref: str | None = None


@dataclass(frozen=True)
class PassRecord:
    """A positive check result that tells the agent what not to undo."""

    id: str
    note: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CoverageInfo:
    """Coverage summary for migration-related validation."""

    migration_related_lines: int = 0
    covered_lines: int = 0
    coverage_ratio: float = 0.0
    tests_used_for_validation: int = 0
    warn_if_below: float = 0.8
    skipped_reason: str | None = None


@dataclass(frozen=True)
class LayerResult:
    """Result produced by one checker layer."""

    layer: LayerName
    passed: bool
    failures: tuple[Failure, ...] = ()
    passes: tuple[PassRecord, ...] = ()
    duration_seconds: float = 0.0
    short_circuit: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckContext:
    """Inputs shared by all checker layers."""

    project: Path
    source: str
    target: str
    scopes: tuple[str, ...] = ()
    api_changes: tuple[Any, ...] = ()
    pre_migration_ref: str | None = None
    test_commands: tuple[str, ...] = ()
    target_version: str | None = None
    layers_to_run: tuple[LayerName, ...] = ("L0", "L1", "L2", "L3")
    enable_l4: bool = False
    timeout_seconds_per_layer: dict[LayerName, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckerReport:
    """Final JSON-serialisable checker report."""

    verifier_version: str
    summary: dict[str, Any]
    layers: tuple[LayerResult, ...]
    failures: tuple[Failure, ...]
    passes: tuple[PassRecord, ...]
    coverage: CoverageInfo
    escalation: dict[str, Any]

    @property
    def passed(self) -> bool:
        return all(layer.passed for layer in self.layers)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["passed"] = self.passed
        return data
