#!/usr/bin/env python3
"""Post-decision CLV evaluation for recorded production picks.

Leakage boundary:
- prediction inputs may only use prices with snapshot_hour <= prediction timestamp;
- CLV evaluation may compare that decision-time price with the last paired same-book
  price strictly before kickoff;
- closing prices are never returned to the model, filter, threshold or ranking logic.

This module is intentionally separate from value_backtest.py because CLV requires a
true time series of decision-time versus closing market snapshots. Historical
validation and the live 2026/27 holdout are reported separately so live outcomes can
never silently contaminate a validation aggregate.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import date, datetime
from statistics import mean, median
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from research_evaluation import stable_seed, week_block_bootstrap

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
VERSION = "production-clv-same-book-paired-v2-holdout-separated"
PRICE_MIN = float(os.getenv("CLV_PRICE_MIN", "1.01"))
PRICE_MAX = float(os.getenv("CLV_PRICE_MAX", "20.0"))
MAX_OVERROUND = float(os.getenv("CLV_MAX_OVERROUND", "1.20"))
BOOTSTRAP_ITERATIONS = int(os.getenv("CLV_BOOTSTRAP_ITERATIONS", "2000"))
TOP10_ONLY = os.getenv("CLV_TOP10_ONLY", "1").strip().lower() in {"1", "true", "yes"}
HISTORICAL_VALIDATION_SEASONS = tuple(
    x.strip() for x in os.getenv("CLV_VALIDATION_SEASONS", "2425,2526").split(",") if x.strip()
)
LIVE_HOLDOUT_SEASON = os.getenv("CLV_LIVE_HOLDOUT_SEASON", "2627").strip()
MARKETS = ("over_2_5", "btts", "corners_over_8_5")


def market_key(name: Any, handicap: Any) -> Optional[str]:
    n = str(name or "").lower()
    try:
        line = float(handicap) if handicap is not None else None
    except Exception:
        line = None
    if "both teams to score" in n:
        return "btts"
    if "corner" in n and line is not None and abs(line - 8.5) < 0.01:
        return "corners_8_5"
    if line is not None and abs(line - 2.5) < 0.01 and ("over under" in n or "total" in n or "goal" in n):
        return "goals_2_5"
    return None


def outcome_key(market: str, outcome: Any) -> Optional[str]:
    s = str(outcome or "").strip().lower()
    if market in {"goals_2_5", "corners_8_5"}:
        if "over" in s:
            return "over"
        if "under" in s:
            return "under"
    if market == "btts":
        if s == "yes" or " yes" in " " + s:
            return "yes"
        if s == "no" or " no" in " " + s:
            return "no"
    return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS clv_backtest_runs(
 id BIGSERIAL PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 version TEXT NOT NULL,
 status TEXT NOT NULL,
 picks_seen INTEGER NOT NULL DEFAULT 0,
 picks_with_clv INTEGER NOT NULL DEFAULT 0,
 results JSONB,
 message TEXT
);
"""


def _valid_price(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if PRICE_MIN <= x <= PRICE_MAX else None


def _selected_sides(market: str, selection_yes: bool) -> Tuple[str, str]:
    if market in {"over_2_5", "corners_over_8_5"}:
        return ("over", "under") if selection_yes else ("under", "over")
    return ("yes", "no") if selection_yes else ("no", "yes")


def _paired_probability(selected_price: float, opposite_price: float) -> Optional[float]:
    qa, qb = 1.0 / selected_price, 1.0 / opposite_price
    z = qa + qb
    if not (1.0 <= z <= MAX_OVERROUND):
        return None
    return qa / z


def _normalized_market(raw_market: Optional[str]) -> Optional[str]:
    if raw_market == "goals_2_5":
        return "over_2_5"
    if raw_market == "corners_8_5":
        return "corners_over_8_5"
    return raw_market


def reconstruct_clv(
    rows: Sequence[Tuple[Any, ...]],
    market: str,
    selection_yes: bool,
    prediction_ts: Any,
    kickoff_ts: Any,
) -> Optional[Dict[str, Any]]:
    """Return median same-book paired CLV for one recorded pick."""
    selected, opposite = _selected_sides(market, selection_yes)
    grouped: Dict[Tuple[str, Any], Dict[str, float]] = defaultdict(dict)
    for snapshot, bookmaker, market_name, handicap, outcome_name, price in rows:
        if snapshot is None or snapshot >= kickoff_ts:
            continue
        raw_market = market_key(market_name, handicap)
        normalized = _normalized_market(raw_market)
        if normalized != market:
            continue
        side = outcome_key(raw_market, outcome_name) if raw_market else None
        px = _valid_price(price)
        if side and px is not None:
            grouped[(str(bookmaker), snapshot)][side] = px

    series_by_book: Dict[str, List[Tuple[Any, float, float]]] = defaultdict(list)
    for (book, snapshot), sides in grouped.items():
        a, b = sides.get(selected), sides.get(opposite)
        if a is None or b is None:
            continue
        p = _paired_probability(a, b)
        if p is not None:
            series_by_book[book].append((snapshot, p, a))

    probability_clv: List[float] = []
    log_price_clv: List[float] = []
    prediction_probs: List[float] = []
    closing_probs: List[float] = []
    prediction_prices: List[float] = []
    closing_prices: List[float] = []
    close_lag_minutes: List[float] = []

    for _book, series in series_by_book.items():
        series.sort(key=lambda x: x[0])
        decision = [x for x in series if x[0] <= prediction_ts]
        closing = [x for x in series if x[0] < kickoff_ts]
        if not decision or not closing:
            continue
        pred = decision[-1]
        close = closing[-1]
        if close[0] <= pred[0]:
            continue
        prediction_probs.append(float(pred[1]))
        closing_probs.append(float(close[1]))
        prediction_prices.append(float(pred[2]))
        closing_prices.append(float(close[2]))
        probability_clv.append(float(close[1]) - float(pred[1]))
        log_price_clv.append(math.log(float(pred[2]) / float(close[2])))
        close_lag_minutes.append((kickoff_ts - close[0]).total_seconds() / 60.0)

    if not probability_clv:
        return None
    return {
        "books": len(probability_clv),
        "prediction_no_vig_probability": float(median(prediction_probs)),
        "closing_no_vig_probability": float(median(closing_probs)),
        "probability_clv": float(median(probability_clv)),
        "prediction_price": float(median(prediction_prices)),
        "closing_price": float(median(closing_prices)),
        "log_price_clv": float(median(log_price_clv)),
        "closing_snapshot_lag_minutes": float(median(close_lag_minutes)),
    }


def _season_code(ts: Any) -> str:
    d = ts.date() if isinstance(ts, datetime) else ts
    if not isinstance(d, date):
        d = date.fromisoformat(str(d)[:10])
    start = d.year if d.month >= 7 else d.year - 1
    return f"{str(start)[-2:]}{str(start + 1)[-2:]}"


def _week(ts: Any) -> str:
    d = ts.date() if isinstance(ts, datetime) else ts
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def split_evaluation_scope(rows: Sequence[Mapping[str, Any]]) -> Dict[str, List[Mapping[str, Any]]]:
    """Separate historical validation from the live holdout before any aggregate is built."""
    historical = [r for r in rows if str(r.get("fold")) in HISTORICAL_VALIDATION_SEASONS]
    live = [r for r in rows if str(r.get("fold")) == LIVE_HOLDOUT_SEASON]
    other = [
        r for r in rows
        if str(r.get("fold")) not in HISTORICAL_VALIDATION_SEASONS
        and str(r.get("fold")) != LIVE_HOLDOUT_SEASON
    ]
    return {"historical_validation": historical, "live_holdout": live, "other": other}


def _clv_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Optional[float]]:
    if not rows:
        return {"probability_clv": None, "log_price_clv": None}
    return {
        "probability_clv": mean(float(r["probability_clv"]) for r in rows),
        "log_price_clv": mean(float(r["log_price_clv"]) for r in rows),
    }


def _summary(rows: Sequence[Mapping[str, Any]], label: str) -> Dict[str, Any]:
    if not rows:
        return {"n": 0}
    point = _clv_metrics(rows)
    return {
        "n": len(rows),
        "positive_probability_clv_share": round(sum(float(r["probability_clv"]) > 0 for r in rows) / len(rows), 4),
        "mean_probability_clv_pp": round(float(point["probability_clv"]) * 100.0, 3),
        "median_probability_clv_pp": round(median(float(r["probability_clv"]) for r in rows) * 100.0, 3),
        "mean_log_price_clv": round(float(point["log_price_clv"]), 6),
        "avg_books": round(mean(int(r["books"]) for r in rows), 2),
        "median_closing_snapshot_lag_minutes": round(median(float(r["closing_snapshot_lag_minutes"]) for r in rows), 1),
        "bootstrap95": week_block_bootstrap(
            rows,
            _clv_metrics,
            iterations=BOOTSTRAP_ITERATIONS,
            seed=stable_seed("clv:" + label),
        ),
    }


def _scope_report(rows: Sequence[Mapping[str, Any]], label: str) -> Dict[str, Any]:
    seasons = sorted({str(r["fold"]) for r in rows})
    return {
        "overall": _summary(rows, label + ":overall"),
        "by_market": {
            m: _summary([r for r in rows if r["market"] == m], label + ":market:" + m)
            for m in MARKETS
        },
        "by_season": {
            s: _summary([r for r in rows if str(r["fold"]) == s], label + ":season:" + s)
            for s in seasons
        },
    }


def _load_picks(conn) -> List[Dict[str, Any]]:
    top_filter = "AND p.top10_rank IS NOT NULL" if TOP10_ONLY else ""
    # One evaluable decision per fixture/market: the latest recorded qualifying pick.
    # Repeated model refreshes for the same fixture must not inflate CLV sample size.
    rows = conn.execute(
        f"""SELECT DISTINCT ON (p.event_id,p.market)
                  p.run_id,p.event_id,p.market,p.selection,p.selection_yes,p.model_probability,
                  p.snapshot_hour,p.match_date,p.league_name,p.home_team,p.away_team,p.market_price,
                  fs.oddspapi_fixture_id
           FROM production_predictions p
           JOIN production_prediction_runs r ON r.id=p.run_id
           LEFT JOIN LATERAL (
             SELECT oddspapi_fixture_id
             FROM prematch_feature_snapshots s
             WHERE s.event_id=p.event_id
               AND s.oddspapi_fixture_id IS NOT NULL
               AND s.snapshot_hour<=p.snapshot_hour
             ORDER BY s.snapshot_hour DESC LIMIT 1
           ) fs ON TRUE
           WHERE p.snapshot_hour < p.match_date
             AND p.market IN ('over_2_5','btts','corners_over_8_5')
             AND r.model_version LIKE '%v1%'
             {top_filter}
           ORDER BY p.event_id,p.market,p.snapshot_hour DESC,p.run_id DESC"""
    ).fetchall()
    keys = [
        "run_id", "event_id", "market", "selection", "selection_yes", "confidence",
        "snapshot_hour", "match_date", "league", "home", "away", "recorded_market_price", "fixture_id",
    ]
    return [dict(zip(keys, row)) for row in rows]


def run_backtest(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    import psycopg
    from psycopg.types.json import Jsonb
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        run_id = conn.execute(
            "INSERT INTO clv_backtest_runs(version,status) VALUES(%s,'running') RETURNING id",
            (VERSION,),
        ).fetchone()[0]
        try:
            picks = _load_picks(conn)
            evaluated: List[Dict[str, Any]] = []
            missing_fixture = no_decision_close_pair = 0
            for pick in picks:
                fixture_id = pick.get("fixture_id")
                if not fixture_id:
                    missing_fixture += 1
                    continue
                raw = conn.execute(
                    """SELECT snapshot_hour,bookmaker,market_name,handicap,outcome_name,price
                       FROM oddspapi_market_prices
                       WHERE fixture_id=%s AND snapshot_hour<%s AND COALESCE(active,TRUE)=TRUE
                       ORDER BY snapshot_hour,bookmaker,market_id,outcome_id""",
                    (str(fixture_id), pick["match_date"]),
                ).fetchall()
                clv = reconstruct_clv(
                    raw,
                    str(pick["market"]),
                    bool(pick["selection_yes"]),
                    pick["snapshot_hour"],
                    pick["match_date"],
                )
                if not clv:
                    no_decision_close_pair += 1
                    continue
                evaluated.append({
                    **pick,
                    **clv,
                    "fold": _season_code(pick["match_date"]),
                    "week": _week(pick["match_date"]),
                })

            scopes = split_evaluation_scope(evaluated)
            historical = scopes["historical_validation"]
            live = scopes["live_holdout"]
            other = scopes["other"]
            results = {
                "version": VERSION,
                "purpose": "post_decision_evaluation_only_no_production_signal",
                "top10_only": TOP10_ONLY,
                "leakage_rule": {
                    "decision_price": "latest paired same-book snapshot <= recorded prediction timestamp",
                    "closing_price": "latest paired same-book snapshot strictly before kickoff",
                    "production_use": "closing price is evaluation-only and must never enter model/filter/threshold/ranking",
                },
                "sampling_rule": "latest qualifying recorded pick per event_id+market; repeated refresh snapshots are deduplicated",
                "picks_seen": len(picks),
                "picks_with_valid_clv": len(evaluated),
                "missing_fixture_mapping": missing_fixture,
                "no_valid_same_book_decision_to_close_pair": no_decision_close_pair,
                "historical_validation": {
                    "seasons": list(HISTORICAL_VALIDATION_SEASONS),
                    "eligible_for_research_gate": True,
                    **_scope_report(historical, "historical"),
                },
                "live_holdout_monitoring": {
                    "season": LIVE_HOLDOUT_SEASON,
                    "eligible_for_research_gate": False,
                    **_scope_report(live, "live-holdout"),
                    "rule": "descriptive monitoring only; never pooled with historical validation for tuning or activation",
                },
                "other_seasons_descriptive": {
                    "n": len(other),
                    "eligible_for_research_gate": False,
                    "by_season": _scope_report(other, "other").get("by_season", {}),
                },
                "promotion_rule": "Only historical_validation may support a challenger gate. CLV remains supporting evidence and cannot activate a feature or threshold by itself.",
            }
            conn.execute(
                "UPDATE clv_backtest_runs SET finished_at=NOW(),status='success',picks_seen=%s,picks_with_clv=%s,results=%s,message='ok' WHERE id=%s",
                (len(picks), len(evaluated), Jsonb(results), run_id),
            )
            print("CLV_BACKTEST_RESULT", json.dumps(results, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return results
        except Exception as exc:
            conn.execute(
                "UPDATE clv_backtest_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                (str(exc)[:2000], run_id),
            )
            raise


if __name__ == "__main__":
    print(json.dumps(run_backtest(), ensure_ascii=False, indent=2, default=str))
