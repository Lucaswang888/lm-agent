"""L1 static AST checker for migration completeness and local correctness."""

from __future__ import annotations

import ast
import difflib
import importlib
import pkgutil
import re
import shutil
import subprocess
import sys
import time
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path

from minisweagent.migration.checker.api_checks import check_api_changes
from minisweagent.migration.checker.models import CheckContext, Failure, LayerResult, PassRecord, SuggestedFix
from minisweagent.migration.checker.utils import (
    collect_python_scan_files,
    has_import_or_api_evidence,
    relative_path,
    strip_strings_and_comments,
)
from minisweagent.migration.discovery import discover_api_occurrences_in_files


@dataclass(frozen=True)
class SemRule:
    """Framework-specific semantic rule grounded in a migration taxonomy property."""

    id: str
    pattern: re.Pattern[str]
    diagnosis: str
    hint: str
    rule_ref: str


_SEM_RULES: dict[tuple[str, str], list[SemRule]] = {
    ("flask", "quart"): [
        SemRule(
            id="quart_request_args_sync",
            pattern=re.compile(r"\bawait\s+\(?\s*request\s*\.\s*args\b"),
            diagnosis="Quart request.args is synchronous; awaiting it changes behaviour.",
            hint="Remove await before request.args.",
            rule_ref="PyMigTax-asyncTrans",
        ),
        SemRule(
            id="quart_request_form_async",
            pattern=re.compile(r"\brequest\s*\.\s*form\b(?!\s*\))"),
            diagnosis="Quart request.form is async and usually must be awaited.",
            hint="Use await request.form where Flask code read request.form synchronously.",
            rule_ref="PyMigTax-asyncTrans",
        ),
        SemRule(
            id="quart_request_json_async",
            pattern=re.compile(r"\brequest\s*\.\s*json\b"),
            diagnosis="Quart request.json should generally become await request.get_json().",
            hint="Replace request.json with await request.get_json() inside async handlers.",
            rule_ref="PyMigTax-outTrans",
        ),
    ],
    ("attrs", "dataclasses"): [
        SemRule(
            id="dataclass_missing_decorator_call",
            pattern=re.compile(r"^@dataclass\s*$", re.MULTILINE),
            diagnosis="A bare dataclass decorator may have dropped attrs decorator arguments.",
            hint="Compare attrs arguments such as frozen/repr/order and preserve them when required.",
            rule_ref="PyMigTax-argTrans",
        ),
    ],
}


def register_rule(source: str, target: str, rule: SemRule) -> None:
    """Register a deterministic SEM rule for a source-target migration pair."""
    _SEM_RULES.setdefault((source.lower(), target.lower()), []).append(rule)


class L1StaticAstLayer:
    """Run alias-aware static checks and PIG-style ModifyPath import validation."""

    layer = "L1"

    def run(self, ctx: CheckContext) -> LayerResult:
        started = time.perf_counter()
        files = collect_python_scan_files(ctx.project, ctx.scopes)
        failures: list[Failure] = []
        passes: list[PassRecord] = []
        syntax_errors: list[str] = []
        target_evidence: list[str] = []
        source_apis = {api for change in ctx.api_changes for api in getattr(change, "source_apis", ()) if api}
        target_apis = {api for change in ctx.api_changes for api in getattr(change, "target_apis", ()) if api}

        for path in files:
            rel = relative_path(ctx.project, path)
            text = path.read_text(errors="replace")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    ast.parse(text, filename=str(path))
            except SyntaxError as exc:
                syntax_errors.append(f"{rel}:{exc.lineno}: {exc.msg}")
                failures.append(
                    Failure(
                        id=f"L1.MIN.{len(syntax_errors):03d}",
                        layer="L1",
                        category="MIN",
                        severity="blocker",
                        file=rel,
                        line=exc.lineno,
                        evidence={"syntax_error": exc.msg},
                        diagnosis=f"Python syntax error: {exc.msg}",
                        suggested_fix=SuggestedFix(kind="fix_syntax", edit_targets=(f"{rel}:{exc.lineno}",)),
                        rule_ref="Pyright/Pyflakes-MIN",
                    )
                )
            if has_import_or_api_evidence(text, ctx.target, target_apis):
                target_evidence.append(rel)
            failures.extend(self._sem_failures(ctx, rel, text))

        occurrences, parse_warnings = discover_api_occurrences_in_files(
            project=ctx.project,
            source=ctx.source,
            files=files,
            api_changes=list(ctx.api_changes),
        )
        mig_failures = [
            Failure(
                id=f"L1.MIG.{index:03d}",
                layer="L1",
                category="MIG",
                severity="blocker",
                file=occurrence.file_path,
                line=occurrence.line,
                evidence={
                    "api": occurrence.api,
                    "qualified_name": occurrence.qualified_name,
                    "kind": occurrence.kind,
                    "source_line": occurrence.source_line,
                },
                diagnosis=f"Source library residue remains: {occurrence.qualified_name}",
                suggested_fix=SuggestedFix(
                    kind="replace_source_call",
                    edit_targets=(f"{occurrence.file_path}:{occurrence.line}",),
                    from_pattern=occurrence.source_line,
                    hint=f"Replace remaining {ctx.source} usage with {ctx.target}.",
                ),
                rule_ref="PIG-Figure2-MIG",
            )
            for index, occurrence in enumerate(occurrences, 1)
        ]
        failures.extend(mig_failures)
        failures.extend(self._inc_failures(ctx, files))
        api_checks = check_api_changes(ctx.project, list(ctx.api_changes))
        for index, check in enumerate((check for check in api_checks if not check.passed), 1):
            failures.append(
                Failure(
                    id=f"L1.MIG.{len(mig_failures) + index:03d}",
                    layer="L1",
                    category="MIG",
                    severity="blocker",
                    file=check.file_path,
                    evidence=asdict(check),
                    diagnosis=check.reason,
                    suggested_fix=SuggestedFix(
                        kind="apply_ground_truth_api_change",
                        edit_targets=(f"{check.file_path}:{check.line}",),
                        from_pattern=check.source_pattern,
                        to_pattern=check.target_pattern,
                    ),
                    rule_ref="UsingLLMs-§IV.A",
                )
            )
        pyflakes_failures, pyflakes_extra = self._pyflakes_failures(ctx, files)
        failures.extend(pyflakes_failures)

        if not mig_failures:
            passes.append(PassRecord(id="L1.MIG", note="No source-library residue found with alias-aware AST scan."))
        if target_evidence:
            passes.append(
                PassRecord(
                    id="L1.target",
                    note=f"Target-library evidence found in {len(set(target_evidence))} file(s).",
                    extra={"target_evidence": tuple(sorted(set(target_evidence)))},
                )
            )
        if parse_warnings:
            passes.append(
                PassRecord(
                    id="L1.parse_warnings",
                    note="Some files could not be AST parsed.",
                    extra={"warnings": parse_warnings},
                )
            )
        return LayerResult(
            layer="L1",
            passed=not any(failure.severity == "blocker" for failure in failures),
            failures=tuple(failures),
            passes=tuple(passes),
            duration_seconds=time.perf_counter() - started,
            extra={
                "syntax_errors": tuple(sorted(set(syntax_errors))),
                "source_residue": tuple(sorted({failure.file for failure in mig_failures if failure.file})),
                "target_evidence": tuple(sorted(set(target_evidence))),
                "api_checks": tuple(asdict(check) for check in api_checks),
                "api_check_failures": tuple(
                    f"{check.file_path}:{check.line}: {check.reason}" for check in api_checks if not check.passed
                ),
                **pyflakes_extra,
            },
        )

    def _sem_failures(self, ctx: CheckContext, rel: str, text: str) -> list[Failure]:
        failures: list[Failure] = []
        code_text = strip_strings_and_comments(text)
        rules = _SEM_RULES.get((ctx.source.lower(), ctx.target.lower()), [])
        for rule in rules:
            for match in rule.pattern.finditer(code_text):
                line = code_text.count("\n", 0, match.start()) + 1
                failures.append(
                    Failure(
                        id=f"L1.SEM.{len(failures) + 1:03d}",
                        layer="L1",
                        category="SEM",
                        severity="blocker",
                        file=rel,
                        line=line,
                        evidence={"matched": match.group(0).strip(), "rule": rule.id},
                        diagnosis=rule.diagnosis,
                        suggested_fix=SuggestedFix(kind=rule.id, edit_targets=(f"{rel}:{line}",), hint=rule.hint),
                        rule_ref=rule.rule_ref,
                    )
                )
        return failures

    def _inc_failures(self, ctx: CheckContext, files: list[Path]) -> list[Failure]:
        valid_paths = _valid_import_paths(ctx.target)
        if not valid_paths:
            return []
        failures: list[Failure] = []
        for path in files:
            rel = relative_path(ctx.project, path)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    tree = ast.parse(path.read_text(errors="replace"), filename=str(path))
            except SyntaxError:
                continue
            for used_path, line in _target_import_paths(tree, ctx.target):
                if used_path in valid_paths:
                    continue
                candidates = tuple(difflib.get_close_matches(used_path, sorted(valid_paths), n=3, cutoff=0.5))
                if not candidates and "." in used_path:
                    candidates = (ctx.target,)
                failures.append(
                    Failure(
                        id=f"L1.INC.{len(failures) + 1:03d}",
                        layer="L1",
                        category="INC",
                        severity="blocker",
                        file=rel,
                        line=line,
                        evidence={"used_path": used_path},
                        diagnosis=f"Target import path does not appear importable: {used_path}",
                        suggested_fix=SuggestedFix(
                            kind="rename_import",
                            edit_targets=(f"{rel}:{line}",),
                            from_pattern=used_path,
                            to_pattern=candidates[0] if candidates else None,
                            candidates=candidates,
                            similarity_score=1.0 if candidates else None,
                        ),
                        rule_ref="PIG-§3.3.2-ModifyPath",
                    )
                )
        return failures

    def _pyflakes_failures(self, ctx: CheckContext, files: list[Path]) -> tuple[list[Failure], dict[str, object]]:
        if not shutil.which("ruff"):
            return [], {"pyflakes_skipped": True}
        failures: list[Failure] = []
        command = [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "F821",
            "--output-format",
            "json",
            *(str(path) for path in files),
        ]
        try:
            result = subprocess.run(command, cwd=ctx.project, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return [], {"pyflakes_skipped": True}
        if result.returncode not in {0, 1}:
            return [], {"pyflakes_skipped": True, "pyflakes_error": result.stderr[-500:]}
        try:
            import json

            diagnostics = json.loads(result.stdout or "[]")
        except ValueError:
            return [], {"pyflakes_skipped": True}
        for diagnostic in diagnostics:
            path = Path(diagnostic.get("filename", ""))
            rel = relative_path(ctx.project, path)
            location = diagnostic.get("location", {})
            line = int(location.get("row", 1))
            failures.append(
                Failure(
                    id=f"L1.MIN.{len(failures) + 1:03d}",
                    layer="L1",
                    category="MIN",
                    severity="blocker",
                    file=rel,
                    line=line,
                    evidence={"message": diagnostic.get("message", ""), "code": diagnostic.get("code", "")},
                    diagnosis=diagnostic.get("message", "Undefined name detected."),
                    suggested_fix=SuggestedFix(kind="add_import_or_define", edit_targets=(f"{rel}:{line}",)),
                    rule_ref="Pyflakes-MIN",
                )
            )
        return failures, {"pyflakes_skipped": False}


def _valid_import_paths(target: str) -> set[str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Warning)
            package = importlib.import_module(target)
    except Exception:
        return set()
    valid = {target}
    package_path = getattr(package, "__path__", None)
    if package_path is None:
        return valid
    for module in pkgutil.walk_packages(package_path, prefix=f"{target}."):
        valid.add(module.name)
    return valid


def _target_import_paths(tree: ast.Module, target: str) -> list[tuple[str, int]]:
    paths: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target or alias.name.startswith(f"{target}."):
                    paths.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == target or node.module.startswith(f"{target}."):
                paths.append((node.module, node.lineno))
    return paths
