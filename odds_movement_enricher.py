#!/usr/bin/env python3
"""Enrich pre-match snapshots with actual first-to-latest market price movement.

The existing context builder records snapshot timing. This module adds outcome-level
price deltas for OU2.5, BTTS and total-corners 8.5 without using the movement as a
prediction signal until it has been backtested.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


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
        if re.search(r"\byes\b", s):
            return "yes"
        if re.search(r"\bno\b", s):
            return "no"
    return None


def snapshot_prices(conn, fixture_id: str, snapshot) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = defaultdict(dict)
    rows = conn.execute(
        """
        SELECT market_name,handicap,outcome_name,price
        FROM oddspapi_market_prices
        WHERE fixture_id=%s AND snapshot_hour=%s
          AND price IS NOT NULL AND price > 1.001 AND COALESCE(active,TRUE)=TRUE
        """,
        (fixture_id, snapshot),
    ).fetchall()
    for name,line,outcome,price in rows:
        market = market_key(name,line)
        if not market:
            continue
        side = outcome_key(market,outcome)
        if not side:
            continue
        value = float(price)
        # One configured bookmaker is expected, but retaining the best available
        # price is deterministic if duplicate rows appear.
        if side not in out[market] or value > out[market][side]:
            out[market][side] = value
    return dict(out)


def run_enrich(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    updated = with_movement = 0
    with psycopg.connect(db, autocommit=True) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON(event_id) event_id,snapshot_hour,oddspapi_fixture_id
            FROM prematch_feature_snapshots
            WHERE oddspapi_fixture_id IS NOT NULL
            ORDER BY event_id,snapshot_hour DESC
            """
        ).fetchall()
        for event_id,hour,fixture_id in rows:
            first,last = conn.execute(
                "SELECT MIN(snapshot_hour),MAX(snapshot_hour) FROM oddspapi_market_prices WHERE fixture_id=%s",
                (fixture_id,),
            ).fetchone()
            movement: Dict[str, Any] = {
                "first_snapshot": first.isoformat() if first else None,
                "latest_snapshot": last.isoformat() if last else None,
                "hours": round((last-first).total_seconds()/3600.0,2) if first and last else 0.0,
                "markets": {},
            }
            if first and last:
                fp = snapshot_prices(conn,str(fixture_id),first)
                lp = snapshot_prices(conn,str(fixture_id),last)
                for market in sorted(set(fp) | set(lp)):
                    sides: Dict[str,Any] = {}
                    for side in sorted(set(fp.get(market,{})) | set(lp.get(market,{}))):
                        a = fp.get(market,{}).get(side)
                        b = lp.get(market,{}).get(side)
                        sides[side] = {
                            "first_price": a,
                            "latest_price": b,
                            "absolute_delta": round(b-a,4) if a is not None and b is not None else None,
                            "percent_delta": round((b/a-1.0)*100.0,3) if a not in (None,0) and b is not None else None,
                        }
                    movement["markets"][market] = sides
                if first != last and movement["markets"]:
                    with_movement += 1
            conn.execute(
                "UPDATE prematch_feature_snapshots SET odds_movement=%s,built_at=NOW() WHERE event_id=%s AND snapshot_hour=%s",
                (Jsonb(movement),event_id,hour),
            )
            updated += 1
    result = {"status":"success","updated":updated,"fixtures_with_multi_snapshot_movement":with_movement}
    print("ODDS_MOVEMENT_RESULT",json.dumps(result,separators=(",",":")))
    return result


if __name__ == "__main__":
    print(json.dumps(run_enrich(),ensure_ascii=False,indent=2))
