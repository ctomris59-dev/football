#!/usr/bin/env python3
"""Isolated research controllers.

These endpoints are diagnostic only. No import-time production refresh is allowed.
The incremental-feature audit can be run once on startup only when the explicit
RUN_INCREMENTAL_FEATURE_AUDIT_ONCE flag is enabled; its runner is DB-idempotent.
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
RUN_INCREMENTAL_FEATURE_AUDIT_ONCE = os.getenv(
    "RUN_INCREMENTAL_FEATURE_AUDIT_ONCE", "false"
).strip().lower() in {"1", "true", "yes"}

router = APIRouter()
log = logging.getLogger("research-controller")

_over25_lock = threading.Lock()
_over25_state: dict[str, Any] = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "error": None,
    "result": None,
}
_incremental_lock = threading.Lock()
_incremental_state: dict[str, Any] = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "error": None,
    "result": None,
}


def _run_over25() -> None:
    if not _over25_lock.acquire(blocking=False):
        return
    _over25_state.update(
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        error=None,
        result=None,
    )
    try:
        from over25_sensitivity_audit import run

        result = run(DATABASE_URL)
        _over25_state["result"] = {
            "version": result.get("version"),
            "candidate_rows": result.get("candidate_rows"),
            "folds": result.get("folds"),
            "sensitivity": result.get("sensitivity"),
        }
        _over25_state["status"] = "success"
    except Exception as exc:
        _over25_state["status"] = "failed"
        _over25_state["error"] = str(exc)[:2000]
        log.exception("OVER25_RESEARCH_CONTROLLER_FAILED")
    finally:
        _over25_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        _over25_lock.release()


def _ensure_over25_started() -> None:
    if DATABASE_URL and _over25_state["status"] == "idle" and not _over25_lock.locked():
        threading.Thread(target=_run_over25, name="over25-isolated-audit", daemon=True).start()


def _run_incremental() -> None:
    if not _incremental_lock.acquire(blocking=False):
        return
    _incremental_state.update(
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        error=None,
        result=None,
    )
    try:
        from incremental_feature_audit_once import run

        result = run(DATABASE_URL)
        _incremental_state["result"] = result
        _incremental_state["status"] = "success"
        log.info("INCREMENTAL_FEATURE_CONTROLLER_COMPLETED %s", result)
    except Exception as exc:
        _incremental_state["status"] = "failed"
        _incremental_state["error"] = str(exc)[:2000]
        log.exception("INCREMENTAL_FEATURE_CONTROLLER_FAILED")
    finally:
        _incremental_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        _incremental_lock.release()


def _ensure_incremental_started() -> None:
    if DATABASE_URL and _incremental_state["status"] == "idle" and not _incremental_lock.locked():
        threading.Thread(target=_run_incremental, name="incremental-feature-audit", daemon=True).start()


# Explicit flag only. This replaces the old unconditional import-time current-week
# migration, which could unexpectedly re-run data refresh work on every web restart.
if DATABASE_URL and RUN_INCREMENTAL_FEATURE_AUDIT_ONCE:
    _ensure_incremental_started()


@router.get(f"/__research/over25/{TOKEN}/start")
def start_over25():
    if not DATABASE_URL:
        return {"ok": False, "status": "failed", "error": "DATABASE_URL missing"}
    _ensure_over25_started()
    return {"ok": True, "status": _over25_state["status"]}


@router.get(f"/__research/over25/{TOKEN}/status")
def status_over25():
    return {"ok": True, **_over25_state}


@router.get(f"/__research/incremental/{TOKEN}/start")
def start_incremental():
    if not DATABASE_URL:
        return {"ok": False, "status": "failed", "error": "DATABASE_URL missing"}
    _ensure_incremental_started()
    return {"ok": True, "status": _incremental_state["status"]}


@router.get(f"/__research/incremental/{TOKEN}/status")
def status_incremental():
    return {"ok": True, **_incremental_state}
