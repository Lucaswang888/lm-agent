#!/usr/bin/env python3

"""Natural-language wrapper for the library migration CLI."""

from __future__ import annotations

import re
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(rich_markup_mode="rich", add_completion=False)


@dataclass(frozen=True)
class ParsedMigrationRequest:
    """Structured migration fields parsed from a user sentence."""

    project: str
    source: str
    target: str
    test_command: str | None = None


@app.command()
def main(
    request: Annotated[str, typer.Argument(help="Natural-language migration request.")],
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project path when the request omits it.")] = None,
    test_command: Annotated[
        list[str],
        typer.Option("-T", "--test-command", help="Validation command to run after migration. Repeatable."),
    ] = [],
    pig_report: Annotated[Path | None, typer.Option("--pig-report", help="Write PIG context JSON to this path.")] = None,
    strict_report: Annotated[
        Path | None,
        typer.Option("--strict-report", help="Write static/API verification JSON to this path."),
    ] = None,
    strict_static_check: Annotated[
        bool,
        typer.Option("--strict-static-check", help="Run static/API verification after the agent exits."),
    ] = False,
    print_task: Annotated[bool, typer.Option("--print-task", help="Print the generated migration task.")] = False,
    yolo: Annotated[bool, typer.Option("-y", "--yolo", help="Run without confirmation.")] = False,
    model_name: Annotated[
        str | None,
        typer.Option("-m", "--model", help="Model to use. Defaults to MSWEA_MIGRATION_MODEL_NAME or DeepSeek when configured."),
    ] = None,
) -> None:
    """Parse a natural-language request and delegate to mini-migrate."""
    parsed = parse_migration_request(request, project=project)
    commands = list(test_command)
    if parsed.test_command and parsed.test_command not in commands:
        commands.append(parsed.test_command)

    args = [
        sys.executable,
        "-m",
        "minisweagent.run.migrate",
        "--project",
        parsed.project,
        "--source",
        parsed.source,
        "--target",
        parsed.target,
    ]
    for command in commands:
        args.extend(["--test-command", command])
    if pig_report:
        args.extend(["--pig-report", str(pig_report)])
    if strict_static_check:
        args.append("--strict-static-check")
    if strict_report:
        args.extend(["--strict-report", str(strict_report)])
    if print_task:
        args.append("--print-task")
    if yolo:
        args.append("--yolo")
    if resolved_model := model_name or _default_migration_model():
        args.extend(["--model", resolved_model])

    raise typer.Exit(subprocess.run(args, check=False).returncode)


def _default_migration_model() -> str | None:
    """Use a stronger migration model for natural-language runs when available."""
    if value := os.getenv("MSWEA_MIGRATION_MODEL_NAME"):
        return value
    if os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek/deepseek-chat"
    return None


def parse_migration_request(request: str, project: str | None = None) -> ParsedMigrationRequest:
    """Parse common Chinese and English library migration requests."""
    text = " ".join(request.strip().split())
    patterns = [
        re.compile(
            r"(?:请把|請把|把|将|將)\s*(?P<project>\S+)\s*(?:这个|這個)?(?:项目|專案|project)?"
            r"\s*从\s*(?P<source>[\w.-]+)\s*(?:迁移|移植|切换|改|升级|轉移|轉換)"
            r"\s*(?:到|为|成|至)\s*(?P<target>[\w.-]+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"migrate\s+(?P<project>\S+)\s+from\s+(?P<source>[\w.-]+)\s+to\s+(?P<target>[\w.-]+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?P<project>\S+)\s+.*?\bfrom\s+(?P<source>[\w.-]+)\s+\bto\s+(?P<target>[\w.-]+)",
            re.IGNORECASE,
        ),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return ParsedMigrationRequest(
                project=project or match.group("project"),
                source=match.group("source").lower(),
                target=match.group("target").lower(),
                test_command=_extract_test_command(text),
            )
    if project:
        pair_patterns = [
            re.compile(
                r"(?:从|由)\s*(?P<source>[\w.-]+)\s*(?:迁移|移植|切换|改|升级|轉移|轉換)"
                r"\s*(?:到|为|成|至)\s*(?P<target>[\w.-]+)",
                re.IGNORECASE,
            ),
            re.compile(r"\bfrom\s+(?P<source>[\w.-]+)\s+\bto\s+(?P<target>[\w.-]+)", re.IGNORECASE),
        ]
        for pattern in pair_patterns:
            match = pattern.search(text)
            if match:
                return ParsedMigrationRequest(
                    project=project,
                    source=match.group("source").lower(),
                    target=match.group("target").lower(),
                    test_command=_extract_test_command(text),
                )
    raise typer.BadParameter(
        "Could not parse the migration request. Try: "
        "'请把 demo_projects/flask_weather_api 这个项目从 Flask 迁移到 Quart'"
    )


def _extract_test_command(text: str) -> str | None:
    patterns = [
        r"(?:并|然后|并且)?(?:运行|執行|跑)(?:测试|測試|test|tests)[:：]?\s*(?P<command>.+)$",
        r"(?:run|execute)\s+(?:test|tests)[:：]?\s*(?P<command>.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            command = match.group("command").strip()
            return command.strip("'\"") if command else None
    return None


if __name__ == "__main__":
    app()
