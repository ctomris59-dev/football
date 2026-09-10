#!/usr/bin/env python3
"""Idempotent production-DB runner for the advanced goal-model audit."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

from advanced_goal_audit import run_audit

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RUN_KEY = os.getenv("ADVANCED_GOAL_AUDIT_ONCE_KEY", "advanced-goal-2026-09-10-v1").strip()

SCHEMA = """
CREATE TABLE IF NOT EXISTS advanced_goal_audit_once(
 run_key TEXT PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL,
 result JSONB,
 message TEXT
);
"""


def _safe(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str, ensure_ascii=False))


def _preview(db: str) -> Dict[str, Any]:
    from weekly_trusted_predictions import build
    result = build(db)
    return _safe({
        "week_key": result.get("week_key"),
        "ready": result.get("ready"),
        "policy": result.get("policy"),
        "picks": [
            {
                "home": p.get("home"), "away": p.get("away"), "market": p.get("market"),
                "selection": p.get("selection"), "confidence": p.get("confidence"),
                "ranking_score": p.get("ranking_score"), "tr_price": p.get("tr_price"),
                "market_check": p.get("market_check"), "goal_model_mode": p.get("goal_model_mode"),
                "schedule_rank_factor": p.get("schedule_rank_factor"),
            }
            for p in (result.get("picks") or [])
        ],
    })


def run(database_url: Optional[str] = None, run_key: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip(); key = (run_key or RUN_KEY).strip()
    if not db: raise RuntimeError("Missing DATABASE_URL")
    if not key: raise RuntimeError("Missing ADVANCED_GOAL_AUDIT_ONCE_KEY")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        row = conn.execute("SELECT status,result FROM advanced_goal_audit_once WHERE run_key=%s", (key,)).fetchone()
        if row and row[0] == "success":
            out = {"status": "already_done", "run_key": key, "result": row[1] if isinstance(row[1], dict) else {}}
            print("ADVANCED_GOAL_ONCE_RESULT", json.dumps(out, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return out
        conn.execute(
            """INSERT INTO advanced_goal_audit_once(run_key,status,message) VALUES(%s,'running','started')
               ON CONFLICT(run_key) DO UPDATE SET started_at=NOW(),finished_at=NULL,status='running',result=NULL,message='retry'""", (key,))
    try:
        audit = run_audit(db)
        preview = _preview(db)
        result = _safe({
            "gate_passed": audit.get("gate_passed"),
            "recommended_activation": audit.get("recommended_activation"),
            "baseline": audit.get("baseline"),
            "challengers": audit.get("challengers"),
            "dc_rho_summary": audit.get("dc_rho_summary"),
            "current_week_preview": preview,
        })
        with psycopg.connect(db, autocommit=True) as conn:
            conn.execute("UPDATE advanced_goal_audit_once SET finished_at=NOW(),status='success',result=%s,message='ok' WHERE run_key=%s", (Jsonb(result), key))
        out = {"status": "success", "run_key": key, **result}
        print("ADVANCED_GOAL_ONCE_RESULT", json.dumps(out, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
        return out
    except Exception as exc:
        with psycopg.connect(db, autocommit=True) as conn:
            conn.execute("UPDATE advanced_goal_audit_once SET finished_at=NOW(),status='failed',message=%s WHERE run_key=%s", (str(exc)[:2000], key))
        print("ADVANCED_GOAL_ONCE_FAILED", json.dumps({"run_key": key, "error": str(exc)}, ensure_ascii=False, separators=(",", ":")), flush=True)
        raise


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
