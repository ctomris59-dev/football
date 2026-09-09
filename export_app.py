#!/usr/bin/env python3
"""Small control/read API for the Thursday-first football betting workflow.

Primary product surface:
- Thursday model/context refresh
- official Turkey opening-price watch
- one frozen weekly decision containing exactly two lists

Legacy research tables remain in Postgres for validation, but are not part of the
weekly user-facing decision path.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, Optional

import psycopg
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DOWNLOAD_TOKEN = os.getenv("DOWNLOAD_TOKEN", "").strip()
VALIDATION_TRIGGER_TOKEN = os.getenv("VALIDATION_TRIGGER_TOKEN", "").strip()
AUTO_LIVE_REFRESH = os.getenv("AUTO_LIVE_REFRESH", "false").lower() in {"1", "true", "yes"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("football-thursday-service")
refresh_lock = threading.Lock()
opening_lock = threading.Lock()
refresh_state: dict[str, Any] = {
    "running": False,
    "last_started": None,
    "last_finished": None,
    "last_status": None,
    "last_error": None,
}

# Only data that directly supports the Thursday decision is surfaced/exported here.
# Older research/backtest tables are intentionally left in Postgres but hidden from
# this operational API so they cannot create user-facing noise.
ACTIVE_TABLES = [
    "football_data_matches",
    "espn_current_matches",
    "espn_upcoming",
    "espn_team_roster_snapshots",
    "understat_player_seasons",
    "player_team_context_snapshots",
    "fotmob_fixture_availability_snapshots",
    "turkey_odds_snapshots",
    "turkey_opening_odds",
    "turkey_odds_import_runs",
    "thursday_decision_runs",
    "thursday_watch_checks",
    "thursday_final_decisions",
    "live_refresh_runs",
]


def json_default(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def auth(token: Optional[str], authorization: Optional[str]) -> None:
    if not DOWNLOAD_TOKEN:
        raise HTTPException(500, "DOWNLOAD_TOKEN is not configured.")
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    if (bearer or token) != DOWNLOAD_TOKEN:
        raise HTTPException(401, "Invalid token.")


def _run_live_refresh() -> None:
    if not refresh_lock.acquire(blocking=False):
        return
    refresh_state.update({
        "running": True,
        "last_started": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
    })
    try:
        from live_refresh import main
        result = main()
        refresh_state["last_status"] = "success"
        log.info("THURSDAY_REFRESH_COMPLETED %s", json.dumps(result, ensure_ascii=False, default=json_default, separators=(",", ":")))
    except Exception as exc:
        refresh_state["last_status"] = "failed"
        refresh_state["last_error"] = str(exc)
        log.exception("Thursday refresh failed")
    finally:
        refresh_state["running"] = False
        refresh_state["last_finished"] = datetime.now(timezone.utc).isoformat()
        refresh_lock.release()


def _start_refresh_thread() -> bool:
    if refresh_lock.locked():
        return False
    threading.Thread(target=_run_live_refresh, name="thursday-refresh", daemon=True).start()
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    if DATABASE_URL and AUTO_LIVE_REFRESH:
        _start_refresh_thread()
    yield


app = FastAPI(title="Football Thursday Decision Service", version="5.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": "5.0",
        "workflow": "Thursday -> two lists -> bet -> done",
        "auto_live_refresh": AUTO_LIVE_REFRESH,
        "refresh": dict(refresh_state),
    }


@app.get("/")
def root():
    return health()


@app.post("/refresh")
def refresh(token: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    auth(token, authorization)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    if not _start_refresh_thread():
        return {"accepted": False, "reason": "refresh_already_running", "state": dict(refresh_state)}
    return {"accepted": True, "message": "Thursday preparation started."}


@app.get("/validation-run")
def validation_run(token: Optional[str] = Query(None)):
    if not VALIDATION_TRIGGER_TOKEN or token != VALIDATION_TRIGGER_TOKEN:
        raise HTTPException(401, "Invalid validation token.")
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    if not _start_refresh_thread():
        return {"accepted": False, "reason": "refresh_already_running", "state": dict(refresh_state)}
    return {"accepted": True, "message": "Validation refresh started."}


@app.get("/opening-watch")
def opening_watch():
    """Public, rate-limited-by-DB check used by the small Render watcher cron.

    It exposes no secrets and cannot change a finalized weekly decision. The watcher
    itself refuses to run outside Thursday evening / Friday-morning fallback, and
    duplicate calls within the same hour are ignored persistently.
    """
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    if not opening_lock.acquire(blocking=False):
        return {"ok": True, "status": "opening_watch_already_running"}
    try:
        from thursday_opening_watch import main
        result = main(DATABASE_URL)
        return {"ok": True, **result}
    finally:
        opening_lock.release()


@app.get("/thursday-list")
def thursday_list():
    """Return only the frozen weekly betting decision; otherwise say it is pending."""
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    from thursday_opening_watch import latest_final
    final = latest_final(DATABASE_URL)
    if not final:
        return {
            "ok": True,
            "status": "pending",
            "message": "This week's Turkish opening-price decision has not been finalized yet.",
        }
    payload = final.get("payload") or {}
    return {
        "ok": True,
        "status": "finalized",
        "week_key": final.get("week_key"),
        "finalized_at": final.get("finalized_at"),
        "high_confidence": payload.get("high_confidence") or [],
        "high_confidence_value": payload.get("high_confidence_value") or [],
        "official_fixture_coverage": payload.get("official_fixture_coverage"),
        "policy": payload.get("policy") or {},
    }


@app.get("/status")
def status(token: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    auth(token, authorization)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    counts: dict[str, Any] = {}
    latest: dict[str, Any] = {}
    with psycopg.connect(DATABASE_URL) as conn:
        for table in ACTIVE_TABLES:
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                conn.rollback()
                counts[table] = None
        try:
            row = conn.execute(
                """SELECT week_key,started_at,finished_at,status,fixture_count,official_fixture_coverage,
                          raw_high_candidates,priced_high_candidates,high_confidence_count,value_count,decision_ready,message
                     FROM thursday_decision_runs ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            if row:
                latest["thursday_decision"] = {
                    "week_key": row[0], "started_at": row[1], "finished_at": row[2], "status": row[3],
                    "fixture_count": row[4], "official_fixture_coverage": row[5],
                    "raw_high_candidates": row[6], "priced_high_candidates": row[7],
                    "high_confidence_count": row[8], "value_count": row[9], "decision_ready": row[10], "message": row[11],
                }
        except Exception:
            conn.rollback()
            latest["thursday_decision"] = None
    return {"ok": True, "refresh": dict(refresh_state), "counts": counts, "latest": latest}


@app.get("/predictions")
def predictions(token: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    """Compatibility endpoint: now returns only the frozen two-list decision."""
    auth(token, authorization)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    from thursday_opening_watch import latest_final
    final = latest_final(DATABASE_URL)
    if not final:
        return {"ok": True, "status": "pending", "high_confidence": [], "high_confidence_value": []}
    payload = final.get("payload") or {}
    return {
        "ok": True,
        "status": "finalized",
        "week_key": final.get("week_key"),
        "finalized_at": final.get("finalized_at"),
        "high_confidence": payload.get("high_confidence") or [],
        "high_confidence_value": payload.get("high_confidence_value") or [],
    }


@app.get("/download")
def download(token: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    """Export only the active operational dataset, not legacy research clutter."""
    auth(token, authorization)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    fd, path = tempfile.mkstemp(prefix="football_thursday_", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            with psycopg.connect(DATABASE_URL) as conn:
                for table in ACTIVE_TABLES:
                    try:
                        cur = conn.execute(f"SELECT * FROM {table}")
                        cols = [d.name for d in cur.description]
                        lines = [json.dumps(dict(zip(cols, row)), ensure_ascii=False, default=json_default) for row in cur.fetchall()]
                        zf.writestr(f"{table}.jsonl", "\n".join(lines))
                    except Exception:
                        conn.rollback()
        return FileResponse(
            path,
            media_type="application/zip",
            filename="football_thursday_dataset.zip",
            background=BackgroundTask(lambda: os.path.exists(path) and os.unlink(path)),
        )
    except Exception:
        if os.path.exists(path):
            os.unlink(path)
        raise
