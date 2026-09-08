#!/usr/bin/env python3
"""Protected dataset/status service plus background data/model jobs."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DOWNLOAD_TOKEN = os.getenv("DOWNLOAD_TOKEN", "").strip()
AUTO_IMPORT_FOOTBALL_DATA = os.getenv("AUTO_IMPORT_FOOTBALL_DATA", "true").lower() in {"1", "true", "yes"}
AUTO_IMPORT_ESPN = os.getenv("AUTO_IMPORT_ESPN", "true").lower() in {"1", "true", "yes"}
AUTO_IMPORT_ESPN_CONTEXT = os.getenv("AUTO_IMPORT_ESPN_CONTEXT", "true").lower() in {"1", "true", "yes"}
AUTO_IMPORT_UNDERSTAT = os.getenv("AUTO_IMPORT_UNDERSTAT", "true").lower() in {"1", "true", "yes"}
AUTO_RUN_BACKTEST = os.getenv("AUTO_RUN_BACKTEST", "false").lower() in {"1", "true", "yes"}
AUTO_BACKTEST_NEW_MODEL = os.getenv("AUTO_BACKTEST_NEW_MODEL", "true").lower() in {"1", "true", "yes"}
AUTO_BACKTEST_HYBRID = os.getenv("AUTO_BACKTEST_HYBRID", "true").lower() in {"1", "true", "yes"}
log = logging.getLogger("football-export")

TABLES = [
    "league_coverage", "fixtures", "fixture_details", "injuries", "season_players", "collection_runs", "api_call_log",
    "football_data_matches", "football_data_upcoming", "football_data_source_state", "football_data_import_runs",
    "espn_current_matches", "espn_upcoming", "espn_import_state", "espn_import_runs",
    "espn_injury_snapshots", "espn_odds_snapshots", "espn_prematch_snapshots", "espn_advanced_match_stats", "espn_context_runs",
    "understat_matches", "understat_team_seasons", "understat_source_state", "understat_import_runs",
    "model_backtest_runs", "model_policy_backtest_runs",
]

def _run_football_data_import() -> None:
    try:
        from football_data_mirror_importer import run_import
        log.info("Football-Data startup import completed: %s", run_import(DATABASE_URL))
    except Exception: log.exception("Football-Data startup import failed")

def _run_espn_import() -> None:
    try:
        from espn_current_importer import run_import
        log.info("ESPN startup import completed: %s", run_import(DATABASE_URL))
    except Exception: log.exception("ESPN startup import failed")

def _run_espn_context() -> None:
    try:
        from espn_context_importer import run_import
        log.info("ESPN context import completed: %s", run_import(DATABASE_URL))
    except Exception: log.exception("ESPN context import failed")

def _run_understat() -> None:
    try:
        from understat_xg_importer import run_import
        log.info("Understat startup import completed: %s", run_import(DATABASE_URL))
    except Exception: log.exception("Understat xG import failed")

def _run_backtest() -> None:
    try:
        from backtest_model import run_backtest
        r = run_backtest(DATABASE_URL)
        log.info("BACKTEST_COMPLETED matches=%s top10=%s markets=%s", r.get("matches_scored"), r.get("top10"), r.get("markets"))
    except Exception: log.exception("Model backtest failed")

def _run_backtest_if_new() -> None:
    try:
        from backtest_model import MODEL_VERSION, run_backtest
        with psycopg.connect(DATABASE_URL) as conn:
            exists = conn.execute("SELECT 1 FROM model_backtest_runs WHERE model_version=%s AND status='success' LIMIT 1", (MODEL_VERSION,)).fetchone()
        if exists:
            log.info("Backtest already exists for model %s; skipping.", MODEL_VERSION)
            return
        r = run_backtest(DATABASE_URL)
        log.info("NEW_MODEL_BACKTEST_COMPLETED version=%s matches=%s top10=%s markets=%s", MODEL_VERSION, r.get("matches_scored"), r.get("top10"), r.get("markets"))
    except Exception: log.exception("New-model backtest failed")

def _run_hybrid_if_new() -> None:
    try:
        from hybrid_policy_backtest import MODEL_VERSION, SCHEMA, run_backtest
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute(SCHEMA)
            exists = conn.execute("SELECT 1 FROM model_policy_backtest_runs WHERE policy_version=%s AND status='success' LIMIT 1", (MODEL_VERSION,)).fetchone()
        if exists:
            log.info("Hybrid policy backtest already exists for %s; skipping.", MODEL_VERSION)
            return
        r = run_backtest(DATABASE_URL)
        log.info("HYBRID_POLICY_BACKTEST_COMPLETED version=%s picks=%s hits=%s hit_rate=%s by_market=%s", MODEL_VERSION, r.get("picks"), r.get("hits"), r.get("hit_rate"), r.get("by_market"))
    except Exception: log.exception("Hybrid policy backtest failed")

@asynccontextmanager
async def lifespan(app: FastAPI):
    if DATABASE_URL and AUTO_IMPORT_FOOTBALL_DATA:
        threading.Thread(target=_run_football_data_import, name="football-data-importer", daemon=True).start()
    if DATABASE_URL and AUTO_IMPORT_ESPN:
        threading.Thread(target=_run_espn_import, name="espn-current-importer", daemon=True).start()
    if DATABASE_URL and AUTO_IMPORT_ESPN_CONTEXT:
        threading.Thread(target=_run_espn_context, name="espn-context-importer", daemon=True).start()
    if DATABASE_URL and AUTO_IMPORT_UNDERSTAT:
        threading.Thread(target=_run_understat, name="understat-xg-importer", daemon=True).start()
    if DATABASE_URL and AUTO_RUN_BACKTEST:
        threading.Thread(target=_run_backtest, name="model-backtest", daemon=True).start()
    elif DATABASE_URL and AUTO_BACKTEST_NEW_MODEL:
        threading.Thread(target=_run_backtest_if_new, name="model-backtest-new-version", daemon=True).start()
    if DATABASE_URL and AUTO_BACKTEST_HYBRID:
        threading.Thread(target=_run_hybrid_if_new, name="hybrid-policy-backtest", daemon=True).start()
    yield

app = FastAPI(title="Football Dataset Export", version="1.8", lifespan=lifespan)

def auth(token: str) -> None:
    if not DOWNLOAD_TOKEN: raise HTTPException(500, "DOWNLOAD_TOKEN is not configured.")
    if token != DOWNLOAD_TOKEN: raise HTTPException(401, "Invalid token.")

def json_default(value: Any):
    if isinstance(value, datetime): return value.isoformat()
    return str(value)

@app.get("/health")
def health(): return {"ok": True, "version": "1.8"}

@app.get("/status")
def status(token: str = Query(...)):
    auth(token)
    if not DATABASE_URL: raise HTTPException(500, "DATABASE_URL is not configured.")
    out = {}
    with psycopg.connect(DATABASE_URL) as conn:
        for table in TABLES:
            try: out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception: conn.rollback(); out[table] = None
        def one(sql: str):
            try: return conn.execute(sql).fetchone()
            except Exception: conn.rollback(); return None
        last_run = one("SELECT run_id::text, started_at, finished_at, status, api_calls, message FROM collection_runs ORDER BY started_at DESC LIMIT 1")
        last_fd_run = one("SELECT id, started_at, finished_at, status, historical_rows, upcoming_rows, message FROM football_data_import_runs ORDER BY id DESC LIMIT 1")
        last_espn_run = one("SELECT id, started_at, finished_at, status, completed_matches, upcoming_matches, summary_calls, message FROM espn_import_runs ORDER BY id DESC LIMIT 1")
        last_context = one("SELECT id, started_at, finished_at, injury_teams, odds_events, prematch_events, xg_matches, status, message FROM espn_context_runs ORDER BY id DESC LIMIT 1")
        last_understat = one("SELECT id, started_at, finished_at, status, requests, match_rows, xg_rows, team_rows, message FROM understat_import_runs ORDER BY id DESC LIMIT 1")
        last_backtest = one("SELECT id, started_at, finished_at, model_version, train_season, test_season, matches_scored, status, metrics, market_metrics, top10_metrics, message FROM model_backtest_runs ORDER BY id DESC LIMIT 1")
        last_policy = one("SELECT id, started_at, finished_at, policy_version, train_season, test_season, matches_scored, candidate_picks, metrics, status, message FROM model_policy_backtest_runs ORDER BY id DESC LIMIT 1")
    return {"counts": out, "last_run": list(last_run) if last_run else None, "last_football_data_run": list(last_fd_run) if last_fd_run else None, "last_espn_run": list(last_espn_run) if last_espn_run else None, "last_context_run": list(last_context) if last_context else None, "last_understat_run": list(last_understat) if last_understat else None, "last_backtest": list(last_backtest) if last_backtest else None, "last_policy_backtest": list(last_policy) if last_policy else None, "generated_at": datetime.now(timezone.utc).isoformat()}

def remove_file(path: str) -> None:
    try: os.remove(path)
    except OSError: pass

@app.get("/download")
def download(token: str = Query(...)):
    auth(token)
    if not DATABASE_URL: raise HTTPException(500, "DATABASE_URL is not configured.")
    tmp = tempfile.NamedTemporaryFile(prefix="football_dataset_", suffix=".zip", delete=False); tmp.close(); zip_path = tmp.name
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(), "format": "JSON Lines (one JSON object per row)", "tables": {}}
    with psycopg.connect(DATABASE_URL) as conn, zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for table in TABLES:
            try:
                with conn.cursor(name=f"export_{table}") as cur:
                    cur.itersize=1000; cur.execute(f"SELECT * FROM {table}"); columns=[d.name for d in cur.description]; count=0
                    with zf.open(f"{table}.jsonl", "w") as out:
                        for row in cur:
                            out.write((json.dumps(dict(zip(columns,row)),ensure_ascii=False,default=json_default,separators=(",",":"))+"\n").encode("utf-8")); count+=1
                    manifest["tables"][table]={"rows":count}
            except Exception as exc: conn.rollback(); manifest["tables"][table]={"error":str(exc)}
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, default=json_default))
    filename=f"football_big5_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.zip"
    return FileResponse(zip_path, media_type="application/zip", filename=filename, background=BackgroundTask(remove_file, zip_path))
