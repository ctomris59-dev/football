"""Temporary 2026-09-10 production bootstrap for the Thursday V2 decision.

Python imports sitecustomize automatically at interpreter startup. This module is
strictly gated to the Render web runtime (PORT + DATABASE_URL) and to 2026-09-10
Europe/Istanbul. It performs one opening-watch pass in a daemon thread and installs
a temporary read-only endpoint exposing the latest candidate diagnostics so the
operator can verify the result without Render workspace access.

Remove this file after the one-shot verification.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

ISTANBUL = ZoneInfo("Europe/Istanbul")
RUN_DATE = date(2026, 9, 10)
PREVIEW_PATH = "/__temp_v2_preview_20260910"


def _is_target_runtime() -> bool:
    return bool(
        os.getenv("PORT")
        and os.getenv("DATABASE_URL")
        and datetime.now(ISTANBUL).date() == RUN_DATE
    )


def _worker() -> None:
    # Allow uvicorn/export_app to finish importing before appending the read-only route.
    time.sleep(4)
    try:
        import psycopg
        import export_app

        if not any(getattr(route, "path", None) == PREVIEW_PATH for route in export_app.app.routes):
            @export_app.app.get(PREVIEW_PATH)
            def _temporary_v2_preview():
                database_url = os.getenv("DATABASE_URL", "").strip()
                if not database_url:
                    return {"ok": False, "error": "DATABASE_URL missing"}
                with psycopg.connect(database_url) as conn:
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

        # Call the narrow opening watcher directly so the live-refresh 45-minute
        # cooldown cannot suppress this intentional one-shot V2 verification.
        from thursday_opening_watch import main as opening_watch

        opening_watch(os.getenv("DATABASE_URL", ""))
    except Exception as exc:
        # Uvicorn must remain healthy even if the one-shot diagnostic fails.
        try:
            import logging
            logging.getLogger("one-shot-v2-preview").exception(
                "ONE_SHOT_V2_PREVIEW_FAILED: %s", exc
            )
        except Exception:
            pass


if _is_target_runtime():
    threading.Thread(target=_worker, name="one-shot-v2-preview", daemon=True).start()
