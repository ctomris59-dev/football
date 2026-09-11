#!/usr/bin/env python3
"""Cost-aware 1X2 coupon V2 with a strict historical development/holdout split.

The probability engine is unchanged. Only coupon thresholds are selected on 2024/25.
The selected thresholds are frozen before one final evaluation on untouched 2025/26.
The live 2026/27 season is never queried for tuning or validation.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.types.json import Jsonb

from incremental_feature_audit import MIN_HISTORY_MATCHES, SEASONS, _load_matches, _order
from model_engine_v1 import predict_match
from one_x_two_engine import (
    POLICY_KEY,
    OneXTwoPrediction,
    actual_outcome,
    coupon_selection_with_thresholds,
    from_v1_prediction,
    ranked_outcomes,
    selection_contains,
)
from production_predictor import canon

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
VERSION = "one-x-two-coupon-v2-dev-holdout"
POLICY_VERSION = "one-x-two-coupon-v2"
ACTIVE_MODE = "one_x_two_v2"
DEV_SEASON = "2425"
FINAL_HOLDOUT_SEASON = "2526"
LIVE_HOLDOUT_SEASON = "2627"
TEST_SEASONS = (DEV_SEASON, FINAL_HOLDOUT_SEASON)

# Candidate grid is declared in code before the V2 audit is run. 2425 selects one
# policy; 2526 is used only once as the final historical holdout.
SINGLE_PROB_GRID = (0.46, 0.48, 0.50, 0.52, 0.54, 0.56)
SINGLE_MARGIN_GRID = (0.02, 0.04, 0.06, 0.08, 0.10)
TRIPLE_TOP_GRID = (0.34, 0.35, 0.36, 0.37, 0.38)
TRIPLE_BOTTOM_GRID = (0.24, 0.26, 0.28, 0.30)

# Development constraints include a safety margin beyond final holdout gates.
DEV_MIN_COUPON_HIT = 0.77
DEV_MAX_AVG_SELECTIONS = 1.65
DEV_MAX_TRIPLE_RATE = 0.08

# Final untouched 2526 gate.
HOLDOUT_MIN_TOP1_ACCURACY = 0.48
HOLDOUT_MAX_BRIER = 0.64
HOLDOUT_MAX_LOG_LOSS = 1.06
HOLDOUT_MAX_ECE = 0.10
HOLDOUT_MIN_COUPON_HIT = 0.74
HOLDOUT_MAX_AVG_SELECTIONS = 1.70
HOLDOUT_MAX_TRIPLE_RATE = 0.10
HOLDOUT_MIN_MATCHES = 1500

SCHEMA = """
CREATE TABLE IF NOT EXISTS one_x_two_v2_audit_runs(
 id BIGSERIAL PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 version TEXT NOT NULL,
 status TEXT NOT NULL,
 results JSONB,
 message TEXT
);
CREATE TABLE IF NOT EXISTS policy_activation_registry(
 policy_key TEXT PRIMARY KEY,
 policy_version TEXT NOT NULL,
 active_mode TEXT NOT NULL,
 validated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
 reason TEXT
);
"""


def _score_folds(conn) -> Dict[str, List[Dict[str, Any]]]:
    raw_matches = _load_matches(conn)
    by_div: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for raw in raw_matches:
        m = dict(raw)
        m["home_team"] = canon(raw.get("home_team"))
        m["away_team"] = canon(raw.get("away_team"))
        by_div[str(m["division"])].append(m)

    out: Dict[str, List[Dict[str, Any]]] = {season: [] for season in TEST_SEASONS}
    for test in TEST_SEASONS:
        for division, div_rows in by_div.items():
            train = [m for m in div_rows if _order(str(m["season_code"])) < _order(test)]
            tests = [m for m in div_rows if str(m["season_code"]) == test]
            train.sort(key=lambda m: (m["match_date"], m["home_team"], m["away_team"]))
            tests.sort(key=lambda m: (m["match_date"], m["home_team"], m["away_team"]))
            if len(train) < MIN_HISTORY_MATCHES:
                continue
            history = list(train)
            for match in tests:
                pred = predict_match(history, match["home_team"], match["away_team"], recent_matches=18)
                one = from_v1_prediction(pred)
                probs = one.probabilities()
                actual = actual_outcome(match["home_goals"], match["away_goals"])
                top_label, top_prob = ranked_outcomes(one)[0]
                y = {label: 1.0 if label == actual else 0.0 for label in ("1", "0", "2")}
                out[test].append({
                    "p1": probs["1"], "p0": probs["0"], "p2": probs["2"],
                    "actual": actual,
                    "top_outcome": top_label,
                    "top_probability": top_prob,
                    "top1_hit": top_label == actual,
                    "brier": sum((probs[label] - y[label]) ** 2 for label in ("1", "0", "2")),
                    "logloss": -math.log(max(1e-12, probs[actual])),
                })
                history.append(match)
    return out


def _prediction(row: Dict[str, Any]) -> OneXTwoPrediction:
    return OneXTwoPrediction(float(row["p1"]), float(row["p0"]), float(row["p2"]), 0.0, 0.0)


def _policy_metrics(rows: Sequence[Dict[str, Any]], policy: Dict[str, float]) -> Dict[str, Any]:
    if not rows:
        return {"matches": 0}
    selections = []
    for row in rows:
        pick = coupon_selection_with_thresholds(
            _prediction(row),
            single_min_prob=policy["single_min_prob"],
            single_min_margin=policy["single_min_margin"],
            triple_max_top=policy["triple_max_top"],
            triple_min_bottom=policy["triple_min_bottom"],
        )
        selections.append((pick, row))
    n = len(selections)
    hit = sum(selection_contains(pick["selection"], row["actual"]) for pick, row in selections) / n
    avg = sum(int(pick["selection_count"]) for pick, _ in selections) / n
    single = sum(pick["tier"] == "single" for pick, _ in selections) / n
    double = sum(pick["tier"] == "double" for pick, _ in selections) / n
    triple = sum(pick["tier"] == "triple" for pick, _ in selections) / n
    return {
        "matches": n,
        "coupon_hit_rate": round(hit, 6),
        "avg_selections": round(avg, 6),
        "single_rate": round(single, 6),
        "double_rate": round(double, 6),
        "triple_rate": round(triple, 6),
    }


def _probability_metrics(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if not n:
        return {"matches": 0}
    top1 = sum(bool(r["top1_hit"]) for r in rows) / n
    brier = sum(float(r["brier"]) for r in rows) / n
    logloss = sum(float(r["logloss"]) for r in rows) / n
    ece = 0.0
    for i in range(10):
        lo, hi = i / 10.0, (i + 1) / 10.0
        bucket = [r for r in rows if lo <= float(r["top_probability"]) < hi or (i == 9 and float(r["top_probability"]) == 1.0)]
        if bucket:
            conf = sum(float(r["top_probability"]) for r in bucket) / len(bucket)
            acc = sum(bool(r["top1_hit"]) for r in bucket) / len(bucket)
            ece += len(bucket) / n * abs(conf - acc)
    return {
        "matches": n,
        "top1_accuracy": round(top1, 6),
        "multiclass_brier": round(brier, 6),
        "log_loss": round(logloss, 6),
        "ece_top_probability": round(ece, 6),
    }


def _select_on_dev(rows: Sequence[Dict[str, Any]]) -> Tuple[Optional[Dict[str, float]], Dict[str, Any]]:
    candidates = []
    tested = 0
    for sp in SINGLE_PROB_GRID:
        for sm in SINGLE_MARGIN_GRID:
            for tt in TRIPLE_TOP_GRID:
                for tb in TRIPLE_BOTTOM_GRID:
                    tested += 1
                    policy = {
                        "single_min_prob": sp,
                        "single_min_margin": sm,
                        "triple_max_top": tt,
                        "triple_min_bottom": tb,
                    }
                    metrics = _policy_metrics(rows, policy)
                    if (
                        float(metrics["coupon_hit_rate"]) >= DEV_MIN_COUPON_HIT
                        and float(metrics["avg_selections"]) <= DEV_MAX_AVG_SELECTIONS
                        and float(metrics["triple_rate"]) <= DEV_MAX_TRIPLE_RATE
                    ):
                        candidates.append((policy, metrics))
    if not candidates:
        return None, {"grid_candidates_tested": tested, "eligible_candidates": 0}
    # Among policies already meeting a hit-rate safety margin, minimize coupon cost.
    candidates.sort(key=lambda item: (
        float(item[1]["avg_selections"]),
        -float(item[1]["coupon_hit_rate"]),
        float(item[1]["triple_rate"]),
        -float(item[1]["single_rate"]),
        item[0]["single_min_prob"],
        item[0]["single_min_margin"],
        item[0]["triple_max_top"],
        item[0]["triple_min_bottom"],
    ))
    policy, metrics = candidates[0]
    return policy, {
        "grid_candidates_tested": tested,
        "eligible_candidates": len(candidates),
        "selected_development_metrics": metrics,
    }


def _holdout_gate(prob: Dict[str, Any], coupon: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    if int(prob.get("matches") or 0) < HOLDOUT_MIN_MATCHES:
        reasons.append("holdout_sample_too_small")
    if float(prob.get("top1_accuracy") or 0.0) < HOLDOUT_MIN_TOP1_ACCURACY:
        reasons.append("holdout_top1_accuracy_low")
    if float(prob.get("multiclass_brier") or 9.0) > HOLDOUT_MAX_BRIER:
        reasons.append("holdout_brier_high")
    if float(prob.get("log_loss") or 9.0) > HOLDOUT_MAX_LOG_LOSS:
        reasons.append("holdout_log_loss_high")
    if float(prob.get("ece_top_probability") or 9.0) > HOLDOUT_MAX_ECE:
        reasons.append("holdout_ece_high")
    if float(coupon.get("coupon_hit_rate") or 0.0) < HOLDOUT_MIN_COUPON_HIT:
        reasons.append("holdout_coupon_hit_low")
    if float(coupon.get("avg_selections") or 9.0) > HOLDOUT_MAX_AVG_SELECTIONS:
        reasons.append("holdout_coupon_cost_high")
    if float(coupon.get("triple_rate") or 9.0) > HOLDOUT_MAX_TRIPLE_RATE:
        reasons.append("holdout_triple_rate_high")
    return not reasons, reasons


def run_v2_audit(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    if LIVE_HOLDOUT_SEASON in TEST_SEASONS:
        raise RuntimeError("Live 2627 holdout must not be used by 1X2 V2")

    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        run_id = conn.execute(
            "INSERT INTO one_x_two_v2_audit_runs(version,status) VALUES(%s,'running') RETURNING id",
            (VERSION,),
        ).fetchone()[0]
        try:
            folds = _score_folds(conn)
            dev_rows = folds[DEV_SEASON]
            holdout_rows = folds[FINAL_HOLDOUT_SEASON]
            policy, dev_search = _select_on_dev(dev_rows)
            dev_prob = _probability_metrics(dev_rows)
            holdout_prob = _probability_metrics(holdout_rows)
            if policy is None:
                holdout_coupon = {"matches": len(holdout_rows)}
                passed, reasons = False, ["no_development_policy_met_safety_constraints"]
            else:
                holdout_coupon = _policy_metrics(holdout_rows, policy)
                passed, reasons = _holdout_gate(holdout_prob, holdout_coupon)

            result = {
                "version": VERSION,
                "policy_key": POLICY_KEY,
                "policy_version": POLICY_VERSION,
                "gate_passed": passed,
                "recommended_activation": ACTIVE_MODE if passed else "v1_only",
                "selected_coupon_policy": policy,
                "validation_protocol": {
                    "gate_version": "two-fold-week-block-v1",
                    "test_seasons": [DEV_SEASON, FINAL_HOLDOUT_SEASON],
                    "development_season": DEV_SEASON,
                    "final_historical_holdout_season": FINAL_HOLDOUT_SEASON,
                    "sequential_history": True,
                    "holdout_excluded": True,
                    "live_holdout_season": LIVE_HOLDOUT_SEASON,
                    "live_holdout_rule": "2627 is never read for tuning or validation",
                },
                "development": {
                    "probability_metrics": dev_prob,
                    **dev_search,
                },
                "final_holdout": {
                    "probability_metrics": holdout_prob,
                    "coupon_metrics": holdout_coupon,
                },
                "gate_fail_reasons": reasons,
                "activation_note": (
                    "1X2 coupon V2 passed 2425 development and untouched 2526 holdout gates."
                    if passed else
                    "1X2 coupon V2 failed the untouched 2526 gate or development safety constraints."
                ),
            }
            conn.execute(
                """INSERT INTO policy_activation_registry(policy_key,policy_version,active_mode,metrics,reason)
                   VALUES(%s,%s,%s,%s,%s)
                   ON CONFLICT(policy_key) DO UPDATE SET policy_version=EXCLUDED.policy_version,
                     active_mode=EXCLUDED.active_mode,validated_at=NOW(),metrics=EXCLUDED.metrics,reason=EXCLUDED.reason""",
                (POLICY_KEY, POLICY_VERSION, ACTIVE_MODE if passed else "v1_only", Jsonb(result), result["activation_note"]),
            )
            conn.execute(
                "UPDATE one_x_two_v2_audit_runs SET finished_at=NOW(),status='success',results=%s,message=%s WHERE id=%s",
                (Jsonb(result), result["activation_note"], run_id),
            )
            print("ONE_X_TWO_V2_AUDIT_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            conn.execute(
                "UPDATE one_x_two_v2_audit_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                (str(exc)[:2000], run_id),
            )
            raise


if __name__ == "__main__":
    print(json.dumps(run_v2_audit(), ensure_ascii=False, indent=2, default=str))
