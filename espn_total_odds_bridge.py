#!/usr/bin/env python3
"""Normalize free ESPN total-goals prices into an event-level fallback market.

The existing ESPN context collector archives raw odds payloads but only materializes
the total line and moneyline. This bridge conservatively extracts explicit overOdds
and underOdds fields from those archived payloads, converts American/decimal prices
to decimal, removes vig, and publishes a 2.5-goal probability only when the upstream
line is exactly 2.5. No price is inferred from a line alone.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LOOKBACK_HOURS = float(os.getenv("ESPN_TOTAL_ODDS_LOOKBACK_HOURS", "72"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS espn_event_total_market_snapshots(
    event_id TEXT NOT NULL,
    snapshot_hour TIMESTAMPTZ NOT NULL,
    provider TEXT,
    goal_line DOUBLE PRECISION,
    over_price DOUBLE PRECISION,
    under_price DOUBLE PRECISION,
    goal_p_over DOUBLE PRECISION,
    source_keys JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(event_id,snapshot_hour)
);
CREATE INDEX IF NOT EXISTS idx_espn_event_total_latest ON espn_event_total_market_snapshots(event_id,snapshot_hour DESC);
CREATE TABLE IF NOT EXISTS espn_total_odds_bridge_runs(
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    line_25 INTEGER NOT NULL DEFAULT 0,
    paired_prices INTEGER NOT NULL DEFAULT 0,
    rows_written INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""


def flatten(obj: Any, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                out.update(flatten(v, p))
            else:
                out[p] = v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{prefix}[{i}]"
            if isinstance(v, (dict, list)):
                out.update(flatten(v, p))
            else:
                out[p] = v
    return out


def fnum(v: Any) -> Optional[float]:
    try:
        if v in (None, ""):
            return None
        return float(str(v).replace(",", "").replace("+", "").strip())
    except Exception:
        return None


def decimal_price(v: Any) -> Optional[float]:
    x = fnum(v)
    if x is None:
        return None
    # Decimal odds are normally >1. American odds are typically <=-100 or >=100.
    if 1.001 < x < 100:
        return x
    if x >= 100:
        return 1.0 + x / 100.0
    if x <= -100:
        return 1.0 + 100.0 / abs(x)
    return None


def first_suffix(flat: Dict[str, Any], suffixes: Iterable[str]) -> Tuple[Optional[str], Optional[Any]]:
    ss = tuple(s.lower() for s in suffixes)
    # Prefer resolved_items over collection refs when both exist.
    items = sorted(flat.items(), key=lambda kv: (0 if "resolved_items" in kv[0] else 1, len(kv[0])))
    for k, v in items:
        kl = k.lower().replace("_", "")
        if any(kl.endswith(s.replace("_", "").lower()) for s in ss):
            return k, v
    return None, None


def no_vig(over: float, under: float) -> Optional[float]:
    if over <= 1.001 or under <= 1.001:
        return None
    a, b = 1.0 / over, 1.0 / under
    return a / (a + b) if a + b else None


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db, autocommit=True) as c:
        c.execute(SCHEMA)
        rid = c.execute("INSERT INTO espn_total_odds_bridge_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        rows_seen = line25 = paired = written = 0
        try:
            rows = c.execute(
                """SELECT DISTINCT ON(event_id) event_id,snapshot_hour,provider,over_under_line,normalized,raw
                   FROM espn_odds_snapshots
                   WHERE snapshot_hour>=NOW()-(%s||' hours')::interval
                   ORDER BY event_id,snapshot_hour DESC""", (LOOKBACK_HOURS,)
            ).fetchall()
            for event_id, hour, provider, line, normalized, raw in rows:
                rows_seen += 1
                line = fnum(line)
                if line is None or abs(line - 2.5) > 1e-9:
                    continue
                line25 += 1
                payload = raw if isinstance(raw, (dict, list)) else {}
                flat = flatten(payload)
                ok, ov = first_suffix(flat, ("overOdds", "over_odds"))
                uk, un = first_suffix(flat, ("underOdds", "under_odds"))
                over, under = decimal_price(ov), decimal_price(un)
                if over is None or under is None:
                    # normalized may gain these keys in a future collector version.
                    nflat = flatten(normalized if isinstance(normalized, dict) else {})
                    ok2, ov2 = first_suffix(nflat, ("overOdds", "over_odds"))
                    uk2, un2 = first_suffix(nflat, ("underOdds", "under_odds"))
                    over = over or decimal_price(ov2); under = under or decimal_price(un2)
                    ok = ok or ok2; uk = uk or uk2
                p = no_vig(over, under) if over is not None and under is not None else None
                if p is None:
                    continue
                paired += 1
                keys = {"over": ok, "under": uk, "conversion": "american-or-decimal-to-decimal", "line_source": "espn_odds_snapshots.over_under_line"}
                c.execute(
                    """INSERT INTO espn_event_total_market_snapshots(event_id,snapshot_hour,provider,goal_line,over_price,under_price,goal_p_over,source_keys,raw)
                       VALUES(%s,%s,%s,2.5,%s,%s,%s,%s,%s)
                       ON CONFLICT(event_id,snapshot_hour) DO UPDATE SET provider=EXCLUDED.provider,goal_line=EXCLUDED.goal_line,
                       over_price=EXCLUDED.over_price,under_price=EXCLUDED.under_price,goal_p_over=EXCLUDED.goal_p_over,
                       source_keys=EXCLUDED.source_keys,raw=EXCLUDED.raw,fetched_at=NOW()""",
                    (str(event_id), hour, str(provider) if provider else None, over, under, p, Jsonb(keys), Jsonb(payload if isinstance(payload, dict) else {"payload": payload})),
                )
                written += 1
            status = "success"
            result = {"status": status, "rows_seen": rows_seen, "line_25": line25, "paired_prices": paired, "rows_written": written}
            c.execute("UPDATE espn_total_odds_bridge_runs SET finished_at=NOW(),status=%s,rows_seen=%s,line_25=%s,paired_prices=%s,rows_written=%s,message=%s WHERE id=%s",
                      (status, rows_seen, line25, paired, written, json.dumps(result, separators=(",", ":"))[:1000], rid))
            print("ESPN_TOTAL_ODDS_BRIDGE_RESULT", json.dumps(result, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            c.execute("UPDATE espn_total_odds_bridge_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s", (str(exc)[:1000], rid))
            raise


if __name__ == "__main__":
    print(json.dumps(run_import(), indent=2))
