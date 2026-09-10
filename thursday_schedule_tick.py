#!/usr/bin/env python3
"""Scheduled trigger for the Thursday betting workflow.

This script is intentionally stateless and secret-free. A Render cron runs it every
15 minutes on Thursday/Friday. It only acts inside the Istanbul decision window:
Thursday 18:00 through Friday 12:00. If the week is already finalized it exits
without calling /opening-watch again.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import requests

ISTANBUL = ZoneInfo("Europe/Istanbul")
SERVICE_BASE_URL = os.getenv(
    "THURSDAY_SERVICE_BASE_URL",
    "https://football-dataset-export.onrender.com",
).rstrip("/")
HTTP_TIMEOUT_SECONDS = int(os.getenv("THURSDAY_SCHEDULER_HTTP_TIMEOUT_SECONDS", "120"))


def in_decision_window(now: datetime | None = None) -> bool:
    local = (now or datetime.now(timezone.utc)).astimezone(ISTANBUL)
    # Monday=0 ... Thursday=3, Friday=4
    if local.weekday() == 3 and local.time() >= time(18, 0):
        return True
    if local.weekday() == 4 and local.time() <= time(12, 0):
        return True
    return False


def get_json(path: str) -> dict:
    response = requests.get(
        f"{SERVICE_BASE_URL}{path}",
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": "football-thursday-scheduler/1.0"},
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    now = datetime.now(timezone.utc)
    local = now.astimezone(ISTANBUL)
    if not in_decision_window(now):
        print("THURSDAY_SCHEDULER_SKIP outside_window", local.isoformat(), flush=True)
        return 0

    try:
        current = get_json("/thursday-list")
    except Exception as exc:
        print("THURSDAY_SCHEDULER_ERROR final_status", repr(exc), flush=True)
        return 2

    if current.get("status") == "finalized":
        print(
            "THURSDAY_SCHEDULER_DONE already_finalized",
            current.get("week_key"),
            current.get("finalized_at"),
            flush=True,
        )
        return 0

    try:
        result = get_json("/opening-watch")
    except Exception as exc:
        print("THURSDAY_SCHEDULER_ERROR opening_watch", repr(exc), flush=True)
        return 3

    print(
        "THURSDAY_SCHEDULER_RESULT",
        json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")),
        flush=True,
    )
    if result.get("ok") is False:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
