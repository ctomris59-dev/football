#!/usr/bin/env python3
"""Two-fold, week-frozen OOS audit for Dixon-Coles and opponent adjustment.

Both challengers are tested independently against frozen V1. 2026/27 outcomes are
never read. Within each test fold, every ISO week's predictions are made from a
history frozen before that week, so Saturday results cannot leak into Sunday picks.
Only a predeclared gate winner is written to the guarded production registry.
"""
from __future__ import annotations

import json
import math
import os
import random
from collections import defaultdict
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.types.json import Jsonb

from advanced_goal_models import (
    MODE_DC,
    MODE_OPP,
    MODE_V1,
    OPPONENT_BLEND_WEIGHT,
    POLICY_KEY,
    POLICY_VERSION,
    best_market_from_probabilities,
    dixon_coles_from_v1,
    fit_dc_rho,
    fit_opponent_strengths,
    opponent_adjusted_probabilities,
)
from incremental_feature_audit import _as_date, _load_matches, _metrics, _order, _outcome, _quantile, _week
from model_engine_v1 import best_market, predict_match

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TEST_SEASONS = ("2425", "2526")
LIVE_HOLDOUT = "2627"
TOP_N = int(os.getenv("ADVANCED_GOAL_TOP_N", "10"))
MIN_HISTORY_MATCHES = int(os.getenv("ADVANCED_GOAL_MIN_HISTORY_MATCHES", "150"))
BOOTSTRAP_ITERATIONS = int(os.getenv("ADVANCED_GOAL_BOOTSTRAP_ITERATIONS", "2000"))

# Same gate used for the earlier feature challengers. Frozen before this audit.
MIN_POOLED_HIT_GAIN = float(os.getenv("ADVANCED_GOAL_MIN_POOLED_HIT_GAIN", "0.005"))
MIN_FOLD_HIT_GAIN = float(os.getenv("ADVANCED_GOAL_MIN_FOLD_HIT_GAIN", "0.0"))
MIN_FOLD_COVERAGE = float(os.getenv("ADVANCED_GOAL_MIN_FOLD_COVERAGE", "0.50"))
MIN_CHANGED_PICKS = int(os.getenv("ADVANCED_GOAL_MIN_CHANGED_PICKS", "20"))
MIN_BOOTSTRAP_P_IMPROVE = float(os.getenv("ADVANCED_GOAL_MIN_BOOTSTRAP_P_IMPROVE", "0.80"))
MAX_SELECTED_BRIER_DELTA = float(os.getenv("ADVANCED_GOAL_MAX_SELECTED_BRIER_DELTA", "0.0"))
VERSION = "advanced-goal-two-fold-week-frozen-v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS advanced_goal_audit_runs(
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


def _candidate_id(r: Dict[str, Any]) -> str:
    return "|".join([str(r["fold"]), str(r["division"]), str(r["date"]), str(r["home"]), str(r["away"]), str(r["market"]), str(r["selection_yes"])])


def _row(fold: str, division: str, match: Dict[str, Any], bm: Dict[str, Any], mode: str, available: bool) -> Optional[Dict[str, Any]]:
    y = _outcome(match, str(bm["market"]))
    if y is None:
        return None
    confidence = float(bm["probability"])
    quality = float(bm["data_quality"])
    return {
        "fold": fold,
        "week": _week(match["match_date"]),
        "division": division,
        "date": _as_date(match["match_date"]),
        "home": str(match["home_team"]),
        "away": str(match["away_team"]),
        "market": str(bm["market"]),
        "selection": str(bm["selection"]),
        "selection_yes": bool(bm["selection_yes"]),
        "confidence": confidence,
        "data_quality": quality,
        "rank": confidence * (0.75 + 0.25 * quality),
        "hit": bool(y) == bool(bm["selection_yes"]),
        "mode": mode,
        "available": bool(available),
    }


def _select(rows_by_week: Dict[Tuple[str, str], List[Dict[str, Any]]]) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str], List[Dict[str, Any]]]]:
    all_rows: List[Dict[str, Any]] = []
    selected: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for key, rows in sorted(rows_by_week.items()):
        picks = sorted(rows, key=lambda r: (float(r["rank"]), float(r["confidence"])), reverse=True)[:TOP_N]
        selected[key] = picks
        all_rows.extend(picks)
    return all_rows, selected


def _bootstrap(base: Dict[Tuple[str, str], List[Dict[str, Any]]], chal: Dict[Tuple[str, str], List[Dict[str, Any]]], seed: int) -> Dict[str, Any]:
    rng = random.Random(seed)
    keys_by_fold: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for k in base:
        if k in chal:
            keys_by_fold[k[0]].append(k)
    deltas: List[float] = []
    for _ in range(max(1, BOOTSTRAP_ITERATIONS)):
        bh = bn = ch = cn = 0
        for fold in TEST_SEASONS:
            keys = keys_by_fold.get(fold, [])
            for _j in range(len(keys)):
                if not keys:
                    break
                k = keys[rng.randrange(len(keys))]
                br, cr = base[k], chal[k]
                bh += sum(bool(r["hit"]) for r in br); bn += len(br)
                ch += sum(bool(r["hit"]) for r in cr); cn += len(cr)
        if bn and cn:
            deltas.append(ch / cn - bh / bn)
    return {
        "iterations": len(deltas),
        "ci95": [round(_quantile(deltas, 0.025), 6), round(_quantile(deltas, 0.975), 6)] if deltas else [None, None],
        "p_gain_gt_0": round(sum(x > 0 for x in deltas) / len(deltas), 4) if deltas else None,
        "median_delta": round(_quantile(deltas, 0.5), 6) if deltas else None,
    }


def _report(mode: str, rows: List[Dict[str, Any]], by_week: Dict[Tuple[str, str], List[Dict[str, Any]]], base_rows: List[Dict[str, Any]], base_by_week: Dict[Tuple[str, str], List[Dict[str, Any]]], coverage: Dict[str, Tuple[int, int]], seed: int) -> Dict[str, Any]:
    overall = _metrics(rows)
    baseline = _metrics(base_rows)
    by_fold = {f: _metrics([r for r in rows if r["fold"] == f]) for f in TEST_SEASONS}
    base_fold = {f: _metrics([r for r in base_rows if r["fold"] == f]) for f in TEST_SEASONS}
    coverage_by_fold = {f: round(coverage[f][0] / coverage[f][1], 4) if coverage[f][1] else 0.0 for f in TEST_SEASONS}
    changed = 0
    for key, br in base_by_week.items():
        cr = by_week.get(key, [])
        bset = {_candidate_id(r) for r in br}
        cset = {_candidate_id(r) for r in cr}
        changed += len(cset - bset)
    gain = (overall.get("hit_rate") or 0.0) - (baseline.get("hit_rate") or 0.0)
    brier_delta = (overall.get("selected_brier") or 9.0) - (baseline.get("selected_brier") or 0.0)
    boot = _bootstrap(base_by_week, by_week, seed)
    reasons: List[str] = []
    if gain < MIN_POOLED_HIT_GAIN: reasons.append("pooled_hit_gain_below_threshold")
    for f in TEST_SEASONS:
        fgain = (by_fold[f].get("hit_rate") or 0.0) - (base_fold[f].get("hit_rate") or 0.0)
        if fgain < MIN_FOLD_HIT_GAIN: reasons.append(f"fold_{f}_regressed")
        if coverage_by_fold[f] < MIN_FOLD_COVERAGE: reasons.append(f"fold_{f}_coverage_low")
    if changed < MIN_CHANGED_PICKS: reasons.append("too_few_changed_picks")
    if float(boot.get("p_gain_gt_0") or 0.0) < MIN_BOOTSTRAP_P_IMPROVE: reasons.append("bootstrap_support_low")
    if brier_delta > MAX_SELECTED_BRIER_DELTA: reasons.append("selected_brier_worse")
    return {
        "mode": mode,
        "overall": overall,
        "by_fold": by_fold,
        "baseline_overall": baseline,
        "baseline_by_fold": base_fold,
        "coverage_by_fold": coverage_by_fold,
        "changed_picks": changed,
        "bootstrap": boot,
        "hit_gain": round(gain, 6),
        "selected_brier_delta": round(brier_delta, 6),
        "gate_passed": not reasons,
        "gate_fail_reasons": reasons,
    }


def run_audit(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db: raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        rid = conn.execute("INSERT INTO advanced_goal_audit_runs(version,status) VALUES(%s,'running') RETURNING id", (VERSION,)).fetchone()[0]
        try:
            matches = _load_matches(conn)
            if any(str(m.get("season_code")) == LIVE_HOLDOUT for m in matches):
                # _load_matches currently only requests 2324/2425/2526; this is an explicit fail-closed assertion.
                raise RuntimeError("Protected 2627 holdout unexpectedly entered advanced goal audit")
            by_div: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for m in matches: by_div[str(m["division"])].append(m)

            base_weekly: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
            dc_weekly: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
            opp_weekly: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
            coverage = {MODE_DC: {f: [0, 0] for f in TEST_SEASONS}, MODE_OPP: {f: [0, 0] for f in TEST_SEASONS}}
            fold_meta: Dict[str, Any] = {}
            rho_meta: Dict[str, List[float]] = {f: [] for f in TEST_SEASONS}

            for fold in TEST_SEASONS:
                scored = skipped = 0
                for division, div_rows in by_div.items():
                    train = [m for m in div_rows if _order(str(m["season_code"])) < _order(fold)]
                    tests = [m for m in div_rows if str(m["season_code"]) == fold]
                    train.sort(key=lambda m: (m["match_date"], m["home_team"], m["away_team"]))
                    tests.sort(key=lambda m: (m["match_date"], m["home_team"], m["away_team"]))
                    if len(train) < MIN_HISTORY_MATCHES:
                        skipped += len(tests); continue
                    history = list(train)
                    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
                    for m in tests: grouped[_week(m["match_date"])].append(m)
                    for wk in sorted(grouped, key=lambda k: min(_as_date(m["match_date"]) for m in grouped[k])):
                        week_matches = sorted(grouped[wk], key=lambda m: (m["match_date"], m["home_team"], m["away_team"]))
                        # Parameters are fitted once using only matches completed before this ISO week.
                        rho, rho_ok, _rho_info = fit_dc_rho(history)
                        opp_model = fit_opponent_strengths(history)
                        rho_meta[fold].append(float(rho))
                        for match in week_matches:
                            pred = predict_match(history, match["home_team"], match["away_team"])
                            base_bm = best_market(pred)
                            base_row = _row(fold, division, match, base_bm, MODE_V1, True)
                            if base_row is None: continue
                            scored += 1
                            base_weekly[(fold, wk)].append(base_row)

                            dc_probs = dixon_coles_from_v1(pred, rho)
                            dc_bm = best_market_from_probabilities(dc_probs, pred.data_quality)
                            dc_row = _row(fold, division, match, dc_bm, MODE_DC, rho_ok)
                            coverage[MODE_DC][fold][1] += 1
                            coverage[MODE_DC][fold][0] += int(rho_ok)
                            if dc_row is not None: dc_weekly[(fold, wk)].append(dc_row)

                            opp_probs = opponent_adjusted_probabilities(pred, opp_model, str(match["home_team"]), str(match["away_team"]))
                            opp_bm = best_market_from_probabilities(opp_probs, pred.data_quality)
                            opp_ok = bool(opp_probs.get("available"))
                            opp_row = _row(fold, division, match, opp_bm, MODE_OPP, opp_ok)
                            coverage[MODE_OPP][fold][1] += 1
                            coverage[MODE_OPP][fold][0] += int(opp_ok)
                            if opp_row is not None: opp_weekly[(fold, wk)].append(opp_row)
                        # Only after the entire week has been predicted may those results enter history.
                        history.extend(week_matches)
                fold_meta[fold] = {
                    "train_seasons": [s for s in ("2324", "2425", "2526") if _order(s) < _order(fold)],
                    "test_season": fold,
                    "matches_scored": scored,
                    "matches_skipped_insufficient_history": skipped,
                    "within_week_history_frozen": True,
                }

            base_rows, base_by_week = _select(base_weekly)
            dc_rows, dc_by_week = _select(dc_weekly)
            opp_rows, opp_by_week = _select(opp_weekly)
            dc_report = _report(MODE_DC, dc_rows, dc_by_week, base_rows, base_by_week, {f: tuple(coverage[MODE_DC][f]) for f in TEST_SEASONS}, 20260910)
            opp_report = _report(MODE_OPP, opp_rows, opp_by_week, base_rows, base_by_week, {f: tuple(coverage[MODE_OPP][f]) for f in TEST_SEASONS}, 20261907)
            reports = {MODE_DC: dc_report, MODE_OPP: opp_report}
            passed = [r for r in reports.values() if r["gate_passed"]]
            passed.sort(key=lambda r: (float(r["hit_gain"]), float(r["bootstrap"].get("p_gain_gt_0") or 0), -float(r["selected_brier_delta"])), reverse=True)
            winner = passed[0]["mode"] if passed else MODE_V1

            result = {
                "version": VERSION,
                "policy_key": POLICY_KEY,
                "policy_version": POLICY_VERSION,
                "gate_passed": winner != MODE_V1,
                "recommended_activation": winner,
                "validation_protocol": {
                    "gate_version": "two-fold-week-block-v1",
                    "test_seasons": list(TEST_SEASONS),
                    "folds": fold_meta,
                    "sequential_history": True,
                    "within_week_history_frozen": True,
                    "week_block_bootstrap": True,
                    "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
                    "holdout_excluded": True,
                    "live_holdout_season": LIVE_HOLDOUT,
                    "holdout_rule": "2627 outcomes are never read for fitting, tuning or this audit",
                },
                "predeclared_gate": {
                    "min_pooled_hit_gain": MIN_POOLED_HIT_GAIN,
                    "min_each_fold_hit_gain": MIN_FOLD_HIT_GAIN,
                    "min_each_fold_feature_coverage": MIN_FOLD_COVERAGE,
                    "min_changed_picks": MIN_CHANGED_PICKS,
                    "min_bootstrap_p_gain_gt_0": MIN_BOOTSTRAP_P_IMPROVE,
                    "max_selected_brier_delta": MAX_SELECTED_BRIER_DELTA,
                },
                "predeclared_models": {
                    MODE_DC: {"rho_range": [-0.20, 0.20], "rho_step": 0.01, "rho_fit": "past-only low-score maximum likelihood", "application": "frozen V1 goal lambdas"},
                    MODE_OPP: {"attack_defence": "iterative opponent-adjusted Poisson multipliers", "recency_half_life_appearances": 24.0, "prior_matches": 8.0, "v1_lambda_blend_weight": OPPONENT_BLEND_WEIGHT},
                },
                "baseline": _metrics(base_rows),
                "challengers": reports,
                "dc_rho_summary": {f: {"weeks": len(v), "mean": round(mean(v), 5) if v else None, "min": min(v) if v else None, "max": max(v) if v else None} for f, v in rho_meta.items()},
                "activation_note": (f"{winner} passed independently and is the strongest passing challenger." if winner != MODE_V1 else "Neither challenger passed; production remains v1_only."),
            }
            conn.execute(
                """INSERT INTO policy_activation_registry(policy_key,policy_version,active_mode,metrics,reason)
                   VALUES(%s,%s,%s,%s,%s)
                   ON CONFLICT(policy_key) DO UPDATE SET policy_version=EXCLUDED.policy_version,active_mode=EXCLUDED.active_mode,
                     validated_at=NOW(),metrics=EXCLUDED.metrics,reason=EXCLUDED.reason""",
                (POLICY_KEY, POLICY_VERSION, winner, Jsonb(result), result["activation_note"]),
            )
            conn.execute("UPDATE advanced_goal_audit_runs SET finished_at=NOW(),status='success',results=%s,message=%s WHERE id=%s", (Jsonb(result), result["activation_note"], rid))
            print("ADVANCED_GOAL_AUDIT_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            conn.execute("UPDATE advanced_goal_audit_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s", (str(exc)[:2000], rid))
            raise


if __name__ == "__main__":
    print(json.dumps(run_audit(), ensure_ascii=False, indent=2, default=str))
