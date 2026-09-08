#!/usr/bin/env python3
"""Leakage-safe rolling backtest for the Big Five probability engine.

Train/history: 2024/25 plus all earlier matches in 2025/26.
Test: every 2025/26 match, scored before it is appended to history.
Also simulates a weekly Top-10 policy: one strongest supported market per match,
then the ten highest-confidence matches per ISO week.
"""
from __future__ import annotations

import json
import logging
import math
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.types.json import Jsonb

from model_engine import Prediction, best_market, predict_match

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BACKTEST_TRAIN_SEASON = os.getenv("BACKTEST_TRAIN_SEASON", "2425")
BACKTEST_TEST_SEASON = os.getenv("BACKTEST_TEST_SEASON", "2526")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("football-backtest")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS model_backtest_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    model_version TEXT NOT NULL,
    train_season TEXT NOT NULL,
    test_season TEXT NOT NULL,
    matches_scored INTEGER NOT NULL DEFAULT 0,
    metrics JSONB,
    market_metrics JSONB,
    top10_metrics JSONB,
    status TEXT NOT NULL,
    message TEXT
);
"""

MODEL_VERSION = "poisson-form-v1"
MARKETS = ("over_2_5", "btts", "corners_over_8_5")


def _safe_log(x: float) -> float:
    return math.log(min(1.0 - 1e-12, max(1e-12, x)))


def _metrics(items: Sequence[Tuple[float, int]]) -> Dict[str, Any]:
    if not items:
        return {"n": 0}
    n = len(items)
    brier = sum((p - y) ** 2 for p, y in items) / n
    logloss = -sum(y * _safe_log(p) + (1 - y) * _safe_log(1 - p) for p, y in items) / n
    acc = sum((p >= 0.5) == bool(y) for p, y in items) / n
    result: Dict[str, Any] = {"n": n, "brier": round(brier, 5), "logloss": round(logloss, 5), "accuracy_0_50": round(acc, 4), "base_rate": round(sum(y for _, y in items) / n, 4)}
    for threshold in (0.55, 0.60, 0.65, 0.70):
        selected = [(p, y) for p, y in items if max(p, 1.0 - p) >= threshold]
        if selected:
            hit = sum((p >= 0.5) == bool(y) for p, y in selected) / len(selected)
            result[f"confidence_{threshold:.2f}"] = {"n": len(selected), "coverage": round(len(selected) / n, 4), "hit_rate": round(hit, 4)}
        else:
            result[f"confidence_{threshold:.2f}"] = {"n": 0, "coverage": 0.0, "hit_rate": None}
    return result


def _outcome(match: Dict[str, Any], market: str) -> Optional[int]:
    v = match.get(market)
    return None if v is None else (1 if bool(v) else 0)


def _prediction_prob(pred: Prediction, market: str) -> float:
    if market == "over_2_5": return pred.p_over_2_5
    if market == "btts": return pred.p_btts
    if market == "corners_over_8_5": return pred.p_corners_over_8_5
    raise KeyError(market)


def load_matches(conn: psycopg.Connection) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT season_code, division, league_name, match_date,
               home_team, away_team, home_goals, away_goals,
               home_shots, away_shots, home_shots_on_target, away_shots_on_target,
               home_corners, away_corners, total_corners,
               over_2_5, btts, corners_over_8_5,
               odds_over_2_5, odds_under_2_5
        FROM football_data_matches
        WHERE season_code IN (%s, %s)
          AND home_goals IS NOT NULL AND away_goals IS NOT NULL
        ORDER BY division, match_date, home_team, away_team
        """,
        (BACKTEST_TRAIN_SEASON, BACKTEST_TEST_SEASON),
    )
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def run_backtest(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db: raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
        run_id = conn.execute("INSERT INTO model_backtest_runs(model_version, train_season, test_season, status) VALUES (%s,%s,%s,'running') RETURNING id", (MODEL_VERSION, BACKTEST_TRAIN_SEASON, BACKTEST_TEST_SEASON)).fetchone()[0]
        try:
            all_matches = load_matches(conn)
            by_div: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for m in all_matches: by_div[str(m["division"])].append(m)
            per_market: Dict[str, List[Tuple[float, int]]] = {m: [] for m in MARKETS}
            weekly_candidates: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            scored = 0
            for division, matches in by_div.items():
                history = [m for m in matches if m["season_code"] == BACKTEST_TRAIN_SEASON]
                tests = [m for m in matches if m["season_code"] == BACKTEST_TEST_SEASON]
                history.sort(key=lambda m: (m["match_date"], m["home_team"], m["away_team"]))
                tests.sort(key=lambda m: (m["match_date"], m["home_team"], m["away_team"]))
                for match in tests:
                    pred = predict_match(history, match["home_team"], match["away_team"])
                    scored += 1
                    for market in MARKETS:
                        y = _outcome(match, market)
                        if y is not None: per_market[market].append((_prediction_prob(pred, market), y))
                    bm = best_market(pred)
                    y_best = _outcome(match, bm["market"])
                    if y_best is not None:
                        y, w, _ = match["match_date"].isocalendar()
                        weekly_candidates[f"{y}-W{w:02d}"].append({"division": division, "match_date": str(match["match_date"]), "home_team": match["home_team"], "away_team": match["away_team"], "market": bm["market"], "selection_yes": bm["selection_yes"], "confidence": float(bm["probability"]), "data_quality": float(bm["data_quality"]), "correct": bool(y_best) == bool(bm["selection_yes"])})
                    history.append(match)
            market_metrics = {market: _metrics(items) for market, items in per_market.items()}
            overall_metrics = _metrics([item for market in MARKETS for item in per_market[market]])
            top10_picks: List[Dict[str, Any]] = []
            week_summaries: Dict[str, Dict[str, Any]] = {}
            for week, cands in sorted(weekly_candidates.items()):
                ranked = sorted(cands, key=lambda x: x["confidence"] * (0.75 + 0.25 * x["data_quality"]), reverse=True)[:10]
                if not ranked: continue
                hits = sum(1 for x in ranked if x["correct"])
                week_summaries[week] = {"n": len(ranked), "hits": hits, "hit_rate": round(hits / len(ranked), 4), "avg_confidence": round(sum(x["confidence"] for x in ranked) / len(ranked), 4)}
                top10_picks.extend(ranked)
            top10_hits = sum(1 for x in top10_picks if x["correct"])
            top10_metrics = {"weeks": len(week_summaries), "picks": len(top10_picks), "hits": top10_hits, "hit_rate": round(top10_hits / len(top10_picks), 4) if top10_picks else None, "avg_confidence": round(sum(x["confidence"] for x in top10_picks) / len(top10_picks), 4) if top10_picks else None, "by_week": week_summaries}
            result = {"model_version": MODEL_VERSION, "train_season": BACKTEST_TRAIN_SEASON, "test_season": BACKTEST_TEST_SEASON, "matches_scored": scored, "overall": overall_metrics, "markets": market_metrics, "top10": top10_metrics}
            conn.execute("UPDATE model_backtest_runs SET finished_at=NOW(), matches_scored=%s, metrics=%s, market_metrics=%s, top10_metrics=%s, status='success', message='ok' WHERE id=%s", (scored, Jsonb(overall_metrics), Jsonb(market_metrics), Jsonb(top10_metrics), run_id))
            log.info("BACKTEST_RESULT %s", json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return result
        except Exception as exc:
            conn.execute("UPDATE model_backtest_runs SET finished_at=NOW(), status='failed', message=%s WHERE id=%s", (str(exc), run_id))
            raise


def main() -> None:
    print(json.dumps(run_backtest(), ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
