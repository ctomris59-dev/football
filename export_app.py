#!/usr/bin/env python3
"""
Small protected download service for the PostgreSQL dataset.

GET /health
GET /status?token=...
GET /download?token=...

The ZIP is generated on demand from PostgreSQL. This avoids relying on Render's
ephemeral local disk after a Cron/One-Off run has ended.
"""
from __future__ import annotations

import json
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DOWNLOAD_TOKEN = os.getenv("DOWNLOAD_TOKEN", "").strip()

app = FastAPI(title="Football Dataset Export", version="1.0")

TABLES = [
    "league_coverage",
    "fixtures",
    "fixture_details",
    "injuries",
    "season_players",
    "collection_runs",
    "api_call_log",
]


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
        last_run = conn.execute(
            """
            SELECT run_id::text, started_at, finished_at, status, api_calls, message
            FROM collection_runs ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
    return {
        "counts": out,
        "last_run": list(last_run) if last_run else None,
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
