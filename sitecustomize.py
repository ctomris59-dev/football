"""Temporary one-shot launcher for the 2026-09-10 Top-N sensitivity audit.

Python imports sitecustomize automatically. This is strictly date/runtime gated and
runs only the research-only audit; it does not change the frozen weekly prediction.
Remove immediately after the result is captured.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

ISTANBUL = ZoneInfo("Europe/Istanbul")
RUN_DATE = date(2026, 9, 10)


def _eligible() -> bool:
    return bool(
        os.getenv("PORT")
        and os.getenv("DATABASE_URL")
        and datetime.now(ISTANBUL).date() == RUN_DATE
    )


def _worker() -> None:
    time.sleep(4)
    try:
        from topn_sensitivity_audit import run
        run(os.getenv("DATABASE_URL", ""))
    except Exception as exc:
        try:
            import logging
            logging.getLogger("topn-sensitivity-once").exception(
                "TOPN_SENSITIVITY_ONCE_FAILED: %s", exc
            )
        except Exception:
            pass


if _eligible():
    threading.Thread(target=_worker, name="topn-sensitivity-once", daemon=True).start()
