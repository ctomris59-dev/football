#!/usr/bin/env python3
"""
Small protected download service for the PostgreSQL dataset.

GET /health
GET /status?token=...
GET /download?token=...

The ZIP is generated on demand from PostgreSQL. This avoids relying on Render's
ephemeral local disk after a Cron/One-Off run has ended.

On startup, the service starts two idempotent background imports:
- Football-Data mirror for completed Big Five seasons
- ESPN public soccer data for the current season and upcoming fixtures
"""
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
AUTO_IMPORT_FOOTBALL_DATA = os.getenv("AUTO_IMPORT_FOOTBALL_DATA", "true").lower() in {
    "1",
    "true",
    "yes",
}
AUTO_IMPORT_ESPN = os.getenv("AUTO_IMPORT_ESPN", "true").lower() in {
    "1",
    "true",
    "yes",
}

log = logging.getLogger("football-export")

TABLES = [
    "league_coverage",
    "fixtures",
    "fixture_details",
    "injuries",
    "season_players",
    "collection_runs",
    "api_call_log",
    "football_data_matches",
    "football_data_upcoming",
    "football_data_source_state",
    "football_data_import_runs",
    "espn_current_matches",
    "espn_upcoming",
    "espn_import_state",
    "espn_import_runs",
]


def _run_football_data_import() -> None:
    try:
        from football_data_mirror_importer import run_import

        result = run_import(DATABASE_URL)
        log.info("Football-Data startup import completed: %s", result)
    except Exception:
        log.exception("Football-Data startup import failed")


def _run_espn_import() -> None:
    try:
        from espn_current_importer import run_import

        result = run_import(DATABASE_URL)
        log.info("ESPN startup import completed: %s", result)
    except Exception:
        # Current-source problems must never take the protected export service down.
        log.exception("ESPN startup import failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if DATABASE_URL and AUTO_IMPORT_FOOTBALL_DATA:
        threading.Thread(
            target=_run_football_data_import,
            name="football-data-importer",
            daemon=True,
        ).start()
    if DATABASE_URL and AUTO_IMPORT_ESPN:
        threading.Thread(
            target=_run_espn_import,
            name="espn-current-importer",
            daemon=True,
        ).start()
    yield


app = FastAPI(title="Football Dataset Export", version="1.3", lifespan=lifespan)


def auth(token: str) -> None:
    if not DOWNLOAD_TOKEN:
        raise HTTPException(500, "DOWNLOAD_TOKEN is not configured.")
    if token != DOWNLOAD_TOKEN:
        raise HTTPException(401, "Invalid token.")


def json_default(value: Any):
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


@app.get("/health")
def health():
    return {"ok": True}


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
                conn.rollback()
                out[table] = None

        try:
            last_run = conn.execute(
                """
                SELECT run_id::text, started_at, finished_at, status, api_calls, message
                FROM collection_runs ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
        except Exception:
            conn.rollback()
            last_run = None

        try:
            last_fd_run = conn.execute(
                """
                SELECT id, started_at, finished_at, status,
                       historical_rows, upcoming_rows, message
                FROM football_data_import_runs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        except Exception:
            conn.rollback()
            last_fd_run = None

        try:
            last_espn_run = conn.execute(
                """
                SELECT id, started_at, finished_at, status,
                       completed_matches, upcoming_matches, summary_calls, message
                FROM espn_import_runs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        except Exception:
            conn.rollback()
            last_espn_run = None

    return {
        "counts": out,
        "last_run": list(last_run) if last_run else None,
        "last_football_data_run": list(last_fd_run) if last_fd_run else None,
        "last_espn_run": list(last_espn_run) if last_espn_run else None,
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

    with psycopg.connect(DATABASE_URL) as conn, zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as zf:
        for table in TABLES:
            try:
                with conn.cursor(name=f"export_{table}") as cur:
                    cur.itersize = 1000
                    cur.execute(f"SELECT * FROM {table}")
                    columns = [d.name for d in cur.description]
                    count = 0
                    with zf.open(f"{table}.jsonl", "w") as out:
                        for row in cur:
                            obj = dict(zip(columns, row))
                            line = json.dumps(
                                obj, ensure_ascii=False, default=json_default, separators=(",", ":")
                            ) + "\n"
                            out.write(line.encode("utf-8"))
                            count += 1
                    manifest["tables"][table] = {"rows": count}
            except Exception as exc:
                conn.rollback()
                manifest["tables"][table] = {"error": str(exc)}

        zf.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2, default=json_default),
        )

    filename = f"football_big5_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.zip"
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(remove_file, zip_path),
    )
