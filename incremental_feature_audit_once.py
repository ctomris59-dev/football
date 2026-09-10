#!/usr/bin/env python3
"""Idempotent startup runner for the incremental feature audit."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

from incremental_feature_audit import run_audit

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RUN_KEY = os.getenv("INCREMENTAL_FEATURE_AUDIT_ONCE_KEY", "incremental-features-2026-09-10-v1").strip()

SCHEMA = """
CREATE TABLE IF NOT EXISTS incremental_feature_audit_once(
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
        raise RuntimeError("Missing incremental feature audit run key")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        row = conn.execute(
            "SELECT status,result,message FROM incremental_feature_audit_once WHERE run_key=%s",
            (key,),
        ).fetchone()
        if row and row[0] == "success":
            result = dict(row[1]) if isinstance(row[1], dict) else {}
            out = {"status": "already_done", "run_key": key, "result": result}
            print("INCREMENTAL_FEATURE_ONCE_RESULT", json.dumps(out, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return out
        conn.execute(
            """INSERT INTO incremental_feature_audit_once(run_key,status,message)
               VALUES(%s,'running','started')
               ON CONFLICT(run_key) DO UPDATE SET
                 started_at=NOW(),finished_at=NULL,status='running',result=NULL,message='retry'""",
            (key,),
        )
    try:
        result = run_audit(db)
        with psycopg.connect(db, autocommit=True) as conn:
            conn.execute(
                "UPDATE incremental_feature_audit_once SET finished_at=NOW(),status='success',result=%s,message='ok' WHERE run_key=%s",
                (Jsonb(result), key),
            )
        compact = {
            "status": "success",
            "run_key": key,
            "gate_passed": result.get("gate_passed"),
            "recommended_activation": result.get("recommended_activation"),
            "baseline": result.get("baseline"),
            "features": {
                name: {
                    "hit_gain": data.get("hit_gain"),
                    "selected_brier_delta": data.get("selected_brier_delta"),
                    "gate_passed": data.get("gate_passed"),
                    "gate_fail_reasons": data.get("gate_fail_reasons"),
                    "coverage_by_fold": data.get("coverage_by_fold"),
                    "changed_picks": data.get("changed_picks"),
                    "bootstrap": data.get("bootstrap"),
                    "overall": data.get("overall"),
                    "by_fold": data.get("by_fold"),
                }
                for name, data in (result.get("features") or {}).items()
            },
        }
        print("INCREMENTAL_FEATURE_ONCE_RESULT", json.dumps(compact, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
        return compact
    except Exception as exc:
        with psycopg.connect(db, autocommit=True) as conn:
            conn.execute(
                "UPDATE incremental_feature_audit_once SET finished_at=NOW(),status='failed',message=%s WHERE run_key=%s",
                (str(exc)[:2000], key),
            )
        print("INCREMENTAL_FEATURE_ONCE_FAILED", json.dumps({"run_key": key, "error": str(exc)}, ensure_ascii=False, separators=(",", ":")), flush=True)
        raise


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
