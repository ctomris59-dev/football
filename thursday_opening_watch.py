#!/usr/bin/env python3
"""Lightweight Thursday/Friday opening-price watcher.

The expensive football model is prepared separately on Thursday. This watcher only:
1) checks official Turkish İddaa prices for Fri-Sun Big-Five fixtures;
2) rebuilds the two actionable lists;
3) freezes the first sufficiently complete decision for the week.

Once frozen, later odds or T-1/T-3 information cannot rewrite the user's betting list.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

from thursday_decision_engine import build_decision, json_default, weekend_bounds
from turkey_iddaa_odds_collector import run_import

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

DDL = """
CREATE TABLE IF NOT EXISTS thursday_final_decisions(
 week_key DATE PRIMARY KEY,
 finalized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 decision_run_id BIGINT NOT NULL,
 payload JSONB NOT NULL,
 source TEXT NOT NULL DEFAULT 'iddaa_official'
);
"""


def latest_final(database_url: str = DATABASE_URL, week_key: Optional[date] = None) -> Optional[Dict[str, Any]]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    week_key = week_key or weekend_bounds()[0]
    with psycopg.connect(database_url) as conn:
        conn.execute(DDL)
        row = conn.execute(
            "SELECT finalized_at,decision_run_id,payload,source FROM thursday_final_decisions WHERE week_key=%s",
            (week_key,),
        ).fetchone()
    if not row:
        return None
    return {
        "week_key": week_key, "finalized_at": row[0], "decision_run_id": int(row[1]),
        "payload": row[2], "source": row[3],
    }


def main(database_url: str = DATABASE_URL) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    week_key, _, _ = weekend_bounds()

    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(DDL)
        existing = conn.execute(
            "SELECT finalized_at,decision_run_id,payload FROM thursday_final_decisions WHERE week_key=%s",
            (week_key,),
        ).fetchone()
        if existing:
            result = {
                "status": "already_finalized", "week_key": week_key,
                "finalized_at": existing[0], "decision_run_id": int(existing[1]), "payload": existing[2],
            }
            print("THURSDAY_FINAL_DECISION", json.dumps(result, ensure_ascii=False, default=json_default, separators=(",", ":")), flush=True)
            return result

    odds = run_import(database_url)
    decision = build_decision(database_url)
    result: Dict[str, Any] = {
        "status": "pending_bulletin", "week_key": week_key, "odds": odds,
        "decision_run_id": decision.get("decision_run_id"),
        "official_fixture_coverage": decision.get("official_fixture_coverage"),
        "raw_high_candidates": decision.get("raw_high_candidates"),
        "priced_high_candidates": decision.get("priced_high_candidates"),
        "high_confidence": decision.get("high_confidence") or [],
        "high_confidence_value": decision.get("high_confidence_value") or [],
    }
    if not decision.get("decision_ready"):
        print("THURSDAY_OPENING_PENDING", json.dumps(result, ensure_ascii=False, default=json_default, separators=(",", ":")), flush=True)
        return result

    payload = {
        "week_key": decision["week_key"], "finalized_at": datetime.now(timezone.utc),
        "decision_run_id": decision["decision_run_id"],
        "official_fixture_coverage": decision["official_fixture_coverage"],
        "high_confidence": decision["high_confidence"],
        "high_confidence_value": decision["high_confidence_value"],
        "policy": decision["diagnostics"]["policy"],
    }
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(DDL)
        conn.execute(
            """INSERT INTO thursday_final_decisions(week_key,decision_run_id,payload)
               VALUES(%s,%s,%s) ON CONFLICT(week_key) DO NOTHING""",
            (week_key, int(decision["decision_run_id"]), Jsonb(payload, dumps=lambda x: json.dumps(x, default=json_default, ensure_ascii=False))),
        )
        stored = conn.execute(
            "SELECT finalized_at,decision_run_id,payload FROM thursday_final_decisions WHERE week_key=%s",
            (week_key,),
        ).fetchone()

    result = {
        "status": "finalized", "week_key": week_key, "finalized_at": stored[0],
        "decision_run_id": int(stored[1]), "payload": stored[2],
    }
    print("THURSDAY_FINAL_DECISION", json.dumps(result, ensure_ascii=False, default=json_default, separators=(",", ":")), flush=True)
    return result


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2, default=json_default))
