"""Target API candidate ranking for PIG-style migrations."""

from __future__ import annotations

import difflib
import importlib
from collections import defaultdict

from minisweagent.migration.pig_models import ApiChange, CandidateApi


def rank_candidate_apis(
    *,
    source: str,
    target: str,
    api_changes: list[ApiChange],
    examples: list[dict],
    max_candidates: int = 8,
    introspect_target: bool = False,
) -> list[CandidateApi]:
    """Rank likely target APIs from benchmark hints, examples, and installed symbols."""
    scores: dict[str, float] = defaultdict(float)
    reasons: dict[str, set[str]] = defaultdict(set)

    for change in api_changes:
        for api in change.target_apis:
            if not api:
                continue
            scores[api] += 100
            reasons[api].add(f"current PyMigBench mapping {', '.join(change.source_apis) or '?'} -> {api}")

    for example in examples:
        pair_bonus = 30 if example.get("source") == source and example.get("target") == target else 8
        for file_record in example.get("files", []) or []:
            for change in file_record.get("code_changes", []) or []:
                for api in change.get("target_apis", []) or []:
                    if not api:
                        continue
                    scores[api] += pair_bonus
                    reasons[api].add(f"historical {example.get('source')}->{example.get('target')} example")

    if introspect_target:
        source_api_names = {
            api.rsplit(".", 1)[-1]
            for change in api_changes
            for api in change.source_apis
            if api
        }
        for symbol in _public_target_symbols(target):
            score = max((_similarity(symbol, api) for api in source_api_names), default=0)
            if score >= 0.72:
                scores[symbol] += score * 10
                reasons[symbol].add(f"installed {target} symbol lexically resembles a source API")

    if target and target not in scores:
        scores[target] += 2
        reasons[target].add("target package import anchor")

    ranked = [
        CandidateApi(api=api, score=round(score, 2), reasons=tuple(sorted(reasons[api])))
        for api, score in scores.items()
        if api
    ]
    return sorted(ranked, key=lambda item: (-item.score, item.api))[:max_candidates]


def _public_target_symbols(target: str) -> list[str]:
    try:
        module = importlib.import_module(target)
    except Exception:
        return []
    symbols = []
    for name in dir(module):
        if name.startswith("_"):
            continue
        value = getattr(module, name, None)
        if callable(value) or isinstance(value, type(module)):
            symbols.append(name)
    return sorted(set(symbols))


def _similarity(left: str, right: str) -> float:
    left_norm = left.lower().replace("_", "")
    right_norm = right.lower().replace("_", "")
    if not left_norm or not right_norm:
        return 0.0
    return difflib.SequenceMatcher(a=left_norm, b=right_norm).ratio()
