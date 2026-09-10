#!/usr/bin/env python3
"""Idempotent one-shot runner for Lineup Stability V2 plus current-week preview."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

from lineup_stability_v2_audit import run_audit

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RUN_KEY = os.getenv("LINEUP_V2_AUDIT_ONCE_KEY", "lineup-v2-2026-09-10-v1").strip()

SCHEMA = """
CREATE TABLE IF NOT EXISTS lineup_stability_v2_audit_once(
 run_key TEXT PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL,
 result JSONB,
 message TEXT
);
"""


def _preview(db: str) -> Dict[str, Any]:
    # The weekly builder deliberately tolerates optional/missing context tables.
    # Run its read path in autocommit mode so a caught optional-query failure cannot
    # poison the entire connection with InFailedSqlTransaction.
    import weekly_trusted_predictions as weekly

    original_connect = weekly.psycopg.connect

    def _autocommit_connect(*args, **kwargs):
        kwargs.setdefault("autocommit", True)
        return original_connect(*args, **kwargs)

    weekly.psycopg.connect = _autocommit_connect
    try:
        result = weekly.build(db)
    finally:
        weekly.psycopg.connect = original_connect

    picks = result.get("picks") or []
    return {
        "week_key": result.get("week_key"),
        "ready": result.get("ready"),
        "ranked_pick_count": result.get("ranked_pick_count"),
        "policy": result.get("policy"),
        "picks": [
            {
                "home": p.get("home"),
                "away": p.get("away"),
                "selection": p.get("selection"),
                "confidence": p.get("confidence"),
                "ranking_score": p.get("ranking_score"),
                "tr_price": p.get("tr_price"),
                "market_check": p.get("market_check"),
                "lineup_v2_active": p.get("lineup_v2_active"),
                "lineup_v2_available": p.get("lineup_v2_available"),
                "lineup_v2_factor": p.get("lineup_v2_factor"),
            }
            for p in picks
        ],
    }


def run(database_url: Optional[str] = None, run_key: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    key = (run_key or RUN_KEY).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    if not key:
        raise RuntimeError("Missing LINEUP_V2_AUDIT_ONCE_KEY")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        row = conn.execute(
            "SELECT status,result FROM lineup_stability_v2_audit_once WHERE run_key=%s", (key,)
        ).fetchone()
        if row and row[0] == "success":
            out = {"status": "already_done", "run_key": key, "result": dict(row[1]) if isinstance(row[1], dict) else {}}
            print("LINEUP_V2_ONCE_RESULT", json.dumps(out, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return out
        conn.execute(
            """INSERT INTO lineup_stability_v2_audit_once(run_key,status,message)
               VALUES(%s,'running','started')
               ON CONFLICT(run_key) DO UPDATE SET started_at=NOW(),finished_at=NULL,status='running',result=NULL,message='retry'""",
            (key,),
        )
    try:
        audit = run_audit(db)
        preview = _preview(db)
        result = {
            "gate_passed": audit.get("gate_passed"),
            "recommended_activation": audit.get("recommended_activation"),
            "baseline": audit.get("baseline"),
            "challenger": audit.get("challenger"),
            "current_week_preview": preview,
        }
        with psycopg.connect(db, autocommit=True) as conn:
            conn.execute(
                "UPDATE lineup_stability_v2_audit_once SET finished_at=NOW(),status='success',result=%s,message='ok' WHERE run_key=%s",
                (Jsonb(result), key),
            )
        compact = {"status": "success", "run_key": key, **result}
        print("LINEUP_V2_ONCE_RESULT", json.dumps(compact, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
        return compact
    except Exception as exc:
        with psycopg.connect(db, autocommit=True) as conn:
            conn.execute(
                "UPDATE lineup_stability_v2_audit_once SET finished_at=NOW(),status='failed',message=%s WHERE run_key=%s",
                (str(exc)[:2000], key),
            )
        print("LINEUP_V2_ONCE_FAILED", json.dumps({"run_key": key, "error": str(exc)}, ensure_ascii=False, separators=(",", ":")), flush=True)
        raise


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
