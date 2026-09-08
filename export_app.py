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
from typing import Any, Callable

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DOWNLOAD_TOKEN = os.getenv("DOWNLOAD_TOKEN", "").strip()

def env_bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes"}

AUTO_IMPORT_FOOTBALL_DATA = env_bool("AUTO_IMPORT_FOOTBALL_DATA")
AUTO_IMPORT_ESPN = env_bool("AUTO_IMPORT_ESPN")
AUTO_IMPORT_ESPN_CONTEXT = env_bool("AUTO_IMPORT_ESPN_CONTEXT")
AUTO_IMPORT_UNDERSTAT = env_bool("AUTO_IMPORT_UNDERSTAT")
AUTO_IMPORT_ODDSPAPI = env_bool("AUTO_IMPORT_ODDSPAPI")
AUTO_RUN_BACKTEST = env_bool("AUTO_RUN_BACKTEST", "false")
AUTO_BACKTEST_NEW_MODEL = env_bool("AUTO_BACKTEST_NEW_MODEL")
AUTO_BACKTEST_HYBRID = env_bool("AUTO_BACKTEST_HYBRID")
AUTO_BACKTEST_VALUE = env_bool("AUTO_BACKTEST_VALUE")

log = logging.getLogger("football-export")

TABLES = [
    "league_coverage", "fixtures", "fixture_details", "injuries", "season_players", "collection_runs", "api_call_log",
    "football_data_matches", "football_data_upcoming", "football_data_source_state", "football_data_import_runs",
    "espn_current_matches", "espn_upcoming", "espn_import_state", "espn_import_runs",
    "espn_injury_snapshots", "espn_odds_snapshots", "espn_prematch_snapshots", "espn_advanced_match_stats", "espn_context_runs",
    "understat_matches", "understat_team_seasons", "understat_source_state", "understat_import_runs",
    "oddspapi_tournaments", "oddspapi_market_catalog", "oddspapi_fixture_snapshots", "oddspapi_market_prices", "oddspapi_import_runs",
    "model_backtest_runs", "model_policy_backtest_runs", "model_value_backtest_runs",
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


def _run_espn_context() -> None:
    try:
        from espn_context_importer import run_import
        log.info("ESPN context import completed: %s", run_import(DATABASE_URL))
    except Exception:
        log.exception("ESPN context import failed")


def _run_understat() -> None:
    try:
        from understat_xg_importer import run_import
        log.info("Understat startup import completed: %s", run_import(DATABASE_URL))
    except Exception:
        log.exception("Understat xG import failed")


def _run_oddspapi() -> None:
    try:
        # Protect the free quota from repeated deploys during the same UTC hour.
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                fresh = conn.execute(
                    """
                    SELECT 1 FROM oddspapi_import_runs
                    WHERE status='success'
                      AND finished_at >= date_trunc('hour', NOW())
                    LIMIT 1
                    """
                ).fetchone()
            if fresh:
                log.info("OddsPapi snapshot already collected this hour; skipping.")
                return
        except Exception:
            pass
        from oddspapi_canonical_importer import run_import
        result = run_import(DATABASE_URL)
        log.info("ODDSPAPI_STARTUP_COMPLETED %s", json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        log.exception("OddsPapi market import failed")


def _run_backtest() -> None:
    try:
        from backtest_model import run_backtest
        r = run_backtest(DATABASE_URL)
        log.info("BACKTEST_COMPLETED matches=%s top10=%s markets=%s", r.get("matches_scored"), r.get("top10"), r.get("markets"))
    except Exception:
        log.exception("Model backtest failed")


def _run_backtest_if_new() -> None:
    try:
        from backtest_model import MODEL_VERSION, run_backtest
        with psycopg.connect(DATABASE_URL) as conn:
            exists = conn.execute(
                "SELECT 1 FROM model_backtest_runs WHERE model_version=%s AND status='success' LIMIT 1",
                (MODEL_VERSION,),
            ).fetchone()
        if exists:
            log.info("Backtest already exists for model %s; skipping.", MODEL_VERSION)
            return
        r = run_backtest(DATABASE_URL)
        log.info("NEW_MODEL_BACKTEST_COMPLETED version=%s matches=%s top10=%s markets=%s", MODEL_VERSION, r.get("matches_scored"), r.get("top10"), r.get("markets"))
    except Exception:
        log.exception("New-model backtest failed")


def _run_hybrid_if_new() -> None:
    try:
        from hybrid_policy_backtest import MODEL_VERSION, SCHEMA, run_backtest
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute(SCHEMA)
            exists = conn.execute(
                "SELECT 1 FROM model_policy_backtest_runs WHERE policy_version=%s AND status='success' LIMIT 1",
                (MODEL_VERSION,),
            ).fetchone()
        if exists:
            log.info("Hybrid policy backtest already exists for %s; skipping.", MODEL_VERSION)
            return
        r = run_backtest(DATABASE_URL)
        log.info("HYBRID_POLICY_BACKTEST_COMPLETED version=%s picks=%s hits=%s hit_rate=%s by_market=%s", MODEL_VERSION, r.get("picks"), r.get("hits"), r.get("hit_rate"), r.get("by_market"))
    except Exception:
        log.exception("Hybrid policy backtest failed")


def _run_value_if_new() -> None:
    try:
        from value_backtest import VERSION, SCHEMA, run_backtest
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute(SCHEMA)
            exists = conn.execute(
                "SELECT 1 FROM model_value_backtest_runs WHERE version=%s AND status='success' LIMIT 1",
                (VERSION,),
            ).fetchone()
        if exists:
            log.info("Value backtest already exists for %s; skipping.", VERSION)
            return
        r = run_backtest(DATABASE_URL)
        log.info("VALUE_BACKTEST_COMPLETED version=%s odds_matches=%s model_brier=%s market_brier=%s gates=%s", VERSION, r.get("odds_matches"), r.get("model_brier"), r.get("market_brier"), r.get("gates"))
    except Exception:
        log.exception("Value backtest failed")


def start_thread(target: Callable[[], None], name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if DATABASE_URL and AUTO_IMPORT_FOOTBALL_DATA:
        start_thread(_run_football_data_import, "football-data-importer")
    if DATABASE_URL and AUTO_IMPORT_ESPN:
        start_thread(_run_espn_import, "espn-current-importer")
    if DATABASE_URL and AUTO_IMPORT_ESPN_CONTEXT:
        start_thread(_run_espn_context, "espn-context-importer")
    if DATABASE_URL and AUTO_IMPORT_UNDERSTAT:
        start_thread(_run_understat, "understat-xg-importer")
    if DATABASE_URL and AUTO_IMPORT_ODDSPAPI:
        start_thread(_run_oddspapi, "oddspapi-market-importer")
    if DATABASE_URL and AUTO_RUN_BACKTEST:
        start_thread(_run_backtest, "model-backtest")
    elif DATABASE_URL and AUTO_BACKTEST_NEW_MODEL:
        start_thread(_run_backtest_if_new, "model-backtest-new-version")
    if DATABASE_URL and AUTO_BACKTEST_HYBRID:
        start_thread(_run_hybrid_if_new, "hybrid-policy-backtest")
    if DATABASE_URL and AUTO_BACKTEST_VALUE:
        start_thread(_run_value_if_new, "value-backtest")
    yield


app = FastAPI(title="Football Dataset Export", version="2.0", lifespan=lifespan)


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
    return {"ok": True, "version": "2.0"}


@app.get("/status")
def status(token: str = Query(...)):
    auth(token)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    out: dict[str, Any] = {}
    with psycopg.connect(DATABASE_URL) as conn:
        for table in TABLES:
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                conn.rollback()
                out[table] = None

        def one(sql: str):
            try:
                return conn.execute(sql).fetchone()
            except Exception:
                conn.rollback()
                return None

        last_run = one("SELECT run_id::text, started_at, finished_at, status, api_calls, message FROM collection_runs ORDER BY started_at DESC LIMIT 1")
        last_fd_run = one("SELECT id, started_at, finished_at, status, historical_rows, upcoming_rows, message FROM football_data_import_runs ORDER BY id DESC LIMIT 1")
        last_espn_run = one("SELECT id, started_at, finished_at, status, completed_matches, upcoming_matches, summary_calls, message FROM espn_import_runs ORDER BY id DESC LIMIT 1")
        last_context = one("SELECT id, started_at, finished_at, injury_teams, odds_events, prematch_events, xg_matches, status, message FROM espn_context_runs ORDER BY id DESC LIMIT 1")
        last_understat = one("SELECT id, started_at, finished_at, status, requests, match_rows, xg_rows, team_rows, message FROM understat_import_runs ORDER BY id DESC LIMIT 1")
        last_oddspapi = one("SELECT id, started_at, finished_at, status, api_calls, tournament_count, fixture_count, price_rows, ou25_fixtures, btts_fixtures, corner85_fixtures, message FROM oddspapi_import_runs ORDER BY id DESC LIMIT 1")
        last_backtest = one("SELECT id, started_at, finished_at, model_version, train_season, test_season, matches_scored, status, metrics, market_metrics, top10_metrics, message FROM model_backtest_runs ORDER BY id DESC LIMIT 1")
        last_policy = one("SELECT id, started_at, finished_at, policy_version, train_season, test_season, matches_scored, candidate_picks, metrics, status, message FROM model_policy_backtest_runs ORDER BY id DESC LIMIT 1")
        last_value = one("SELECT id, started_at, finished_at, version, train_season, test_season, matches_scored, odds_matches, metrics, status, message FROM model_value_backtest_runs ORDER BY id DESC LIMIT 1")

    return {
        "counts": out,
        "last_run": list(last_run) if last_run else None,
        "last_football_data_run": list(last_fd_run) if last_fd_run else None,
        "last_espn_run": list(last_espn_run) if last_espn_run else None,
        "last_context_run": list(last_context) if last_context else None,
        "last_understat_run": list(last_understat) if last_understat else None,
        "last_oddspapi_run": list(last_oddspapi) if last_oddspapi else None,
        "last_backtest": list(last_backtest) if last_backtest else None,
        "last_policy_backtest": list(last_policy) if last_policy else None,
        "last_value_backtest": list(last_value) if last_value else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


@app.get("/download")
def download(token: str = Query(...)):
    auth(token)
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")
    tmp = tempfile.NamedTemporaryFile(prefix="football_dataset_", suffix=".zip", delete=False)
    tmp.close()
    zip_path = tmp.name
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "format": "JSON Lines (one JSON object per row)",
        "tables": {},
    }
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
                            line = json.dumps(dict(zip(columns, row)), ensure_ascii=False, default=json_default, separators=(",", ":")) + "\n"
                            out_file.write(line.encode("utf-8"))
                            count += 1
                    manifest["tables"][table] = {"rows": count}
            except Exception as exc:
                conn.rollback()
                manifest["tables"][table] = {"error": str(exc)}
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, default=json_default))
    filename = f"football_big5_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.zip"
    return FileResponse(zip_path, media_type="application/zip", filename=filename, background=BackgroundTask(remove_file, zip_path))
