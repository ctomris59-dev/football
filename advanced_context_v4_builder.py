#!/usr/bin/env python3
"""Merge Expected-XI, market, pressure proxy and squad continuity into one fixture context."""
from __future__ import annotations
import json, os
from datetime import date, datetime, timezone
from typing import Any, Dict
import psycopg
from psycopg.types.json import Jsonb
from asian_event_context import resolve_event_market

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
SCHEMA="""
CREATE TABLE IF NOT EXISTS advanced_fixture_context_v4(event_id TEXT NOT NULL,snapshot_hour TIMESTAMPTZ NOT NULL,home_expected_xi_strength DOUBLE PRECISION,away_expected_xi_strength DOUBLE PRECISION,home_injury_impact DOUBLE PRECISION,away_injury_impact DOUBLE PRECISION,home_goalkeeper_injured BOOLEAN,away_goalkeeper_injured BOOLEAN,home_retained_minutes_share DOUBLE PRECISION,away_retained_minutes_share DOUBLE PRECISION,home_starter_continuity DOUBLE PRECISION,away_starter_continuity DOUBLE PRECISION,home_threat_share DOUBLE PRECISION,away_threat_share DOUBLE PRECISION,goal_pressure_signal DOUBLE PRECISION,corner_pressure_signal DOUBLE PRECISION,asian_goal_p_over_2_5 DOUBLE PRECISION,asian_corner_p_over_8_5 DOUBLE PRECISION,asian_goal_movement DOUBLE PRECISION,asian_corner_movement DOUBLE PRECISION,asian_bookmakers INTEGER NOT NULL DEFAULT 0,player_coverage DOUBLE PRECISION NOT NULL DEFAULT 0,pressure_coverage DOUBLE PRECISION NOT NULL DEFAULT 0,total_coverage DOUBLE PRECISION NOT NULL DEFAULT 0,context JSONB NOT NULL DEFAULT '{}'::jsonb,PRIMARY KEY(event_id,snapshot_hour));
CREATE TABLE IF NOT EXISTS advanced_context_v4_runs(id BIGSERIAL PRIMARY KEY,started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),finished_at TIMESTAMPTZ,status TEXT NOT NULL,fixtures INTEGER NOT NULL DEFAULT 0,player_context INTEGER NOT NULL DEFAULT 0,asian_context INTEGER NOT NULL DEFAULT 0,pressure_context INTEGER NOT NULL DEFAULT 0,message TEXT);
"""
def rowdict(row,keys):return dict(zip(keys,row)) if row else {}
def json_default(value:Any):
 if isinstance(value,(datetime,date)):return value.isoformat()
 if isinstance(value,set):return sorted(value)
 return str(value)
def json_dumps(value:Any)->str:return json.dumps(value,default=json_default,separators=(",",":"))
def run_build(database_url=None)->Dict[str,Any]:
 db=(database_url or DATABASE_URL).strip()
 if not db:raise RuntimeError("Missing DATABASE_URL")
 with psycopg.connect(db,autocommit=True) as c:
  c.execute(SCHEMA);rid=c.execute("INSERT INTO advanced_context_v4_runs(status) VALUES('running') RETURNING id").fetchone()[0];fixtures=pc=ac=prc=0
  try:
   ups=c.execute("SELECT event_id,home_team,away_team FROM espn_upcoming WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+INTERVAL '8 days'").fetchall();hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)
   for eid,h,a in ups:
    fixtures+=1;keys=["expected","top11","impact","gk","retained","continuity","coverage","absences","meta"]
    def player(team):
     try:r=c.execute("SELECT expected_xi_strength,top11_strength,injury_impact,goalkeeper_injured,retained_minutes_share,starter_continuity,player_coverage,key_absences,source_meta FROM player_team_context_snapshots WHERE team_name=%s ORDER BY snapshot_hour DESC LIMIT 1",(team,)).fetchone();return rowdict(r,keys)
     except Exception:return {}
    hp,ap=player(h),player(a);pc+=int(bool(hp or ap))
    try:pres=c.execute("SELECT home_threat_share,away_threat_share,goal_pressure_signal,corner_pressure_signal,coverage,raw FROM fixture_pressure_snapshots WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1",(eid,)).fetchone()
    except Exception:pres=None
    P=rowdict(pres,["hthreat","athreat","goal","corner","coverage","raw"]);prc+=int(bool(P))
    A=resolve_event_market(c,str(eid));ac+=int(bool(A.get("has_any")))
    player_cov=(float(hp.get("coverage") or 0)+float(ap.get("coverage") or 0))/2
    press_cov=float(P.get("coverage") or 0)
    asian_cov=min(1.0,float(A.get("books") or 0)/3.0) if A.get("has_any") else 0.0
    total=.40*player_cov+.30*press_cov+.30*asian_cov
    context={"home_player":hp,"away_player":ap,"pressure":P,"asian":A,"activation":"shadow-only-until-calibrated"}
    c.execute("""INSERT INTO advanced_fixture_context_v4(event_id,snapshot_hour,home_expected_xi_strength,away_expected_xi_strength,home_injury_impact,away_injury_impact,home_goalkeeper_injured,away_goalkeeper_injured,home_retained_minutes_share,away_retained_minutes_share,home_starter_continuity,away_starter_continuity,home_threat_share,away_threat_share,goal_pressure_signal,corner_pressure_signal,asian_goal_p_over_2_5,asian_corner_p_over_8_5,asian_goal_movement,asian_corner_movement,asian_bookmakers,player_coverage,pressure_coverage,total_coverage,context) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id,snapshot_hour) DO UPDATE SET home_expected_xi_strength=EXCLUDED.home_expected_xi_strength,away_expected_xi_strength=EXCLUDED.away_expected_xi_strength,home_injury_impact=EXCLUDED.home_injury_impact,away_injury_impact=EXCLUDED.away_injury_impact,home_goalkeeper_injured=EXCLUDED.home_goalkeeper_injured,away_goalkeeper_injured=EXCLUDED.away_goalkeeper_injured,home_retained_minutes_share=EXCLUDED.home_retained_minutes_share,away_retained_minutes_share=EXCLUDED.away_retained_minutes_share,home_starter_continuity=EXCLUDED.home_starter_continuity,away_starter_continuity=EXCLUDED.away_starter_continuity,home_threat_share=EXCLUDED.home_threat_share,away_threat_share=EXCLUDED.away_threat_share,goal_pressure_signal=EXCLUDED.goal_pressure_signal,corner_pressure_signal=EXCLUDED.corner_pressure_signal,asian_goal_p_over_2_5=EXCLUDED.asian_goal_p_over_2_5,asian_corner_p_over_8_5=EXCLUDED.asian_corner_p_over_8_5,asian_goal_movement=EXCLUDED.asian_goal_movement,asian_corner_movement=EXCLUDED.asian_corner_movement,asian_bookmakers=EXCLUDED.asian_bookmakers,player_coverage=EXCLUDED.player_coverage,pressure_coverage=EXCLUDED.pressure_coverage,total_coverage=EXCLUDED.total_coverage,context=EXCLUDED.context""",(eid,hour,hp.get("expected"),ap.get("expected"),hp.get("impact"),ap.get("impact"),hp.get("gk"),ap.get("gk"),hp.get("retained"),ap.get("retained"),hp.get("continuity"),ap.get("continuity"),P.get("hthreat"),P.get("athreat"),P.get("goal"),P.get("corner"),A.get("goal_p"),A.get("corner_p"),A.get("goal_move"),A.get("corner_move"),int(A.get("books") or 0),player_cov,press_cov,total,Jsonb(context,dumps=json_dumps)))
   c.execute("UPDATE advanced_context_v4_runs SET finished_at=NOW(),status='success',fixtures=%s,player_context=%s,asian_context=%s,pressure_context=%s,message='four-layer shadow context with datetime-safe JSON' WHERE id=%s",(fixtures,pc,ac,prc,rid));res={"status":"success","fixtures":fixtures,"player_context":pc,"asian_context":ac,"pressure_context":prc};print("ADVANCED_CONTEXT_V4_RESULT",json.dumps(res,separators=(",",":")));return res
  except Exception as exc:c.execute("UPDATE advanced_context_v4_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:700],rid));raise
if __name__=="__main__":print(json.dumps(run_build(),indent=2))
