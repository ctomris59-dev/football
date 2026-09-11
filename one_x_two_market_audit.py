#!/usr/bin/env python3
"""Probability-only promotion gate for adding 1X2 to the production market pool.

This gate is intentionally separate from the coupon single/double/triple policy.
A failed coupon-cost policy must not invalidate a well-calibrated probability engine,
but 1X2 still enters production only after both historical folds meet predeclared
probability-quality thresholds. Live 2026/27 is never queried for tuning/validation.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

from one_x_two_coupon_v2_audit import (
    DEV_SEASON,
    FINAL_HOLDOUT_SEASON,
    LIVE_HOLDOUT_SEASON,
    _probability_metrics,
    _score_folds,
)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
POLICY_KEY = "one-x-two-market-v1"
POLICY_VERSION = "one-x-two-market-poisson-v1"
ACTIVE_MODE = "one_x_two_market_v1"
VERSION = "one-x-two-market-two-fold-v1"
TEST_SEASONS = (DEV_SEASON, FINAL_HOLDOUT_SEASON)
GATE_VERSION = "two-fold-week-block-v1"

MIN_MATCHES_PER_FOLD = 1500
MIN_TOP1_ACCURACY = 0.48
MAX_MULTICLASS_BRIER = 0.64
MAX_LOG_LOSS = 1.06
MAX_ECE = 0.10

SCHEMA = """
CREATE TABLE IF NOT EXISTS one_x_two_market_audit_runs(
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


def _fold_gate(fold: str, metrics: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    if int(metrics.get("matches") or 0) < MIN_MATCHES_PER_FOLD:
        reasons.append(f"fold_{fold}_sample_too_small")
    if float(metrics.get("top1_accuracy") or 0.0) < MIN_TOP1_ACCURACY:
        reasons.append(f"fold_{fold}_top1_accuracy_low")
    if float(metrics.get("multiclass_brier") or 9.0) > MAX_MULTICLASS_BRIER:
        reasons.append(f"fold_{fold}_brier_high")
    if float(metrics.get("log_loss") or 9.0) > MAX_LOG_LOSS:
        reasons.append(f"fold_{fold}_log_loss_high")
    if float(metrics.get("ece_top_probability") or 9.0) > MAX_ECE:
        reasons.append(f"fold_{fold}_ece_high")
    return reasons


def run_audit(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    if LIVE_HOLDOUT_SEASON in TEST_SEASONS or TEST_SEASONS != ("2425", "2526"):
        raise RuntimeError("1X2 market gate requires 2425/2526 folds with live 2627 excluded")

    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        run_id = int(conn.execute(
            "INSERT INTO one_x_two_market_audit_runs(version,status) VALUES(%s,'running') RETURNING id",
            (VERSION,),
        ).fetchone()[0])
        try:
            folds = _score_folds(conn)
            by_fold = {fold: _probability_metrics(folds.get(fold) or []) for fold in TEST_SEASONS}
            reasons: List[str] = []
            for fold in TEST_SEASONS:
                reasons.extend(_fold_gate(fold, by_fold[fold]))
            passed = not reasons
            validation_protocol = {
                "gate_version": GATE_VERSION,
                "test_seasons": list(TEST_SEASONS),
                "holdout_excluded": True,
                "live_holdout_season": LIVE_HOLDOUT_SEASON,
                "holdout_rule": "2627 outcomes are never queried for tuning or validation",
            }
            result = {
                "version": VERSION,
                "policy_key": POLICY_KEY,
                "policy_version": POLICY_VERSION,
                "gate_passed": passed,
                "recommended_activation": ACTIVE_MODE if passed else "v1_only",
                "validation_protocol": validation_protocol,
                # Backward-compatible top-level fields retained for older diagnostics.
                "test_seasons": list(TEST_SEASONS),
                "live_holdout_season": LIVE_HOLDOUT_SEASON,
                "live_holdout_excluded": True,
                "by_fold": by_fold,
                "predeclared_gate": {
                    "min_matches_per_fold": MIN_MATCHES_PER_FOLD,
                    "min_top1_accuracy": MIN_TOP1_ACCURACY,
                    "max_multiclass_brier": MAX_MULTICLASS_BRIER,
                    "max_log_loss": MAX_LOG_LOSS,
                    "max_ece_top_probability": MAX_ECE,
                },
                "gate_fail_reasons": reasons,
                "activation_note": (
                    "1X2 probability engine passed both historical folds and may enter the guarded production market pool."
                    if passed else
                    "1X2 probability engine did not pass the market-quality gate and remains excluded from production."
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
                "UPDATE one_x_two_market_audit_runs SET finished_at=NOW(),status='success',results=%s,message=%s WHERE id=%s",
                (Jsonb(result), result["activation_note"], run_id),
            )
            print("ONE_X_TWO_MARKET_AUDIT_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            conn.execute(
                "UPDATE one_x_two_market_audit_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                (str(exc)[:2000], run_id),
            )
            raise


if __name__ == "__main__":
    print(json.dumps(run_audit(), ensure_ascii=False, indent=2, default=str))
