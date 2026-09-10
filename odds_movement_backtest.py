#!/usr/bin/env python3
"""Research-only odds-movement backtest for production V1 selections.

Important leakage rule: movement is reconstructed from RAW OddsPapi snapshots whose
snapshot_hour is <= the model pick timestamp. The mutable odds_movement JSON stored
on prematch_feature_snapshots is deliberately NOT used, because a later enrichment
can contain prices that were not known when an earlier prediction was made.

Nothing in this module changes production selection or activation state.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from statistics import mean, median
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.types.json import Jsonb

from odds_movement_enricher import market_key, outcome_key

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
VERSION = "v1-odds-movement-leakage-safe-v2"
PRICE_MIN = float(os.getenv("MOVEMENT_PRICE_MIN", "1.01"))
PRICE_MAX = float(os.getenv("MOVEMENT_PRICE_MAX", "8.0"))
MAX_OVERROUND = float(os.getenv("MOVEMENT_MAX_OVERROUND", "1.18"))
MOVE_PP = float(os.getenv("MOVEMENT_SIGNAL_PP", "0.015"))
MIN_TOTAL = int(os.getenv("MOVEMENT_MIN_TOTAL_SAMPLE", "100"))
MIN_BUCKET = int(os.getenv("MOVEMENT_MIN_BUCKET_SAMPLE", "30"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS odds_movement_backtest_runs(
 id BIGSERIAL PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 version TEXT NOT NULL,
 status TEXT NOT NULL,
 sample_size INTEGER NOT NULL DEFAULT 0,
 results JSONB,
 message TEXT
);
"""


def is_production_v1_version(version: Any) -> bool:
    """True only for the production V1 family, never the old xG diagnostic v1."""
    text = str(version or "")
    return text.startswith("production-poisson-form-v1-") and "xg" not in text.lower()


def _wilson(hits: int, n: int, z: float = 1.96) -> Optional[List[float]]:
    if n <= 0:
        return None
    p = hits / n
    d = 1 + z*z/n
    c = (p + z*z/(2*n))/d
    h = z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/d
    return [round(max(0.0, c-h), 4), round(min(1.0, c+h), 4)]


def _summary(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0, "hits": 0, "hit_rate": None, "wilson95": None}
    hits = sum(bool(r["hit"]) for r in rows)
    return {
        "n": n,
        "hits": hits,
        "hit_rate": round(hits/n, 4),
        "wilson95": _wilson(hits, n),
        "avg_model_confidence": round(mean(float(r["confidence"]) for r in rows), 4),
        "avg_market_move_pp": round(mean(float(r["movement_delta"]) for r in rows)*100, 3),
        "avg_books": round(mean(int(r["books"]) for r in rows), 2),
    }


def _valid_price(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if PRICE_MIN <= x <= PRICE_MAX else None


def _selected_sides(market: str, selection_yes: bool) -> Tuple[str, str]:
    if market in {"over_2_5", "corners_over_8_5"}:
        return ("over", "under") if selection_yes else ("under", "over")
    return ("yes", "no") if selection_yes else ("no", "yes")


def _paired_selected_probability(a: float, b: float) -> Optional[float]:
    qa, qb = 1.0/a, 1.0/b
    z = qa + qb
    if not (1.0 <= z <= MAX_OVERROUND):
        return None
    return qa/z


def reconstruct_movement(
    rows: Sequence[Tuple[Any, ...]], market: str, selection_yes: bool
) -> Optional[Dict[str, Any]]:
    """Build selected-side no-vig movement from same-book paired snapshots."""
    selected, opposite = _selected_sides(market, selection_yes)
    grouped: Dict[Tuple[str, Any], Dict[str, float]] = defaultdict(dict)
    for snapshot, bookmaker, market_name, handicap, outcome_name, price in rows:
        mk = market_key(market_name, handicap)
        normalized = "over_2_5" if mk == "goals_2_5" else "corners_over_8_5" if mk == "corners_8_5" else mk
        if normalized != market:
            continue
        side = outcome_key(mk, outcome_name) if mk else None
        px = _valid_price(price)
        if not side or px is None:
            continue
        grouped[(str(bookmaker), snapshot)][side] = px

    by_book: Dict[str, List[Tuple[Any, float]]] = defaultdict(list)
    for (book, snapshot), sides in grouped.items():
        a, b = sides.get(selected), sides.get(opposite)
        if a is None or b is None:
            continue
        p = _paired_selected_probability(a, b)
        if p is not None:
            by_book[book].append((snapshot, p))

    deltas, first_probs, latest_probs = [], [], []
    for _book, series in by_book.items():
        series.sort(key=lambda x: x[0])
        if len(series) < 2 or series[-1][0] <= series[0][0]:
            continue
        first, latest = float(series[0][1]), float(series[-1][1])
        first_probs.append(first)
        latest_probs.append(latest)
        deltas.append(latest-first)
    if not deltas:
        return None
    d = float(median(deltas))
    bucket = "toward_pick" if d >= MOVE_PP else "against_pick" if d <= -MOVE_PP else "flat"
    return {
        "movement_delta": d,
        "bucket": bucket,
        "books": len(deltas),
        "first_consensus": float(median(first_probs)),
        "latest_consensus": float(median(latest_probs)),
    }


def _load_picks(conn) -> List[Dict[str, Any]]:
    """Earliest recorded true production-V1 pre-match pick for each event/market."""
    rows = conn.execute(
        """SELECT DISTINCT ON (p.event_id,p.market)
                  p.event_id,p.market,p.selection,p.selection_yes,p.model_probability,
                  p.snapshot_hour,p.match_date,p.league_name,p.home_team,p.away_team,
                  CASE p.market
                    WHEN 'over_2_5' THEN e.over_2_5
                    WHEN 'btts' THEN e.btts
                    WHEN 'corners_over_8_5' THEN e.corners_over_8_5
                  END AS outcome,
                  fs.oddspapi_fixture_id
           FROM production_predictions p
           JOIN production_prediction_runs r ON r.id=p.run_id
           JOIN espn_current_matches e ON e.event_id=p.event_id
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
             AND r.model_version LIKE 'production-poisson-form-v1-%'
             AND LOWER(r.model_version) NOT LIKE '%xg%'
             AND CASE p.market
                   WHEN 'over_2_5' THEN e.over_2_5
                   WHEN 'btts' THEN e.btts
                   WHEN 'corners_over_8_5' THEN e.corners_over_8_5
                 END IS NOT NULL
           ORDER BY p.event_id,p.market,p.snapshot_hour ASC,p.run_id ASC"""
    ).fetchall()
    keys = ["event_id","market","selection","selection_yes","confidence","snapshot_hour","match_date","league","home","away","outcome","fixture_id"]
    return [dict(zip(keys, row)) for row in rows]


def run_backtest(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        run_id = conn.execute(
            "INSERT INTO odds_movement_backtest_runs(version,status) VALUES(%s,'running') RETURNING id",
            (VERSION,),
        ).fetchone()[0]
        try:
            picks = _load_picks(conn)
            evaluated: List[Dict[str, Any]] = []
            missing_fixture = no_valid_movement = 0
            for pick in picks:
                if not pick.get("fixture_id"):
                    missing_fixture += 1
                    continue
                prices = conn.execute(
                    """SELECT snapshot_hour,bookmaker,market_name,handicap,outcome_name,price
                       FROM oddspapi_market_prices
                       WHERE fixture_id=%s AND snapshot_hour<=%s
                         AND COALESCE(active,TRUE)=TRUE
                       ORDER BY snapshot_hour,bookmaker,market_id,outcome_id""",
                    (str(pick["fixture_id"]), pick["snapshot_hour"]),
                ).fetchall()
                movement = reconstruct_movement(prices, str(pick["market"]), bool(pick["selection_yes"]))
                if not movement:
                    no_valid_movement += 1
                    continue
                hit = bool(pick["outcome"]) == bool(pick["selection_yes"])
                evaluated.append({**pick, **movement, "hit": hit})

            def subset(min_conf: float) -> Dict[str, Any]:
                rows = [r for r in evaluated if float(r["confidence"]) >= min_conf]
                buckets = {b: _summary([r for r in rows if r["bucket"] == b]) for b in ("toward_pick","flat","against_pick")}
                by_market = {
                    m: {
                        "overall": _summary([r for r in rows if r["market"] == m]),
                        "toward_pick": _summary([r for r in rows if r["market"] == m and r["bucket"] == "toward_pick"]),
                        "flat": _summary([r for r in rows if r["market"] == m and r["bucket"] == "flat"]),
                        "against_pick": _summary([r for r in rows if r["market"] == m and r["bucket"] == "against_pick"]),
                    }
                    for m in ("over_2_5","btts","corners_over_8_5")
                }
                toward = buckets["toward_pick"]
                other_rows = [r for r in rows if r["bucket"] != "toward_pick"]
                other = _summary(other_rows)
                lift = None
                if toward.get("hit_rate") is not None and other.get("hit_rate") is not None:
                    lift = round(float(toward["hit_rate"])-float(other["hit_rate"]), 4)
                adequate = bool(
                    len(rows) >= MIN_TOTAL
                    and int(toward.get("n") or 0) >= MIN_BUCKET
                    and int(other.get("n") or 0) >= MIN_BUCKET
                )
                return {
                    "overall": _summary(rows),
                    "buckets": buckets,
                    "by_market": by_market,
                    "toward_vs_other_hit_rate_lift": lift,
                    "sample_adequate_for_interpretation": adequate,
                }

            results = {
                "version": VERSION,
                "purpose": "research_only_no_production_change",
                "production_model_filter": "production-poisson-form-v1-* excluding any xg version",
                "leakage_guard": "raw OddsPapi snapshots only; snapshot_hour <= earliest recorded V1 pick timestamp",
                "movement_definition": f"median same-book paired no-vig probability change; toward >= +{MOVE_PP*100:.1f}pp, against <= -{MOVE_PP*100:.1f}pp",
                "raw_picks": len(picks),
                "evaluated_with_valid_predecision_movement": len(evaluated),
                "missing_oddspapi_fixture_mapping": missing_fixture,
                "no_valid_multi_snapshot_paired_movement": no_valid_movement,
                "all": subset(0.0),
                "confidence_0.65": subset(0.65),
                "confidence_0.70": subset(0.70),
            }
            high = results["confidence_0.65"]
            if not high["sample_adequate_for_interpretation"]:
                verdict = "insufficient_sample"
            elif (high.get("toward_vs_other_hit_rate_lift") or 0) > 0:
                verdict = "candidate_for_future_forward_validation"
            else:
                verdict = "no_positive_signal_detected"
            results["verdict"] = verdict
            results["promotion_rule"] = "Never activate from this diagnostic alone. Require a predeclared future/OOS confirmation with adequate samples by market and no calibration regression."
            conn.execute(
                "UPDATE odds_movement_backtest_runs SET finished_at=NOW(),status='success',sample_size=%s,results=%s,message=%s WHERE id=%s",
                (len(evaluated), Jsonb(results), verdict, run_id),
            )
            print("ODDS_MOVEMENT_BACKTEST_RESULT", json.dumps(results, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return results
        except Exception as exc:
            conn.execute(
                "UPDATE odds_movement_backtest_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                (str(exc)[:2000], run_id),
            )
            raise


if __name__ == "__main__":
    print(json.dumps(run_backtest(), ensure_ascii=False, indent=2, default=str))
