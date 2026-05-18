"""Shared helpers for deterministic migration checker layers."""

from __future__ import annotations

import configparser
import io
import re
import tokenize
import tomllib
from importlib import metadata
from pathlib import Path

from minisweagent.migration.discovery import IGNORED_DIRS, iter_python_files

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

ENTRYPOINT_DIRS = ("bin", "scripts", "tools")


def collect_python_scan_files(project: Path, scopes: tuple[str, ...] | list[str] = ()) -> list[Path]:
    """Return `.py` files plus executable Python entry scripts that can hide migration residue."""
    files = set(iter_python_files(project, list(scopes)))
    for path in _entrypoint_candidates(project, scopes):
        if _is_python_script(path):
            files.add(path)
    for module_name in _console_script_modules(project):
        module_file = _module_name_to_path(project, module_name)
        if module_file:
            files.add(module_file)
    return sorted(files)


def dependency_findings(project: Path, source: str, target: str) -> list[str]:
    """Scan dependency/config files for source residue and target evidence."""
    findings: list[str] = []
    source_lower = source.lower()
    target_lower = target.lower()
    for dep_file in DEPENDENCY_FILES:
        path = project / dep_file
        if not path.exists() or not path.is_file():
            continue
        text_lower = path.read_text(errors="replace").lower()
        if source and source_lower in text_lower and target_lower not in text_lower:
            findings.append(f"{dep_file}: still mentions {source!r} without {target!r}")
        elif target and target_lower in text_lower:
            findings.append(f"{dep_file}: mentions target {target!r}")
    return findings


def import_aliases_for_distribution(package_name: str) -> tuple[str, ...]:
    """Resolve package names to import names using installed package metadata when available."""
    aliases = {package_name}
    try:
        packages = metadata.packages_distributions()
    except Exception:
        packages = {}
    normalized = _normalize_dist_name(package_name)
    for import_name, distributions in packages.items():
        if any(_normalize_dist_name(dist) == normalized for dist in distributions):
            aliases.add(import_name)
    try:
        dist = metadata.distribution(package_name)
    except metadata.PackageNotFoundError:
        return tuple(sorted(aliases))
    top_level = dist.read_text("top_level.txt")
    if top_level:
        aliases.update(line.strip() for line in top_level.splitlines() if line.strip())
    return tuple(sorted(aliases))


def strip_strings_and_comments(text: str) -> str:
    """Return code-shaped text so comments/docstrings do not count as migration residue."""
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        return tokenize.untokenize(
            (token.type, "" if token.type in {tokenize.STRING, tokenize.COMMENT} else token.string)
            for token in tokens
        )
    except (tokenize.TokenError, IndentationError):
        return text


def has_import_or_api_evidence(text: str, library: str, apis: set[str]) -> bool:
    """Detect import/API evidence in token-filtered source text."""
    code_text = strip_strings_and_comments(text)
    if library and re.search(rf"\b(import|from)\s+{re.escape(library)}\b", code_text, flags=re.IGNORECASE):
        return True
    for api in apis:
        if "." in api and re.search(rf"\b{re.escape(api)}\b", code_text):
            return True
        leaf = api.rsplit(".", 1)[-1]
        if len(leaf) >= 3 and re.search(rf"\b{re.escape(leaf)}\b", code_text):
            return True
    return False


def relative_path(project: Path, path: Path) -> str:
    """Return a stable project-relative path string."""
    try:
        return str(path.relative_to(project))
    except ValueError:
        return str(path)


def _entrypoint_candidates(project: Path, scopes: tuple[str, ...] | list[str]) -> list[Path]:
    roots: list[Path] = []
    if scopes:
        for scope in scopes:
            path = project / scope.split(":", 1)[0]
            roots.append(path)
    else:
        roots.extend(project / dirname for dirname in ENTRYPOINT_DIRS)
    candidates: list[Path] = []
    for root in roots:
        if root.is_file():
            candidates.append(root)
            continue
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(project).parts
            if any(part in IGNORED_DIRS for part in rel_parts):
                continue
            candidates.append(path)
    return candidates


def _is_python_script(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            first_line = handle.readline(200).decode(errors="ignore").lower()
    except OSError:
        return False
    return first_line.startswith("#!") and "python" in first_line


def _console_script_modules(project: Path) -> set[str]:
    modules: set[str] = set()
    modules.update(_pyproject_script_modules(project / "pyproject.toml"))
    modules.update(_setup_cfg_script_modules(project / "setup.cfg"))
    return modules


def _pyproject_script_modules(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = tomllib.loads(path.read_text(errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return set()
    scripts = data.get("project", {}).get("scripts", {})
    gui_scripts = data.get("project", {}).get("gui-scripts", {})
    modules: set[str] = set()
    for value in [*scripts.values(), *gui_scripts.values()]:
        if isinstance(value, str):
            modules.add(value.split(":", 1)[0])
    return modules


def _setup_cfg_script_modules(path: Path) -> set[str]:
    if not path.exists():
        return set()
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except configparser.Error:
        return set()
    modules: set[str] = set()
    if parser.has_section("options.entry_points") and parser.has_option("options.entry_points", "console_scripts"):
        value = parser.get("options.entry_points", "console_scripts")
        modules.update(_entrypoint_modules_from_lines(value.splitlines()))
    return modules


def _entrypoint_modules_from_lines(lines: list[str]) -> set[str]:
    modules: set[str] = set()
    for line in lines:
        if "=" not in line:
            continue
        _, target = line.split("=", 1)
        module = target.strip().split(":", 1)[0]
        if module:
            modules.add(module)
    return modules


def _module_name_to_path(project: Path, module_name: str) -> Path | None:
    parts = module_name.split(".")
    module_path = project.joinpath(*parts).with_suffix(".py")
    if module_path.exists() and module_path.is_file():
        return module_path
    package_path = project.joinpath(*parts, "__init__.py")
    if package_path.exists() and package_path.is_file():
        return package_path
    return None


def _normalize_dist_name(name: str) -> str:
    return name.lower().replace("_", "-")
