#!/usr/bin/env python3
"""Idempotent one-shot runner for the sequential ranking challenger audit."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

from ranking_challenger_stack_audit import run_audit

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RUN_KEY = os.getenv("RANKING_STACK_AUDIT_ONCE_KEY", "ranking-stack-2026-09-11-v1").strip()

SCHEMA = """
CREATE TABLE IF NOT EXISTS ranking_challenger_stack_audit_once(
 run_key TEXT PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL,
 result JSONB,
 message TEXT
);
"""


def run(database_url: Optional[str] = None, run_key: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    key = (run_key or RUN_KEY).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    if not key:
        raise RuntimeError("Missing RANKING_STACK_AUDIT_ONCE_KEY")

    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        row = conn.execute(
            "SELECT status,result FROM ranking_challenger_stack_audit_once WHERE run_key=%s", (key,)
        ).fetchone()
        if row and row[0] == "success":
            result = dict(row[1]) if isinstance(row[1], dict) else {}
            out = {"status": "already_done", "run_key": key, "result": result}
            print("RANKING_STACK_ONCE_RESULT", json.dumps(out, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return out
        conn.execute(
            """INSERT INTO ranking_challenger_stack_audit_once(run_key,status,message)
               VALUES(%s,'running','started')
               ON CONFLICT(run_key) DO UPDATE SET started_at=NOW(),finished_at=NULL,
                 status='running',result=NULL,message='retry'""",
            (key,),
        )

    try:
        result = run_audit(db)
        with psycopg.connect(db, autocommit=True) as conn:
            conn.execute(
                "UPDATE ranking_challenger_stack_audit_once SET finished_at=NOW(),status='success',result=%s,message='ok' WHERE run_key=%s",
                (Jsonb(result), key),
            )
        out = {"status": "success", "run_key": key, "result": result}
        print("RANKING_STACK_ONCE_RESULT", json.dumps(out, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
        return out
    except Exception as exc:
        with psycopg.connect(db, autocommit=True) as conn:
            conn.execute(
                "UPDATE ranking_challenger_stack_audit_once SET finished_at=NOW(),status='failed',message=%s WHERE run_key=%s",
                (str(exc)[:2000], key),
            )
        raise


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
