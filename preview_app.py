"""Temporary read-only production preview wrapper for 2026-09-10.

Imports the normal FastAPI app and adds one diagnostic endpoint exposing only the
latest Thursday decision previews. Remove after current-bulletin verification.
"""
from __future__ import annotations

import os

import psycopg

from export_app import app

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


@app.get("/__temp_v2_preview_20260910")
def temporary_v2_preview():
    if not DATABASE_URL:
        return {"ok": False, "error": "DATABASE_URL missing"}
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            """SELECT id,week_key,started_at,finished_at,status,decision_ready,
                      high_confidence,high_confidence_value,diagnostics,message
                 FROM thursday_decision_runs
                ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    if not row:
        return {"ok": True, "status": "no_decision_run"}
    diagnostics = row[8] or {}
    return {
        "ok": True,
        "decision_run_id": int(row[0]),
        "week_key": row[1],
        "started_at": row[2],
        "finished_at": row[3],
        "status": row[4],
        "decision_ready": bool(row[5]),
        "high_confidence": row[6] or [],
        "high_confidence_value": row[7] or [],
        "candidate_preview": diagnostics.get("candidate_preview") or [],
        "raw_high_preview": diagnostics.get("raw_high_preview") or [],
        "offered_market_counts": diagnostics.get("offered_market_counts") or {},
        "playable_candidate_rows": diagnostics.get("playable_candidate_rows"),
        "unpriced_candidate_rows": diagnostics.get("unpriced_candidate_rows"),
        "international_candidate_rows": diagnostics.get("international_candidate_rows"),
        "aligned_candidate_rows": diagnostics.get("aligned_candidate_rows"),
        "message": row[9],
    }
