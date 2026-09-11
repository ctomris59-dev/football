#!/usr/bin/env python3
"""Leakage-safe historical audit for the 1X2 probability/coupon engine.

Protocol:
- test folds: 2024/25 and 2025/26;
- only earlier seasons plus earlier matches in the same test season are visible;
- 2026/27 outcomes are never queried or used for tuning;
- probability quality and cost-aware coupon coverage are both required to pass.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.types.json import Jsonb

from incremental_feature_audit import (
    MIN_HISTORY_MATCHES,
    SEASONS,
    TEST_SEASONS,
    _load_matches,
    _order,
)
from model_engine_v1 import predict_match
from one_x_two_engine import (
    ACTIVE_MODE,
    POLICY_KEY,
    POLICY_VERSION,
    actual_outcome,
    coupon_selection,
    from_v1_prediction,
    ranked_outcomes,
    selection_contains,
)
from production_predictor import canon

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LIVE_HOLDOUT = "2627"
VERSION = "one-x-two-two-fold-v1"

# Absolute promotion bar, frozen before looking at either test fold.
MIN_POOLED_TOP1_ACCURACY = 0.48
MIN_FOLD_TOP1_ACCURACY = 0.45
MAX_POOLED_MULTICLASS_BRIER = 0.64
MAX_POOLED_LOG_LOSS = 1.06
MAX_POOLED_ECE = 0.10
MIN_FOLD_COUPON_HIT = 0.74
MAX_POOLED_AVG_SELECTIONS = 1.70
MAX_POOLED_TRIPLE_RATE = 0.15
MIN_POOLED_MATCHES = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS one_x_two_audit_runs(
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


def _metrics(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if not n:
        return {
            "matches": 0, "top1_accuracy": None, "multiclass_brier": None,
            "log_loss": None, "ece_top_probability": None, "coupon_hit_rate": None,
            "avg_selections": None, "single_rate": None, "double_rate": None,
            "triple_rate": None,
        }
    top1 = sum(1 for r in rows if r["top1_hit"]) / n
    brier = sum(float(r["brier"]) for r in rows) / n
    logloss = sum(float(r["logloss"]) for r in rows) / n
    coupon_hit = sum(1 for r in rows if r["coupon_hit"]) / n
    avg_sel = sum(int(r["selection_count"]) for r in rows) / n
    tier_counts = {tier: sum(1 for r in rows if r["tier"] == tier) for tier in ("single", "double", "triple")}

    # Expected calibration error of the top-1 confidence, 10 fixed-width bins.
    ece = 0.0
    for i in range(10):
        lo, hi = i / 10.0, (i + 1) / 10.0
        bucket = [r for r in rows if lo <= float(r["top_probability"]) < hi or (i == 9 and float(r["top_probability"]) == 1.0)]
        if not bucket:
            continue
        conf = sum(float(r["top_probability"]) for r in bucket) / len(bucket)
        acc = sum(1 for r in bucket if r["top1_hit"]) / len(bucket)
        ece += (len(bucket) / n) * abs(conf - acc)

    return {
        "matches": n,
        "top1_accuracy": round(top1, 6),
        "multiclass_brier": round(brier, 6),
        "log_loss": round(logloss, 6),
        "ece_top_probability": round(ece, 6),
        "coupon_hit_rate": round(coupon_hit, 6),
        "avg_selections": round(avg_sel, 6),
        "single_rate": round(tier_counts["single"] / n, 6),
        "double_rate": round(tier_counts["double"] / n, 6),
        "triple_rate": round(tier_counts["triple"] / n, 6),
    }


def _gate(overall: Dict[str, Any], by_fold: Dict[str, Dict[str, Any]]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    if int(overall.get("matches") or 0) < MIN_POOLED_MATCHES:
        reasons.append("pooled_sample_too_small")
    if float(overall.get("top1_accuracy") or 0.0) < MIN_POOLED_TOP1_ACCURACY:
        reasons.append("pooled_top1_accuracy_low")
    if float(overall.get("multiclass_brier") or 9.0) > MAX_POOLED_MULTICLASS_BRIER:
        reasons.append("pooled_multiclass_brier_high")
    if float(overall.get("log_loss") or 9.0) > MAX_POOLED_LOG_LOSS:
        reasons.append("pooled_log_loss_high")
    if float(overall.get("ece_top_probability") or 9.0) > MAX_POOLED_ECE:
        reasons.append("pooled_calibration_error_high")
    if float(overall.get("avg_selections") or 9.0) > MAX_POOLED_AVG_SELECTIONS:
        reasons.append("coupon_cost_too_high")
    if float(overall.get("triple_rate") or 9.0) > MAX_POOLED_TRIPLE_RATE:
        reasons.append("triple_rate_too_high")
    for fold in TEST_SEASONS:
        fm = by_fold.get(fold) or {}
        if float(fm.get("top1_accuracy") or 0.0) < MIN_FOLD_TOP1_ACCURACY:
            reasons.append(f"fold_{fold}_top1_accuracy_low")
        if float(fm.get("coupon_hit_rate") or 0.0) < MIN_FOLD_COUPON_HIT:
            reasons.append(f"fold_{fold}_coupon_hit_low")
    return not reasons, reasons


def run_audit(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    if tuple(TEST_SEASONS) != ("2425", "2526") or LIVE_HOLDOUT in TEST_SEASONS:
        raise RuntimeError("1X2 gate requires test folds 2425,2526 with 2627 excluded")

    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        run_id = conn.execute(
            "INSERT INTO one_x_two_audit_runs(version,status) VALUES(%s,'running') RETURNING id",
            (VERSION,),
        ).fetchone()[0]
        try:
            matches = _load_matches(conn)
            by_div: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for m in matches:
                by_div[str(m["division"])].append(m)

            scored_rows: List[Dict[str, Any]] = []
            fold_meta: Dict[str, Any] = {}
            for test in TEST_SEASONS:
                scored = skipped = 0
                for division, div_rows in by_div.items():
                    train = [m for m in div_rows if _order(str(m["season_code"])) < _order(test)]
                    tests = [m for m in div_rows if str(m["season_code"]) == test]
                    train.sort(key=lambda m: (m["match_date"], m["home_team"], m["away_team"]))
                    tests.sort(key=lambda m: (m["match_date"], m["home_team"], m["away_team"]))
                    if len(train) < MIN_HISTORY_MATCHES:
                        skipped += len(tests)
                        continue
                    history = list(train)
                    for match in tests:
                        pred = predict_match(history, canon(match["home_team"]), canon(match["away_team"]), recent_matches=18)
                        one = from_v1_prediction(pred)
                        probs = one.probabilities()
                        actual = actual_outcome(match["home_goals"], match["away_goals"])
                        ranked = ranked_outcomes(one)
                        top_label, top_prob = ranked[0]
                        coupon = coupon_selection(one)
                        y = {label: 1.0 if label == actual else 0.0 for label in ("1", "0", "2")}
                        brier = sum((probs[label] - y[label]) ** 2 for label in ("1", "0", "2"))
                        logloss = -math.log(max(1e-12, probs[actual]))
                        scored_rows.append({
                            "fold": test,
                            "division": division,
                            "match_date": match["match_date"],
                            "home": str(match["home_team"]),
                            "away": str(match["away_team"]),
                            "actual": actual,
                            "p1": probs["1"], "px": probs["0"], "p2": probs["2"],
                            "top_outcome": top_label,
                            "top_probability": top_prob,
                            "top1_hit": top_label == actual,
                            "brier": brier,
                            "logloss": logloss,
                            "coupon_selection": coupon["selection"],
                            "selection_count": coupon["selection_count"],
                            "tier": coupon["tier"],
                            "coupon_hit": selection_contains(coupon["selection"], actual),
                        })
                        scored += 1
                        history.append(match)
                fold_meta[test] = {
                    "train_seasons": [s for s in SEASONS if _order(s) < _order(test)],
                    "test_season": test,
                    "matches_scored": scored,
                    "matches_skipped_insufficient_history": skipped,
                }

            overall = _metrics(scored_rows)
            by_fold = {fold: _metrics([r for r in scored_rows if r["fold"] == fold]) for fold in TEST_SEASONS}
            passed, reasons = _gate(overall, by_fold)
            result = {
                "version": VERSION,
                "policy_key": POLICY_KEY,
                "policy_version": POLICY_VERSION,
                "gate_passed": passed,
                "recommended_activation": ACTIVE_MODE if passed else "v1_only",
                "validation_protocol": {
                    "gate_version": "two-fold-week-block-v1",
                    "test_seasons": list(TEST_SEASONS),
                    "folds": fold_meta,
                    "sequential_history": True,
                    "holdout_excluded": True,
                    "live_holdout_season": LIVE_HOLDOUT,
                    "holdout_rule": "2627 outcomes are never read for tuning or this audit",
                },
                "predeclared_gate": {
                    "min_pooled_top1_accuracy": MIN_POOLED_TOP1_ACCURACY,
                    "min_each_fold_top1_accuracy": MIN_FOLD_TOP1_ACCURACY,
                    "max_pooled_multiclass_brier": MAX_POOLED_MULTICLASS_BRIER,
                    "max_pooled_log_loss": MAX_POOLED_LOG_LOSS,
                    "max_pooled_ece": MAX_POOLED_ECE,
                    "min_each_fold_coupon_hit": MIN_FOLD_COUPON_HIT,
                    "max_pooled_avg_selections": MAX_POOLED_AVG_SELECTIONS,
                    "max_pooled_triple_rate": MAX_POOLED_TRIPLE_RATE,
                    "min_pooled_matches": MIN_POOLED_MATCHES,
                },
                "overall": overall,
                "by_fold": by_fold,
                "gate_fail_reasons": reasons,
                "activation_note": (
                    "1X2 V1 passed the predeclared two-fold absolute quality/cost gate."
                    if passed else
                    "1X2 V1 did not pass the predeclared gate; it remains research-only."
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
                "UPDATE one_x_two_audit_runs SET finished_at=NOW(),status='success',results=%s,message=%s WHERE id=%s",
                (Jsonb(result), result["activation_note"], run_id),
            )
            print("ONE_X_TWO_AUDIT_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            conn.execute(
                "UPDATE one_x_two_audit_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                (str(exc)[:2000], run_id),
            )
            raise


if __name__ == "__main__":
    print(json.dumps(run_audit(), ensure_ascii=False, indent=2, default=str))
