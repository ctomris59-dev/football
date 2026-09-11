#!/usr/bin/env python3
"""Sequential, leakage-safe audit for the weekly ranking challenger stack.

The frozen V1 market probabilities stay untouched. Features are challenged in the
predeclared order Lineup Stability V2 -> Missing Player Impact -> Corner-specific.
A stage is added to the accepted baseline only when it passes the same two-fold,
ISO-week-block gate used by the existing incremental-feature audit. 2026/27
outcomes are never read.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import psycopg
from psycopg.types.json import Jsonb

from incremental_feature_audit import (
    BOOTSTRAP_ITERATIONS,
    MIN_BOOTSTRAP_P_IMPROVE,
    MIN_CHANGED_PICKS,
    MIN_FOLD_COVERAGE,
    MIN_FOLD_HIT_GAIN,
    MIN_HISTORY_MATCHES,
    MIN_POOLED_HIT_GAIN,
    MAX_SELECTED_BRIER_DELTA,
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
from lineup_stability_v2_policy import continuity_v2_from_lineups, lineup_stability_v2_factor
from model_engine_v1 import best_market, predict_match
from production_predictor import canon
from ranking_challenger_stack_policy import (
    FEATURE_CORNER,
    FEATURE_LINEUP,
    FEATURE_MISSING,
    FEATURE_ORDER,
    MODE_V1,
    POLICY_KEY,
    POLICY_VERSION,
    corner_specific_factor,
    missing_player_factor,
    mode_from_features,
)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LIVE_HOLDOUT = "2627"
VERSION = "ranking-challenger-stack-two-fold-week-block-v1"
CORNER_RECENT = int(os.getenv("RANKING_STACK_CORNER_RECENT", "12"))
CORNER_MIN_MATCHES = int(os.getenv("RANKING_STACK_CORNER_MIN_MATCHES", "6"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS ranking_challenger_stack_audit_runs(
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


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _continuity_at(lineups, season_year: int, team: str, before: Any) -> Optional[float]:
    d = _as_date(before)
    prior = [s for dt, s in lineups.get((season_year, canon(team)), []) if dt < d][-6:]
    return continuity_v2_from_lineups(prior)


def _covered_injury_league_seasons(conn) -> Set[Tuple[int, int]]:
    covered: Set[Tuple[int, int]] = set()
    try:
        rows = conn.execute(
            "SELECT state_key FROM collection_state WHERE state_key LIKE 'injuries:%'"
        ).fetchall()
    except Exception:
        return covered
    for (key,) in rows:
        parts = str(key).split(":")
        if len(parts) != 3:
            continue
        try:
            covered.add((int(parts[1]), int(parts[2])))
        except (TypeError, ValueError):
            continue
    return covered


def _load_fixture_injuries(conn) -> Dict[Tuple[int, Any, str, str], Dict[str, Any]]:
    """Load historical injury observations only where league-season collection completed."""
    covered = _covered_injury_league_seasons(conn)
    out: Dict[Tuple[int, Any, str, str], Dict[str, Any]] = {}
    try:
        rows = conn.execute(
            """SELECT f.season,f.fixture_date,f.league_id,f.home_team_name,f.away_team_name,
                      i.team_name,i.player_name
                 FROM fixtures f
                 LEFT JOIN injuries i ON i.fixture_id=f.fixture_id
                WHERE f.season IN (2024,2025)
                  AND f.status_short IN ('FT','AET','PEN')
                ORDER BY f.fixture_date"""
        ).fetchall()
    except Exception:
        return out
    for season, fixture_date, league_id, home, away, injured_team, injured_player in rows:
        key = (int(season), _as_date(fixture_date), canon(home), canon(away))
        item = out.setdefault(
            key,
            {
                "covered": (int(league_id), int(season)) in covered,
                "injured": defaultdict(set),
            },
        )
        if injured_team and injured_player:
            item["injured"][canon(injured_team)].add(canon(injured_player))
    return out


def _historical_missing_impact(
    lineups,
    injuries: Dict[Tuple[int, Any, str, str], Dict[str, Any]],
    season_year: int,
    match_date: Any,
    home: str,
    away: str,
    team: str,
) -> Optional[float]:
    d = _as_date(match_date)
    fixture = injuries.get((season_year, d, canon(home), canon(away)))
    if not fixture or not fixture.get("covered"):
        return None
    prior = [s for dt, s in lineups.get((season_year, canon(team)), []) if dt < d][-6:]
    if len(prior) < 2:
        return None

    importance: Dict[str, float] = defaultdict(float)
    for weight, starters in enumerate(prior, start=1):
        for player in starters:
            importance[canon(player)] += float(weight)
    if not importance:
        return None
    top = sorted(importance.values(), reverse=True)[:11]
    denom = sum(top)
    if denom <= 0:
        return None
    missing = fixture["injured"].get(canon(team), set())
    impact = sum(importance.get(canon(player), 0.0) for player in missing) / denom
    return _clip(impact, 0.0, 0.55)


def _recent_team_corner_environment(history: Sequence[Dict[str, Any]], team: str) -> Tuple[Optional[float], int]:
    ct = canon(team)
    values: List[float] = []
    for row in reversed(history):
        if ct not in (canon(row.get("home_team")), canon(row.get("away_team"))):
            continue
        total = row.get("total_corners")
        if total is None and row.get("home_corners") is not None and row.get("away_corners") is not None:
            total = float(row["home_corners"]) + float(row["away_corners"])
        if total is not None:
            values.append(float(total))
        if len(values) >= max(1, CORNER_RECENT):
            break
    return (mean(values) if values else None, len(values))


def _corner_signal(history: Sequence[Dict[str, Any]], home: str, away: str) -> Tuple[Optional[float], bool]:
    home_env, hn = _recent_team_corner_environment(history, home)
    away_env, an = _recent_team_corner_environment(history, away)
    if home_env is None or away_env is None or min(hn, an) < CORNER_MIN_MATCHES:
        return None, False
    signal = _clip(((home_env + away_env) / 2.0) / 9.5, 0.55, 1.45)
    return signal, True


def _stage_gate(report: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    overall, base = report["overall"], report["baseline_overall"]
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


def _product(row: Dict[str, Any], features: Iterable[str]) -> float:
    value = float(row["v1"])
    for feature in features:
        value *= float(row[f"factor_{feature}"])
    return value


def _coverage_for_stage(
    weekly: Dict[Tuple[str, str], List[Dict[str, Any]]], feature: str, fold: str
) -> float:
    available = total = 0
    for (row_fold, _week_key), rows in weekly.items():
        if row_fold != fold:
            continue
        for row in rows:
            if feature == FEATURE_CORNER and not str(row["market"]).startswith("corners_over_"):
                continue
            total += 1
            available += int(bool(row[f"available_{feature}"]))
    return round(available / total, 4) if total else 0.0


def _evaluate_stage(
    weekly: Dict[Tuple[str, str], List[Dict[str, Any]]],
    accepted: Sequence[str],
    feature: str,
    seed: int,
) -> Dict[str, Any]:
    for rows in weekly.values():
        for row in rows:
            row["_stage_base"] = _product(row, accepted)
            row["_stage_candidate"] = row["_stage_base"] * float(row[f"factor_{feature}"])

    base_rows, base_by_week = _selection_sets(weekly, "_stage_base")
    chal_rows, chal_by_week = _selection_sets(weekly, "_stage_candidate")
    baseline_overall, overall = _metrics(base_rows), _metrics(chal_rows)
    baseline_by_fold = {
        fold: _metrics([r for r in base_rows if r["fold"] == fold]) for fold in TEST_SEASONS
    }
    by_fold = {
        fold: _metrics([r for r in chal_rows if r["fold"] == fold]) for fold in TEST_SEASONS
    }
    changed = 0
    for key, base_week in base_by_week.items():
        challenger_week = chal_by_week.get(key, [])
        changed += len({_candidate_id(r) for r in challenger_week} - {_candidate_id(r) for r in base_week})

    report = {
        "feature": feature,
        "baseline_features": list(accepted),
        "baseline_overall": baseline_overall,
        "overall": overall,
        "baseline_by_fold": baseline_by_fold,
        "by_fold": by_fold,
        "coverage_by_fold": {
            fold: _coverage_for_stage(weekly, feature, fold) for fold in TEST_SEASONS
        },
        "changed_picks": changed,
        "bootstrap": _bootstrap_delta(base_by_week, chal_by_week, seed=seed),
    }
    passed, reasons = _stage_gate(report)
    report["gate_passed"] = passed
    report["gate_fail_reasons"] = reasons
    report["hit_gain"] = round(
        (overall["hit_rate"] or 0.0) - (baseline_overall["hit_rate"] or 0.0), 6
    )
    report["selected_brier_delta"] = round(
        (overall["selected_brier"] or 0.0) - (baseline_overall["selected_brier"] or 0.0), 6
    )
    return report


def run_audit(database_url: Optional[str] = None) -> Dict[str, Any]:
    if tuple(TEST_SEASONS) != ("2425", "2526") or LIVE_HOLDOUT in TEST_SEASONS:
        raise RuntimeError("Ranking stack gate requires test folds 2425,2526 with 2627 excluded")
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")

    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        run_id = conn.execute(
            "INSERT INTO ranking_challenger_stack_audit_runs(version,status) VALUES(%s,'running') RETURNING id",
            (VERSION,),
        ).fetchone()[0]
        try:
            matches = _load_matches(conn)
            lineups = _load_lineups(conn)
            fixture_injuries = _load_fixture_injuries(conn)
            by_div: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for match in matches:
                by_div[str(match["division"])].append(match)

            weekly: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
            fold_meta: Dict[str, Any] = {}

            for test in TEST_SEASONS:
                scored = skipped = 0
                season_year = _season_year(test)
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
                        best = best_market(pred)
                        market = str(best["market"])
                        outcome = _outcome(match, market)
                        d = _as_date(match["match_date"])
                        if outcome is not None:
                            scored += 1
                            base_rank = float(best["probability"]) * (
                                0.75 + 0.25 * float(best["data_quality"])
                            )
                            hc = _continuity_at(lineups, season_year, str(match["home_team"]), d)
                            ac = _continuity_at(lineups, season_year, str(match["away_team"]), d)
                            lineup_factor, lineup_available = lineup_stability_v2_factor(hc, ac)

                            hi = _historical_missing_impact(
                                lineups, fixture_injuries, season_year, d,
                                str(match["home_team"]), str(match["away_team"]), str(match["home_team"])
                            )
                            ai = _historical_missing_impact(
                                lineups, fixture_injuries, season_year, d,
                                str(match["home_team"]), str(match["away_team"]), str(match["away_team"])
                            )
                            missing_factor, missing_available = missing_player_factor(
                                {"injury_impact": hi} if hi is not None else None,
                                {"injury_impact": ai} if ai is not None else None,
                            )

                            corner_signal, corner_signal_available = _corner_signal(
                                history, str(match["home_team"]), str(match["away_team"])
                            )
                            corner_factor, corner_available = corner_specific_factor(
                                market, bool(best["selection_yes"]), corner_signal
                            )
                            corner_available = bool(corner_available and corner_signal_available)

                            row = {
                                "fold": test,
                                "week": _week(d),
                                "division": division,
                                "league": str(match["league_name"]),
                                "date": d,
                                "home": str(match["home_team"]),
                                "away": str(match["away_team"]),
                                "market": market,
                                "selection": str(best["selection"]),
                                "confidence": float(best["probability"]),
                                "data_quality": float(best["data_quality"]),
                                "hit": bool(outcome) == bool(best["selection_yes"]),
                                "v1": base_rank,
                                f"factor_{FEATURE_LINEUP}": float(lineup_factor),
                                f"available_{FEATURE_LINEUP}": bool(lineup_available),
                                f"factor_{FEATURE_MISSING}": float(missing_factor),
                                f"available_{FEATURE_MISSING}": bool(missing_available),
                                f"factor_{FEATURE_CORNER}": float(corner_factor),
                                f"available_{FEATURE_CORNER}": bool(corner_available),
                            }
                            weekly[(test, row["week"])].append(row)
                        history.append(match)
                fold_meta[test] = {
                    "train_seasons": [s for s in SEASONS if _order(s) < _order(test)],
                    "test_season": test,
                    "matches_scored": scored,
                    "matches_skipped_insufficient_history": skipped,
                }

            accepted: List[str] = []
            stages: Dict[str, Any] = {}
            seeds = {
                FEATURE_LINEUP: 202609111,
                FEATURE_MISSING: 202609112,
                FEATURE_CORNER: 202609113,
            }
            for feature in FEATURE_ORDER:
                report = _evaluate_stage(weekly, accepted, feature, seeds[feature])
                stages[feature] = report
                if report["gate_passed"]:
                    accepted.append(feature)

            mode = mode_from_features(accepted)
            passed_any = bool(accepted)
            result = {
                "version": VERSION,
                "policy_key": POLICY_KEY,
                "policy_version": POLICY_VERSION,
                "gate_passed": passed_any,
                "recommended_activation": mode,
                "accepted_features": accepted,
                "rejected_features": [feature for feature in FEATURE_ORDER if feature not in accepted],
                "stages": stages,
                "validation_protocol": {
                    "gate_version": "two-fold-week-block-v1",
                    "test_seasons": list(TEST_SEASONS),
                    "folds": fold_meta,
                    "sequential_history": True,
                    "sequential_feature_acceptance": True,
                    "feature_order": list(FEATURE_ORDER),
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
                "activation_note": (
                    f"Accepted sequential stack: {mode}" if passed_any
                    else "No challenger stage passed; production remains v1_only."
                ),
            }
            conn.execute(
                """INSERT INTO policy_activation_registry(policy_key,policy_version,active_mode,metrics,reason)
                   VALUES(%s,%s,%s,%s,%s)
                   ON CONFLICT(policy_key) DO UPDATE SET policy_version=EXCLUDED.policy_version,
                     active_mode=EXCLUDED.active_mode,validated_at=NOW(),metrics=EXCLUDED.metrics,reason=EXCLUDED.reason""",
                (POLICY_KEY, POLICY_VERSION, mode, Jsonb(result), result["activation_note"]),
            )
            conn.execute(
                "UPDATE ranking_challenger_stack_audit_runs SET finished_at=NOW(),status='success',results=%s,message=%s WHERE id=%s",
                (Jsonb(result), result["activation_note"], run_id),
            )
            print("RANKING_CHALLENGER_STACK_AUDIT_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            conn.execute(
                "UPDATE ranking_challenger_stack_audit_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                (str(exc)[:2000], run_id),
            )
            raise


if __name__ == "__main__":
    print(json.dumps(run_audit(), ensure_ascii=False, indent=2, default=str))
