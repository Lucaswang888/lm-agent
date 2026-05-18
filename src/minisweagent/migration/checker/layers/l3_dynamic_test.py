"""L3 dynamic test checker."""

from __future__ import annotations

import shlex
import subprocess
import time

from minisweagent.migration.checker.models import CheckContext, Failure, LayerResult, PassRecord, SuggestedFix


class L3DynamicTestLayer:
    """Run project validation commands as the dynamic checker gate."""

    layer = "L3"

    def run(self, ctx: CheckContext) -> LayerResult:
        started = time.perf_counter()
        commands = ctx.test_commands or _discover_test_commands(ctx.project)
        if not commands:
            warning = Failure(
                id="L3.COV.001",
                layer="L3",
                category="COV",
                severity="warning",
                evidence={"reason": "no_test_command_discovered"},
                diagnosis="No project test command was provided or discovered; L3 cannot validate behaviour.",
                suggested_fix=SuggestedFix(kind="add_smoke_test", hint="Add or provide a smoke test that exercises migrated modules."),
                rule_ref="UsingLLMs-§V.A.3-coverage-filter",
            )
            return LayerResult(
                layer="L3",
                passed=True,
                failures=(warning,),
                duration_seconds=time.perf_counter() - started,
                extra={"skipped_reason": "no_test_command_discovered"},
            )
        failures: list[Failure] = []
        timeout = ctx.timeout_seconds_per_layer.get("L3", 300)
        for command in commands:
            try:
                result = subprocess.run(
                    shlex.split(command),
                    cwd=ctx.project,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                failures.append(
                    Failure(
                        id=f"L3.ENV.{len(failures) + 1:03d}",
                        layer="L3",
                        category="ENV",
                        severity="warning",
                        evidence={"command": command, "timeout_seconds": timeout},
                        diagnosis=f"Validation command timed out: {command}",
                        suggested_fix=SuggestedFix(kind="narrow_test_command", hint=str(exc)),
                        rule_ref="ExecutionAgent-test-timeout",
                    )
                )
                continue
            if result.returncode != 0:
                output_tail = "\n".join((result.stdout + "\n" + result.stderr).splitlines()[-80:])
                failures.append(
                    Failure(
                        id=f"L3.SEM.{len(failures) + 1:03d}",
                        layer="L3",
                        category="SEM",
                        severity="blocker",
                        evidence={"command": command, "returncode": result.returncode, "output_tail": output_tail},
                        diagnosis=f"Validation command failed after migration: {command}",
                        suggested_fix=SuggestedFix(kind="fix_test_regression", hint="Run the failing command in isolation and inspect output_tail."),
                        rule_ref="MigrateLib-§4-dynamic-validation",
                    )
                )
        return LayerResult(
            layer="L3",
            passed=not any(failure.severity == "blocker" for failure in failures),
            failures=tuple(failures),
            passes=(PassRecord(id="L3.tests", note=f"Ran {len(commands)} validation command(s).", extra={"commands": tuple(commands)}),),
            duration_seconds=time.perf_counter() - started,
            extra={"commands": tuple(commands)},
        )


def _discover_test_commands(project) -> tuple[str, ...]:
    if (project / "pytest.ini").exists() or (project / "tests").exists():
        return ("python -m pytest -q",)
    if (project / "tox.ini").exists():
        return ("python -m pytest -q",)
    return ()
