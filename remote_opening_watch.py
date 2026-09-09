#!/usr/bin/env python3
"""Wake the public opening-watch endpoint only during the useful Turkey window."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

ISTANBUL = ZoneInfo("Europe/Istanbul")
URL = os.getenv("OPENING_WATCH_URL", "https://football-dataset-export.onrender.com/opening-watch").strip()
TIMEOUT = float(os.getenv("OPENING_WATCH_HTTP_TIMEOUT", "90"))


def main() -> int:
    local = datetime.now(timezone.utc).astimezone(ISTANBUL)
    useful = (local.weekday() == 3 and local.hour >= 18) or (local.weekday() == 4 and local.hour <= 12)
    if not useful:
        print("REMOTE_OPENING_WATCH_SKIPPED", local.isoformat())
        return 0
    try:
        r = requests.get(URL, timeout=TIMEOUT, headers={"User-Agent": "football-opening-watch-cron/1.0"})
        print("REMOTE_OPENING_WATCH", r.status_code, r.text[:5000])
        return 0 if r.status_code == 200 else 2
    except Exception as exc:
        print("REMOTE_OPENING_WATCH_FAILED", repr(exc))
        return 3


if __name__ == "__main__":
    sys.exit(main())
