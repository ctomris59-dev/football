#!/usr/bin/env python3
"""Small control/read API for the finalized Thursday betting workflow.

Primary product surface:
- Thursday model/context refresh;
- official Turkey opening-price watch;
- international paired same-book no-vig validation;
- one frozen weekly decision containing exactly two lists.

Legacy research tables remain in Postgres for validation, but are not part of the
weekly user-facing decision path.
"""
from __future__ import annotations

import hmac
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
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DOWNLOAD_TOKEN = os.getenv("DOWNLOAD_TOKEN", "").strip()
VALIDATION_TRIGGER_TOKEN = os.getenv("VALIDATION_TRIGGER_TOKEN", "").strip()
THURSDAY_SCHEDULER_KEY = os.getenv("THURSDAY_SCHEDULER_KEY", "").strip()
THURSDAY_RECENT_REFRESH_MINUTES = float(os.getenv("THURSDAY_RECENT_REFRESH_MINUTES", "20"))
AUTO_LIVE_REFRESH = os.getenv("AUTO_LIVE_REFRESH", "false").lower() in {"1", "true", "yes"}
RUN_OVER25_RESEARCH_ONCE = os.getenv("RUN_OVER25_RESEARCH_ONCE", "false").lower() in {"1", "true", "yes"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("football-thursday-service")
refresh_lock = threading.Lock()
opening_lock = threading.Lock()
research_lock = threading.Lock()
refresh_state: dict[str, Any] = {
    "running": False,
    "last_started": None,
    "last_finished": None,
    "last_status": None,
    "last_error": None,
}

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
    "market_consensus_snapshots",
    "market_consensus_runs",
    "international_market_refs",
    "international_market_ref_runs",
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


def scheduler_auth(key: Optional[str]) -> None:
    if not THURSDAY_SCHEDULER_KEY:
        raise HTTPException(500, "THURSDAY_SCHEDULER_KEY is not configured.")
    candidate = (key or "").strip()
    if not candidate or not hmac.compare_digest(candidate, THURSDAY_SCHEDULER_KEY):
        raise HTTPException(401, "Invalid scheduler key.")


def recent_thursday_refresh() -> Optional[dict[str, Any]]:
    if not DATABASE_URL or THURSDAY_RECENT_REFRESH_MINUTES <= 0:
        return None
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute(
                """SELECT id,started_at,finished_at,status,trigger_name
                   FROM live_refresh_runs
                   WHERE trigger_name='thursday-decision-prep'
                     AND (
                       (status='running' AND started_at>=NOW()-(%s||' minutes')::interval)
                       OR
                       (status='success' AND COALESCE(finished_at,started_at)>=NOW()-(%s||' minutes')::interval)
                     )
                   ORDER BY started_at DESC LIMIT 1""",
                (THURSDAY_RECENT_REFRESH_MINUTES, THURSDAY_RECENT_REFRESH_MINUTES),
            ).fetchone()
        if not row:
            return None
        return {
            "run_id": int(row[0]),
            "started_at": row[1],
            "finished_at": row[2],
            "status": row[3],
            "trigger_name": row[4],
        }
    except Exception as exc:
        log.warning("THURSDAY_RECENT_REFRESH_CHECK_FAILED %s", str(exc)[:500])
        return None


def _run_live_refresh() -> None:
    if not refresh_lock.acquire(blocking=False):
        return
    refresh_state.update({"running": True, "last_started": datetime.now(timezone.utc).isoformat(), "last_error": None})
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


def _run_over25_research_once() -> None:
    if not research_lock.acquire(blocking=False):
        return
    try:
        from over25_sensitivity_audit import run
        result = run(DATABASE_URL)
        compact = {
            "version": result.get("version"),
            "folds": result.get("folds"),
            "candidate_rows": result.get("candidate_rows"),
            "sensitivity": result.get("sensitivity"),
        }
        log.info("OVER25_RESEARCH_ONCE_COMPLETED %s", json.dumps(compact, ensure_ascii=False, default=json_default, separators=(",", ":")))
    except Exception:
        log.exception("OVER25_RESEARCH_ONCE_FAILED")
    finally:
        research_lock.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if DATABASE_URL and AUTO_LIVE_REFRESH:
        _start_refresh_thread()
    if DATABASE_URL and RUN_OVER25_RESEARCH_ONCE:
        threading.Thread(target=_run_over25_research_once, name="over25-research-once", daemon=True).start()
    yield


app = FastAPI(title="Football Thursday Decision Service", version="5.3", lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": "5.3",
        "workflow": "Thursday -> model + international no-vig + Turkey price -> two lists -> bet -> done",
        "auto_live_refresh": AUTO_LIVE_REFRESH,
        "scheduler_auth_configured": bool(THURSDAY_SCHEDULER_KEY),
        "refresh": dict(refresh_state),
    }


@app.get("/")
def root():
    return health()


@app.get("/persembe", response_class=HTMLResponse)
def persembe_page():
    from thursday_page import render_page
    return HTMLResponse(render_page(), headers={"Cache-Control": "no-store"})


@app.get("/thursday", response_class=HTMLResponse)
def thursday_page_alias():
    from thursday_page import render_page
    return HTMLResponse(render_page(), headers={"Cache-Control": "no-store"})


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
def opening_watch(x_scheduler_key: Optional[str] = Header(None, alias="X-Scheduler-Key")):
    scheduler_auth(x_scheduler_key)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    recent = recent_thursday_refresh()
    if recent:
        return {"ok": True, "status": "skipped_recent_thursday_refresh", "recent_refresh": recent, "cooldown_minutes": THURSDAY_RECENT_REFRESH_MINUTES}
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
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    from thursday_opening_watch import latest_final
    final = latest_final(DATABASE_URL)
    if not final:
        return {"ok": True, "status": "pending", "message": "This week's model + international + Turkey opening decision has not been finalized yet."}
    payload = final.get("payload") or {}
    return {
        "ok": True,
        "status": "finalized",
        "week_key": final.get("week_key"),
        "finalized_at": final.get("finalized_at"),
        "source": final.get("source"),
        "sources": payload.get("sources") or {},
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
        try:
            row = conn.execute(
                """SELECT started_at,finished_at,status,target_fixtures,matched_fixtures,reference_rows,message
                   FROM international_market_ref_runs ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            if row:
                latest["international_reference"] = {
                    "started_at": row[0], "finished_at": row[1], "status": row[2],
                    "target_fixtures": row[3], "matched_fixtures": row[4], "reference_rows": row[5], "message": row[6],
                }
        except Exception:
            conn.rollback()
            latest["international_reference"] = None
    return {"ok": True, "refresh": dict(refresh_state), "counts": counts, "latest": latest}


@app.get("/predictions")
def predictions(token: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
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
        "source": final.get("source"),
        "sources": payload.get("sources") or {},
        "high_confidence": payload.get("high_confidence") or [],
        "high_confidence_value": payload.get("high_confidence_value") or [],
    }


@app.get("/download")
def download(token: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
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
        return FileResponse(path, media_type="application/zip", filename="football_thursday_dataset.zip", background=BackgroundTask(lambda: os.path.exists(path) and os.unlink(path)))
    except Exception:
        if os.path.exists(path):
            os.unlink(path)
        raise
