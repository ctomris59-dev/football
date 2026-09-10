#!/usr/bin/env python3
"""Leakage-safe V1 vs Lineup Stability V2 weekly ranking audit.

This is a single predeclared challenger. It never reads 2026/27 outcomes. For each
2024/25 and 2025/26 test fixture, only lineups dated strictly before that fixture
are eligible. V1 probabilities/markets stay frozen; only weekly Top-10 ranking is
challenged.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

from incremental_feature_audit import (
    BOOTSTRAP_ITERATIONS,
    MIN_HISTORY_MATCHES,
    SEASONS,
    TEST_SEASONS,
    _as_date,
    _bootstrap_delta,
    _candidate_id,
    _load_lineups,
    _load_matches,
    _metrics,
    _order,
    _outcome,
    _season_year,
    _selection_sets,
    _week,
)
from lineup_stability_v2_policy import (
    ACTIVE_MODE,
    POLICY_KEY,
    POLICY_VERSION,
    continuity_v2_from_lineups,
    lineup_stability_v2_factor,
)
from model_engine_v1 import best_market, predict_match
from production_predictor import canon

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LIVE_HOLDOUT = "2627"
VERSION = "lineup-stability-v2-two-fold-week-block-v1"

# Exactly the same promotion bar used by the earlier incremental audit.
MIN_POOLED_HIT_GAIN = 0.005
MIN_FOLD_HIT_GAIN = 0.0
MIN_FOLD_COVERAGE = 0.50
MIN_CHANGED_PICKS = 20
MIN_BOOTSTRAP_P_IMPROVE = 0.80
MAX_SELECTED_BRIER_DELTA = 0.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS lineup_stability_v2_audit_runs(
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


def _continuity_at(lineups, season_year: int, team: str, before: Any) -> Optional[float]:
    d = _as_date(before)
    prior = [s for dt, s in lineups.get((season_year, canon(team)), []) if dt < d][-6:]
    return continuity_v2_from_lineups(prior)


def _gate(report: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    overall = report["overall"]
    base = report["baseline_overall"]
    gain = (overall["hit_rate"] or 0.0) - (base["hit_rate"] or 0.0)
    brier_delta = (overall["selected_brier"] or 9.0) - (base["selected_brier"] or 0.0)
    if gain < MIN_POOLED_HIT_GAIN:
        reasons.append("pooled_hit_gain_below_threshold")
    for fold in TEST_SEASONS:
        fm, bm = report["by_fold"][fold], report["baseline_by_fold"][fold]
        fgain = (fm["hit_rate"] or 0.0) - (bm["hit_rate"] or 0.0)
        if fgain < MIN_FOLD_HIT_GAIN:
            reasons.append(f"fold_{fold}_regressed")
        if float(report["coverage_by_fold"].get(fold) or 0.0) < MIN_FOLD_COVERAGE:
            reasons.append(f"fold_{fold}_coverage_low")
    if int(report.get("changed_picks") or 0) < MIN_CHANGED_PICKS:
        reasons.append("too_few_changed_picks")
    if float((report.get("bootstrap") or {}).get("p_gain_gt_0") or 0.0) < MIN_BOOTSTRAP_P_IMPROVE:
        reasons.append("bootstrap_support_low")
    if brier_delta > MAX_SELECTED_BRIER_DELTA:
        reasons.append("selected_brier_worse")
    return not reasons, reasons


def run_audit(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    if tuple(TEST_SEASONS) != ("2425", "2526") or LIVE_HOLDOUT in TEST_SEASONS:
        raise RuntimeError("Lineup V2 gate requires test folds 2425,2526 with 2627 excluded")

    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        rid = conn.execute(
            "INSERT INTO lineup_stability_v2_audit_runs(version,status) VALUES(%s,'running') RETURNING id",
            (VERSION,),
        ).fetchone()[0]
        try:
            matches = _load_matches(conn)
            lineups = _load_lineups(conn)
            by_div: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for m in matches:
                by_div[str(m["division"])].append(m)

            weekly: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
            coverage: Dict[str, List[int]] = {fold: [0, 0] for fold in TEST_SEASONS}
            fold_meta: Dict[str, Any] = {}

            for test in TEST_SEASONS:
                scored = skipped = 0
                sy = _season_year(test)
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
                        pred = predict_match(history, match["home_team"], match["away_team"])
                        bm = best_market(pred)
                        y = _outcome(match, str(bm["market"]))
                        d = _as_date(match["match_date"])
                        if y is not None:
                            scored += 1
                            base_rank = float(bm["probability"]) * (0.75 + 0.25 * float(bm["data_quality"]))
                            hs = _continuity_at(lineups, sy, str(match["home_team"]), d)
                            ass = _continuity_at(lineups, sy, str(match["away_team"]), d)
                            factor, available = lineup_stability_v2_factor(hs, ass)
                            coverage[test][1] += 1
                            coverage[test][0] += int(available)
                            row = {
                                "fold": test,
                                "week": _week(d),
                                "division": division,
                                "league": str(match["league_name"]),
                                "date": d,
                                "home": str(match["home_team"]),
                                "away": str(match["away_team"]),
                                "market": str(bm["market"]),
                                "selection": str(bm["selection"]),
                                "confidence": float(bm["probability"]),
                                "data_quality": float(bm["data_quality"]),
                                "hit": bool(y) == bool(bm["selection_yes"]),
                                "v1": base_rank,
                                "lineup_stability_v2": base_rank * factor,
                                "lineup_v2_available": available,
                                "home_continuity_v2": hs,
                                "away_continuity_v2": ass,
                            }
                            weekly[(test, row["week"])].append(row)
                        history.append(match)
                fold_meta[test] = {
                    "train_seasons": [s for s in SEASONS if _order(s) < _order(test)],
                    "test_season": test,
                    "matches_scored": scored,
                    "matches_skipped_insufficient_history": skipped,
                }

            base_rows, base_by_week = _selection_sets(weekly, "v1")
            chal_rows, chal_by_week = _selection_sets(weekly, "lineup_stability_v2")
            baseline_overall = _metrics(base_rows)
            overall = _metrics(chal_rows)
            baseline_by_fold = {fold: _metrics([r for r in base_rows if r["fold"] == fold]) for fold in TEST_SEASONS}
            by_fold = {fold: _metrics([r for r in chal_rows if r["fold"] == fold]) for fold in TEST_SEASONS}
            coverage_by_fold = {
                fold: round(coverage[fold][0] / coverage[fold][1], 4) if coverage[fold][1] else 0.0
                for fold in TEST_SEASONS
            }
            changed = 0
            for key, br in base_by_week.items():
                cr = chal_by_week.get(key, [])
                bset = {_candidate_id(r) for r in br}
                cset = {_candidate_id(r) for r in cr}
                changed += len(cset - bset)
            report = {
                "feature": ACTIVE_MODE,
                "overall": overall,
                "by_fold": by_fold,
                "baseline_overall": baseline_overall,
                "baseline_by_fold": baseline_by_fold,
                "coverage_by_fold": coverage_by_fold,
                "changed_picks": changed,
                "bootstrap": _bootstrap_delta(base_by_week, chal_by_week, seed=20260911),
            }
            passed, reasons = _gate(report)
            report["gate_passed"] = passed
            report["gate_fail_reasons"] = reasons
            report["hit_gain"] = round((overall["hit_rate"] or 0.0) - (baseline_overall["hit_rate"] or 0.0), 6)
            report["selected_brier_delta"] = round((overall["selected_brier"] or 0.0) - (baseline_overall["selected_brier"] or 0.0), 6)

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
                    "week_block_bootstrap": True,
                    "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
                    "holdout_excluded": True,
                    "live_holdout_season": LIVE_HOLDOUT,
                    "holdout_rule": "2627 outcomes are never read for tuning or this audit",
                },
                "predeclared_gate": {
                    "min_pooled_hit_gain": MIN_POOLED_HIT_GAIN,
                    "min_each_fold_hit_gain": MIN_FOLD_HIT_GAIN,
                    "min_each_fold_feature_coverage": MIN_FOLD_COVERAGE,
                    "min_changed_picks": MIN_CHANGED_PICKS,
                    "min_bootstrap_p_gain_gt_0": MIN_BOOTSTRAP_P_IMPROVE,
                    "max_selected_brier_delta": MAX_SELECTED_BRIER_DELTA,
                },
                "v2_definition": {
                    "minimum_prior_lineups": 2,
                    "maximum_prior_lineups": 6,
                    "continuity": "recency-weighted consecutive starter overlap",
                    "ranking_factor": "bounded 0.97/0.985/1.00/1.01; frozen before audit",
                },
                "baseline": baseline_overall,
                "challenger": report,
                "activation_note": (
                    "Lineup Stability V2 passed the predeclared two-fold gate."
                    if passed else
                    "Lineup Stability V2 did not pass; production remains v1_only."
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
                "UPDATE lineup_stability_v2_audit_runs SET finished_at=NOW(),status='success',results=%s,message=%s WHERE id=%s",
                (Jsonb(result), result["activation_note"], rid),
            )
            print("LINEUP_STABILITY_V2_AUDIT_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            conn.execute(
                "UPDATE lineup_stability_v2_audit_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                (str(exc)[:2000], rid),
            )
            raise


if __name__ == "__main__":
    print(json.dumps(run_audit(), ensure_ascii=False, indent=2, default=str))
