#!/usr/bin/env python3
"""Fetch real current team rosters from ESPN and build DB-v3 player context.

ESPN is already the production fixture source and its public team-roster endpoint does
not require an API key. Raw roster payloads are archived for auditability. Roster-only
context is deliberately conservative: it provides real player presence/coverage but
never fabricates starter continuity, retained minutes, injuries, or calibrated player
strength. Those fields remain NULL/neutral and the V5 activation gate remains owned by
the leakage-safe backtest.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
import requests
from psycopg.types.json import Jsonb

import understat_player_continuity_v2 as v2
from espn_current_importer import SITE_BASE

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
LOOKAHEAD_DAYS=int(os.getenv("PLAYER_CONTEXT_LOOKAHEAD_DAYS","8"))
TIMEOUT=float(os.getenv("ESPN_ROSTER_TIMEOUT_SECONDS","12"))
WORKERS=max(1,min(10,int(os.getenv("ESPN_ROSTER_WORKERS","6"))))

SCHEMA="""
CREATE TABLE IF NOT EXISTS espn_team_roster_snapshots(
  league_slug TEXT NOT NULL,
  team_id TEXT NOT NULL,
  snapshot_date DATE NOT NULL,
  team_name TEXT NOT NULL,
  player_count INTEGER NOT NULL DEFAULT 0,
  raw JSONB NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(league_slug,team_id,snapshot_date)
);
CREATE TABLE IF NOT EXISTS espn_team_roster_runs(
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  teams_requested INTEGER NOT NULL DEFAULT 0,
  teams_fetched INTEGER NOT NULL DEFAULT 0,
  teams_with_players INTEGER NOT NULL DEFAULT 0,
  player_rows INTEGER NOT NULL DEFAULT 0,
  message TEXT
);
"""

def pname(obj:Any)->Optional[str]:
    if not isinstance(obj,dict):return None
    for k in ("displayName","fullName","shortName","name"):
        v=obj.get(k)
        if isinstance(v,str) and v.strip():return v.strip()
    return None

def is_player(obj:Dict[str,Any])->bool:
    name=pname(obj)
    if not name:return False
    # Avoid treating team/league objects as athletes.
    player_keys={"jersey","position","age","dateOfBirth","birthPlace","height","weight","experience","citizenship","uid","guid"}
    return bool(player_keys.intersection(obj.keys())) or str(obj.get("uid") or "").startswith("s:1~")

def extract_players(raw:Any)->List[Dict[str,Any]]:
    found=[]
    def walk(x:Any):
        if isinstance(x,dict):
            ath=x.get("athlete")
            if isinstance(ath,dict) and pname(ath):
                p=dict(ath);p["_entry"]=x;found.append(p)
            elif is_player(x):found.append(dict(x))
            for v in x.values():walk(v)
        elif isinstance(x,list):
            for v in x:walk(v)
    walk(raw)
    out=[];seen=set()
    for p in found:
        pid=str(p.get("id") or p.get("uid") or p.get("guid") or v2.canon(pname(p)))
        if not pid or pid in seen:continue
        seen.add(pid);out.append(p)
    return out

def fetch_one(league_slug:str,team_id:str)->Tuple[int,Dict[str,Any],str]:
    urls=[
      f"{SITE_BASE}/{league_slug}/teams/{team_id}/roster",
      f"{SITE_BASE}/{league_slug}/teams/{team_id}/athletes",
    ]
    last=""
    s=requests.Session();s.headers.update({"Accept":"application/json"})
    for url in urls:
        try:
            r=s.get(url,timeout=(4.0,TIMEOUT))
            last=f"{r.status_code}:{url.rsplit('/',1)[-1]}"
            if r.status_code==200:
                data=r.json()
                if isinstance(data,dict):return r.status_code,data,url.rsplit('/',1)[-1]
        except Exception as exc:last=type(exc).__name__
    raise RuntimeError(last or "ESPN roster request failed")

def run_bridge(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(v2.SCHEMA);conn.execute(SCHEMA)
        rid=conn.execute("INSERT INTO espn_team_roster_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        teams=conn.execute("""SELECT DISTINCT league_slug,home_team_id,home_team FROM espn_upcoming
                              WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+(%s||' days')::interval
                              UNION
                              SELECT DISTINCT league_slug,away_team_id,away_team FROM espn_upcoming
                              WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+(%s||' days')::interval""",(LOOKAHEAD_DAYS,LOOKAHEAD_DAYS)).fetchall()
        targets=[(str(l),str(tid),str(name)) for l,tid,name in teams if tid]
        today=date.today();fetched=with_players=player_rows=0;errors={};payloads={}
        try:
            with ThreadPoolExecutor(max_workers=min(WORKERS,max(1,len(targets)))) as ex:
                futs={ex.submit(fetch_one,l,tid):(l,tid,name) for l,tid,name in targets}
                for fut in as_completed(futs):
                    l,tid,name=futs[fut]
                    try:
                        status,raw,endpoint=fut.result();fetched+=1;players=extract_players(raw);player_rows+=len(players);with_players+=int(bool(players));payloads[(l,tid,name)]=(raw,players,endpoint)
                    except Exception as exc:errors[f"{l}:{tid}"]=str(exc)[:200]
            hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0);ctx_params=[]
            for (league,tid,name),(raw,players,endpoint) in payloads.items():
                conn.execute("""INSERT INTO espn_team_roster_snapshots(league_slug,team_id,snapshot_date,team_name,player_count,raw)
                              VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(league_slug,team_id,snapshot_date) DO UPDATE SET
                              team_name=EXCLUDED.team_name,player_count=EXCLUDED.player_count,raw=EXCLUDED.raw,fetched_at=NOW()""",
                             (league,tid,today,name,len(players),Jsonb(raw)))
                if not players:continue
                # Real roster presence, deliberately neutral strength. This feature remains shadow-only unless separately validated.
                n=len(players);coverage=min(.80,.80*n/22.0);expected=.50;top11=.50
                meta={"source":"espn-team-roster","team_id":tid,"league_slug":league,"roster_players":n,"endpoint":endpoint,
                      "roster_only":True,"calibrated_player_strength":False,"historical_continuity_available":False}
                ctx_params.append((name,hour,expected,top11,coverage,Jsonb(meta)))
            if ctx_params:
                sql="""INSERT INTO player_team_context_snapshots(team_name,snapshot_hour,current_season,previous_season,expected_xi_strength,top11_strength,
                       injury_impact,goalkeeper_injured,retained_minutes_share,starter_continuity,player_coverage,key_absences,source_meta)
                       VALUES(%s,%s,2026,2025,%s,%s,NULL,NULL,NULL,NULL,%s,'[]'::jsonb,%s)
                       ON CONFLICT(team_name,snapshot_hour) DO UPDATE SET expected_xi_strength=EXCLUDED.expected_xi_strength,top11_strength=EXCLUDED.top11_strength,
                       injury_impact=NULL,goalkeeper_injured=NULL,retained_minutes_share=NULL,starter_continuity=NULL,
                       player_coverage=EXCLUDED.player_coverage,key_absences='[]'::jsonb,source_meta=EXCLUDED.source_meta"""
                with conn.cursor() as cur:cur.executemany(sql,ctx_params)
            status="success" if ctx_params else "failed"
            result={"status":status,"teams_requested":len(targets),"teams_fetched":fetched,"teams_with_players":with_players,
                    "player_rows":player_rows,"contexts_written":len(ctx_params),"errors":errors}
            conn.execute("""UPDATE espn_team_roster_runs SET finished_at=NOW(),status=%s,teams_requested=%s,teams_fetched=%s,
                          teams_with_players=%s,player_rows=%s,message=%s WHERE id=%s""",
                         (status,len(targets),fetched,with_players,player_rows,json.dumps({"contexts_written":len(ctx_params),"errors":errors},separators=(",",":")),rid))
            print("ESPN_TEAM_ROSTER_PLAYER_BRIDGE_RESULT",json.dumps(result,separators=(",",":")),flush=True)
            if not ctx_params:raise RuntimeError("ESPN team roster bridge produced no real player context")
            return result
        except Exception as exc:
            try:conn.execute("UPDATE espn_team_roster_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:700],rid))
            except Exception:pass
            raise

if __name__=="__main__":print(json.dumps(run_bridge(),indent=2))
