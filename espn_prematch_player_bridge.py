#!/usr/bin/env python3
"""Build current player-team context from persisted ESPN pre-match roster/lineup JSON.

This bridge uses only already archived `espn_prematch_snapshots.raw` data. It does not
invent historical continuity: current roster/lineup presence can provide conservative
Expected-XI coverage, while retained-minutes/starter-continuity remain NULL unless a
real historical source exists.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

import understat_player_continuity_v2 as v2

DATABASE_URL=os.getenv("DATABASE_URL","").strip()


def canon(v:Any)->str:
    return v2.canon(v)


def player_name(obj:Any)->Optional[str]:
    if not isinstance(obj,dict):return None
    for k in ("displayName","fullName","shortName","name"):
        v=obj.get(k)
        if isinstance(v,str) and v.strip():return v.strip()
    return None


def athlete_from_entry(obj:Any)->Optional[Dict[str,Any]]:
    if not isinstance(obj,dict):return None
    ath=obj.get("athlete")
    if isinstance(ath,dict) and player_name(ath):
        out=dict(ath);out["_entry"]=obj;return out
    # Direct athlete objects usually carry position/jersey/uid fields; avoid team objects.
    if player_name(obj) and any(k in obj for k in ("position","jersey","uid","guid","starter","subbedIn","subbedOut")):
        return dict(obj)
    return None


def extract_players(obj:Any)->List[Dict[str,Any]]:
    out=[]
    def walk(x:Any):
        if isinstance(x,dict):
            p=athlete_from_entry(x)
            if p:out.append(p)
            for v in x.values():walk(v)
        elif isinstance(x,list):
            for v in x:walk(v)
    walk(obj)
    seen=set();dedup=[]
    for p in out:
        pid=str(p.get("id") or p.get("uid") or p.get("guid") or canon(player_name(p)))
        if not pid or pid in seen:continue
        seen.add(pid);dedup.append(p)
    return dedup


def team_identity(team:Any)->Tuple[Optional[str],Optional[str]]:
    if not isinstance(team,dict):return None,None
    tid=team.get("id")
    name=team.get("displayName") or team.get("shortDisplayName") or team.get("name")
    return (str(tid) if tid is not None else None,str(name) if name else None)


def grouped_team_players(raw:Any)->List[Tuple[Optional[str],Optional[str],List[Dict[str,Any]],str]]:
    """Find nested ESPN objects that pair a team identity with roster/lineup/player lists."""
    groups=[]
    keys=("roster","rosters","athletes","players","lineup","lineups","entries")
    def walk(x:Any,path:str="root"):
        if isinstance(x,dict):
            team=x.get("team")
            tid,tname=team_identity(team)
            if tid or tname:
                candidates=[]
                for k in keys:
                    v=x.get(k)
                    if isinstance(v,(list,dict)):candidates.extend(extract_players(v))
                if candidates:groups.append((tid,tname,candidates,path))
            for k,v in x.items():walk(v,f"{path}.{k}")
        elif isinstance(x,list):
            for i,v in enumerate(x):walk(v,f"{path}[{i}]")
    walk(raw)
    return groups


def starter_flag(p:Dict[str,Any])->Optional[bool]:
    entry=p.get("_entry") if isinstance(p.get("_entry"),dict) else {}
    for src in (entry,p):
        for k in ("starter","isStarter","starting"):
            if k in src:
                v=src.get(k)
                if isinstance(v,bool):return v
                if str(v).lower() in {"true","1","yes"}:return True
                if str(v).lower() in {"false","0","no"}:return False
    return None


def strength_rows(players:List[Dict[str,Any]])->Tuple[List[Dict[str,Any]],int]:
    flags=[starter_flag(p) for p in players];explicit=sum(x is True for x in flags)
    out=[]
    for i,p in enumerate(players):
        name=player_name(p)
        if not name:continue
        flag=flags[i]
        # If ESPN explicitly supplies starters, honor them. Otherwise treat the roster as
        # available-player context without pretending the first 11 are confirmed starters.
        starts=1.0 if flag is True else 0.0
        minutes=90.0 if flag is True else 15.0
        pos=p.get("position") or ((p.get("_entry") or {}).get("position") if isinstance(p.get("_entry"),dict) else None)
        out.append({"player_id":str(p.get("id") or p.get("uid") or canon(name)),"player_name":name,
                    "games":1.0,"starts":starts,"minutes":minutes,"goals":0.0,"xg":0.0,"assists":0.0,"xa":0.0,
                    "xgchain":0.0,"xgbuildup":0.0,"raw":{"source":"espn-prematch-roster","starter":flag,"position":pos}})
    return out,explicit


def run_bridge(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(v2.SCHEMA)
        rows=conn.execute("""SELECT DISTINCT ON(p.event_id) p.event_id,p.raw,
                     COALESCE(u.home_team_id,''),u.home_team,COALESCE(u.away_team_id,''),u.away_team
                     FROM espn_prematch_snapshots p JOIN espn_upcoming u ON u.event_id=p.event_id
                     WHERE u.is_current=TRUE AND u.match_date>=NOW()-INTERVAL '2 hours' AND u.match_date<=NOW()+INTERVAL '8 days'
                     ORDER BY p.event_id,p.snapshot_hour DESC""").fetchall()
        hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)
        teams:Dict[str,Dict[str,Any]]={}
        events_with_groups=groups_seen=0
        for eid,raw,hid,hname,aid,aname in rows:
            groups=grouped_team_players(raw)
            if groups:events_with_groups+=1
            groups_seen+=len(groups)
            targets=[(str(hid or ""),str(hname)),(str(aid or ""),str(aname))]
            for tid,tname,players,path in groups:
                best=None;best_score=0.0
                for target_id,target_name in targets:
                    score=0.0
                    if tid and target_id and tid==target_id:score=1.0
                    elif tname and canon(tname)==canon(target_name):score=.95
                    if score>best_score:best_score=score;best=(target_id,target_name)
                if not best:continue
                _target_id,target_name=best
                parsed,explicit=strength_rows(players)
                if not parsed:continue
                rec=teams.setdefault(target_name,{"players":{},"events":0,"explicit":0,"paths":set()})
                rec["events"]+=1;rec["explicit"]+=explicit;rec["paths"].add(path)
                for p in parsed:
                    rec["players"][p["player_id"]]=p
        written=expected_ready=explicit_starter_teams=0
        sql="""INSERT INTO player_team_context_snapshots(team_name,snapshot_hour,current_season,previous_season,
               expected_xi_strength,top11_strength,injury_impact,goalkeeper_injured,retained_minutes_share,starter_continuity,
               player_coverage,key_absences,source_meta)
               VALUES(%s,%s,2026,2025,%s,%s,0.0,FALSE,NULL,NULL,%s,'[]'::jsonb,%s)
               ON CONFLICT(team_name,snapshot_hour) DO UPDATE SET expected_xi_strength=EXCLUDED.expected_xi_strength,
               top11_strength=EXCLUDED.top11_strength,injury_impact=0.0,goalkeeper_injured=FALSE,
               retained_minutes_share=NULL,starter_continuity=NULL,player_coverage=EXCLUDED.player_coverage,
               key_absences='[]'::jsonb,source_meta=EXCLUDED.source_meta"""
        params=[]
        for team,rec in teams.items():
            players=list(rec["players"].values());scores=v2.player_scores(players)
            if not players or not scores:continue
            ranked=sorted(players,key=lambda p:(float(p.get("starts") or 0),float(p.get("minutes") or 0),scores.get(p["player_id"],.5)),reverse=True)
            top=ranked[:11];topvals=[scores.get(p["player_id"],.5) for p in top]
            expected=sum(topvals)/len(topvals) if topvals else None
            coverage=min(.75,len(players)/22.0*.75)
            explicit=int(rec["explicit"] or 0)
            if expected is not None:expected_ready+=1
            if explicit>=7:explicit_starter_teams+=1
            meta={"source":"espn-prematch-roster","roster_players":len(players),"explicit_starters":explicit,
                  "events":rec["events"],"historical_continuity_available":False,"paths":sorted(rec["paths"])[:6]}
            params.append((team,hour,expected,expected,coverage,Jsonb(meta)));written+=1
        if params:
            with conn.cursor() as cur:cur.executemany(sql,params)
        result={"status":"success" if written>0 else "empty","prematch_events":len(rows),"events_with_team_groups":events_with_groups,
                "groups_seen":groups_seen,"teams_written":written,"expected_xi_ready":expected_ready,
                "explicit_starter_teams":explicit_starter_teams}
        print("ESPN_PREMATCH_PLAYER_BRIDGE_RESULT",json.dumps(result,separators=(",",":")),flush=True)
        return result

if __name__=="__main__":print(json.dumps(run_bridge(),indent=2))
