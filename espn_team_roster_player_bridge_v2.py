#!/usr/bin/env python3
"""FBref-first previous-season continuity + resilient current ESPN roster bridge.

Fixes the legacy bridge's non-empty previous-cache unpack bug and avoids dropping to
legacy DB-v3 on transient ESPN roster failures by reusing a recent archived ESPN
roster. Previous activity is merged by canonical player name, with FBref Starts/Min
primary and explicit ESPN historical starts as a fallback/validator.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

import espn_team_roster_player_bridge as base
import understat_player_continuity_v2 as v2

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
LOOKAHEAD_DAYS=int(os.getenv("PLAYER_CONTEXT_LOOKAHEAD_DAYS","8"))
PREVIOUS_SEASON=int(os.getenv("PLAYER_CONTEXT_PREVIOUS_SEASON","2025"))
CACHE_MAX_DAYS=int(os.getenv("ESPN_ROSTER_CACHE_MAX_DAYS","2"))
WORKERS=max(1,min(10,int(os.getenv("ESPN_ROSTER_WORKERS","6"))))


def merge_activity(conn)->Dict[str,Tuple[str,List[Dict[str,Any]]]]:
    by_team:Dict[str,Dict[str,Dict[str,Any]]]=defaultdict(dict);labels={}
    def add(team,name,pid,games,starts,minutes,source,priority):
        tk=v2.canon(team);pk=v2.canon(name)
        if not tk or not pk:return
        labels[tk]=str(team);slot=by_team[tk].setdefault(pk,{"player_id":str(pid or pk),"player_name":str(name),"games":0.0,"starts":0.0,"minutes":0.0,"sources":set(),"priority":0})
        slot["sources"].add(source);slot["games"]=max(slot["games"],float(games or 0));slot["starts"]=max(slot["starts"],float(starts or 0));slot["minutes"]=max(slot["minutes"],float(minutes or 0));slot["priority"]=max(slot["priority"],priority)
    # Primary: final-season FBref Starts + Minutes.
    try:
        for team,pid,name,games,starts,minutes,method in conn.execute("""SELECT team_name,player_id,player_name,games,starts,minutes,source_method
          FROM fbref_player_season_stats WHERE season=%s AND (starts>0 OR minutes>0)""",(PREVIOUS_SEASON,)).fetchall():
            add(team,name,pid,games,starts,minutes,f"fbref:{method}",100)
    except Exception:pass
    # Secondary/validator: Understat, API-Football mirrors and explicit ESPN XI aggregate.
    try:
        for team,pid,name,games,starts,minutes,raw in conn.execute("""SELECT team_name,player_id,player_name,games,starts,minutes,raw
          FROM understat_player_seasons WHERE season=%s AND (COALESCE(starts,0)>0 OR COALESCE(minutes,0)>0)""",(PREVIOUS_SEASON,)).fetchall():
            r=raw if isinstance(raw,dict) else {};add(team,name,pid,games,starts,minutes,str(r.get("source") or "shared-player-cache"),50)
    except Exception:pass
    out={}
    for tk,players in by_team.items():
        vals=[]
        for p in players.values():vals.append({**p,"sources":sorted(p["sources"])})
        if vals:out[tk]=(labels[tk],vals)
    return out


def retained_context(team:str,current_players:List[Dict[str,Any]],previous)->Dict[str,Any]:
    matched=v2.match_team(team,previous)
    if not matched:return {"retained_minutes":None,"retained_starts":None,"previous_players":0,"sources":[],"match_score":None}
    label,prev,score=matched  # v2.match_team returns THREE values; legacy bridge unpacked two.
    current={v2.canon(base.pname(p)) for p in current_players if base.pname(p)}
    tm=sum(float(p.get("minutes") or 0) for p in prev);ts=sum(float(p.get("starts") or 0) for p in prev)
    km=sum(float(p.get("minutes") or 0) for p in prev if v2.canon(p.get("player_name")) in current);ks=sum(float(p.get("starts") or 0) for p in prev if v2.canon(p.get("player_name")) in current)
    sources=sorted({s for p in prev for s in p.get("sources",[])})
    return {"retained_minutes":km/tm if tm>0 else None,"retained_starts":ks/ts if ts>0 else None,"previous_players":len(prev),"sources":sources,"match_score":float(score),"matched_team":label}


def cached(conn,league,tid)->Optional[Tuple[Dict[str,Any],List[Dict[str,Any]],str]]:
    row=conn.execute("""SELECT raw FROM espn_team_roster_snapshots WHERE league_slug=%s AND team_id=%s
      AND snapshot_date>=CURRENT_DATE-(%s||' days')::interval ORDER BY snapshot_date DESC LIMIT 1""",(league,tid,CACHE_MAX_DAYS)).fetchone()
    if not row or not isinstance(row[0],dict):return None
    ps=base.extract_players(row[0]);return (row[0],ps,"cached-roster") if ps else None


def run_bridge(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(v2.SCHEMA);conn.execute(base.SCHEMA)
        rid=conn.execute("INSERT INTO espn_team_roster_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        targets=[(str(l),str(tid),str(name)) for l,tid,name in conn.execute("""SELECT DISTINCT league_slug,home_team_id,home_team FROM espn_upcoming WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+(%s||' days')::interval
          UNION SELECT DISTINCT league_slug,away_team_id,away_team FROM espn_upcoming WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+(%s||' days')::interval""",(LOOKAHEAD_DAYS,LOOKAHEAD_DAYS)).fetchall() if tid]
        previous=merge_activity(conn);payloads={};errors={};fetched=with_players=player_rows=cache_hits=0
        try:
            with ThreadPoolExecutor(max_workers=min(WORKERS,max(1,len(targets)))) as ex:
                futs={ex.submit(base.fetch_one,l,tid):(l,tid,name) for l,tid,name in targets}
                for fut in as_completed(futs):
                    l,tid,name=futs[fut]
                    try:
                        _status,raw,endpoint=fut.result();ps=base.extract_players(raw);fetched+=1
                        if ps:payloads[(l,tid,name)]=(raw,ps,endpoint)
                    except Exception as exc:
                        c=cached(conn,l,tid)
                        if c:payloads[(l,tid,name)]=c;cache_hits+=1
                        else:errors[f"{l}:{tid}"]=str(exc)[:180]
            hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0);today=date.today();params=[];retained_ready=starter_ready=0
            for (league,tid,name),(raw,players,endpoint) in payloads.items():
                if not players:continue
                with_players+=1;player_rows+=len(players)
                conn.execute("""INSERT INTO espn_team_roster_snapshots(league_slug,team_id,snapshot_date,team_name,player_count,raw) VALUES(%s,%s,%s,%s,%s,%s)
                  ON CONFLICT(league_slug,team_id,snapshot_date) DO UPDATE SET team_name=EXCLUDED.team_name,player_count=EXCLUDED.player_count,raw=EXCLUDED.raw,fetched_at=NOW()""",(league,tid,today,name,len(players),Jsonb(raw)))
                rc=retained_context(name,players,previous);rm,rs=rc["retained_minutes"],rc["retained_starts"];retained_ready+=int(rm is not None);starter_ready+=int(rs is not None)
                n=len(players);base_cov=min(.80,.80*n/22.0);history=.08*min(1.0,rc["previous_players"]/18.0) if rm is not None else 0;start_bonus=.08 if rs is not None else 0;coverage=min(.96,base_cov+history+start_bonus)
                # 0.50 remains an explicitly neutral interface value. It is NOT counted as a calibrated/confirmed XI downstream.
                meta={"source":"espn-team-roster-v2","team_id":tid,"league_slug":league,"endpoint":endpoint,"roster_players":n,"roster_only":True,"neutral_strength_interface":True,"calibrated_player_strength":False,
                      "historical_continuity_available":rm is not None or rs is not None,"retained_minutes_method":"fbref-or-real-cache/current-roster-name-overlap" if rm is not None else None,
                      "starter_continuity_method":"fbref-or-explicit-espn-starts/current-roster-name-overlap" if rs is not None else None,"previous_players":rc["previous_players"],"historical_sources":rc["sources"],"team_match_score":rc["match_score"]}
                params.append((name,hour,.50,.50,rm,rs,coverage,Jsonb(meta)))
            if params:
                sql="""INSERT INTO player_team_context_snapshots(team_name,snapshot_hour,current_season,previous_season,expected_xi_strength,top11_strength,injury_impact,goalkeeper_injured,retained_minutes_share,starter_continuity,player_coverage,key_absences,source_meta)
                  VALUES(%s,%s,2026,2025,%s,%s,NULL,NULL,%s,%s,%s,'[]'::jsonb,%s) ON CONFLICT(team_name,snapshot_hour) DO UPDATE SET expected_xi_strength=EXCLUDED.expected_xi_strength,top11_strength=EXCLUDED.top11_strength,injury_impact=NULL,goalkeeper_injured=NULL,retained_minutes_share=EXCLUDED.retained_minutes_share,starter_continuity=EXCLUDED.starter_continuity,player_coverage=EXCLUDED.player_coverage,key_absences='[]'::jsonb,source_meta=EXCLUDED.source_meta"""
                with conn.cursor() as cur:cur.executemany(sql,params)
            status="success" if params else "failed";result={"status":status,"teams_requested":len(targets),"teams_live_fetched":fetched,"cache_hits":cache_hits,"teams_with_players":with_players,"player_rows":player_rows,"contexts_written":len(params),"previous_cache_teams":len(previous),"retained_minutes_ready":retained_ready,"starter_continuity_ready":starter_ready,"errors":errors}
            conn.execute("UPDATE espn_team_roster_runs SET finished_at=NOW(),status=%s,teams_requested=%s,teams_fetched=%s,teams_with_players=%s,player_rows=%s,message=%s WHERE id=%s",(status,len(targets),fetched+cache_hits,with_players,player_rows,json.dumps(result,separators=(",",":"))[:1000],rid));print("ESPN_TEAM_ROSTER_PLAYER_BRIDGE_V2_RESULT",json.dumps(result,separators=(",",":")),flush=True)
            if not params:raise RuntimeError("ESPN roster v2 produced no current roster context")
            return result
        except Exception as exc:
            try:conn.execute("UPDATE espn_team_roster_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:900],rid))
            except Exception:pass
            raise

if __name__=="__main__":print(json.dumps(run_bridge(),indent=2))
