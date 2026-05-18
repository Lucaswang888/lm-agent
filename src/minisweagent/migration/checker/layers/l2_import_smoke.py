"""L2 import smoke checker."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from minisweagent.migration.checker.models import CheckContext, Failure, LayerResult, PassRecord, SuggestedFix
from minisweagent.migration.checker.utils import collect_python_scan_files, has_import_or_api_evidence, relative_path


WEB_FRAMEWORKS = {"flask", "quart", "fastapi", "starlette"}
WEB_ENTRYPOINT_NAMES = {"app.py", "wsgi.py", "asgi.py"}


@dataclass(frozen=True)
class ImportTarget:
    module: str
    cwd: Path
    source_file: Path

    def label(self, project: Path) -> str:
        return f"{self.module} @ {relative_path(project, self.cwd)} ({relative_path(project, self.source_file)})"


class L2ImportSmokeLayer:
    """Import project modules in isolated subprocesses to catch import-time regressions."""

    layer = "L2"

    def run(self, ctx: CheckContext) -> LayerResult:
        started = time.perf_counter()
        targets = tuple(sorted(_import_targets(ctx, collect_python_scan_files(ctx.project, ctx.scopes)), key=lambda item: item.label(ctx.project)))
        if not targets:
            return LayerResult(
                layer="L2",
                passed=True,
                passes=(PassRecord(id="L2.smoke", note="No safe importable project modules found; import smoke skipped."),),
                duration_seconds=time.perf_counter() - started,
                extra={"modules": (), "skipped_reason": "no_importable_modules"},
            )
        failures: list[Failure] = []
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        timeout = ctx.timeout_seconds_per_layer.get("L2", 30)
        for target in targets:
            result = subprocess.run(
                [sys.executable, "-c", f"import {target.module}"],
                cwd=target.cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if result.returncode == 0:
                continue
            traceback_tail = "\n".join((result.stderr or result.stdout).splitlines()[-50:])
            failures.append(
                Failure(
                    id=f"L2.MIN.{len(failures) + 1:03d}",
                    layer="L2",
                    category="MIN",
                    severity="blocker",
                    evidence={
                        "module": target.module,
                        "cwd": relative_path(ctx.project, target.cwd),
                        "source_file": relative_path(ctx.project, target.source_file),
                        "traceback_tail": traceback_tail,
                    },
                    diagnosis=f"Module import failed: {target.label(ctx.project)}",
                    suggested_fix=SuggestedFix(kind="fix_import_time_error", edit_targets=(), hint="Inspect traceback_tail first."),
                    rule_ref="UsingLLMs-§IX.B-import-regression",
                )
            )
        module_labels = tuple(target.label(ctx.project) for target in targets)
        return LayerResult(
            layer="L2",
            passed=not failures,
            failures=tuple(failures),
            passes=(PassRecord(id="L2.smoke", note=f"{len(targets) - len(failures)} module(s) imported cleanly."),),
            duration_seconds=time.perf_counter() - started,
            extra={"modules": module_labels},
        )


def _import_targets(ctx: CheckContext, files: list[Path]) -> set[ImportTarget]:
    targets: set[ImportTarget] = set()
    for path in files:
        if path.name.startswith("test_") or "tests" in path.parts:
            continue
        if path.suffix != ".py":
            continue
        module = _module_name(ctx.project, path)
        if module:
            targets.add(ImportTarget(module=module, cwd=ctx.project, source_file=path))
    targets.update(_web_entrypoint_targets(ctx, files))
    return targets


def _module_name(project: Path, path: Path) -> str | None:
    rel = Path(relative_path(project, path))
    if rel.name == "__init__.py":
        parts = rel.parent.parts
    else:
        parts = rel.with_suffix("").parts
    if not parts:
        return None
    if len(parts) > 1:
        parent = project.joinpath(*parts[:-1])
        if not (parent / "__init__.py").exists():
            return None
    elif not (project / parts[0] / "__init__.py").exists() and rel.name != "__init__.py":
        return None
    return ".".join(parts)


def _web_entrypoint_targets(ctx: CheckContext, files: list[Path]) -> set[ImportTarget]:
    libraries = {ctx.source.lower(), ctx.target.lower()}
    if not libraries.intersection(WEB_FRAMEWORKS):
        return set()
    targets: set[ImportTarget] = set()
    for path in files:
        if path.name not in WEB_ENTRYPOINT_NAMES:
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if not (
            has_import_or_api_evidence(text, ctx.source, set())
            or has_import_or_api_evidence(text, ctx.target, set())
        ):
            continue
        targets.add(ImportTarget(module=path.stem, cwd=path.parent, source_file=path))
    return targets
