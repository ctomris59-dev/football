#!/usr/bin/env python3
"""Call the protected football refresh endpoint from a small Render cron.

Secrets stay on the web service; scheduled jobs only hold a dedicated bearer token.
The script waits for the background refresh to finish so cron success means the
pipeline really completed, not merely that the HTTP request was accepted.
"""
from __future__ import annotations

import json
import os
import sys
import time

import requests

REFRESH_URL = os.getenv("REMOTE_REFRESH_URL", "").strip()
TOKEN = os.getenv("REMOTE_REFRESH_TOKEN", "").strip()
POLL_SECONDS = int(os.getenv("REMOTE_REFRESH_POLL_SECONDS", "20"))
MAX_WAIT_SECONDS = int(os.getenv("REMOTE_REFRESH_MAX_WAIT_SECONDS", "1800"))


def health_url(refresh_url: str) -> str:
    if refresh_url.endswith("/refresh"):
        return refresh_url[:-8] + "/health"
    return refresh_url.rstrip("/") + "/health"


def main() -> int:
    if not REFRESH_URL or not TOKEN:
        print("REMOTE_REFRESH_SKIPPED missing REMOTE_REFRESH_URL/TOKEN")
        return 2

    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = requests.post(REFRESH_URL, headers=headers, timeout=60)
    print("REMOTE_REFRESH_START", response.status_code, response.text[:1000])
    if response.status_code not in {200, 202}:
        return 3

    try:
        body = response.json()
    except Exception:
        body = {}

    # If another refresh is already running, wait for that one rather than failing.
    accepted = body.get("accepted")
    if accepted is False and body.get("reason") != "refresh_already_running":
        return 4

    deadline = time.time() + MAX_WAIT_SECONDS
    hurl = health_url(REFRESH_URL)
    seen_running = False
    while time.time() < deadline:
        try:
            health = requests.get(hurl, timeout=30).json()
            state = health.get("refresh") or {}
            running = bool(state.get("running"))
            seen_running = seen_running or running
            if not running and (seen_running or state.get("last_finished")):
                status = state.get("last_status")
                print("REMOTE_REFRESH_FINISH", json.dumps(state, ensure_ascii=False, default=str))
                return 0 if status == "success" else 5
        except Exception as exc:
            print("REMOTE_REFRESH_POLL_WARNING", repr(exc))
        time.sleep(max(5, POLL_SECONDS))

    print("REMOTE_REFRESH_TIMEOUT", MAX_WAIT_SECONDS)
    return 6


if __name__ == "__main__":
    sys.exit(main())
