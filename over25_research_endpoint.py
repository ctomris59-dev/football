#!/usr/bin/env python3
"""Isolated research controllers.

No unconditional production refresh is allowed. Research audits start only behind
explicit environment flags or diagnostic endpoints and use DB-idempotent runners.
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
RUN_INCREMENTAL_FEATURE_AUDIT_ONCE = os.getenv("RUN_INCREMENTAL_FEATURE_AUDIT_ONCE", "false").strip().lower() in {"1", "true", "yes"}
RUN_LINEUP_V2_AUDIT_ONCE = os.getenv("RUN_LINEUP_V2_AUDIT_ONCE", "false").strip().lower() in {"1", "true", "yes"}
RUN_ADVANCED_GOAL_AUDIT_ONCE = os.getenv("RUN_ADVANCED_GOAL_AUDIT_ONCE", "false").strip().lower() in {"1", "true", "yes"}

router = APIRouter()
log = logging.getLogger("research-controller")


def _state() -> dict[str, Any]:
    return {"status": "idle", "started_at": None, "finished_at": None, "error": None, "result": None}

_over25_lock = threading.Lock(); _over25_state = _state()
_incremental_lock = threading.Lock(); _incremental_state = _state()
_lineup_v2_lock = threading.Lock(); _lineup_v2_state = _state()
_advanced_goal_lock = threading.Lock(); _advanced_goal_state = _state()


def _run_over25() -> None:
    if not _over25_lock.acquire(blocking=False): return
    _over25_state.update(status="running", started_at=datetime.now(timezone.utc).isoformat(), finished_at=None, error=None, result=None)
    try:
        from over25_sensitivity_audit import run
        result = run(DATABASE_URL)
        _over25_state["result"] = {"version": result.get("version"), "candidate_rows": result.get("candidate_rows"), "folds": result.get("folds"), "sensitivity": result.get("sensitivity")}
        _over25_state["status"] = "success"
    except Exception as exc:
        _over25_state["status"] = "failed"; _over25_state["error"] = str(exc)[:2000]; log.exception("OVER25_RESEARCH_CONTROLLER_FAILED")
    finally:
        _over25_state["finished_at"] = datetime.now(timezone.utc).isoformat(); _over25_lock.release()


def _ensure_over25_started() -> None:
    if DATABASE_URL and _over25_state["status"] == "idle" and not _over25_lock.locked():
        threading.Thread(target=_run_over25, name="over25-isolated-audit", daemon=True).start()


def _run_incremental() -> None:
    if not _incremental_lock.acquire(blocking=False): return
    _incremental_state.update(status="running", started_at=datetime.now(timezone.utc).isoformat(), finished_at=None, error=None, result=None)
    try:
        from incremental_feature_audit_once import run
        result = run(DATABASE_URL)
        _incremental_state["result"] = result; _incremental_state["status"] = "success"; log.info("INCREMENTAL_FEATURE_CONTROLLER_COMPLETED %s", result)
    except Exception as exc:
        _incremental_state["status"] = "failed"; _incremental_state["error"] = str(exc)[:2000]; log.exception("INCREMENTAL_FEATURE_CONTROLLER_FAILED")
    finally:
        _incremental_state["finished_at"] = datetime.now(timezone.utc).isoformat(); _incremental_lock.release()


def _ensure_incremental_started() -> None:
    if DATABASE_URL and _incremental_state["status"] == "idle" and not _incremental_lock.locked():
        threading.Thread(target=_run_incremental, name="incremental-feature-audit", daemon=True).start()


def _run_lineup_v2() -> None:
    if not _lineup_v2_lock.acquire(blocking=False): return
    _lineup_v2_state.update(status="running", started_at=datetime.now(timezone.utc).isoformat(), finished_at=None, error=None, result=None)
    try:
        from lineup_stability_v2_once import run
        result = run(DATABASE_URL)
        _lineup_v2_state["result"] = result; _lineup_v2_state["status"] = "success"; log.info("LINEUP_V2_CONTROLLER_COMPLETED %s", result)
    except Exception as exc:
        _lineup_v2_state["status"] = "failed"; _lineup_v2_state["error"] = str(exc)[:2000]; log.exception("LINEUP_V2_CONTROLLER_FAILED")
    finally:
        _lineup_v2_state["finished_at"] = datetime.now(timezone.utc).isoformat(); _lineup_v2_lock.release()


def _ensure_lineup_v2_started() -> None:
    if DATABASE_URL and _lineup_v2_state["status"] == "idle" and not _lineup_v2_lock.locked():
        threading.Thread(target=_run_lineup_v2, name="lineup-stability-v2-audit", daemon=True).start()


def _run_advanced_goal() -> None:
    if not _advanced_goal_lock.acquire(blocking=False): return
    _advanced_goal_state.update(status="running", started_at=datetime.now(timezone.utc).isoformat(), finished_at=None, error=None, result=None)
    try:
        from advanced_goal_audit_once import run
        result = run(DATABASE_URL)
        _advanced_goal_state["result"] = result; _advanced_goal_state["status"] = "success"
        log.info("ADVANCED_GOAL_CONTROLLER_COMPLETED %s", result)
    except Exception as exc:
        _advanced_goal_state["status"] = "failed"; _advanced_goal_state["error"] = str(exc)[:2000]; log.exception("ADVANCED_GOAL_CONTROLLER_FAILED")
    finally:
        _advanced_goal_state["finished_at"] = datetime.now(timezone.utc).isoformat(); _advanced_goal_lock.release()


def _ensure_advanced_goal_started() -> None:
    if DATABASE_URL and _advanced_goal_state["status"] == "idle" and not _advanced_goal_lock.locked():
        threading.Thread(target=_run_advanced_goal, name="advanced-goal-audit", daemon=True).start()


if DATABASE_URL and RUN_INCREMENTAL_FEATURE_AUDIT_ONCE: _ensure_incremental_started()
if DATABASE_URL and RUN_LINEUP_V2_AUDIT_ONCE: _ensure_lineup_v2_started()
if DATABASE_URL and RUN_ADVANCED_GOAL_AUDIT_ONCE: _ensure_advanced_goal_started()


@router.get("/weekly-preview")
def weekly_preview():
    """Fresh operational ranking from current DB state without rewriting the frozen Thursday decision."""
    if not DATABASE_URL:
        return {"ok": False, "status": "failed", "error": "DATABASE_URL missing"}
    from weekly_trusted_predictions import build
    result = build(DATABASE_URL)
    return {
        "ok": True,
        "status": "fresh_non_frozen_preview",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **result,
    }


@router.get("/fixture-context/{event_id}")
def fixture_context(event_id: str):
    """Price-independent current V1 + all-competition schedule/player context for one fixture."""
    if not DATABASE_URL:
        return {"ok": False, "status": "failed", "error": "DATABASE_URL missing"}
    import psycopg
    from model_engine_v1 import predict_match
    from production_predictor import canon
    from schedule_context import SCHEDULE_CONTEXT_VERSION, team_schedule_context
    from thursday_decision_engine import _early_gate, _history_rows, _last_rest_days, _player_context

    as_of = datetime.now(timezone.utc)
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        fixture = conn.execute(
            """SELECT event_id,match_date,league_name,home_team,away_team
                 FROM espn_upcoming WHERE event_id=%s LIMIT 1""",
            (event_id,),
        ).fetchone()
        if not fixture:
            return {"ok": False, "status": "not_found", "event_id": event_id}
        eid, match_date, league, home, away = fixture
        history = _history_rows(conn, str(league), match_date)
        pred = predict_match(history, canon(home), canon(away), recent_matches=18)
        home_player_ctx = _player_context(conn, str(home))
        away_player_ctx = _player_context(conn, str(away))
        legacy_home_rest = _last_rest_days(history, str(home), match_date)
        legacy_away_rest = _last_rest_days(history, str(away), match_date)
        home_sched = team_schedule_context(conn, str(home), match_date, as_of=as_of, fallback_rest_days=legacy_home_rest)
        away_sched = team_schedule_context(conn, str(away), match_date, as_of=as_of, fallback_rest_days=legacy_away_rest)
        eligible, blockers, gate_diag = _early_gate(
            pred, home_player_ctx, away_player_ctx,
            home_sched.get("rest_days"), away_sched.get("rest_days")
        )
        schedule_factor = min(float(home_sched.get("rank_factor") or 0.0), float(away_sched.get("rank_factor") or 0.0))
        return {
            "ok": True,
            "status": "success",
            "event_id": str(eid),
            "match_date": match_date,
            "league": str(league),
            "home": str(home),
            "away": str(away),
            "v1": {
                "p_over_2_5": float(pred.p_over_2_5),
                "p_btts": float(pred.p_btts),
                "p_corners_over_8_5": float(pred.p_corners_over_8_5),
            },
            "eligible": bool(eligible),
            "blockers": list(blockers),
            "model_data_quality": gate_diag.get("model_data_quality"),
            "player_coverage": gate_diag.get("player_coverage"),
            "min_starter_continuity": gate_diag.get("min_starter_continuity"),
            "max_known_injury_impact": gate_diag.get("max_known_injury_impact"),
            "schedule_context_version": SCHEDULE_CONTEXT_VERSION,
            "schedule_rank_factor": schedule_factor,
            "home_schedule": home_sched,
            "away_schedule": away_sched,
            "generated_at": as_of.isoformat(),
        }


@router.get(f"/__research/over25/{TOKEN}/start")
def start_over25():
    if not DATABASE_URL: return {"ok": False, "status": "failed", "error": "DATABASE_URL missing"}
    _ensure_over25_started(); return {"ok": True, "status": _over25_state["status"]}

@router.get(f"/__research/over25/{TOKEN}/status")
def status_over25(): return {"ok": True, **_over25_state}

@router.get(f"/__research/incremental/{TOKEN}/start")
def start_incremental():
    if not DATABASE_URL: return {"ok": False, "status": "failed", "error": "DATABASE_URL missing"}
    _ensure_incremental_started(); return {"ok": True, "status": _incremental_state["status"]}

@router.get(f"/__research/incremental/{TOKEN}/status")
def status_incremental(): return {"ok": True, **_incremental_state}

@router.get(f"/__research/lineup-v2/{TOKEN}/start")
def start_lineup_v2():
    if not DATABASE_URL: return {"ok": False, "status": "failed", "error": "DATABASE_URL missing"}
    _ensure_lineup_v2_started(); return {"ok": True, "status": _lineup_v2_state["status"]}

@router.get(f"/__research/lineup-v2/{TOKEN}/status")
def status_lineup_v2(): return {"ok": True, **_lineup_v2_state}

@router.get(f"/__research/advanced-goal/{TOKEN}/start")
def start_advanced_goal():
    if not DATABASE_URL: return {"ok": False, "status": "failed", "error": "DATABASE_URL missing"}
    _ensure_advanced_goal_started(); return {"ok": True, "status": _advanced_goal_state["status"]}

@router.get(f"/__research/advanced-goal/{TOKEN}/status")
def status_advanced_goal(): return {"ok": True, **_advanced_goal_state}
