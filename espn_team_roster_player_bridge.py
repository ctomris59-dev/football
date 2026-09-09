#!/usr/bin/env python3
"""Fetch real current ESPN rosters and build DB-v3 player context.

Current player presence comes from ESPN's public team-roster endpoint. Real 2025/26
player activity is merged by canonical player name across source-specific cache rows:
minutes use the maximum observed real value per player (avoiding double counting
FotMob/API-Football mirrors) and starts use the maximum explicit starts count per
player (e.g. ESPN historical lineups). Thus retained_minutes_share and
starter_continuity can both be populated without fabrication.

V5 activation remains owned by the leakage-safe backtest.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
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
PREVIOUS_SEASON=int(os.getenv("PLAYER_CONTEXT_PREVIOUS_SEASON","2025"))

SCHEMA="""
CREATE TABLE IF NOT EXISTS espn_team_roster_snapshots(
  league_slug TEXT NOT NULL, team_id TEXT NOT NULL, snapshot_date DATE NOT NULL, team_name TEXT NOT NULL,
  player_count INTEGER NOT NULL DEFAULT 0, raw JSONB NOT NULL, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(league_slug,team_id,snapshot_date));
CREATE TABLE IF NOT EXISTS espn_team_roster_runs(
  id BIGSERIAL PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ,
  status TEXT NOT NULL, teams_requested INTEGER NOT NULL DEFAULT 0, teams_fetched INTEGER NOT NULL DEFAULT 0,
  teams_with_players INTEGER NOT NULL DEFAULT 0, player_rows INTEGER NOT NULL DEFAULT 0, message TEXT);
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
    urls=[f"{SITE_BASE}/{league_slug}/teams/{team_id}/roster",f"{SITE_BASE}/{league_slug}/teams/{team_id}/athletes"]
    last="";s=requests.Session();s.headers.update({"Accept":"application/json"})
    for url in urls:
        try:
            r=s.get(url,timeout=(4.0,TIMEOUT));last=f"{r.status_code}:{url.rsplit('/',1)[-1]}"
            if r.status_code==200:
                data=r.json()
                if isinstance(data,dict):return r.status_code,data,url.rsplit('/',1)[-1]
        except Exception as exc:last=type(exc).__name__
    raise RuntimeError(last or "ESPN roster request failed")

def load_previous_activity(conn)->Dict[str,Tuple[str,List[Dict[str,Any]]]]:
    """Merge duplicate provider rows by canonical player name without double-counting."""
    try:
        rows=conn.execute("""SELECT team_name,player_id,player_name,games,starts,minutes,raw FROM understat_player_seasons
                           WHERE season=%s AND (COALESCE(minutes,0)>0 OR COALESCE(starts,0)>0)""",(PREVIOUS_SEASON,)).fetchall()
    except Exception:return {}
    by_team:Dict[str,Dict[str,Dict[str,Any]]]=defaultdict(dict);labels:Dict[str,str]={}
    for team,pid,name,games,starts,minutes,raw in rows:
        tkey=v2.canon(team);pkey=v2.canon(name)
        if not tkey or not pkey:continue
        labels[tkey]=str(team)
        slot=by_team[tkey].setdefault(pkey,{"player_id":str(pid),"player_name":str(name or ""),"games":0.0,"starts":0.0,"minutes":0.0,"sources":set(),"raw_rows":[]})
        slot["games"]=max(float(slot.get("games") or 0),float(games or 0))
        slot["starts"]=max(float(slot.get("starts") or 0),float(starts or 0))
        slot["minutes"]=max(float(slot.get("minutes") or 0),float(minutes or 0))
        r=raw if isinstance(raw,dict) else {};slot["sources"].add(str(r.get("source") or "player-cache"));slot["raw_rows"].append(r)
    out={}
    for tkey,players in by_team.items():
        vals=[]
        for p in players.values():
            vals.append({"player_id":p["player_id"],"player_name":p["player_name"],"games":p["games"],"starts":p["starts"],"minutes":p["minutes"],
                         "sources":sorted(p["sources"]),"raw":p["raw_rows"][0] if p["raw_rows"] else {}})
        if vals:out[tkey]=(labels[tkey],vals)
    return out

def retained_context(team:str,current_players:List[Dict[str,Any]],previous:Dict[str,Tuple[str,List[Dict[str,Any]]]])->Dict[str,Any]:
    matched=v2.match_team(team,previous)
    if not matched:return {"retained_minutes":None,"retained_starts":None,"previous_players":0,"historical_source":None,"minutes_method":None,"starts_method":None}
    _label,prev=matched;current_names={v2.canon(pname(p)) for p in current_players if pname(p)}
    total_minutes=sum(float(p.get("minutes") or 0) for p in prev);kept_minutes=sum(float(p.get("minutes") or 0) for p in prev if v2.canon(p.get("player_name")) in current_names)
    total_starts=sum(float(p.get("starts") or 0) for p in prev);kept_starts=sum(float(p.get("starts") or 0) for p in prev if v2.canon(p.get("player_name")) in current_names)
    mshare=(kept_minutes/total_minutes) if total_minutes>0 else None;sshare=(kept_starts/total_starts) if total_starts>0 else None
    sources=sorted({s for p in prev for s in (p.get("sources") or [])})
    return {"retained_minutes":mshare,"retained_starts":sshare,"previous_players":len(prev),"historical_source":"+".join(sources[:6]) if sources else None,
            "minutes_method":"previous-season-real-minutes/current-roster-name-overlap" if mshare is not None else None,
            "starts_method":"previous-season-explicit-starts/current-roster-name-overlap" if sshare is not None else None}

def run_bridge(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(v2.SCHEMA);conn.execute(SCHEMA);rid=conn.execute("INSERT INTO espn_team_roster_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        teams=conn.execute("""SELECT DISTINCT league_slug,home_team_id,home_team FROM espn_upcoming
                              WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+(%s||' days')::interval
                              UNION SELECT DISTINCT league_slug,away_team_id,away_team FROM espn_upcoming
                              WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+(%s||' days')::interval""",(LOOKAHEAD_DAYS,LOOKAHEAD_DAYS)).fetchall()
        targets=[(str(l),str(tid),str(name)) for l,tid,name in teams if tid];previous=load_previous_activity(conn)
        today=date.today();fetched=with_players=player_rows=0;errors={};payloads={}
        try:
            with ThreadPoolExecutor(max_workers=min(WORKERS,max(1,len(targets)))) as ex:
                futs={ex.submit(fetch_one,l,tid):(l,tid,name) for l,tid,name in targets}
                for fut in as_completed(futs):
                    l,tid,name=futs[fut]
                    try:
                        status,raw,endpoint=fut.result();fetched+=1;players=extract_players(raw);player_rows+=len(players);with_players+=int(bool(players));payloads[(l,tid,name)]=(raw,players,endpoint)
                    except Exception as exc:errors[f"{l}:{tid}"]=str(exc)[:200]
            hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0);ctx_params=[];retained_ready=starter_ready=0
            for (league,tid,name),(raw,players,endpoint) in payloads.items():
                conn.execute("""INSERT INTO espn_team_roster_snapshots(league_slug,team_id,snapshot_date,team_name,player_count,raw)
                              VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(league_slug,team_id,snapshot_date) DO UPDATE SET
                              team_name=EXCLUDED.team_name,player_count=EXCLUDED.player_count,raw=EXCLUDED.raw,fetched_at=NOW()""",(league,tid,today,name,len(players),Jsonb(raw)))
                if not players:continue
                n=len(players);rc=retained_context(name,players,previous);retained=rc["retained_minutes"];starter=rc["retained_starts"]
                retained_ready+=int(retained is not None);starter_ready+=int(starter is not None)
                base_cov=min(.80,.80*n/22.0);history_bonus=.08*min(1.0,float(rc["previous_players"])/18.0) if retained is not None else 0.0;starter_bonus=.08 if starter is not None else 0.0
                coverage=min(.96,base_cov+history_bonus+starter_bonus);expected=.50;top11=.50
                meta={"source":"espn-team-roster","team_id":tid,"league_slug":league,"roster_players":n,"endpoint":endpoint,"roster_only":True,
                      "calibrated_player_strength":False,"historical_continuity_available":retained is not None or starter is not None,
                      "retained_minutes_method":rc["minutes_method"],"starter_continuity_method":rc["starts_method"],"previous_players":rc["previous_players"],
                      "historical_source":rc["historical_source"],"exact_starter_continuity_available":starter is not None}
                ctx_params.append((name,hour,expected,top11,retained,starter,coverage,Jsonb(meta)))
            if ctx_params:
                sql="""INSERT INTO player_team_context_snapshots(team_name,snapshot_hour,current_season,previous_season,expected_xi_strength,top11_strength,
                       injury_impact,goalkeeper_injured,retained_minutes_share,starter_continuity,player_coverage,key_absences,source_meta)
                       VALUES(%s,%s,2026,2025,%s,%s,NULL,NULL,%s,%s,%s,'[]'::jsonb,%s)
                       ON CONFLICT(team_name,snapshot_hour) DO UPDATE SET expected_xi_strength=EXCLUDED.expected_xi_strength,top11_strength=EXCLUDED.top11_strength,
                       injury_impact=NULL,goalkeeper_injured=NULL,retained_minutes_share=EXCLUDED.retained_minutes_share,starter_continuity=EXCLUDED.starter_continuity,
                       player_coverage=EXCLUDED.player_coverage,key_absences='[]'::jsonb,source_meta=EXCLUDED.source_meta"""
                with conn.cursor() as cur:cur.executemany(sql,ctx_params)
            status="success" if ctx_params else "failed"
            result={"status":status,"teams_requested":len(targets),"teams_fetched":fetched,"teams_with_players":with_players,"player_rows":player_rows,
                    "contexts_written":len(ctx_params),"previous_cache_teams":len(previous),"retained_minutes_ready":retained_ready,"starter_continuity_ready":starter_ready,"errors":errors}
            conn.execute("""UPDATE espn_team_roster_runs SET finished_at=NOW(),status=%s,teams_requested=%s,teams_fetched=%s,teams_with_players=%s,player_rows=%s,message=%s WHERE id=%s""",
                         (status,len(targets),fetched,with_players,player_rows,json.dumps({"contexts_written":len(ctx_params),"previous_cache_teams":len(previous),"retained_minutes_ready":retained_ready,
                          "starter_continuity_ready":starter_ready,"errors":errors},separators=(",",":")),rid))
            print("ESPN_TEAM_ROSTER_PLAYER_BRIDGE_RESULT",json.dumps(result,separators=(",",":")),flush=True)
            if not ctx_params:raise RuntimeError("ESPN team roster bridge produced no real player context")
            return result
        except Exception as exc:
            try:conn.execute("UPDATE espn_team_roster_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:700],rid))
            except Exception:pass
            raise

if __name__=="__main__":print(json.dumps(run_bridge(),indent=2))
