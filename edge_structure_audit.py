#!/usr/bin/env python3
"""Research-only structural audit for the frozen production V1 model.

This script does NOT change production selection. It answers the questions that must
be resolved before promoting any new feature:

* where does V1 actually work by league and market?
* does the weekly ranking survive season-by-season walk-forward validation?
* how well calibrated is each market/confidence band?
* for O/U 2.5, does V1 add information beyond the historical no-vig market?

Default folds are strictly forward in time:
  2023/24 -> test 2024/25
  2023/24 + 2024/25 -> test 2025/26
Within a test season, each completed match is appended only AFTER it is predicted.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import date, datetime
from statistics import mean, stdev
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg
from psycopg.types.json import Jsonb

from model_engine_v1 import Prediction, best_market, predict_match

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SEASONS = [x.strip() for x in os.getenv("EDGE_AUDIT_SEASONS", "2324,2425,2526").split(",") if x.strip()]
TEST_SEASONS = [x.strip() for x in os.getenv("EDGE_AUDIT_TEST_SEASONS", "2425,2526").split(",") if x.strip()]
MIN_HISTORY_MATCHES = int(os.getenv("EDGE_AUDIT_MIN_HISTORY_MATCHES", "150"))
TOP_N_PER_WEEK = int(os.getenv("EDGE_AUDIT_TOP_N_PER_WEEK", "10"))
VERSION = "v1-structural-walkforward-v1"
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


def _wilson(hits: int, n: int, z: float = 1.96) -> Optional[List[float]]:
    if n <= 0:
        return None
    phat = hits / n
    den = 1.0 + z * z / n
    center = (phat + z * z / (2.0 * n)) / den
    half = z * math.sqrt((phat * (1.0 - phat) + z * z / (4.0 * n)) / n) / den
    return [round(max(0.0, center - half), 4), round(min(1.0, center + half), 4)]


def _binary_metrics(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"n": 0}
    n = len(rows)
    brier = mean((float(r["p"]) - int(r["y"])) ** 2 for r in rows)
    ll = -mean(int(r["y"]) * _safe_log(float(r["p"])) + (1 - int(r["y"])) * _safe_log(1.0 - float(r["p"])) for r in rows)
    out: Dict[str, Any] = {
        "n": n,
        "brier": round(brier, 5),
        "logloss": round(ll, 5),
        "base_rate": round(mean(int(r["y"]) for r in rows), 4),
    }
    for t in CONF_THRESHOLDS:
        chosen = [r for r in rows if max(float(r["p"]), 1.0 - float(r["p"])) >= t]
        hits = sum((float(r["p"]) >= 0.5) == bool(r["y"]) for r in chosen)
        out[f"confidence_{t:.2f}"] = {
            "n": len(chosen),
            "coverage": round(len(chosen) / n, 4),
            "hit_rate": round(hits / len(chosen), 4) if chosen else None,
            "wilson95": _wilson(hits, len(chosen)),
        }
    return out


def _pick_metrics(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"n": 0, "hits": 0, "hit_rate": None, "wilson95": None}
    n = len(rows)
    hits = sum(bool(r["hit"]) for r in rows)
    return {
        "n": n,
        "hits": hits,
        "hit_rate": round(hits / n, 4),
        "wilson95": _wilson(hits, n),
        "avg_confidence": round(mean(float(r["confidence"]) for r in rows), 4),
        "avg_data_quality": round(mean(float(r["data_quality"]) for r in rows), 4),
        "small_sample": n < 50,
    }


def _group_pick_metrics(rows: Sequence[Dict[str, Any]], key: str) -> Dict[str, Any]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[str(r[key])].append(r)
    return {k: _pick_metrics(v) for k, v in sorted(groups.items())}


def _market_probs(over_price: Any, under_price: Any) -> Optional[Tuple[float, float]]:
    try:
        oo, ou = float(over_price), float(under_price)
    except (TypeError, ValueError):
        return None
    if not (1.01 <= oo <= 20.0 and 1.01 <= ou <= 20.0):
        return None
    qo, qu = 1.0 / oo, 1.0 / ou
    z = qo + qu
    # Historical average two-way football totals should not carry absurd margins.
    if z <= 1.0 or z > 1.20:
        return None
    return qo / z, qu / z


def _paired_brier(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"n": 0}
    model_losses = [(float(r["model_p"]) - int(r["y"])) ** 2 for r in rows]
    market_losses = [(float(r["market_p"]) - int(r["y"])) ** 2 for r in rows]
    diffs = [a - b for a, b in zip(model_losses, market_losses)]
    delta = mean(diffs)
    if len(diffs) >= 2:
        se = stdev(diffs) / math.sqrt(len(diffs))
        ci = [round(delta - 1.96 * se, 5), round(delta + 1.96 * se, 5)]
    else:
        ci = None
    return {
        "n": len(rows),
        "model_brier": round(mean(model_losses), 5),
        "market_brier": round(mean(market_losses), 5),
        "delta_model_minus_market": round(delta, 5),
        "delta_95_approx": ci,
        "interpretation": "positive_delta_market_better; negative_delta_model_better",
    }


def _calibration_bins(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bins = [(0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 1.001)]
    out = []
    for lo, hi in bins:
        part = [r for r in rows if lo <= float(r["confidence"]) < hi]
        if not part:
            continue
        hits = sum(bool(r["hit"]) for r in part)
        out.append({
            "band": f"{lo:.2f}-{min(1.0, hi):.2f}",
            "n": len(part),
            "avg_stated_confidence": round(mean(float(r["confidence"]) for r in part), 4),
            "actual_hit_rate": round(hits / len(part), 4),
            "wilson95": _wilson(hits, len(part)),
        })
    return out


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


def run_audit(database_url: Optional[str] = None) -> Dict[str, Any]:
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
                        for market in MARKETS:
                            y = _outcome(match, market)
                            if y is None:
                                continue
                            p = _prob(pred, market)
                            binary_rows.append({
                                "fold": test_season, "division": division, "league": match["league_name"],
                                "market": market, "date": match["match_date"], "p": p, "y": y,
                            })

                        bm = best_market(pred)
                        yb = _outcome(match, str(bm["market"]))
                        if yb is not None:
                            iso = match["match_date"].isocalendar()
                            candidates.append({
                                "fold": test_season,
                                "week": f"{iso.year}-W{iso.week:02d}",
                                "division": division,
                                "league": match["league_name"],
                                "market": bm["market"],
                                "selection": bm["selection"],
                                "confidence": float(bm["probability"]),
                                "data_quality": float(bm["data_quality"]),
                                "ranking": float(bm["probability"]) * (0.75 + 0.25 * float(bm["data_quality"])),
                                "hit": bool(yb) == bool(bm["selection_yes"]),
                            })

                        mp = _market_probs(match.get("odds_over_2_5"), match.get("odds_under_2_5"))
                        if mp and match.get("over_2_5") is not None:
                            benchmark_rows.append({
                                "fold": test_season,
                                "division": division,
                                "league": match["league_name"],
                                "model_p": float(pred.p_over_2_5),
                                "market_p": float(mp[0]),
                                "y": int(bool(match["over_2_5"])),
                            })
                        history.append(match)
                fold_meta[test_season] = {"matches_scored": fold_scored, "matches_skipped_insufficient_history": fold_skipped}

            weekly: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
            for row in candidates:
                weekly[(str(row["fold"]), str(row["week"]))].append(row)
            top_picks: List[Dict[str, Any]] = []
            for _, rows in sorted(weekly.items()):
                top_picks.extend(sorted(rows, key=lambda r: (float(r["ranking"]), float(r["confidence"])), reverse=True)[:TOP_N_PER_WEEK])

            by_market_binary = {}
            for market in MARKETS:
                by_market_binary[market] = _binary_metrics([r for r in binary_rows if r["market"] == market])
            by_league_binary = {}
            for league in sorted({str(r["league"]) for r in binary_rows}):
                by_league_binary[league] = {
                    market: _binary_metrics([r for r in binary_rows if r["league"] == league and r["market"] == market])
                    for market in MARKETS
                }

            benchmark_by_fold = {fold: _paired_brier([r for r in benchmark_rows if r["fold"] == fold]) for fold in TEST_SEASONS}
            benchmark_by_league = {league: _paired_brier([r for r in benchmark_rows if r["league"] == league]) for league in sorted({str(r["league"]) for r in benchmark_rows})}

            results = {
                "version": VERSION,
                "purpose": "research_only_no_production_change",
                "folds": fold_meta,
                "all_market_probabilities": {
                    "overall": _binary_metrics(binary_rows),
                    "by_market": by_market_binary,
                    "by_league_market": by_league_binary,
                },
                "weekly_topn": {
                    "n_per_week": TOP_N_PER_WEEK,
                    "overall": _pick_metrics(top_picks),
                    "by_fold": _group_pick_metrics(top_picks, "fold"),
                    "by_market": _group_pick_metrics(top_picks, "market"),
                    "by_league": _group_pick_metrics(top_picks, "league"),
                    "by_league_market": {
                        f"{league}|{market}": _pick_metrics([r for r in top_picks if r["league"] == league and r["market"] == market])
                        for league in sorted({str(r["league"]) for r in top_picks})
                        for market in MARKETS
                    },
                    "calibration_bands": _calibration_bins(top_picks),
                },
                "ou25_market_benchmark": {
                    "note": "V1 production probability versus historical Football-Data two-way no-vig average odds; diagnostic, not execution-price ROI.",
                    "overall": _paired_brier(benchmark_rows),
                    "by_fold": benchmark_by_fold,
                    "by_league": benchmark_by_league,
                },
                "promotion_rule": "No feature is promoted from this audit alone. Require repeatable forward-fold improvement with adequate subgroup sample and no material calibration regression.",
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
