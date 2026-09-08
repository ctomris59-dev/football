#!/usr/bin/env python3
"""Ordered live refresh pipeline for the Big Five prediction system.

Runs data producers sequentially so downstream pre-match features are built only
after the freshest available fixture, lineup/context, xG, schedule, availability
and market snapshots are stored. Optional providers fail soft and are reported.
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
RUN_BBS = os.getenv("LIVE_REFRESH_BBS", "true").lower() in {"1", "true", "yes"}
RUN_BBS_LINEUPS = os.getenv("LIVE_REFRESH_BBS_LINEUPS", "true").lower() in {"1", "true", "yes"}
RUN_SOFASCORE = os.getenv("LIVE_REFRESH_SOFASCORE", "true").lower() in {"1", "true", "yes"}
RUN_PREMATCH = os.getenv("LIVE_REFRESH_PREMATCH", "true").lower() in {"1", "true", "yes"}
RUN_AVAILABILITY_ENRICH = os.getenv("LIVE_REFRESH_AVAILABILITY_ENRICH", "true").lower() in {"1", "true", "yes"}
RUN_READINESS = os.getenv("LIVE_REFRESH_READINESS", "true").lower() in {"1", "true", "yes"}

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("live-refresh")

def utcnow() -> datetime:return datetime.now(timezone.utc)

def run_step(name: str, fn: Callable[[], Any], summary: Dict[str, Any], *, optional: bool = False) -> None:
    try:
        result = fn(); summary[name] = {"status":"ok","result":result}; log.info("LIVE_REFRESH_STEP step=%s status=ok result=%s",name,result)
    except Exception as exc:
        summary[name] = {"status":"failed","error":str(exc)}
        if optional: log.warning("LIVE_REFRESH_STEP step=%s status=failed_optional error=%s",name,exc)
        else: log.exception("LIVE_REFRESH_STEP step=%s status=failed",name); raise

def oddspapi_fresh_this_hour() -> bool:
    if not DATABASE_URL:return False
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            row=conn.execute("SELECT 1 FROM oddspapi_import_runs WHERE status='success' AND finished_at>=date_trunc('hour',NOW()) LIMIT 1").fetchone()
        return bool(row)
    except Exception:return False

def main() -> Dict[str, Any]:
    if not DATABASE_URL:raise RuntimeError("Missing DATABASE_URL")
    started=utcnow();summary:Dict[str,Any]={"started_at":started.isoformat(),"steps":{}};steps=summary["steps"]
    if RUN_FD2324:
        from football_data_2324_importer import run_import as fn; run_step("football_data_2324",lambda:fn(DATABASE_URL),steps,optional=True)
    if RUN_FOOTBALL_DATA:
        from football_data_mirror_importer import run_import as fn; run_step("football_data",lambda:fn(DATABASE_URL),steps,optional=True)
    if RUN_ESPN:
        from espn_current_importer import run_import as fn; run_step("espn_current",lambda:fn(DATABASE_URL),steps)
    if RUN_ESPN_CONTEXT:
        from espn_prematch_refresh import run_import as fn; run_step("espn_context",lambda:fn(DATABASE_URL),steps,optional=True)
    if RUN_ESPN_TEAM_SCHEDULE:
        from espn_team_schedule_importer import run_import as fn; run_step("espn_team_schedule",lambda:fn(DATABASE_URL),steps,optional=True)
    if RUN_UNDERSTAT:
        from understat_xg_importer import run_import as fn; run_step("understat",lambda:fn(DATABASE_URL),steps,optional=True)
    if RUN_ODDSPAPI:
        if oddspapi_fresh_this_hour():
            steps["oddspapi"]={"status":"skipped","reason":"successful snapshot already exists this UTC hour"};log.info("LIVE_REFRESH_STEP step=oddspapi status=skipped reason=same_hour_success")
        else:
            from oddspapi_canonical_importer import run_import as fn; run_step("oddspapi",lambda:fn(DATABASE_URL),steps,optional=True)
    if RUN_BBS:
        from bbs_availability_canonical import run_import as fn; run_step("bbs_availability",lambda:fn(DATABASE_URL),steps,optional=True)
    if RUN_BBS_LINEUPS:
        from bbs_lineups_importer import run_import as fn; run_step("bbs_lineups",lambda:fn(DATABASE_URL),steps,optional=True)
    if RUN_SOFASCORE:
        from sofascore_availability_importer import run_import as fn; run_step("sofascore_availability",lambda:fn(DATABASE_URL),steps,optional=True)
    if RUN_PREMATCH:
        from prematch_context_builder_fixed import run_build as fn; run_step("prematch_context",lambda:fn(DATABASE_URL),steps)
    if RUN_AVAILABILITY_ENRICH:
        from availability_enricher import run_enrich as fn; run_step("availability_enrich",lambda:fn(DATABASE_URL),steps,optional=True)
    if RUN_READINESS:
        from data_readiness_audit_v2 import run_audit as fn; run_step("data_readiness",lambda:fn(DATABASE_URL),steps)
    summary["finished_at"]=utcnow().isoformat();summary["status"]="success";log.info("LIVE_REFRESH_RESULT %s",json.dumps(summary,ensure_ascii=False,default=str,separators=(",",":")));return summary

if __name__=="__main__":print(json.dumps(main(),ensure_ascii=False,indent=2,default=str))
