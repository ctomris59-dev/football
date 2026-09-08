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
AUTO_RUN_BACKTEST = os.getenv("AUTO_RUN_BACKTEST", "true").lower() in {"1", "true", "yes"}

log = logging.getLogger("football-export")

TABLES = [
    "league_coverage", "fixtures", "fixture_details", "injuries", "season_players",
    "collection_runs", "api_call_log",
    "football_data_matches", "football_data_upcoming", "football_data_source_state", "football_data_import_runs",
    "espn_current_matches", "espn_upcoming", "espn_import_state", "espn_import_runs",
    "model_backtest_runs",
]


def _run_football_data_import() -> None:
    try:
        from football_data_mirror_importer import run_import
        log.info("Football-Data startup import completed: %s", run_import(DATABASE_URL))
    except Exception:
        log.exception("Football-Data startup import failed")


def _run_espn_import() -> None:
    try:
        from espn_current_importer import run_import
        log.info("ESPN startup import completed: %s", run_import(DATABASE_URL))
    except Exception:
        log.exception("ESPN startup import failed")


def _run_backtest() -> None:
    try:
        from backtest_model import run_backtest
        result = run_backtest(DATABASE_URL)
        log.info("BACKTEST_COMPLETED matches=%s top10=%s markets=%s", result.get("matches_scored"), result.get("top10"), result.get("markets"))
    except Exception:
        log.exception("Model backtest failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if DATABASE_URL and AUTO_IMPORT_FOOTBALL_DATA:
        threading.Thread(target=_run_football_data_import, name="football-data-importer", daemon=True).start()
    if DATABASE_URL and AUTO_IMPORT_ESPN:
        threading.Thread(target=_run_espn_import, name="espn-current-importer", daemon=True).start()
    if DATABASE_URL and AUTO_RUN_BACKTEST:
        threading.Thread(target=_run_backtest, name="model-backtest", daemon=True).start()
    yield


app = FastAPI(title="Football Dataset Export", version="1.4", lifespan=lifespan)


def auth(token: str) -> None:
    if not DOWNLOAD_TOKEN:
        raise HTTPException(500, "DOWNLOAD_TOKEN is not configured.")
    if token != DOWNLOAD_TOKEN:
        raise HTTPException(401, "Invalid token.")


def json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@app.get("/health")
def health():
    return {"ok": True, "version": "1.4"}


@app.get("/status")
def status(token: str = Query(...)):
    auth(token)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    out = {}
    with psycopg.connect(DATABASE_URL) as conn:
        for table in TABLES:
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                conn.rollback(); out[table] = None
        def one(sql: str):
            try: return conn.execute(sql).fetchone()
            except Exception: conn.rollback(); return None
        last_run = one("SELECT run_id::text, started_at, finished_at, status, api_calls, message FROM collection_runs ORDER BY started_at DESC LIMIT 1")
        last_fd_run = one("SELECT id, started_at, finished_at, status, historical_rows, upcoming_rows, message FROM football_data_import_runs ORDER BY id DESC LIMIT 1")
        last_espn_run = one("SELECT id, started_at, finished_at, status, completed_matches, upcoming_matches, summary_calls, message FROM espn_import_runs ORDER BY id DESC LIMIT 1")
        last_backtest = one("SELECT id, started_at, finished_at, model_version, train_season, test_season, matches_scored, status, metrics, market_metrics, top10_metrics, message FROM model_backtest_runs ORDER BY id DESC LIMIT 1")
    return {
        "counts": out,
        "last_run": list(last_run) if last_run else None,
        "last_football_data_run": list(last_fd_run) if last_fd_run else None,
        "last_espn_run": list(last_espn_run) if last_espn_run else None,
        "last_backtest": list(last_backtest) if last_backtest else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def remove_file(path: str) -> None:
    try: os.remove(path)
    except OSError: pass


@app.get("/download")
def download(token: str = Query(...)):
    auth(token)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    tmp = tempfile.NamedTemporaryFile(prefix="football_dataset_", suffix=".zip", delete=False); tmp.close(); zip_path = tmp.name
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(), "format": "JSON Lines (one JSON object per row)", "tables": {}}
    with psycopg.connect(DATABASE_URL) as conn, zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for table in TABLES:
            try:
                with conn.cursor(name=f"export_{table}") as cur:
                    cur.itersize = 1000; cur.execute(f"SELECT * FROM {table}"); columns = [d.name for d in cur.description]; count = 0
                    with zf.open(f"{table}.jsonl", "w") as out:
                        for row in cur:
                            out.write((json.dumps(dict(zip(columns, row)), ensure_ascii=False, default=json_default, separators=(",", ":")) + "\n").encode("utf-8")); count += 1
                    manifest["tables"][table] = {"rows": count}
            except Exception as exc:
                conn.rollback(); manifest["tables"][table] = {"error": str(exc)}
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, default=json_default))
    filename = f"football_big5_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.zip"
    return FileResponse(zip_path, media_type="application/zip", filename=filename, background=BackgroundTask(remove_file, zip_path))
