"""L0 environment and scan-surface probe."""

from __future__ import annotations

import time

from minisweagent.migration.checker.models import CheckContext, Failure, LayerResult, PassRecord
from minisweagent.migration.checker.utils import (
    collect_python_scan_files,
    dependency_findings,
    import_aliases_for_distribution,
    relative_path,
)


class L0EnvProbeLayer:
    """Discover the files and dependency metadata the later checker layers should inspect."""

    layer = "L0"

    def run(self, ctx: CheckContext) -> LayerResult:
        started = time.perf_counter()
        files = collect_python_scan_files(ctx.project, ctx.scopes)
        findings = dependency_findings(ctx.project, ctx.source, ctx.target)
        aliases = import_aliases_for_distribution(ctx.source)
        failures = tuple(
            Failure(
                id=f"L0.ENV.{index:03d}",
                layer="L0",
                category="ENV",
                severity="warning",
                file=finding.split(":", 1)[0],
                evidence={"finding": finding},
                diagnosis=finding,
                rule_ref="ExecutionAgent-env-probe",
            )
            for index, finding in enumerate(findings, 1)
            if "still mentions" in finding
        )
        rel_files = tuple(relative_path(ctx.project, path) for path in files)
        passes = (
            PassRecord(
                id="L0.scan",
                note=f"Inspected {len(rel_files)} Python source files including shebang/console entrypoints.",
                extra={"inspected_files": rel_files},
            ),
        )
        return LayerResult(
            layer="L0",
            passed=True,
            failures=failures,
            passes=passes,
            duration_seconds=time.perf_counter() - started,
            extra={
                "inspected_files": rel_files,
                "import_aliases": {ctx.source: aliases},
                "dependency_findings": tuple(sorted(findings)),
            },
        )
