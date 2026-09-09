#!/usr/bin/env python3
"""Ordered live refresh pipeline with cross-process serialization.

Provider calls are freshness/coverage-gated. Optional sources fail soft; core
mapping/readiness fails closed. A PostgreSQL advisory lock prevents duplicate cron or
manual refreshes from consuming providers concurrently.
"""
from __future__ import annotations
import json, logging, os
from datetime import datetime, timezone
from typing import Any, Callable, Dict
import psycopg
DATABASE_URL=os.getenv("DATABASE_URL","").strip();LOG_LEVEL=os.getenv("LOG_LEVEL","INFO").upper();REFRESH_LOCK_KEY=int(os.getenv("LIVE_REFRESH_ADVISORY_LOCK_KEY","856420261"))
def envb(k,d="true"):return os.getenv(k,d).lower() in {"1","true","yes"}
RUN_FD2324=envb("LIVE_REFRESH_FD2324");RUN_FOOTBALL_DATA=envb("LIVE_REFRESH_FOOTBALL_DATA");RUN_FD_ASIAN=envb("LIVE_REFRESH_FD_ASIAN");RUN_ESPN=envb("LIVE_REFRESH_ESPN");RUN_ESPN_CONTEXT=envb("LIVE_REFRESH_ESPN_CONTEXT");RUN_ESPN_TOTAL_ODDS=envb("LIVE_REFRESH_ESPN_TOTAL_ODDS");RUN_ESPN_TEAM_SCHEDULE=envb("LIVE_REFRESH_ESPN_TEAM_SCHEDULE");RUN_UNDERSTAT=envb("LIVE_REFRESH_UNDERSTAT");RUN_ODDSPAPI=envb("LIVE_REFRESH_ODDSPAPI");RUN_FOTMOB_AVAILABILITY=envb("LIVE_REFRESH_FOTMOB_AVAILABILITY");RUN_FOTMOB_LINEUPS=envb("LIVE_REFRESH_FOTMOB_LINEUPS");RUN_BBS=envb("LIVE_REFRESH_BBS","false");RUN_BBS_LINEUPS=envb("LIVE_REFRESH_BBS_LINEUPS","false");RUN_SOFASCORE=envb("LIVE_REFRESH_SOFASCORE","false");RUN_ADVANCED=envb("LIVE_REFRESH_ADVANCED");RUN_FOUR_LAYER=envb("LIVE_REFRESH_FOUR_LAYER");RUN_PREMATCH=envb("LIVE_REFRESH_PREMATCH");RUN_ODDS_MOVEMENT=envb("LIVE_REFRESH_ODDS_MOVEMENT");RUN_AVAILABILITY_ENRICH=envb("LIVE_REFRESH_AVAILABILITY_ENRICH");RUN_READINESS=envb("LIVE_REFRESH_READINESS");RUN_PREDICTIONS=envb("LIVE_REFRESH_PREDICTIONS");RUN_TURKEY_LISTS=envb("LIVE_REFRESH_TURKEY_LISTS","true")
ESPN_CONTEXT_REFRESH_HOURS=float(os.getenv("ESPN_CONTEXT_REFRESH_HOURS","20"));TEAM_SCHEDULE_REFRESH_HOURS=float(os.getenv("TEAM_SCHEDULE_REFRESH_HOURS","120"));UNDERSTAT_REFRESH_HOURS=float(os.getenv("UNDERSTAT_REFRESH_HOURS","48"));ODDSPAPI_REFRESH_HOURS=float(os.getenv("ODDSPAPI_REFRESH_HOURS","20"));FOTMOB_REFRESH_HOURS=float(os.getenv("FOTMOB_REFRESH_HOURS","12"));FOTMOB_LINEUP_REFRESH_HOURS=float(os.getenv("FOTMOB_LINEUP_REFRESH_HOURS","0.5"));FOTMOB_MIN_ACTIVE_COVERAGE=float(os.getenv("FOTMOB_MIN_ACTIVE_COVERAGE","0.85"))
logging.basicConfig(level=getattr(logging,LOG_LEVEL,logging.INFO),format="%(asctime)s | %(levelname)s | %(message)s");log=logging.getLogger("live-refresh")
def utcnow():return datetime.now(timezone.utc)
def run_step(name:str,fn:Callable[[],Any],summary:Dict[str,Any],*,optional:bool=False)->None:
 try:result=fn();summary[name]={"status":"ok","result":result};log.info("LIVE_REFRESH_STEP step=%s status=ok result=%s",name,result)
 except Exception as exc:
  msg=str(exc)[:1000];summary[name]={"status":"failed","error":msg}
  if optional:log.warning("LIVE_REFRESH_STEP step=%s status=failed_optional error=%s",name,msg)
  else:log.exception("LIVE_REFRESH_STEP step=%s status=failed",name);raise
def recent_success(table:str,hours:float)->bool:
 if not DATABASE_URL or hours<=0:return False
 allowed={"espn_context_runs","espn_team_schedule_runs","oddspapi_import_runs","oddspapi_allbooks_runs","fotmob_lineup_runs","understat_import_runs"}
 if table not in allowed:return False
 try:
  with psycopg.connect(DATABASE_URL) as conn:return bool(conn.execute(f"SELECT 1 FROM {table} WHERE status='success' AND finished_at>=NOW()-(%s||' hours')::interval LIMIT 1",(hours,)).fetchone())
 except Exception:return False
def recent_rows(table:str,hours:float)->bool:
 if table not in {"asian_market_prices"} or not DATABASE_URL:return False
 try:
  with psycopg.connect(DATABASE_URL) as conn:return bool(conn.execute(f"SELECT 1 FROM {table} WHERE fetched_at>=NOW()-(%s||' hours')::interval LIMIT 1",(hours,)).fetchone())
 except Exception:return False
def fotmob_availability_fresh()->tuple[bool,Dict[str,Any]]:
 if not DATABASE_URL or FOTMOB_REFRESH_HOURS<=0:return False,{"reason":"disabled freshness"}
 try:
  with psycopg.connect(DATABASE_URL) as conn:
   row=conn.execute("""WITH active AS (SELECT event_id FROM espn_upcoming WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+INTERVAL '8 days'), covered AS (SELECT DISTINCT f.espn_event_id FROM fotmob_fixture_availability_snapshots f JOIN active a ON a.event_id=f.espn_event_id WHERE f.fetched_at>=NOW()-(%s||' hours')::interval) SELECT (SELECT COUNT(*) FROM active),(SELECT COUNT(*) FROM covered)""",(FOTMOB_REFRESH_HOURS,)).fetchone()
  total,covered=int(row[0] or 0),int(row[1] or 0);rate=covered/total if total else 0.0;meta={"active":total,"fresh_covered":covered,"rate":round(rate,4),"max_age_hours":FOTMOB_REFRESH_HOURS,"min_rate":FOTMOB_MIN_ACTIVE_COVERAGE};return bool(total>0 and rate>=FOTMOB_MIN_ACTIVE_COVERAGE),meta
 except Exception as exc:return False,{"error":str(exc)[:300]}
def skip(steps,name,reason):steps[name]={"status":"skipped","reason":reason};log.info("LIVE_REFRESH_STEP step=%s status=skipped reason=%s",name,reason)
def _run()->Dict[str,Any]:
 started=utcnow();summary={"started_at":started.isoformat(),"steps":{}};steps=summary["steps"]
 if RUN_FD2324:
  from football_data_2324_importer import run_import as fn;run_step("football_data_2324",lambda:fn(DATABASE_URL),steps,optional=True)
 if RUN_FOOTBALL_DATA:
  from football_data_mirror_importer import run_import as fn;run_step("football_data",lambda:fn(DATABASE_URL),steps,optional=True)
 if RUN_ESPN:
  from espn_current_importer import run_import as fn;run_step("espn_current",lambda:fn(DATABASE_URL),steps)
 if RUN_ESPN_CONTEXT:
  if recent_success("espn_context_runs",ESPN_CONTEXT_REFRESH_HOURS):skip(steps,"espn_context",f"fresh<{ESPN_CONTEXT_REFRESH_HOURS}h")
  else:
   from espn_prematch_refresh import run_import as fn;run_step("espn_context",lambda:fn(DATABASE_URL),steps,optional=True)
 if RUN_ESPN_TOTAL_ODDS:
  from espn_total_odds_bridge import run_import as fn;run_step("espn_total_odds",lambda:fn(DATABASE_URL),steps,optional=True)
 if RUN_ESPN_TEAM_SCHEDULE:
  if recent_success("espn_team_schedule_runs",TEAM_SCHEDULE_REFRESH_HOURS):skip(steps,"espn_team_schedule",f"fresh<{TEAM_SCHEDULE_REFRESH_HOURS}h")
  else:
   from espn_team_schedule_importer import run_import as fn;run_step("espn_team_schedule",lambda:fn(DATABASE_URL),steps,optional=True)
 if RUN_UNDERSTAT:
  if recent_success("understat_import_runs",UNDERSTAT_REFRESH_HOURS):skip(steps,"understat",f"fresh<{UNDERSTAT_REFRESH_HOURS}h")
  else:
   from understat_xg_importer import run_import as fn;run_step("understat",lambda:fn(DATABASE_URL),steps,optional=True)
 if RUN_ODDSPAPI:
  if recent_success("oddspapi_allbooks_runs",ODDSPAPI_REFRESH_HOURS) and recent_rows("asian_market_prices",ODDSPAPI_REFRESH_HOURS):skip(steps,"oddspapi_allbooks",f"allbooks+asian fresh<{ODDSPAPI_REFRESH_HOURS}h")
  else:
   from oddspapi_allbooks_importer_v4 import run_import as fn;run_step("oddspapi_allbooks",lambda:fn(DATABASE_URL),steps,optional=True)
 if RUN_FD_ASIAN:
  from football_data_asian_bridge_v2 import run_import as fn;run_step("football_data_asian",lambda:fn(DATABASE_URL),steps,optional=True)
 if RUN_FOTMOB_AVAILABILITY:
  fresh,meta=fotmob_availability_fresh()
  if fresh:skip(steps,"fotmob_availability",f"active coverage fresh {meta}")
  else:
   log.info("FOTMOB_AVAILABILITY_REFRESH_REQUIRED %s",json.dumps(meta,separators=(",",":")));from fotmob_availability_importer import run_import as fn;run_step("fotmob_availability",lambda:fn(DATABASE_URL),steps,optional=True)
 if RUN_FOTMOB_LINEUPS:
  if recent_success("fotmob_lineup_runs",FOTMOB_LINEUP_REFRESH_HOURS):skip(steps,"fotmob_lineups",f"fresh<{FOTMOB_LINEUP_REFRESH_HOURS}h")
  else:
   from fotmob_lineups_importer import run_import as fn;run_step("fotmob_lineups",lambda:fn(DATABASE_URL),steps,optional=True)
 if RUN_BBS:
  from bbs_availability_canonical import run_import as fn;run_step("bbs_availability",lambda:fn(DATABASE_URL),steps,optional=True)
 if RUN_BBS_LINEUPS:
  from bbs_lineups_importer import run_import as fn;run_step("bbs_lineups",lambda:fn(DATABASE_URL),steps,optional=True)
 else:skip(steps,"bbs_lineups","disabled in standard refresh; use near-kickoff only")
 if RUN_SOFASCORE:
  from sofascore_availability_www import run_import as fn;run_step("sofascore_availability",lambda:fn(DATABASE_URL),steps,optional=True)
 else:skip(steps,"sofascore_availability","disabled after persistent Render 403")
 if RUN_ADVANCED:
  from advanced_features_pipeline_v2 import run as fn;run_step("advanced_features",lambda:fn(DATABASE_URL),steps)
 if RUN_FOUR_LAYER:
  from player_context_orchestrator import run as fn;run_step("player_context_db_v3",lambda:fn(DATABASE_URL),steps);from player_context_enrichment_bridge import run_bridge as fn;run_step("player_context_bridge",lambda:fn(DATABASE_URL),steps);from pressure_features_builder import build as fn;run_step("pressure_features",lambda:fn(DATABASE_URL),steps,optional=True)
 if RUN_PREMATCH:
  from prematch_context_builder_fixed import run_build as fn;run_step("prematch_context",lambda:fn(DATABASE_URL),steps)
 if RUN_FOUR_LAYER:
  from asian_market_features_builder import run_build as fn;run_step("asian_market_features",lambda:fn(DATABASE_URL),steps,optional=True);from advanced_context_v4_builder import run_build as fn;run_step("advanced_context_v4",lambda:fn(DATABASE_URL),steps,optional=True);from four_layer_coverage_audit import run_audit as fn;run_step("four_layer_coverage",lambda:fn(DATABASE_URL),steps);from v1_v5_policy_backtest import ensure_validation as fn;run_step("v1_v5_policy_validation",lambda:fn(DATABASE_URL),steps)
 if RUN_ODDS_MOVEMENT:
  from odds_movement_enricher import run_enrich as fn;run_step("odds_movement",lambda:fn(DATABASE_URL),steps,optional=True)
 if RUN_AVAILABILITY_ENRICH:
  from availability_enricher_v4 import run_enrich as fn;run_step("availability_enrich",lambda:fn(DATABASE_URL),steps)
 if RUN_READINESS:
  from data_readiness_audit_v5 import run_audit as fn;run_step("data_readiness",lambda:fn(DATABASE_URL),steps)
 if RUN_PREDICTIONS:
  from production_predictor_v5 import run_predictions as fn;run_step("production_predictions",lambda:fn(DATABASE_URL),steps)
  if RUN_TURKEY_LISTS:
   from turkey_value_workflow import build_lists as fn;run_step("turkey_dual_lists",lambda:fn(DATABASE_URL),steps)
 summary["finished_at"]=utcnow().isoformat();summary["status"]="success";log.info("LIVE_REFRESH_RESULT %s",json.dumps(summary,ensure_ascii=False,default=str,separators=(",",":")));return summary
def main()->Dict[str,Any]:
 if not DATABASE_URL:raise RuntimeError("Missing DATABASE_URL")
 lock=psycopg.connect(DATABASE_URL,autocommit=True);got=False
 try:
  got=bool(lock.execute("SELECT pg_try_advisory_lock(%s)",(REFRESH_LOCK_KEY,)).fetchone()[0])
  if not got:return {"status":"skipped_duplicate_refresh","reason":"postgres_advisory_lock_busy","lock_key":REFRESH_LOCK_KEY,"at":utcnow().isoformat()}
  return _run()
 finally:
  if got:
   try:lock.execute("SELECT pg_advisory_unlock(%s)",(REFRESH_LOCK_KEY,))
   except Exception:pass
  lock.close()
if __name__=="__main__":print(json.dumps(main(),ensure_ascii=False,indent=2,default=str))
