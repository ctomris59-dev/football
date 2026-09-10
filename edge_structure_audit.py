#!/usr/bin/env python3
"""Leakage-safe structural audit for the frozen production V1 model.

This module preserves the sequential-history mechanism: every test match is predicted
using only earlier seasons plus earlier completed matches from the same test season.
It adds the missing methodology around that engine:

* two expanding season folds (23/24 -> 24/25, then 23/24+24/25 -> 25/26);
* fold-stratified week-block bootstrap confidence intervals;
* league x market x confidence buckets with hierarchical shrinkage;
* paired no-vig O/U 2.5 market Brier benchmark;
* explicit 2026/27 live-holdout exclusion.

Nothing here activates a challenger or changes production probabilities.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.types.json import Jsonb

from model_engine_v1 import Prediction, best_market, predict_match
from research_evaluation import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_PRIOR_STRENGTH,
    confidence_band,
    evidence_level,
    paired_brier_metrics,
    selection_metrics,
    shrink_mean,
    shrink_rate,
    stable_seed,
    week_block_bootstrap,
)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SEASONS = [x.strip() for x in os.getenv("EDGE_AUDIT_SEASONS", "2324,2425,2526").split(",") if x.strip()]
TEST_SEASONS = [x.strip() for x in os.getenv("EDGE_AUDIT_TEST_SEASONS", "2425,2526").split(",") if x.strip()]
MIN_HISTORY_MATCHES = int(os.getenv("EDGE_AUDIT_MIN_HISTORY_MATCHES", "150"))
TOP_N_PER_WEEK = int(os.getenv("EDGE_AUDIT_TOP_N_PER_WEEK", "10"))
BOOTSTRAP_ITERATIONS = int(os.getenv("EDGE_AUDIT_BOOTSTRAP_ITERATIONS", str(DEFAULT_BOOTSTRAP_ITERATIONS)))
SHRINKAGE_PRIOR_N = float(os.getenv("EDGE_AUDIT_SHRINKAGE_PRIOR_N", str(DEFAULT_PRIOR_STRENGTH)))
LIVE_HOLDOUT_SEASON = os.getenv("EDGE_AUDIT_LIVE_HOLDOUT_SEASON", "2627").strip()
ALLOW_LIVE_HOLDOUT = os.getenv("EDGE_AUDIT_ALLOW_LIVE_HOLDOUT", "0").strip().lower() in {"1", "true", "yes"}
VERSION = "v1-structural-walkforward-v2-block-bootstrap-shrinkage"
MARKETS = ("over_2_5", "btts", "corners_over_8_5")
CONF_THRESHOLDS = (0.60, 0.65, 0.70, 0.75)

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_edge_audit_runs(
 id BIGSERIAL PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 version TEXT NOT NULL,
 seasons JSONB NOT NULL,
 test_seasons JSONB NOT NULL,
 status TEXT NOT NULL,
 results JSONB,
 message TEXT
);
"""


def _order(code: str) -> int:
    s = str(code or "").strip()
    if len(s) == 4 and s.isdigit():
        return int(s[:2])
    try:
        return int(s)
    except Exception:
        return -1


def _week(value: Any) -> str:
    iso = value.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _safe_log(p: float) -> float:
    return math.log(max(1e-12, min(1.0 - 1e-12, p)))


def _prob(pred: Prediction, market: str) -> float:
    if market == "over_2_5":
        return float(pred.p_over_2_5)
    if market == "btts":
        return float(pred.p_btts)
    return float(pred.p_corners_over_8_5)


def _outcome(match: Dict[str, Any], market: str) -> Optional[int]:
    value = match.get(market)
    return None if value is None else int(bool(value))


def _round(value: Optional[float], digits: int = 5) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _binary_scalar(rows: Sequence[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    if not rows:
        return {"model_brier": None}
    return {"model_brier": mean((float(r["p"]) - int(r["y"])) ** 2 for r in rows)}


def _binary_metrics(rows: Sequence[Dict[str, Any]], label: str) -> Dict[str, Any]:
    if not rows:
        return {"n": 0}
    n = len(rows)
    brier = mean((float(r["p"]) - int(r["y"])) ** 2 for r in rows)
    ll = -mean(
        int(r["y"]) * _safe_log(float(r["p"]))
        + (1 - int(r["y"])) * _safe_log(1.0 - float(r["p"]))
        for r in rows
    )
    out: Dict[str, Any] = {
        "n": n,
        "brier": round(brier, 5),
        "logloss": round(ll, 5),
        "base_rate": round(mean(int(r["y"]) for r in rows), 4),
        "bootstrap95": week_block_bootstrap(
            rows,
            _binary_scalar,
            iterations=BOOTSTRAP_ITERATIONS,
            seed=stable_seed("binary:" + label),
        ),
    }
    for t in CONF_THRESHOLDS:
        chosen = [r for r in rows if max(float(r["p"]), 1.0 - float(r["p"])) >= t]
        hits = sum((float(r["p"]) >= 0.5) == bool(r["y"]) for r in chosen)
        out[f"confidence_{t:.2f}"] = {
            "n": len(chosen),
            "coverage": round(len(chosen) / n, 4),
            "hit_rate": round(hits / len(chosen), 4) if chosen else None,
        }
    return out


def _market_probs(over_price: Any, under_price: Any) -> Optional[Tuple[float, float]]:
    try:
        oo, ou = float(over_price), float(under_price)
    except (TypeError, ValueError):
        return None
    if not (1.01 <= oo <= 20.0 and 1.01 <= ou <= 20.0):
        return None
    qo, qu = 1.0 / oo, 1.0 / ou
    z = qo + qu
    if z <= 1.0 or z > 1.20:
        return None
    return qo / z, qu / z


def _paired_brier_report(rows: Sequence[Dict[str, Any]], label: str) -> Dict[str, Any]:
    point = paired_brier_metrics(rows)
    if not rows or point.get("model_brier") is None:
        return {"n": 0}
    ci = week_block_bootstrap(
        rows,
        paired_brier_metrics,
        iterations=BOOTSTRAP_ITERATIONS,
        seed=stable_seed("paired:" + label),
    )
    return {
        "n": len(rows),
        "model_brier": _round(point.get("model_brier")),
        "market_brier": _round(point.get("market_brier")),
        "delta_model_minus_market": _round(point.get("delta_model_minus_market")),
        "brier_skill_vs_market": _round(point.get("brier_skill_vs_market")),
        "bootstrap95": ci,
        "interpretation": "negative delta = model better; positive delta = market better",
    }


def _selection_report(rows: Sequence[Dict[str, Any]], label: str) -> Dict[str, Any]:
    if not rows:
        return {"n": 0}
    point = selection_metrics(rows)
    ci = week_block_bootstrap(
        rows,
        selection_metrics,
        iterations=BOOTSTRAP_ITERATIONS,
        seed=stable_seed("selection:" + label),
    )
    hits = sum(bool(r.get("hit")) for r in rows)
    priced = [r for r in rows if r.get("execution_price") is not None]
    paired = [r for r in rows if r.get("market_selected_p") is not None]
    avg_price = mean(float(r["execution_price"]) for r in priced) if priced else None
    return {
        "n": len(rows),
        "hits": hits,
        "hit_rate": _round(point.get("hit_rate"), 4),
        "model_brier": _round(point.get("model_brier")),
        "market_brier": _round(point.get("market_brier")),
        "delta_model_minus_market": _round(point.get("delta_model_minus_market")),
        "roi": _round(point.get("roi"), 4),
        "priced_n": len(priced),
        "paired_market_n": len(paired),
        "avg_odds": _round(avg_price, 3),
        "avg_confidence": round(mean(float(r["confidence"]) for r in rows), 4),
        "avg_data_quality": round(mean(float(r["data_quality"]) for r in rows), 4),
        "bootstrap95": ci,
    }


def _hierarchical_bucket_report(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Three-level partial pooling: market -> league/market -> confidence bucket."""
    market_rows: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    league_market_rows: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    bucket_rows: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        market = str(r["market"])
        league = str(r["league"])
        band = confidence_band(r.get("confidence"))
        market_rows[market].append(r)
        league_market_rows[(league, market)].append(r)
        bucket_rows[(league, market, band)].append(r)

    market_point = {m: selection_metrics(v) for m, v in market_rows.items()}
    lm_parent: Dict[Tuple[str, str], Dict[str, Optional[float]]] = {}
    lm_reports: Dict[str, Any] = {}
    for (league, market), part in sorted(league_market_rows.items()):
        raw = _selection_report(part, f"lm:{league}:{market}")
        parent = market_point.get(market, {})
        paired_n = int(raw.get("paired_market_n") or 0)
        shrunk = {
            "hit_rate": shrink_rate(int(raw.get("hits") or 0), len(part), parent.get("hit_rate"), SHRINKAGE_PRIOR_N),
            "model_brier": shrink_mean(raw.get("model_brier"), len(part), parent.get("model_brier"), SHRINKAGE_PRIOR_N),
            "market_brier": shrink_mean(raw.get("market_brier"), paired_n, parent.get("market_brier"), SHRINKAGE_PRIOR_N),
            "delta_model_minus_market": shrink_mean(raw.get("delta_model_minus_market"), paired_n, parent.get("delta_model_minus_market"), SHRINKAGE_PRIOR_N),
        }
        shrunk = {k: _round(v, 5 if "brier" in k or "delta" in k else 4) for k, v in shrunk.items()}
        lm_parent[(league, market)] = shrunk
        lm_reports[f"{league}|{market}"] = {"raw": raw, "shrunk": shrunk}

    buckets: Dict[str, Any] = {}
    for (league, market, band), part in sorted(bucket_rows.items()):
        raw = _selection_report(part, f"bucket:{league}:{market}:{band}")
        parent = lm_parent.get((league, market), {})
        paired_n = int(raw.get("paired_market_n") or 0)
        shrunk = {
            "hit_rate": shrink_rate(int(raw.get("hits") or 0), len(part), parent.get("hit_rate"), SHRINKAGE_PRIOR_N),
            "model_brier": shrink_mean(raw.get("model_brier"), len(part), parent.get("model_brier"), SHRINKAGE_PRIOR_N),
            "market_brier": shrink_mean(raw.get("market_brier"), paired_n, parent.get("market_brier"), SHRINKAGE_PRIOR_N),
            "delta_model_minus_market": shrink_mean(raw.get("delta_model_minus_market"), paired_n, parent.get("delta_model_minus_market"), SHRINKAGE_PRIOR_N),
        }
        shrunk = {k: _round(v, 5 if "brier" in k or "delta" in k else 4) for k, v in shrunk.items()}
        hit_ci = (raw.get("bootstrap95") or {}).get("hit_rate")
        level = evidence_level(len(part), hit_ci)
        buckets[f"{league}|{market}|{band}"] = {
            "raw": raw,
            "shrunk": shrunk,
            "evidence": level,
            "production_use": "NO" if level in {"insufficient", "weak"} else "RESEARCH_ONLY_UNTIL_GATE",
            "parent": f"{league}|{market}",
        }
    return {
        "method": "hierarchical_partial_pooling_market_then_league_market_then_confidence",
        "prior_strength_equivalent_n": SHRINKAGE_PRIOR_N,
        "league_market_parents": lm_reports,
        "league_market_confidence": buckets,
    }


def _load(conn) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """SELECT season_code,season_start,division,league_name,match_date,home_team,away_team,
                  home_goals,away_goals,home_shots,away_shots,home_shots_on_target,away_shots_on_target,
                  home_corners,away_corners,total_corners,over_2_5,btts,corners_over_8_5,
                  odds_over_2_5,odds_under_2_5
           FROM football_data_matches
           WHERE season_code = ANY(%s) AND home_goals IS NOT NULL AND away_goals IS NOT NULL
           ORDER BY division,match_date,home_team,away_team""",
        (SEASONS,),
    )
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _validate_protocol() -> None:
    if LIVE_HOLDOUT_SEASON and LIVE_HOLDOUT_SEASON in TEST_SEASONS and not ALLOW_LIVE_HOLDOUT:
        raise RuntimeError(
            f"Live holdout season {LIVE_HOLDOUT_SEASON} is protected and cannot be used as a backtest fold. "
            "Complete/close the season and explicitly set EDGE_AUDIT_ALLOW_LIVE_HOLDOUT=1 before changing this."
        )
    ordered_tests = sorted(TEST_SEASONS, key=_order)
    for test in ordered_tests:
        if not any(_order(s) < _order(test) for s in SEASONS):
            raise RuntimeError(f"No earlier training season available for test fold {test}")


def run_audit(database_url: Optional[str] = None) -> Dict[str, Any]:
    _validate_protocol()
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        run_id = conn.execute(
            "INSERT INTO research_edge_audit_runs(version,seasons,test_seasons,status) VALUES(%s,%s,%s,'running') RETURNING id",
            (VERSION, Jsonb(SEASONS), Jsonb(TEST_SEASONS)),
        ).fetchone()[0]
        try:
            all_matches = _load(conn)
            by_div: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for m in all_matches:
                by_div[str(m["division"])].append(m)

            binary_rows: List[Dict[str, Any]] = []
            candidates: List[Dict[str, Any]] = []
            benchmark_rows: List[Dict[str, Any]] = []
            fold_meta: Dict[str, Any] = {}

            for test_season in sorted(TEST_SEASONS, key=_order):
                fold_scored = fold_skipped = 0
                train_seasons = sorted({str(s) for s in SEASONS if _order(s) < _order(test_season)}, key=_order)
                for division, matches in by_div.items():
                    train = [m for m in matches if _order(m["season_code"]) < _order(test_season)]
                    tests = [m for m in matches if str(m["season_code"]) == str(test_season)]
                    train.sort(key=lambda x: (x["match_date"], x["home_team"], x["away_team"]))
                    tests.sort(key=lambda x: (x["match_date"], x["home_team"], x["away_team"]))
                    if len(train) < MIN_HISTORY_MATCHES:
                        fold_skipped += len(tests)
                        continue
                    history = list(train)
                    for match in tests:
                        pred = predict_match(history, match["home_team"], match["away_team"])
                        fold_scored += 1
                        wk = _week(match["match_date"])
                        for market in MARKETS:
                            y = _outcome(match, market)
                            if y is None:
                                continue
                            p = _prob(pred, market)
                            binary_rows.append({
                                "fold": test_season,
                                "week": wk,
                                "division": division,
                                "league": match["league_name"],
                                "market": market,
                                "date": match["match_date"],
                                "p": p,
                                "y": y,
                            })

                        bm = best_market(pred)
                        yb = _outcome(match, str(bm["market"]))
                        if yb is not None:
                            selection_yes = bool(bm["selection_yes"])
                            hit = bool(yb) == selection_yes
                            mp = _market_probs(match.get("odds_over_2_5"), match.get("odds_under_2_5"))
                            market_selected_p = execution_price = None
                            if str(bm["market"]) == "over_2_5" and mp:
                                market_selected_p = float(mp[0] if selection_yes else mp[1])
                                try:
                                    execution_price = float(match.get("odds_over_2_5") if selection_yes else match.get("odds_under_2_5"))
                                    if execution_price <= 1.0:
                                        execution_price = None
                                except (TypeError, ValueError):
                                    execution_price = None
                            candidates.append({
                                "fold": test_season,
                                "week": wk,
                                "division": division,
                                "league": match["league_name"],
                                "market": bm["market"],
                                "selection": bm["selection"],
                                "confidence": float(bm["probability"]),
                                "data_quality": float(bm["data_quality"]),
                                "ranking": float(bm["probability"]) * (0.75 + 0.25 * float(bm["data_quality"])),
                                "hit": hit,
                                "p_selected": float(bm["probability"]),
                                "y_selected": int(hit),
                                "market_selected_p": market_selected_p,
                                "execution_price": execution_price,
                            })

                        mp = _market_probs(match.get("odds_over_2_5"), match.get("odds_under_2_5"))
                        if mp and match.get("over_2_5") is not None:
                            benchmark_rows.append({
                                "fold": test_season,
                                "week": wk,
                                "division": division,
                                "league": match["league_name"],
                                "model_p": float(pred.p_over_2_5),
                                "market_p": float(mp[0]),
                                "y": int(bool(match["over_2_5"])),
                            })
                        history.append(match)
                fold_meta[test_season] = {
                    "train_seasons": train_seasons,
                    "test_season": test_season,
                    "matches_scored": fold_scored,
                    "matches_skipped_insufficient_history": fold_skipped,
                }

            weekly: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
            for row in candidates:
                weekly[(str(row["fold"]), str(row["week"]))].append(row)
            top_picks: List[Dict[str, Any]] = []
            for _, rows in sorted(weekly.items()):
                top_picks.extend(
                    sorted(rows, key=lambda r: (float(r["ranking"]), float(r["confidence"])), reverse=True)[:TOP_N_PER_WEEK]
                )

            by_market_binary = {
                market: _binary_metrics([r for r in binary_rows if r["market"] == market], f"market:{market}")
                for market in MARKETS
            }
            by_league_binary: Dict[str, Any] = {}
            for league in sorted({str(r["league"]) for r in binary_rows}):
                by_league_binary[league] = {
                    market: _binary_metrics(
                        [r for r in binary_rows if r["league"] == league and r["market"] == market],
                        f"league:{league}:{market}",
                    )
                    for market in MARKETS
                }

            benchmark_by_fold = {
                fold: _paired_brier_report([r for r in benchmark_rows if r["fold"] == fold], f"fold:{fold}")
                for fold in TEST_SEASONS
            }
            benchmark_by_league = {
                league: _paired_brier_report([r for r in benchmark_rows if r["league"] == league], f"league:{league}")
                for league in sorted({str(r["league"]) for r in benchmark_rows})
            }
            fold_deltas = [
                benchmark_by_fold[f].get("delta_model_minus_market")
                for f in TEST_SEASONS
                if benchmark_by_fold.get(f, {}).get("delta_model_minus_market") is not None
            ]
            fold_stability = {
                "folds_with_paired_market_data": len(fold_deltas),
                "same_direction": bool(fold_deltas) and (all(x < 0 for x in fold_deltas) or all(x > 0 for x in fold_deltas)),
                "model_better_in_every_fold": bool(fold_deltas) and all(x < 0 for x in fold_deltas),
                "market_better_in_every_fold": bool(fold_deltas) and all(x > 0 for x in fold_deltas),
                "note": "fold stability is interpreted separately from pooled sample-size-weighted performance",
            }

            weekly_by_fold = {
                fold: _selection_report([r for r in top_picks if r["fold"] == fold], f"topn-fold:{fold}")
                for fold in TEST_SEASONS
            }
            bucket_analysis = _hierarchical_bucket_report(top_picks)

            results = {
                "version": VERSION,
                "purpose": "research_only_no_production_change",
                "validation_protocol": {
                    "gate_version": "two-fold-week-block-v1",
                    "sequential_history": True,
                    "test_seasons": list(TEST_SEASONS),
                    "folds": fold_meta,
                    "bootstrap": {
                        "unit": "ISO-week block",
                        "pooled_stratification": "resample weeks within each fold, then concatenate",
                        "iterations": BOOTSTRAP_ITERATIONS,
                    },
                    "shrinkage": {
                        "method": "hierarchical partial pooling",
                        "prior_strength_equivalent_n": SHRINKAGE_PRIOR_N,
                    },
                    "live_holdout_season": LIVE_HOLDOUT_SEASON,
                    "holdout_excluded": LIVE_HOLDOUT_SEASON not in TEST_SEASONS,
                    "holdout_rule": "2026/27 may be monitored but must not be used for tuning or backtest folds while live",
                },
                "all_market_probabilities": {
                    "overall": _binary_metrics(binary_rows, "overall"),
                    "by_fold": {
                        fold: _binary_metrics([r for r in binary_rows if r["fold"] == fold], f"binary-fold:{fold}")
                        for fold in TEST_SEASONS
                    },
                    "by_market": by_market_binary,
                    "by_league_market": by_league_binary,
                },
                "weekly_topn": {
                    "n_per_week": TOP_N_PER_WEEK,
                    "overall": _selection_report(top_picks, "topn-overall"),
                    "by_fold": weekly_by_fold,
                    "bucket_analysis": bucket_analysis,
                    "price_note": "avg_odds/ROI exist only where Football-Data historical O/U2.5 prices cover the selected side; they are diagnostic, not guaranteed execution prices",
                },
                "ou25_market_benchmark": {
                    "note": "V1 probability versus historical Football-Data two-way no-vig average O/U2.5 odds; closing odds are not assumed here.",
                    "overall": _paired_brier_report(benchmark_rows, "benchmark-overall"),
                    "by_fold": benchmark_by_fold,
                    "by_league": benchmark_by_league,
                    "fold_stability": fold_stability,
                },
                "promotion_rule": "No challenger is promoted from this audit. Promotion requires predeclared two-fold OOS improvement, fold-stratified week-block CI, stability, and no use of 2026/27 outcomes for tuning.",
            }
            conn.execute(
                "UPDATE research_edge_audit_runs SET finished_at=NOW(),status='success',results=%s,message='ok' WHERE id=%s",
                (Jsonb(results), run_id),
            )
            print("EDGE_STRUCTURE_AUDIT_RESULT", json.dumps(results, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return results
        except Exception as exc:
            conn.execute(
                "UPDATE research_edge_audit_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                (str(exc)[:2000], run_id),
            )
            raise


if __name__ == "__main__":
    print(json.dumps(run_audit(), ensure_ascii=False, indent=2, default=str))
