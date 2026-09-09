#!/usr/bin/env python3
"""Ordered live refresh pipeline for the Big Five prediction system.

Provider calls are freshness-gated. Optional providers fail soft; deterministic
feature/readiness transforms fail closed so stale or malformed context cannot be
silently promoted into the weekly Top-10.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
RUN_FD2324 = os.getenv("LIVE_REFRESH_FD2324", "true").lower() in {"1", "true", "yes"}
RUN_FOOTBALL_DATA = os.getenv("LIVE_REFRESH_FOOTBALL_DATA", "true").lower() in {"1", "true", "yes"}
RUN_ESPN = os.getenv("LIVE_REFRESH_ESPN", "true").lower() in {"1", "true", "yes"}
RUN_ESPN_CONTEXT = os.getenv("LIVE_REFRESH_ESPN_CONTEXT", "true").lower() in {"1", "true", "yes"}
RUN_ESPN_TEAM_SCHEDULE = os.getenv("LIVE_REFRESH_ESPN_TEAM_SCHEDULE", "true").lower() in {"1", "true", "yes"}
RUN_UNDERSTAT = os.getenv("LIVE_REFRESH_UNDERSTAT", "true").lower() in {"1", "true", "yes"}
RUN_ODDSPAPI = os.getenv("LIVE_REFRESH_ODDSPAPI", "true").lower() in {"1", "true", "yes"}
RUN_FOTMOB_AVAILABILITY = os.getenv("LIVE_REFRESH_FOTMOB_AVAILABILITY", "true").lower() in {"1", "true", "yes"}
RUN_BBS = os.getenv("LIVE_REFRESH_BBS", "false").lower() in {"1", "true", "yes"}
# Match lineups are only useful shortly before kickoff and cost many calls; disabled in the standard refresh.
RUN_BBS_LINEUPS = os.getenv("LIVE_REFRESH_BBS_LINEUPS", "false").lower() in {"1", "true", "yes"}
# Render is currently blocked by Sofascore (403); keep it as an explicit opt-in diagnostic source.
RUN_SOFASCORE = os.getenv("LIVE_REFRESH_SOFASCORE", "false").lower() in {"1", "true", "yes"}
RUN_ADVANCED = os.getenv("LIVE_REFRESH_ADVANCED", "true").lower() in {"1", "true", "yes"}
RUN_PREMATCH = os.getenv("LIVE_REFRESH_PREMATCH", "true").lower() in {"1", "true", "yes"}
RUN_ODDS_MOVEMENT = os.getenv("LIVE_REFRESH_ODDS_MOVEMENT", "true").lower() in {"1", "true", "yes"}
RUN_AVAILABILITY_ENRICH = os.getenv("LIVE_REFRESH_AVAILABILITY_ENRICH", "true").lower() in {"1", "true", "yes"}
RUN_READINESS = os.getenv("LIVE_REFRESH_READINESS", "true").lower() in {"1", "true", "yes"}
RUN_PREDICTIONS = os.getenv("LIVE_REFRESH_PREDICTIONS", "true").lower() in {"1", "true", "yes"}

ESPN_CONTEXT_REFRESH_HOURS = float(os.getenv("ESPN_CONTEXT_REFRESH_HOURS", "20"))
TEAM_SCHEDULE_REFRESH_HOURS = float(os.getenv("TEAM_SCHEDULE_REFRESH_HOURS", "120"))
UNDERSTAT_REFRESH_HOURS = float(os.getenv("UNDERSTAT_REFRESH_HOURS", "48"))
ODDSPAPI_REFRESH_HOURS = float(os.getenv("ODDSPAPI_REFRESH_HOURS", "20"))
FOTMOB_REFRESH_HOURS = float(os.getenv("FOTMOB_REFRESH_HOURS", "20"))

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("live-refresh")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_step(name: str, fn: Callable[[], Any], summary: Dict[str, Any], *, optional: bool = False) -> None:
    try:
        result = fn()
        summary[name] = {"status": "ok", "result": result}
        log.info("LIVE_REFRESH_STEP step=%s status=ok result=%s", name, result)
    except Exception as exc:
        msg = str(exc)[:1000]
        summary[name] = {"status": "failed", "error": msg}
        if optional:
            log.warning("LIVE_REFRESH_STEP step=%s status=failed_optional error=%s", name, msg)
        else:
            log.exception("LIVE_REFRESH_STEP step=%s status=failed", name)
            raise


def recent_success(table: str, hours: float) -> bool:
    if not DATABASE_URL or hours <= 0:
        return False
    allowed = {
        "espn_context_runs", "espn_team_schedule_runs", "oddspapi_import_runs", "oddspapi_allbooks_runs",
        "fotmob_availability_runs", "understat_import_runs",
    }
    if table not in allowed:
        return False
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute(
                f"SELECT 1 FROM {table} WHERE status='success' AND finished_at >= NOW()-(%s||' hours')::interval LIMIT 1",
                (hours,),
            ).fetchone()
        return bool(row)
    except Exception:
        return False


def skip(steps: Dict[str, Any], name: str, reason: str) -> None:
    steps[name] = {"status": "skipped", "reason": reason}
    log.info("LIVE_REFRESH_STEP step=%s status=skipped reason=%s", name, reason)


def main() -> Dict[str, Any]:
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
    started = utcnow()
    summary: Dict[str, Any] = {"started_at": started.isoformat(), "steps": {}}
    steps = summary["steps"]

    if RUN_FD2324:
        from football_data_2324_importer import run_import as fn
        run_step("football_data_2324", lambda: fn(DATABASE_URL), steps, optional=True)
    if RUN_FOOTBALL_DATA:
        from football_data_mirror_importer import run_import as fn
        run_step("football_data", lambda: fn(DATABASE_URL), steps, optional=True)
    if RUN_ESPN:
        from espn_current_importer import run_import as fn
        run_step("espn_current", lambda: fn(DATABASE_URL), steps)

    if RUN_ESPN_CONTEXT:
        if recent_success("espn_context_runs", ESPN_CONTEXT_REFRESH_HOURS):
            skip(steps, "espn_context", f"fresh<{ESPN_CONTEXT_REFRESH_HOURS}h")
        else:
            from espn_prematch_refresh import run_import as fn
            run_step("espn_context", lambda: fn(DATABASE_URL), steps, optional=True)

    if RUN_ESPN_TEAM_SCHEDULE:
        if recent_success("espn_team_schedule_runs", TEAM_SCHEDULE_REFRESH_HOURS):
            skip(steps, "espn_team_schedule", f"fresh<{TEAM_SCHEDULE_REFRESH_HOURS}h")
        else:
            from espn_team_schedule_importer import run_import as fn
            run_step("espn_team_schedule", lambda: fn(DATABASE_URL), steps, optional=True)

    if RUN_UNDERSTAT:
        if recent_success("understat_import_runs", UNDERSTAT_REFRESH_HOURS):
            skip(steps, "understat", f"fresh<{UNDERSTAT_REFRESH_HOURS}h")
        else:
            from understat_xg_importer import run_import as fn
            run_step("understat", lambda: fn(DATABASE_URL), steps, optional=True)

    if RUN_ODDSPAPI:
        if recent_success("oddspapi_allbooks_runs", ODDSPAPI_REFRESH_HOURS):
            skip(steps, "oddspapi_allbooks", f"fresh<{ODDSPAPI_REFRESH_HOURS}h")
        else:
            from oddspapi_allbooks_importer_v2 import run_import as fn
            run_step("oddspapi_allbooks", lambda: fn(DATABASE_URL), steps, optional=True)

    if RUN_FOTMOB_AVAILABILITY:
        if recent_success("fotmob_availability_runs", FOTMOB_REFRESH_HOURS):
            skip(steps, "fotmob_availability", f"fresh<{FOTMOB_REFRESH_HOURS}h")
        else:
            from fotmob_availability_importer import run_import as fn
            run_step("fotmob_availability", lambda: fn(DATABASE_URL), steps, optional=True)

    if RUN_BBS:
        from bbs_availability_canonical import run_import as fn
        run_step("bbs_availability", lambda: fn(DATABASE_URL), steps, optional=True)
    if RUN_BBS_LINEUPS:
        from bbs_lineups_importer import run_import as fn
        run_step("bbs_lineups", lambda: fn(DATABASE_URL), steps, optional=True)
    else:
        skip(steps, "bbs_lineups", "disabled in standard refresh; use near-kickoff only")
    if RUN_SOFASCORE:
        from sofascore_availability_www import run_import as fn
        run_step("sofascore_availability", lambda: fn(DATABASE_URL), steps, optional=True)
    else:
        skip(steps, "sofascore_availability", "disabled after persistent Render 403")

    if RUN_ADVANCED:
        from advanced_features_pipeline_v2 import run as fn
        run_step("advanced_features", lambda: fn(DATABASE_URL), steps)

    if RUN_PREMATCH:
        from prematch_context_builder_fixed import run_build as fn
        run_step("prematch_context", lambda: fn(DATABASE_URL), steps)
    if RUN_ODDS_MOVEMENT:
        from odds_movement_enricher import run_enrich as fn
        run_step("odds_movement", lambda: fn(DATABASE_URL), steps, optional=True)
    if RUN_AVAILABILITY_ENRICH:
        from availability_enricher_v3 import run_enrich as fn
        run_step("availability_enrich", lambda: fn(DATABASE_URL), steps)
    if RUN_READINESS:
        from data_readiness_audit_v4 import run_audit as fn
        run_step("data_readiness", lambda: fn(DATABASE_URL), steps)
    if RUN_PREDICTIONS:
        from production_predictor_v3 import run_predictions as fn
        run_step("production_predictions", lambda: fn(DATABASE_URL), steps)

    summary["finished_at"] = utcnow().isoformat()
    summary["status"] = "success"
    log.info("LIVE_REFRESH_RESULT %s", json.dumps(summary, ensure_ascii=False, default=str, separators=(",", ":")))
    return summary


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2, default=str))
