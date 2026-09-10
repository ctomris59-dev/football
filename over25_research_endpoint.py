#!/usr/bin/env python3
"""Temporary research controller plus one idempotent current-week migration hook.

The Over 2.5 controller remains isolated. During this deployment only, importing this
module also starts the multi-line corner upgrade in a daemon thread. The upgrade is
idempotent in the weekly payload, so replacement instances safely return
``already_upgraded`` after the first successful write.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

TOKEN = "7c61b34f9e2a4f6d8b5c"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
router = APIRouter()
log = logging.getLogger("temporary-research-controller")
_lock = threading.Lock()
_state: dict[str, Any] = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "error": None,
    "result": None,
}


def _run() -> None:
    if not _lock.acquire(blocking=False):
        return
    _state.update({
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "error": None,
        "result": None,
    })
    try:
        from over25_sensitivity_audit import run
        result = run(DATABASE_URL)
        _state["result"] = {
            "version": result.get("version"),
            "candidate_rows": result.get("candidate_rows"),
            "folds": result.get("folds"),
            "sensitivity": result.get("sensitivity"),
        }
        _state["status"] = "success"
    except Exception as exc:
        _state["status"] = "failed"
        _state["error"] = str(exc)[:2000]
    finally:
        _state["finished_at"] = datetime.now(timezone.utc).isoformat()
        _lock.release()


def _ensure_started() -> None:
    if DATABASE_URL and _state["status"] == "idle" and not _lock.locked():
        threading.Thread(target=_run, name="over25-isolated-audit", daemon=True).start()


def _upgrade_current_week_multiline_corners() -> None:
    if not DATABASE_URL:
        return
    try:
        from multiline_corner_upgrade import upgrade_final
        result = upgrade_final(DATABASE_URL)
        log.info("CURRENT_WEEK_MULTILINE_CORNER_BOOTSTRAP %s", result)
    except Exception:
        log.exception("CURRENT_WEEK_MULTILINE_CORNER_BOOTSTRAP_FAILED")


# One current-week migration hook. Safe on replacement instances because
# multiline_corner_upgrade.upgrade_final() is idempotent after a successful write.
if DATABASE_URL:
    threading.Thread(
        target=_upgrade_current_week_multiline_corners,
        name="current-week-multiline-corners",
        daemon=True,
    ).start()


@router.get(f"/__research/over25/{TOKEN}/start")
def start():
    if not DATABASE_URL:
        return {"ok": False, "status": "failed", "error": "DATABASE_URL missing"}
    _ensure_started()
    return {"ok": True, "status": _state["status"]}


@router.get(f"/__research/over25/{TOKEN}/status")
def status():
    _ensure_started()
    return {"ok": True, **_state}
