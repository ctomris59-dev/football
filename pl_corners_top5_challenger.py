#!/usr/bin/env python3
"""Research-only gate for the Premier League corners Top-5 challenger.

The Top-N sensitivity audit found a repeatable hit-rate signal for Premier League
corners within the weekly Top-5 ranking. This module formalizes that observation
without treating hit rate as proof of a betting edge.

Promotion is deliberately fail-closed: even when fold stability/sample/calibration
criteria pass, the challenger remains blocked until a valid market benchmark and
post-decision CLV evidence exist for this exact corners policy.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence

from edge_structure_audit import DATABASE_URL, TEST_SEASONS
from research_evaluation import selection_metrics, stable_seed, week_block_bootstrap
from topn_sensitivity_audit import _score_candidates, _weekly_top

VERSION = "pl-corners-top5-challenger-v1"
TARGET_LEAGUE = "Premier League"
TARGET_MARKET = "corners_over_8_5"
TOP_N = 5
MIN_TOTAL_N = 200
MIN_FOLD_N = 75
MIN_FOLD_HIT_RATE = 0.70
MAX_ABS_CALIBRATION_GAP = 0.08
BOOTSTRAP_ITERATIONS = 2000


def _round(value: Any, digits: int = 5):
    return None if value is None else round(float(value), digits)


def _report(rows: Sequence[Dict[str, Any]], label: str) -> Dict[str, Any]:
    if not rows:
        return {"n": 0, "hits": 0, "hit_rate": None, "avg_confidence": None, "model_brier": None, "calibration_gap": None, "bootstrap95": {}}
    point = selection_metrics(rows)
    n = len(rows)
    hits = sum(bool(r.get("hit")) for r in rows)
    hit_rate = hits / n
    avg_conf = sum(float(r["confidence"]) for r in rows) / n
    ci = week_block_bootstrap(
        rows,
        selection_metrics,
        iterations=BOOTSTRAP_ITERATIONS,
        seed=stable_seed("pl-corners-top5:" + label),
    )
    return {
        "n": n,
        "hits": hits,
        "hit_rate": _round(hit_rate, 4),
        "avg_confidence": _round(avg_conf, 4),
        "model_brier": _round(point.get("model_brier")),
        "calibration_gap": _round(avg_conf - hit_rate, 4),
        "bootstrap95": ci,
    }


def select_policy_rows(candidates: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply weekly Top-5 first, then evaluate the predeclared PL-corners subgroup."""
    top5 = _weekly_top(candidates, TOP_N)
    return [
        r for r in top5
        if str(r.get("league")) == TARGET_LEAGUE and str(r.get("market")) == TARGET_MARKET
    ]


def activation_gate(
    overall: Dict[str, Any],
    folds: Dict[str, Dict[str, Any]],
    *,
    market_benchmark_available: bool,
    clv_available: bool,
    market_edge_pass: bool = False,
    clv_not_degraded: bool = False,
) -> Dict[str, Any]:
    """Fail-closed production gate; hit-rate stability alone can never promote."""
    fold_values = [folds.get(str(f), {}) for f in TEST_SEASONS]
    statistical = {
        "total_sample": int(overall.get("n") or 0) >= MIN_TOTAL_N,
        "each_fold_sample": all(int(x.get("n") or 0) >= MIN_FOLD_N for x in fold_values),
        "each_fold_hit_rate": all((x.get("hit_rate") is not None and float(x["hit_rate"]) >= MIN_FOLD_HIT_RATE) for x in fold_values),
        "calibration_gap": overall.get("calibration_gap") is not None and abs(float(overall["calibration_gap"])) <= MAX_ABS_CALIBRATION_GAP,
    }
    evidence = {
        "market_benchmark_available": bool(market_benchmark_available),
        "market_edge_pass": bool(market_edge_pass) if market_benchmark_available else False,
        "clv_available": bool(clv_available),
        "clv_not_degraded": bool(clv_not_degraded) if clv_available else False,
    }
    statistical_pass = all(statistical.values())
    production_pass = statistical_pass and all(evidence.values())
    blockers: List[str] = []
    blockers.extend(k for k, v in statistical.items() if not v)
    if not market_benchmark_available:
        blockers.append("missing_corners_market_benchmark")
    elif not market_edge_pass:
        blockers.append("no_proven_market_edge")
    if not clv_available:
        blockers.append("missing_corners_clv")
    elif not clv_not_degraded:
        blockers.append("clv_degraded")
    return {
        "statistical_stability_pass": statistical_pass,
        "production_activation_pass": production_pass,
        "statistical_checks": statistical,
        "market_clv_checks": evidence,
        "blockers": blockers,
        "decision": "PASS" if production_pass else "BLOCKED",
    }


def evaluate(database_url: str = DATABASE_URL) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    candidates, fold_meta = _score_candidates(database_url)
    rows = select_policy_rows(candidates)
    overall = _report(rows, "overall")
    by_fold = {
        str(fold): _report([r for r in rows if str(r.get("fold")) == str(fold)], f"fold:{fold}")
        for fold in TEST_SEASONS
    }

    # The current historical dataset has no paired corners execution/closing-price
    # series for this exact policy. Therefore these inputs intentionally remain false.
    gate = activation_gate(
        overall,
        by_fold,
        market_benchmark_available=False,
        clv_available=False,
    )
    result = {
        "version": VERSION,
        "purpose": "research_only_no_production_change",
        "policy": {
            "weekly_rank": "existing V1 probability x data-quality ranking",
            "top_n": TOP_N,
            "league": TARGET_LEAGUE,
            "market": TARGET_MARKET,
            "selection_order": "select weekly Top-5 first, then filter Premier League corners",
        },
        "protocol": {
            "test_seasons": list(TEST_SEASONS),
            "live_2627_outcomes_used": False,
            "week_block_bootstrap_iterations": BOOTSTRAP_ITERATIONS,
            "production_probability_engine_changed": False,
        },
        "folds": fold_meta,
        "overall": overall,
        "by_fold": by_fold,
        "gate": gate,
        "interpretation": "Repeatable historical hit-rate subgroup only. Not a proven value/profit edge until paired corners market benchmark and CLV are available.",
    }
    print("PL_CORNERS_TOP5_CHALLENGER_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
    return result


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2, default=str))
