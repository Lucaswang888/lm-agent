"""Pipeline runner for the multi-layer migration checker."""

from __future__ import annotations

import time

from minisweagent.migration.checker.layers import (
    L0EnvProbeLayer,
    L1StaticAstLayer,
    L2ImportSmokeLayer,
    L3DynamicTestLayer,
    L4BehaviorDiffLayer,
)
from minisweagent.migration.checker.models import CheckContext, CheckerReport, CoverageInfo, Failure, LayerName, LayerResult, PassRecord

VERIFIER_VERSION = "v2.0"

_DEFAULT_REGISTRY = {
    "L0": L0EnvProbeLayer,
    "L1": L1StaticAstLayer,
    "L2": L2ImportSmokeLayer,
    "L3": L3DynamicTestLayer,
    "L4": L4BehaviorDiffLayer,
}


class CheckerPipeline:
    """Run checker layers in order and short-circuit on blocker failures."""

    def __init__(self, layers: list[LayerName] | list[str], registry: dict[str, type] | None = None) -> None:
        registry = registry or _DEFAULT_REGISTRY
        self.layers = [registry[name]() for name in layers]

    def run(self, ctx: CheckContext) -> CheckerReport:
        started = time.perf_counter()
        results: list[LayerResult] = []
        failures: list[Failure] = []
        passes: list[PassRecord] = []
        layer_failed: str | None = None
        for layer in self.layers:
            result = layer.run(ctx)
            failures.extend(result.failures)
            passes.extend(result.passes)
            if not result.passed and any(failure.severity == "blocker" for failure in result.failures):
                layer_failed = result.layer
                result = LayerResult(
                    layer=result.layer,
                    passed=result.passed,
                    failures=result.failures,
                    passes=result.passes,
                    duration_seconds=result.duration_seconds,
                    short_circuit=True,
                    extra=result.extra,
                )
                results.append(result)
                break
            results.append(result)
        blocker_count = sum(1 for failure in failures if failure.severity == "blocker")
        warning_count = sum(1 for failure in failures if failure.severity == "warning")
        coverage = _coverage_from_results(results)
        summary = {
            "passed": all(result.passed for result in results),
            "layer_failed": layer_failed,
            "blocker_count": blocker_count,
            "warning_count": warning_count,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        escalation = {
            "trigger": f"{layer_failed} blocker" if layer_failed else None,
            "human_review_required": False,
            "next_action_hint": _next_action_hint(layer_failed, failures),
        }
        return CheckerReport(
            verifier_version=VERIFIER_VERSION,
            summary=summary,
            layers=tuple(results),
            failures=tuple(failures),
            passes=tuple(passes),
            coverage=coverage,
            escalation=escalation,
        )


def run_default_pipeline(ctx: CheckContext) -> CheckerReport:
    """Run the configured checker layers and return a CheckerReport."""
    return CheckerPipeline(list(ctx.layers_to_run)).run(ctx)


def _coverage_from_results(results: list[LayerResult]) -> CoverageInfo:
    for result in results:
        if result.layer == "L3" and result.extra.get("coverage"):
            coverage = result.extra["coverage"]
            return CoverageInfo(**coverage)
    return CoverageInfo(skipped_reason="coverage_filter_not_configured")


def _next_action_hint(layer_failed: str | None, failures: list[Failure]) -> str:
    blockers = [failure for failure in failures if failure.severity == "blocker"]
    if not blockers:
        return "No blocker failures. Review warnings and run project-specific tests if available."
    first = blockers[0]
    target = ", ".join(first.suggested_fix.edit_targets) if first.suggested_fix else first.file
    if target:
        return f"Fix {first.id} first by inspecting {target}, then rerun `check`."
    return f"Fix {first.id} first, then rerun `check`."
