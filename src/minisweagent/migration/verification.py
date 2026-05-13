"""Offline migration verification helpers."""

from __future__ import annotations

import ast
import io
import re
import subprocess
import tokenize
from dataclasses import asdict, dataclass
from pathlib import Path

from minisweagent.migration.discovery import iter_python_files
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


@dataclass(frozen=True)
class ApiCheckResult:
    """One strict API-level migration check."""

    file_path: str
    line: str
    source_removed: bool
    target_present: bool
    passed: bool
    source_pattern: str
    target_pattern: str
    reason: str


@dataclass(frozen=True)
class VerificationReport:
    """Static checks that support migration validation."""

    syntax_errors: tuple[str, ...]
    source_residue: tuple[str, ...]
    target_evidence: tuple[str, ...]
    dependency_findings: tuple[str, ...]
    api_checks: tuple[ApiCheckResult, ...] = ()
    api_check_failures: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.syntax_errors and not self.source_residue and not self.api_check_failures

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
    """Run lightweight static checks for a migrated project."""
    syntax_errors: list[str] = []
    source_residue: list[str] = []
    target_evidence: list[str] = []
    dependency_findings: list[str] = []
    source_apis = {api for change in api_changes or [] for api in change.source_apis if api}
    target_apis = {api for change in api_changes or [] for api in change.target_apis if api}
    api_checks = _check_api_changes(project, api_changes or [])
    api_check_failures = [
        f"{check.file_path}:{check.line}: {check.reason}" for check in api_checks if not check.passed
    ]

    for path in iter_python_files(project, scopes or []):
        rel = str(path.relative_to(project))
        text = path.read_text(errors="replace")
        try:
            ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            syntax_errors.append(f"{rel}:{exc.lineno}: {exc.msg}")
        if _has_source_residue(text, source, source_apis):
            source_residue.append(rel)
        if _has_target_evidence(text, target, target_apis):
            target_evidence.append(rel)
        api_check_failures.extend(_framework_specific_failures(rel, text, target))

    for dep_file in DEPENDENCY_FILES:
        path = project / dep_file
        if not path.exists() or not path.is_file():
            continue
        text = path.read_text(errors="replace")
        if source and source in text and target not in text:
            dependency_findings.append(f"{dep_file}: still mentions {source!r} without {target!r}")
        elif target and target in text:
            dependency_findings.append(f"{dep_file}: mentions target {target!r}")

    return VerificationReport(
        syntax_errors=tuple(sorted(set(syntax_errors))),
        source_residue=tuple(sorted(set(source_residue))),
        target_evidence=tuple(sorted(set(target_evidence))),
        dependency_findings=tuple(sorted(set(dependency_findings))),
        api_checks=tuple(api_checks),
        api_check_failures=tuple(api_check_failures),
    )


def _framework_specific_failures(rel: str, text: str, target: str) -> list[str]:
    failures: list[str] = []
    if target.lower() == "quart" and re.search(r"\bawait\s+\(?\s*request\.args\b", text):
        failures.append(f"{rel}: Quart request.args is synchronous; remove await before request.args")
    return failures


def _has_source_residue(text: str, source: str, source_apis: set[str]) -> bool:
    code_text = _strip_strings_and_comments(text)
    if source and re.search(rf"\b(import|from)\s+{re.escape(source)}\b", code_text):
        return True
    for api in source_apis:
        if "." in api and re.search(rf"\b{re.escape(api)}\b", code_text):
            return True
    return False


def _has_target_evidence(text: str, target: str, target_apis: set[str]) -> bool:
    code_text = _strip_strings_and_comments(text)
    if target and re.search(rf"\b(import|from)\s+{re.escape(target)}\b", code_text):
        return True
    for api in target_apis:
        if "." in api and re.search(rf"\b{re.escape(api)}\b", code_text):
            return True
        leaf = api.rsplit(".", 1)[-1]
        if len(leaf) >= 3 and re.search(rf"\b{re.escape(leaf)}\b", code_text):
            return True
    return False


def _strip_strings_and_comments(text: str) -> str:
    """Return code-shaped text so negative test assertions do not count as residue."""
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        return tokenize.untokenize(
            (token.type, "" if token.type in {tokenize.STRING, tokenize.COMMENT} else token.string)
            for token in tokens
        )
    except (tokenize.TokenError, IndentationError):
        return text


def _check_api_changes(project: Path, api_changes: list[ApiChange]) -> list[ApiCheckResult]:
    checks: list[ApiCheckResult] = []
    for change in api_changes:
        path = project / change.file_path
        if not path.exists() or not path.is_file():
            checks.append(
                ApiCheckResult(
                    file_path=change.file_path,
                    line=change.line,
                    source_removed=False,
                    target_present=False,
                    passed=False,
                    source_pattern=_pattern_label(change.source_snippet, change.source_apis),
                    target_pattern=_pattern_label(change.target_snippet, change.target_apis),
                    reason="expected migration file is missing",
                )
            )
            continue
        text = path.read_text(errors="replace")
        source_removed = _source_removed_for_change(text, change)
        target_present = _target_present_for_change(text, change)
        semantic_reason = _semantic_api_check(project, change, text)
        if semantic_reason:
            target_present = False
        passed = source_removed and target_present
        reason = "passed"
        if semantic_reason:
            reason = semantic_reason
        elif not source_removed and not target_present:
            reason = "source pattern remains and target pattern is missing"
        elif not source_removed:
            reason = "source pattern remains"
        elif not target_present:
            reason = "target pattern is missing"
        checks.append(
            ApiCheckResult(
                file_path=change.file_path,
                line=change.line,
                source_removed=source_removed,
                target_present=target_present,
                passed=passed,
                source_pattern=_pattern_label(change.source_snippet, change.source_apis),
                target_pattern=_pattern_label(change.target_snippet, change.target_apis),
                reason=reason,
            )
        )
    return checks


def _source_removed_for_change(text: str, change: ApiChange) -> bool:
    if not change.source_removed_required:
        return True
    if change.source_snippet:
        return not _contains_normalized(text, change.source_snippet)
    return not _has_source_residue(text, "", set(change.source_apis))


def _target_present_for_change(text: str, change: ApiChange) -> bool:
    if not change.target_required:
        return True
    if change.target_snippet:
        return _contains_normalized(text, change.target_snippet)
    return _has_target_evidence(text, "", set(change.target_apis))


def _contains_normalized(text: str, snippet: str) -> bool:
    return _normalize_code(snippet) in _normalize_code(text)


def _normalize_code(value: str) -> str:
    return "\n".join(line.strip() for line in value.strip().splitlines() if line.strip())


def _pattern_label(snippet: str | None, apis: tuple[str, ...]) -> str:
    if snippet:
        return " ".join(snippet.strip().split())
    return ", ".join(apis)


def _semantic_api_check(project: Path, change: ApiChange, migrated_text: str) -> str | None:
    if not _is_attrs_dataclass_decorator_change(change):
        return None
    original_text = _git_head_file_text(project, change.file_path)
    if not original_text:
        return None
    source_line_number, target_line_number = _line_pair(change.line)
    if source_line_number is None or target_line_number is None:
        return None
    original_line = _line_at(original_text, source_line_number)
    migrated_line = _nearest_decorator_line(migrated_text, target_line_number)
    if not original_line or not migrated_line:
        return None
    missing: list[str] = []
    for argument in ("frozen", "repr"):
        expected = _keyword_argument(original_line, argument)
        if expected and f"{argument}={expected}" not in migrated_line:
            missing.append(f"{argument}={expected}")
    if missing:
        return "target dataclass decorator is missing preserved argument(s): " + ", ".join(missing)
    return None


def _is_attrs_dataclass_decorator_change(change: ApiChange) -> bool:
    return (
        "decorator" in change.source_program_elements
        and "decorator" in change.target_program_elements
        and any(api in {"s", "attr.s"} for api in change.source_apis)
        and any(api in {"dataclass", "dataclasses.dataclass"} for api in change.target_apis)
    )


def _git_head_file_text(project: Path, file_path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{file_path}"],
            cwd=project,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _line_pair(value: str) -> tuple[int | None, int | None]:
    match = re.match(r"^(\d+)(?:-\d+)?:?(\d+)?", value or "")
    if not match:
        return None, None
    source_line = int(match.group(1))
    target_line = int(match.group(2)) if match.group(2) else source_line
    return source_line, target_line


def _line_at(text: str, line_number: int) -> str | None:
    lines = text.splitlines()
    if line_number < 1 or line_number > len(lines):
        return None
    return lines[line_number - 1].strip()


def _nearest_decorator_line(text: str, target_line: int) -> str | None:
    lines = text.splitlines()
    start = max(1, target_line - 3)
    end = min(len(lines), target_line + 3)
    for line_number in range(start, end + 1):
        line = lines[line_number - 1].strip()
        if line.startswith("@") and "dataclass" in line:
            return line
    return None


def _keyword_argument(line: str, name: str) -> str | None:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*([^,\)]+)", line)
    return match.group(1).strip() if match else None
