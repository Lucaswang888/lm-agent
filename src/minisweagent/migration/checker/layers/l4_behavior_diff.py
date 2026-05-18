"""L4 behaviour-diff checker placeholder."""

from __future__ import annotations

import time

from minisweagent.migration.checker.models import CheckContext, LayerResult, PassRecord


class L4BehaviorDiffLayer:
    """Optional expensive behaviour diff layer; currently reports a deterministic skip."""

    layer = "L4"

    def run(self, ctx: CheckContext) -> LayerResult:
        started = time.perf_counter()
        if not ctx.enable_l4:
            return LayerResult(
                layer="L4",
                passed=True,
                passes=(PassRecord(id="L4.skip", note="L4 behaviour diff is disabled; pass --enable-l4 to run future expensive checks."),),
                duration_seconds=time.perf_counter() - started,
                extra={"skipped_reason": "enable_l4_false"},
            )
        return LayerResult(
            layer="L4",
            passed=True,
            passes=(PassRecord(id="L4.todo", note="L4 behaviour diff hook is enabled but no expensive strategy is configured yet."),),
            duration_seconds=time.perf_counter() - started,
            extra={"skipped_reason": "strategy_not_configured"},
        )
