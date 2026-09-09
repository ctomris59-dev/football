#!/usr/bin/env python3
"""Persistent cooldown guard for full football refreshes.

The advisory lock prevents concurrent execution; this guard also prevents multiple
Thursday cron jobs from running the same expensive pipeline sequentially minutes
apart. Failed runs do not suppress retries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

DDL = """
CREATE TABLE IF NOT EXISTS live_refresh_runs(
 id BIGSERIAL PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL,
 trigger_name TEXT,
 message TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_refresh_runs_recent
 ON live_refresh_runs(status,started_at DESC);
"""


def begin(conn, min_interval_minutes: float, force: bool = False, trigger_name: str = "scheduled") -> Tuple[Optional[int], Optional[dict]]:
    conn.execute(DDL)
    if not force and min_interval_minutes > 0:
        recent = conn.execute(
            """SELECT id,started_at,status FROM live_refresh_runs
               WHERE status IN ('success','running')
                 AND started_at>=NOW()-(%s||' minutes')::interval
               ORDER BY started_at DESC LIMIT 1""",
            (float(min_interval_minutes),),
        ).fetchone()
        if recent:
            return None, {
                "status": "skipped_recent_refresh",
                "reason": "full_refresh_cooldown",
                "recent_run_id": int(recent[0]),
                "recent_started_at": recent[1],
                "recent_status": recent[2],
                "min_interval_minutes": float(min_interval_minutes),
                "at": datetime.now(timezone.utc),
            }
    row = conn.execute(
        "INSERT INTO live_refresh_runs(status,trigger_name) VALUES('running',%s) RETURNING id",
        (trigger_name,),
    ).fetchone()
    return int(row[0]), None


def finish(conn, run_id: int, status: str, message: str = "") -> None:
    conn.execute(
        "UPDATE live_refresh_runs SET finished_at=NOW(),status=%s,message=%s WHERE id=%s",
        (status, str(message)[:2000], int(run_id)),
    )
