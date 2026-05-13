"""AST-based source API discovery for library migrations."""

from __future__ import annotations

import ast
from pathlib import Path

from minisweagent.migration.pig_models import ApiChange, ApiOccurrence

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
}


def iter_python_files(project: Path, scopes: list[str] | None = None) -> list[Path]:
    """Return Python files under priority scopes or the project root."""
    roots = _scope_roots(project, scopes or [])
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
            continue
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if any(part in IGNORED_DIRS for part in path.relative_to(project).parts):
                continue
            files.append(path)
    return sorted(set(files))


def parse_python_file(path: Path) -> ast.Module | None:
    """Parse a Python file, returning None when the file is not parseable."""
    try:
        return ast.parse(path.read_text(errors="replace"), filename=str(path))
    except SyntaxError:
        return None


def discover_api_occurrences(
    project: Path,
    source: str,
    scopes: list[str] | None = None,
    api_changes: list[ApiChange] | None = None,
) -> tuple[list[ApiOccurrence], list[str]]:
    """Find source-library imports, calls, and attributes in project files."""
    warnings: list[str] = []
    occurrences: list[ApiOccurrence] = []
    wanted_apis = _wanted_source_apis(api_changes or [])
    for path in iter_python_files(project, scopes):
        tree = parse_python_file(path)
        if tree is None:
            warnings.append(f"Could not parse {path.relative_to(project)}; skipped AST discovery for that file.")
            continue
        text = path.read_text(errors="replace")
        aliases = _source_aliases(tree, source)
        visitor = _OccurrenceVisitor(
            project=project,
            path=path,
            source=source,
            aliases=aliases,
            wanted_apis=wanted_apis,
            lines=text.splitlines(),
        )
        visitor.visit(tree)
        occurrences.extend(visitor.occurrences)
    return _dedupe_occurrences(occurrences), warnings


def _scope_roots(project: Path, scopes: list[str]) -> list[Path]:
    if not scopes:
        return [project]
    roots: list[Path] = []
    for scope in scopes:
        clean_scope = scope.split(":", 1)[0]
        path = Path(clean_scope)
        roots.append(path if path.is_absolute() else project / path)
    return roots


def _wanted_source_apis(api_changes: list[ApiChange]) -> set[str]:
    apis: set[str] = set()
    for change in api_changes:
        for api in change.source_apis:
            apis.add(api)
            apis.add(api.rsplit(".", 1)[-1])
    return {api for api in apis if api}


def _source_aliases(tree: ast.Module, source: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_matches_source(alias.name, source):
                    aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _module_matches_source(node.module, source):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local_name = alias.asname or alias.name
                    aliases[local_name] = f"{node.module}.{alias.name}"
    return aliases


class _OccurrenceVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        project: Path,
        path: Path,
        source: str,
        aliases: dict[str, str],
        wanted_apis: set[str],
        lines: list[str],
    ) -> None:
        self.project = project
        self.path = path
        self.source = source
        self.aliases = aliases
        self.wanted_apis = wanted_apis
        self.lines = lines
        self.scope_stack: list[str] = []
        self.occurrences: list[ApiOccurrence] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _module_matches_source(alias.name, self.source):
                self._add(node, alias.name, alias.name, "import")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and _module_matches_source(node.module, self.source):
            for alias in node.names:
                self._add(node, alias.name, f"{node.module}.{alias.name}", "import-from")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _expr_name(node.func)
        if name:
            self._maybe_add_expr(node.func, name, "call")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = _expr_name(node)
        if name:
            self._maybe_add_expr(node, name, "attribute")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.aliases or node.id in self.wanted_apis:
            qualified = self.aliases.get(node.id, node.id)
            self._add(node, node.id, qualified, "name")
        self.generic_visit(node)

    def _maybe_add_expr(self, node: ast.AST, name: str, kind: str) -> None:
        root = name.split(".", 1)[0]
        leaf = name.rsplit(".", 1)[-1]
        if root in self.aliases:
            qualified = f"{self.aliases[root]}{name[len(root):]}"
            self._add(node, leaf, qualified, kind)
        elif leaf in self.wanted_apis or name in self.wanted_apis:
            self._add(node, leaf, name, kind)

    def _add(self, node: ast.AST, api: str, qualified_name: str, kind: str) -> None:
        line = getattr(node, "lineno", 1)
        column = getattr(node, "col_offset", 0)
        source_line = self.lines[line - 1].strip() if 0 < line <= len(self.lines) else ""
        self.occurrences.append(
            ApiOccurrence(
                file_path=str(self.path.relative_to(self.project)),
                line=line,
                column=column,
                api=api,
                qualified_name=qualified_name,
                kind=kind,
                source_line=source_line,
                enclosing_scope=".".join(self.scope_stack) or None,
            )
        )


def _expr_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _expr_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _module_matches_source(module: str, source: str) -> bool:
    module_lower = module.lower()
    source_lower = source.lower()
    return module_lower == source_lower or module_lower.startswith(f"{source_lower}.")


def _dedupe_occurrences(occurrences: list[ApiOccurrence]) -> list[ApiOccurrence]:
    seen: set[tuple[str, int, int, str, str]] = set()
    unique: list[ApiOccurrence] = []
    for occurrence in sorted(occurrences, key=lambda item: (item.file_path, item.line, item.column, item.kind)):
        key = (
            occurrence.file_path,
            occurrence.line,
            occurrence.column,
            occurrence.qualified_name,
            occurrence.kind,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(occurrence)
    return unique
