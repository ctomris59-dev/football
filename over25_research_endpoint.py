#!/usr/bin/env python3
"""Temporary, research-only controller for one isolated Over 2.5 audit run.

The random route token limits accidental discovery while this short-lived endpoint
is deployed. The audit is read-only with respect to production decisions. Status
polling auto-starts an idle audit so a Render replacement instance cannot strand
the capture workflow in an idle state.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

TOKEN = "7c61b34f9e2a4f6d8b5c"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
router = APIRouter()
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
