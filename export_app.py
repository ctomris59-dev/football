#!/usr/bin/env python3
"""Protected status/export/prediction control service for the football system."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import psycopg
import requests
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DOWNLOAD_TOKEN = os.getenv("DOWNLOAD_TOKEN", "").strip()
VALIDATION_TRIGGER_TOKEN = os.getenv("VALIDATION_TRIGGER_TOKEN", "").strip()


def env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes"}


# Deploys must not consume provider quota by default. Refresh is explicit/scheduled.
AUTO_LIVE_REFRESH = env_bool("AUTO_LIVE_REFRESH", "false")
AUTO_RUN_BACKTEST = env_bool("AUTO_RUN_BACKTEST", "false")
AUTO_BACKTEST_NEW_MODEL = env_bool("AUTO_BACKTEST_NEW_MODEL", "false")
AUTO_BACKTEST_HYBRID = env_bool("AUTO_BACKTEST_HYBRID", "false")
AUTO_BACKTEST_VALUE = env_bool("AUTO_BACKTEST_VALUE", "false")
VALIDATION_KEEPALIVE = env_bool("VALIDATION_KEEPALIVE", "true")
KEEPALIVE_INTERVAL = max(8.0, float(os.getenv("VALIDATION_KEEPALIVE_INTERVAL_SECONDS", "15")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("football-export")
refresh_lock = threading.Lock()
refresh_state: dict[str, Any] = {"running": False, "last_started": None, "last_finished": None, "last_status": None, "last_error": None}
validation_root_started = False

TABLES = [
    "league_coverage", "fixtures", "fixture_details", "injuries", "season_players", "collection_runs", "api_call_log",
    "football_data_matches", "football_data_upcoming", "football_data_source_state", "football_data_import_runs",
    "second_tier_matches", "second_tier_import_runs",
    "promotion_transfer_factors", "promotion_priors", "promotion_prior_runs", "promotion_prior_backtest_runs",
    "espn_current_matches", "espn_upcoming", "espn_import_state", "espn_import_runs",
    "espn_injury_snapshots", "espn_odds_snapshots", "espn_prematch_snapshots", "espn_advanced_match_stats", "espn_context_runs",
    "espn_team_schedule_events", "espn_team_schedule_runs",
    "understat_matches", "understat_team_seasons", "understat_source_state", "understat_import_runs",
    "clubelo_daily_snapshots", "clubelo_team_map", "clubelo_history", "clubelo_import_runs",
    "oddspapi_tournaments", "oddspapi_market_catalog", "oddspapi_fixture_snapshots", "oddspapi_market_prices", "oddspapi_import_runs", "oddspapi_allbooks_runs",
    "market_consensus_snapshots", "market_consensus_runs",
    "bbs_absence_snapshots", "bbs_availability_runs", "bbs_lineup_snapshots", "bbs_lineup_runs",
    "sofascore_availability_snapshots", "sofascore_availability_runs",
    "fotmob_team_availability_snapshots", "fotmob_fixture_availability_snapshots", "fotmob_availability_runs",
    "fotmob_player_strength_snapshots", "fotmob_team_style_snapshots", "fotmob_strength_runs",
    "score_state_adjusted_matches", "score_state_runs", "score_state_backtest_runs",
    "fixture_enrichment_snapshots", "fixture_enrichment_runs",
    "prematch_feature_snapshots", "prematch_context_runs",
    "prediction_readiness_snapshots", "data_readiness_runs",
    "model_backtest_runs", "model_policy_backtest_runs", "model_value_backtest_runs",
    "production_prediction_runs", "production_predictions",
]


def _run_live_refresh() -> None:
    if not refresh_lock.acquire(blocking=False):
        log.info("Live refresh already running; duplicate request ignored")
        return
    refresh_state.update({"running": True, "last_started": datetime.now(timezone.utc).isoformat(), "last_error": None})
    try:
        from live_refresh import main
        result = main()
        refresh_state["last_status"] = "success"
        log.info("LIVE_REFRESH_COMPLETED %s", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")))
    except Exception as exc:
        refresh_state["last_status"] = "failed"
        refresh_state["last_error"] = str(exc)
        log.exception("Ordered live refresh failed")
    finally:
        refresh_state["running"] = False
        refresh_state["last_finished"] = datetime.now(timezone.utc).isoformat()
        refresh_lock.release()


def _validation_keepalive() -> None:
    """Keep Render Free awake only while an explicitly enabled startup validation runs.

    Render may immediately re-apply an already-expired idle timer after deployment. An
    external self-request counts as real HTTP traffic and prevents the validation
    container from being suspended halfway through a resume-safe historical backfill.
    This thread exists only when AUTO_LIVE_REFRESH is explicitly true; normal
    production remains quota-safe and naturally sleepable.
    """
    if not (AUTO_LIVE_REFRESH and VALIDATION_KEEPALIVE):
        return
    base = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if not base:
        name = os.getenv("RENDER_SERVICE_NAME", "football-dataset-export").strip()
        base = f"https://{name}.onrender.com"
    url = base + "/health"
    # Lifespan runs before the socket is announced ready. Give uvicorn a moment, then
    # keep traffic alive until the refresh has definitely finished.
    time.sleep(4.0)
    failures = 0
    while True:
        if refresh_state.get("last_finished") and not refresh_state.get("running"):
            break
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "football-validation-keepalive/1.0"})
            failures = 0
            log.info("VALIDATION_KEEPALIVE status=%s running=%s", r.status_code, bool(refresh_state.get("running")))
        except Exception as exc:
            failures += 1
            # Fail soft: keepalive must never make data validation fail.
            if failures <= 3 or failures % 10 == 0:
                log.warning("VALIDATION_KEEPALIVE_FAILED failures=%s error=%s", failures, str(exc)[:250])
        time.sleep(KEEPALIVE_INTERVAL)
    log.info("VALIDATION_KEEPALIVE_STOP status=%s", refresh_state.get("last_status"))


def _run_backtest() -> None:
    try:
        from backtest_model import run_backtest
        run_backtest(DATABASE_URL)
    except Exception:
        log.exception("Model backtest failed")


def _run_backtest_if_new() -> None:
    try:
        from backtest_model import MODEL_VERSION, run_backtest
        with psycopg.connect(DATABASE_URL) as conn:
            exists = conn.execute("SELECT 1 FROM model_backtest_runs WHERE model_version=%s AND status='success' LIMIT 1", (MODEL_VERSION,)).fetchone()
        if not exists:
            run_backtest(DATABASE_URL)
    except Exception:
        log.exception("New-model backtest failed")


def _run_hybrid_if_new() -> None:
    try:
        from hybrid_policy_backtest import MODEL_VERSION, SCHEMA, run_backtest
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute(SCHEMA)
            exists = conn.execute("SELECT 1 FROM model_policy_backtest_runs WHERE policy_version=%s AND status='success' LIMIT 1", (MODEL_VERSION,)).fetchone()
        if not exists:
            run_backtest(DATABASE_URL)
    except Exception:
        log.exception("Hybrid policy backtest failed")


def _run_value_if_new() -> None:
    try:
        from value_backtest import VERSION, SCHEMA, run_backtest
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute(SCHEMA)
            exists = conn.execute("SELECT 1 FROM model_value_backtest_runs WHERE version=%s AND status='success' LIMIT 1", (VERSION,)).fetchone()
        if not exists:
            run_backtest(DATABASE_URL)
    except Exception:
        log.exception("Value backtest failed")


def start_thread(target: Callable[[], None], name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if DATABASE_URL and AUTO_LIVE_REFRESH:
        start_thread(_run_live_refresh, "ordered-live-refresh")
        start_thread(_validation_keepalive, "validation-keepalive")
    if DATABASE_URL and AUTO_RUN_BACKTEST:
        start_thread(_run_backtest, "model-backtest")
    elif DATABASE_URL and AUTO_BACKTEST_NEW_MODEL:
        start_thread(_run_backtest_if_new, "model-backtest-new-version")
    if DATABASE_URL and AUTO_BACKTEST_HYBRID:
        start_thread(_run_hybrid_if_new, "hybrid-policy-backtest")
    if DATABASE_URL and AUTO_BACKTEST_VALUE:
        start_thread(_run_value_if_new, "value-backtest")
    yield


app = FastAPI(title="Football Prediction Data Service", version="4.1", lifespan=lifespan)


def auth(token: Optional[str], authorization: Optional[str]) -> None:
    if not DOWNLOAD_TOKEN:
        raise HTTPException(500, "DOWNLOAD_TOKEN is not configured.")
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    provided = bearer or token
    if provided != DOWNLOAD_TOKEN:
        raise HTTPException(401, "Invalid token.")


def json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def rowdict(cur, row) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return dict(zip([d.name for d in cur.description], row))


@app.get("/health")
def health():
    return {"ok": True, "version": "4.1", "auto_live_refresh": AUTO_LIVE_REFRESH, "refresh": dict(refresh_state)}


@app.get("/")
def root():
    global validation_root_started
    if VALIDATION_TRIGGER_TOKEN and not validation_root_started and not refresh_lock.locked():
        validation_root_started = True
        start_thread(_run_live_refresh, "validation-root-one-shot")
    return health()


@app.post("/refresh")
def refresh(token: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    auth(token, authorization)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    if refresh_lock.locked():
        return {"accepted": False, "reason": "refresh_already_running", "state": dict(refresh_state)}
    start_thread(_run_live_refresh, "manual-live-refresh")
    return {"accepted": True, "message": "Refresh started. Check /health or /status for completion."}


@app.get("/validation-run")
def validation_run(token: Optional[str] = Query(None)):
    if not VALIDATION_TRIGGER_TOKEN:
        raise HTTPException(404, "Validation trigger is disabled.")
    if token != VALIDATION_TRIGGER_TOKEN:
        raise HTTPException(401, "Invalid validation token.")
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    if refresh_lock.locked():
        return {"accepted": False, "reason": "refresh_already_running", "state": dict(refresh_state)}
    start_thread(_run_live_refresh, "validation-one-shot")
    return {"accepted": True, "message": "Validation run started."}


@app.get("/status")
def status(token: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    auth(token, authorization)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    out: dict[str, Any] = {}
    details: dict[str, Any] = {}
    with psycopg.connect(DATABASE_URL) as conn:
        for table in TABLES:
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                conn.rollback()
                out[table] = None

        def one(name: str, sql: str) -> None:
            try:
                cur = conn.execute(sql)
                details[name] = rowdict(cur, cur.fetchone())
            except Exception:
                conn.rollback()
                details[name] = None

        one("collector", "SELECT run_id::text AS run_id,started_at,finished_at,status,api_calls,message FROM collection_runs ORDER BY started_at DESC LIMIT 1")
        one("football_data", "SELECT started_at,finished_at,status,seasons_loaded,rows_loaded,message FROM football_data_import_runs ORDER BY id DESC LIMIT 1")
        one("espn", "SELECT started_at,finished_at,status,api_calls,matches_seen,results_written,upcoming_written,message FROM espn_import_runs ORDER BY id DESC LIMIT 1")
        one("understat", "SELECT started_at,finished_at,status,api_calls,matches_seen,results_written,message FROM understat_import_runs ORDER BY id DESC LIMIT 1")
        one("oddspapi", "SELECT started_at,finished_at,status,api_calls,fixtures_seen,prices_written,message FROM oddspapi_import_runs ORDER BY id DESC LIMIT 1")
        one("predictions", "SELECT id,started_at,finished_at,status,model_version,horizon_start,horizon_end,fixtures_scored,market_rows,top10_count,message FROM production_prediction_runs ORDER BY id DESC LIMIT 1")
    return {"ok": True, "refresh": dict(refresh_state), "counts": out, "latest": details}


@app.get("/predictions")
def predictions(token: Optional[str] = Query(None), authorization: Optional[str] = Header(None), limit: int = Query(10, ge=1, le=100)):
    auth(token, authorization)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    with psycopg.connect(DATABASE_URL) as conn:
        cur = conn.execute("""SELECT * FROM production_predictions WHERE run_id=(SELECT MAX(id) FROM production_prediction_runs WHERE status='success') ORDER BY rank NULLS LAST,ranking_score DESC LIMIT %s""", (limit,))
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return {"ok": True, "count": len(rows), "rows": rows}


@app.get("/download")
def download(token: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    auth(token, authorization)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    fd, path = tempfile.mkstemp(prefix="football_dataset_", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            with psycopg.connect(DATABASE_URL) as conn:
                for table in TABLES:
                    try:
                        cur = conn.execute(f"SELECT * FROM {table}")
                        cols = [d.name for d in cur.description]
                        lines = [json.dumps(dict(zip(cols, row)), ensure_ascii=False, default=json_default) for row in cur.fetchall()]
                        zf.writestr(f"{table}.jsonl", "\n".join(lines))
                    except Exception:
                        conn.rollback()
        return FileResponse(path, media_type="application/zip", filename="football_dataset.zip", background=BackgroundTask(lambda: os.path.exists(path) and os.unlink(path)))
    except Exception:
        if os.path.exists(path):
            os.unlink(path)
        raise
