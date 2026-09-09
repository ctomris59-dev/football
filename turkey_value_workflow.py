#!/usr/bin/env python3
"""Turkey-first dual-list workflow.

HIGH_CONFIDENCE is driven only by calibrated model probability.
HIGH_CONFIDENCE_VALUE additionally requires a recent, validated Turkish price.
Opening prices are immutable; current prices are freshness-gated and used for EV.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
HIGH_CONFIDENCE_MIN = float(os.getenv("HIGH_CONFIDENCE_MIN", "0.70"))
VALUE_MIN_CONFIDENCE = float(os.getenv("VALUE_MIN_CONFIDENCE", "0.65"))
VALUE_MIN_EDGE = float(os.getenv("VALUE_MIN_EDGE", "0.015"))
VALUE_MIN_EV = float(os.getenv("VALUE_MIN_EV", "0.02"))
TR_PRICE_MIN = float(os.getenv("TR_PRICE_MIN", "1.01"))
TR_PRICE_MAX = float(os.getenv("TR_PRICE_MAX", "5.00"))
TR_PRICE_MAX_AGE_HOURS = float(os.getenv("TR_PRICE_MAX_AGE_HOURS", "6"))

DDL = """
CREATE TABLE IF NOT EXISTS turkey_odds_snapshots(
 id BIGSERIAL PRIMARY KEY,
 event_id TEXT NOT NULL,
 market TEXT NOT NULL,
 selection TEXT NOT NULL,
 source TEXT NOT NULL,
 price NUMERIC NOT NULL,
 fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 UNIQUE(event_id,market,selection,source,fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_turkey_odds_latest
 ON turkey_odds_snapshots(event_id,market,selection,fetched_at DESC);
CREATE TABLE IF NOT EXISTS turkey_opening_odds(
 event_id TEXT NOT NULL,
 market TEXT NOT NULL,
 selection TEXT NOT NULL,
 source TEXT NOT NULL,
 opening_price NUMERIC NOT NULL,
 first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(event_id,market,selection,source)
);
"""


def valid_price(price: Any) -> bool:
    try:
        return TR_PRICE_MIN <= float(price) <= TR_PRICE_MAX
    except (TypeError, ValueError):
        return False


def store_price(conn, event_id: str, market: str, selection: str, source: str, price: float,
                at: Optional[datetime] = None) -> bool:
    """Store a TR snapshot and freeze the first valid price as opening_price."""
    if not valid_price(price):
        return False
    conn.execute(DDL)
    at = at or datetime.now(timezone.utc)
    source = source.strip().lower()
    conn.execute(
        """INSERT INTO turkey_odds_snapshots(event_id,market,selection,source,price,fetched_at)
           VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (str(event_id), market, selection, source, float(price), at),
    )
    conn.execute(
        """INSERT INTO turkey_opening_odds(event_id,market,selection,source,opening_price,first_seen_at)
           VALUES(%s,%s,%s,%s,%s,%s)
           ON CONFLICT(event_id,market,selection,source) DO NOTHING""",
        (str(event_id), market, selection, source, float(price), at),
    )
    return True


def latest_tr_price(conn, event_id: str, market: str, selection: str):
    return conn.execute(
        """SELECT s.source,s.price,s.fetched_at,o.opening_price,o.first_seen_at
           FROM turkey_odds_snapshots s
           LEFT JOIN turkey_opening_odds o
             ON o.event_id=s.event_id AND o.market=s.market AND o.selection=s.selection AND o.source=s.source
           WHERE s.event_id=%s AND s.market=%s AND s.selection=%s
             AND s.price BETWEEN %s AND %s
             AND s.fetched_at>=NOW()-(%s||' hours')::interval
           ORDER BY s.fetched_at DESC LIMIT 1""",
        (str(event_id), market, selection, TR_PRICE_MIN, TR_PRICE_MAX, TR_PRICE_MAX_AGE_HOURS),
    ).fetchone()


def build_lists(db: str = DATABASE_URL, run_id: Optional[int] = None, limit: int = 10) -> Dict[str, Any]:
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(DDL)
        if run_id is None:
            row = conn.execute("SELECT MAX(run_id) FROM production_predictions").fetchone()
            run_id = int(row[0]) if row and row[0] else None
        if not run_id:
            return {"run_id": None, "high_confidence": [], "high_confidence_value": []}

        rows = conn.execute(
            """SELECT event_id,match_date,league_name,home_team,away_team,market,selection,
                      model_probability,ranking_score,final_context_ready
               FROM production_predictions
               WHERE run_id=%s AND provisional_ready=TRUE
               ORDER BY model_probability DESC,ranking_score DESC""",
            (run_id,),
        ).fetchall()

        high: list[Dict[str, Any]] = []
        value: list[Dict[str, Any]] = []
        seen_high, seen_value = set(), set()
        fresh_price_rows = 0

        for eid, dt, league, home, away, market, selection, probability, ranking, final in rows:
            eid, market, selection = str(eid), str(market), str(selection)
            p = float(probability)
            base = {
                "event_id": eid,
                "match_date": dt,
                "league": league,
                "home": home,
                "away": away,
                "market": market,
                "selection": selection,
                "confidence": p,
                "ranking": float(ranking),
                "final": bool(final),
            }
            if p >= HIGH_CONFIDENCE_MIN and eid not in seen_high:
                high.append(dict(base))
                seen_high.add(eid)

            tr = latest_tr_price(conn, eid, market, selection)
            if not tr:
                continue
            fresh_price_rows += 1
            source, current_price, fetched_at, opening_price, first_seen_at = tr
            current_price = float(current_price)
            opening_price = float(opening_price) if opening_price is not None else None
            market_p = 1.0 / current_price
            edge = p - market_p
            ev = p * current_price - 1.0

            if p >= VALUE_MIN_CONFIDENCE and eid not in seen_value and edge >= VALUE_MIN_EDGE and ev >= VALUE_MIN_EV:
                item = dict(base)
                item.update({
                    "tr_source": source,
                    "tr_price": current_price,
                    "tr_price_at": fetched_at,
                    "tr_opening_price": opening_price,
                    "tr_opening_at": first_seen_at,
                    "tr_price_change_from_open": (current_price - opening_price) if opening_price is not None else None,
                    "market_implied_probability": market_p,
                    "edge": edge,
                    "ev": ev,
                })
                value.append(item)
                seen_value.add(eid)

        high = sorted(high, key=lambda x: (x["confidence"], x["ranking"]), reverse=True)[:limit]
        value = sorted(value, key=lambda x: (x["confidence"], x["ev"], x["edge"], x["ranking"]), reverse=True)[:limit]
        return {
            "run_id": run_id,
            "generated_at": datetime.now(timezone.utc),
            "policy": {
                "high_confidence_min": HIGH_CONFIDENCE_MIN,
                "value_min_confidence": VALUE_MIN_CONFIDENCE,
                "value_min_edge": VALUE_MIN_EDGE,
                "value_min_ev": VALUE_MIN_EV,
                "turkey_price_required": True,
                "turkey_price_max_age_hours": TR_PRICE_MAX_AGE_HOURS,
                "turkey_price_bounds": [TR_PRICE_MIN, TR_PRICE_MAX],
            },
            "fresh_turkey_price_rows": fresh_price_rows,
            "high_confidence": high,
            "high_confidence_value": value,
        }


if __name__ == "__main__":
    print(json.dumps(build_lists(), ensure_ascii=False, indent=2, default=str))
