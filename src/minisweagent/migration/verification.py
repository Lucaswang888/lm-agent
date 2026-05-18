"""Backward-compatible migration verification entrypoints."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from minisweagent.migration.checker import CheckContext, CheckerReport, run_default_pipeline
from minisweagent.migration.checker.api_checks import ApiCheckResult
from minisweagent.migration.pig_models import ApiChange

DEPENDENCY_FILES = (
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

WEB_FRAMEWORKS = {"flask", "quart", "fastapi", "starlette"}


@dataclass(frozen=True)
class VerificationReport:
    """Legacy static checks that support migration validation."""

    syntax_errors: tuple[str, ...]
    source_residue: tuple[str, ...]
    target_evidence: tuple[str, ...]
    dependency_findings: tuple[str, ...]
    api_checks: tuple[ApiCheckResult, ...] = ()
    api_check_failures: tuple[str, ...] = ()
    checker_report: dict[str, object] | None = None

    @property
    def passed(self) -> bool:
        dependency_failures = [finding for finding in self.dependency_findings if "still mentions" in finding]
        return (
            not self.syntax_errors
            and not self.source_residue
            and not self.api_check_failures
            and not dependency_failures
            and not _checker_has_blockers(self.checker_report)
        )

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["passed"] = self.passed
        return data


def verify_project_migration(
    *,
    project: Path,
    source: str,
    target: str,
    scopes: list[str] | None = None,
    api_changes: list[ApiChange] | None = None,
) -> VerificationReport:
    """Run the v2 checker through a legacy-compatible strict verification API."""
    ctx = CheckContext(
        project=project,
        source=source,
        target=target,
        scopes=tuple(scopes or []),
        api_changes=tuple(api_changes or []),
        layers_to_run=_strict_layers_for(source, target),
    )
    return _legacy_report_from_checker(run_default_pipeline(ctx))


def _legacy_report_from_checker(report: CheckerReport) -> VerificationReport:
    l0_extra = _layer_extra(report, "L0")
    l1_extra = _layer_extra(report, "L1")
    api_checks = tuple(_api_check_from_dict(item) for item in l1_extra.get("api_checks", ()))
    source_residue = tuple(sorted({failure.file for failure in report.failures if failure.category == "MIG" and failure.file}))
    syntax_errors = tuple(l1_extra.get("syntax_errors", ()))
    target_evidence = tuple(l1_extra.get("target_evidence", ()))
    dependency_findings = tuple(l0_extra.get("dependency_findings", ()))
    api_check_failures = tuple(l1_extra.get("api_check_failures", ()))
    return VerificationReport(
        syntax_errors=syntax_errors,
        source_residue=source_residue,
        target_evidence=target_evidence,
        dependency_findings=dependency_findings,
        api_checks=api_checks,
        api_check_failures=api_check_failures,
        checker_report=report.to_dict(),
    )


def _layer_extra(report: CheckerReport, layer_name: str) -> dict[str, object]:
    for layer in report.layers:
        if layer.layer == layer_name:
            return layer.extra
    return {}


def _api_check_from_dict(data: object) -> ApiCheckResult:
    if isinstance(data, ApiCheckResult):
        return data
    if not isinstance(data, dict):
        return ApiCheckResult("", "", False, False, False, "", "", "invalid api check payload")
    return ApiCheckResult(
        file_path=str(data.get("file_path", "")),
        line=str(data.get("line", "")),
        source_removed=bool(data.get("source_removed", False)),
        target_present=bool(data.get("target_present", False)),
        passed=bool(data.get("passed", False)),
        source_pattern=str(data.get("source_pattern", "")),
        target_pattern=str(data.get("target_pattern", "")),
        reason=str(data.get("reason", "")),
    )


def _checker_has_blockers(checker_report: dict[str, object] | None) -> bool:
    if not checker_report:
        return False
    summary = checker_report.get("summary")
    if not isinstance(summary, dict):
        return False
    return int(summary.get("blocker_count", 0)) > 0


def _strict_layers_for(source: str, target: str) -> tuple[str, ...]:
    libraries = {source.lower(), target.lower()}
    if libraries.intersection(WEB_FRAMEWORKS):
        return ("L0", "L1", "L2")
    return ("L0", "L1")
