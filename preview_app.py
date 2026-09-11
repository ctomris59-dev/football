"""Temporary read-only production preview wrapper for 2026-09-10.

Imports the normal FastAPI app and adds one diagnostic endpoint exposing only the
latest Thursday decision previews. Also supports a fail-closed, opt-in, one-shot
1X2 historical audit on startup so research can run on the existing free web
service without creating another Render resource.
"""
from __future__ import annotations

import logging
import os
import threading

import psycopg

from export_app import app

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RUN_ONE_X_TWO_AUDIT_ONCE = os.getenv("RUN_ONE_X_TWO_AUDIT_ONCE", "false").lower() in {"1", "true", "yes"}
log = logging.getLogger("football-preview")
one_x_two_audit_lock = threading.Lock()


def _run_one_x_two_audit_once() -> None:
    if not one_x_two_audit_lock.acquire(blocking=False):
        return
    try:
        from one_x_two_audit import run_audit
        result = run_audit(DATABASE_URL)
        compact = {
            "version": result.get("version"),
            "gate_passed": result.get("gate_passed"),
            "recommended_activation": result.get("recommended_activation"),
            "overall": result.get("overall"),
            "by_fold": result.get("by_fold"),
            "gate_fail_reasons": result.get("gate_fail_reasons"),
        }
        log.info("ONE_X_TWO_AUDIT_ONCE_COMPLETED %s", compact)
    except Exception:
        log.exception("ONE_X_TWO_AUDIT_ONCE_FAILED")
    finally:
        one_x_two_audit_lock.release()


if DATABASE_URL and RUN_ONE_X_TWO_AUDIT_ONCE:
    threading.Thread(target=_run_one_x_two_audit_once, name="one-x-two-audit-once", daemon=True).start()


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
