#!/usr/bin/env python3
"""Official Turkey price storage used by the Thursday decision engine.

This module has one job: validate/store Turkey prices, freeze the first seen opening
price, and return the freshest executable price. It does not build betting lists.
The only production list builder is thursday_decision_engine.build_decision().
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

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


def store_price(
    conn,
    event_id: str,
    market: str,
    selection: str,
    source: str,
    price: float,
    at: Optional[datetime] = None,
) -> bool:
    """Store one valid Turkey price and freeze the first valid price as opening."""
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
    """Return the freshest valid executable Turkey price plus immutable opening."""
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
