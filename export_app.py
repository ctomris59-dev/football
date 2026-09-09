#!/usr/bin/env python3
"""Protected status/export/prediction control service for the football system."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import psycopg
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("football-export")
refresh_lock = threading.Lock()
refresh_state: dict[str, Any] = {"running": False, "last_started": None, "last_finished": None, "last_status": None, "last_error": None}

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
    if DATABASE_URL and AUTO_RUN_BACKTEST:
        start_thread(_run_backtest, "model-backtest")
    elif DATABASE_URL and AUTO_BACKTEST_NEW_MODEL:
        start_thread(_run_backtest_if_new, "model-backtest-new-version")
    if DATABASE_URL and AUTO_BACKTEST_HYBRID:
        start_thread(_run_hybrid_if_new, "hybrid-policy-backtest")
    if DATABASE_URL and AUTO_BACKTEST_VALUE:
        start_thread(_run_value_if_new, "value-backtest")
    yield


app = FastAPI(title="Football Prediction Data Service", version="4.0", lifespan=lifespan)


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
    return {"ok": True, "version": "4.0", "auto_live_refresh": AUTO_LIVE_REFRESH, "refresh": dict(refresh_state)}


@app.get("/")
def root():
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
    # Deliberately disabled unless a short-lived one-shot token is configured.
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
        one("football_data", "SELECT * FROM football_data_import_runs ORDER BY id DESC LIMIT 1")
        one("second_tier", "SELECT * FROM second_tier_import_runs ORDER BY id DESC LIMIT 1")
        one("promotion_priors", "SELECT * FROM promotion_prior_runs ORDER BY id DESC LIMIT 1")
        one("promotion_backtest", "SELECT * FROM promotion_prior_backtest_runs ORDER BY id DESC LIMIT 1")
        one("espn_current", "SELECT * FROM espn_import_runs ORDER BY id DESC LIMIT 1")
        one("espn_context", "SELECT * FROM espn_context_runs ORDER BY id DESC LIMIT 1")
        one("team_schedule", "SELECT * FROM espn_team_schedule_runs ORDER BY id DESC LIMIT 1")
        one("understat", "SELECT * FROM understat_import_runs ORDER BY id DESC LIMIT 1")
        one("clubelo", "SELECT * FROM clubelo_import_runs ORDER BY id DESC LIMIT 1")
        one("oddspapi", "SELECT * FROM oddspapi_import_runs ORDER BY id DESC LIMIT 1")
        one("oddspapi_allbooks", "SELECT * FROM oddspapi_allbooks_runs ORDER BY id DESC LIMIT 1")
        one("market_consensus", "SELECT * FROM market_consensus_runs ORDER BY id DESC LIMIT 1")
        one("bbs_lineups", "SELECT * FROM bbs_lineup_runs ORDER BY id DESC LIMIT 1")
        one("sofascore_availability", "SELECT * FROM sofascore_availability_runs ORDER BY id DESC LIMIT 1")
        one("fotmob_availability", "SELECT * FROM fotmob_availability_runs ORDER BY id DESC LIMIT 1")
        one("fotmob_strength", "SELECT * FROM fotmob_strength_runs ORDER BY id DESC LIMIT 1")
        one("score_state", "SELECT * FROM score_state_runs ORDER BY id DESC LIMIT 1")
        one("score_state_backtest", "SELECT * FROM score_state_backtest_runs ORDER BY id DESC LIMIT 1")
        one("fixture_enrichment", "SELECT * FROM fixture_enrichment_runs ORDER BY id DESC LIMIT 1")
        one("prematch_context", "SELECT * FROM prematch_context_runs ORDER BY id DESC LIMIT 1")
        one("readiness", "SELECT * FROM data_readiness_runs ORDER BY id DESC LIMIT 1")
        one("backtest", "SELECT * FROM model_backtest_runs ORDER BY id DESC LIMIT 1")
        one("policy_backtest", "SELECT * FROM model_policy_backtest_runs ORDER BY id DESC LIMIT 1")
        one("value_backtest", "SELECT * FROM model_value_backtest_runs ORDER BY id DESC LIMIT 1")
        one("production_predictions", "SELECT * FROM production_prediction_runs ORDER BY id DESC LIMIT 1")
        try:
            season_counts = conn.execute("SELECT season_code,COUNT(*) FROM football_data_matches GROUP BY season_code ORDER BY season_code").fetchall()
        except Exception:
            conn.rollback()
            season_counts = []
    return {
        "counts": out,
        "season_counts": [[str(a), int(b)] for a,b in season_counts],
        "latest": details,
        "refresh": dict(refresh_state),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/predictions")
def predictions(token: Optional[str] = Query(None), authorization: Optional[str] = Header(None), top_only: bool = Query(True)):
    auth(token, authorization)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    with psycopg.connect(DATABASE_URL) as conn:
        try:
            cur = conn.execute("SELECT * FROM production_prediction_runs WHERE status='success' ORDER BY id DESC LIMIT 1")
            run = rowdict(cur, cur.fetchone())
            if not run:
                return {"run": None, "predictions": []}
            sql = "SELECT * FROM production_predictions WHERE run_id=%s"
            params: list[Any] = [run["id"]]
            if top_only:
                sql += " AND top10_rank IS NOT NULL ORDER BY top10_rank"
            else:
                sql += " ORDER BY match_date, event_id, market"
            cur = conn.execute(sql, params)
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            return {"run": run, "predictions": rows}
        except Exception as exc:
            conn.rollback()
            raise HTTPException(503, f"Predictions are not ready: {exc}")


def remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


@app.get("/download")
def download(token: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    auth(token, authorization)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    tmp = tempfile.NamedTemporaryFile(prefix="football_dataset_", suffix=".zip", delete=False)
    tmp.close()
    zip_path = tmp.name
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(), "format": "JSON Lines", "tables": {}}
    with psycopg.connect(DATABASE_URL) as conn, zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for table in TABLES:
            try:
                with conn.cursor(name=f"export_{table}") as cur:
                    cur.itersize = 1000
                    cur.execute(f"SELECT * FROM {table}")
                    columns = [d.name for d in cur.description]
                    count = 0
                    with zf.open(f"{table}.jsonl", "w") as out_file:
                        for row in cur:
                            out_file.write((json.dumps(dict(zip(columns,row)), ensure_ascii=False, default=json_default, separators=(",", ":")) + "\n").encode("utf-8"))
                            count += 1
                    manifest["tables"][table] = {"rows": count}
            except Exception as exc:
                conn.rollback()
                manifest["tables"][table] = {"error": str(exc)}
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, default=json_default))
    filename = f"football_big5_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.zip"
    return FileResponse(zip_path, media_type="application/zip", filename=filename, background=BackgroundTask(remove_file, zip_path))
