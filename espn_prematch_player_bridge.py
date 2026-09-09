#!/usr/bin/env python3
"""Build current player-team context from persisted ESPN pre-match roster/lineup JSON."""
from __future__ import annotations
import json, os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import psycopg
from psycopg.types.json import Jsonb
import understat_player_continuity_v2 as v2
DATABASE_URL=os.getenv("DATABASE_URL","").strip()
def canon(v:Any)->str:return v2.canon(v)
def player_name(obj:Any)->Optional[str]:
    if not isinstance(obj,dict):return None
    for k in ("displayName","fullName","shortName","name"):
        v=obj.get(k)
        if isinstance(v,str) and v.strip():return v.strip()
    return None
def athlete_from_entry(obj:Any)->Optional[Dict[str,Any]]:
    if not isinstance(obj,dict):return None
    ath=obj.get("athlete")
    if isinstance(ath,dict) and player_name(ath):out=dict(ath);out["_entry"]=obj;return out
    if player_name(obj) and any(k in obj for k in ("position","jersey","uid","guid","starter","subbedIn","subbedOut","athleteId")):return dict(obj)
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
    walk(obj);seen=set();dedup=[]
    for p in out:
        pid=str(p.get("id") or p.get("uid") or p.get("guid") or p.get("athleteId") or canon(player_name(p)))
        if pid and pid not in seen:seen.add(pid);dedup.append(p)
    return dedup
def team_identity(team:Any)->Tuple[Optional[str],Optional[str]]:
    if not isinstance(team,dict):return None,None
    tid=team.get("id") or team.get("teamId");name=team.get("displayName") or team.get("shortDisplayName") or team.get("name") or team.get("abbreviation")
    return (str(tid) if tid is not None else None,str(name) if name else None)
def side_hint(obj:Dict[str,Any])->Optional[str]:
    for k in ("homeAway","side","designation"):
        v=obj.get(k)
        if isinstance(v,str) and v.lower() in {"home","away"}:return v.lower()
    return None
def grouped_team_players(raw:Any)->List[Tuple[Optional[str],Optional[str],Optional[str],List[Dict[str,Any]],str]]:
    groups=[];keys=("roster","rosters","athletes","players","lineup","lineups","entries","starters","substitutes")
    def walk(x:Any,path:str="root",inherited_team:Any=None,inherited_side:Optional[str]=None):
        if isinstance(x,dict):
            team=x.get("team") or inherited_team;tid,tname=team_identity(team);side=side_hint(x) or inherited_side;candidates=[]
            for k in keys:
                v=x.get(k)
                if isinstance(v,(list,dict)):candidates.extend(extract_players(v))
            if candidates and (tid or tname or side):groups.append((tid,tname,side,candidates,path))
            for k,v in x.items():
                child_side=side
                kl=str(k).lower()
                if kl in {"home","away"}:child_side=kl
                walk(v,f"{path}.{k}",team,child_side)
        elif isinstance(x,list):
            for i,v in enumerate(x):walk(v,f"{path}[{i}]",inherited_team,inherited_side)
    walk(raw);return groups
def schema_signature(raw:Any)->List[str]:
    sig=[]
    if isinstance(raw,dict) and "rosters" in raw:
        r=raw.get("rosters")
        if isinstance(r,dict):sig.append("root.rosters:dict["+",".join(sorted(str(k) for k in r.keys())[:20])+"]")
        elif isinstance(r,list):
            sig.append(f"root.rosters:list[{len(r)}]")
            if r and isinstance(r[0],dict):sig.append("root.rosters[0]:dict["+",".join(sorted(str(k) for k in r[0].keys())[:20])+"]")
        else:sig.append(f"root.rosters:{type(r).__name__}")
    return sig
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
    flags=[starter_flag(p) for p in players];explicit=sum(x is True for x in flags);out=[]
    for i,p in enumerate(players):
        name=player_name(p)
        if not name:continue
        flag=flags[i];starts=1.0 if flag is True else 0.0;minutes=90.0 if flag is True else 15.0;pos=p.get("position") or ((p.get("_entry") or {}).get("position") if isinstance(p.get("_entry"),dict) else None)
        out.append({"player_id":str(p.get("id") or p.get("uid") or p.get("guid") or p.get("athleteId") or canon(name)),"player_name":name,"games":1.0,"starts":starts,"minutes":minutes,"goals":0.0,"xg":0.0,"assists":0.0,"xa":0.0,"xgchain":0.0,"xgbuildup":0.0,"raw":{"source":"espn-prematch-roster","starter":flag,"position":pos}})
    return out,explicit
def run_bridge(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(v2.SCHEMA)
        rows=conn.execute("""SELECT DISTINCT ON(p.event_id) p.event_id,p.raw,COALESCE(u.home_team_id,''),u.home_team,COALESCE(u.away_team_id,''),u.away_team FROM espn_prematch_snapshots p JOIN espn_upcoming u ON u.event_id=p.event_id WHERE u.is_current=TRUE AND u.match_date>=NOW()-INTERVAL '2 hours' AND u.match_date<=NOW()+INTERVAL '8 days' ORDER BY p.event_id,p.snapshot_hour DESC""").fetchall()
        hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0);teams:Dict[str,Dict[str,Any]]={};events_with_groups=groups_seen=0;shape=[]
        for ix,(eid,raw,hid,hname,aid,aname) in enumerate(rows):
            if ix==0:shape=schema_signature(raw)
            groups=grouped_team_players(raw);events_with_groups+=int(bool(groups));groups_seen+=len(groups);targets=[("home",str(hid or ""),str(hname)),("away",str(aid or ""),str(aname))]
            for tid,tname,side,players,path in groups:
                best=None;best_score=0.0
                for target_side,target_id,target_name in targets:
                    score=1.0 if tid and target_id and tid==target_id else .95 if tname and canon(tname)==canon(target_name) else .75 if side and side==target_side else 0.0
                    if score>best_score:best_score=score;best=(target_id,target_name)
                if not best:continue
                target_name=best[1];parsed,explicit=strength_rows(players)
                if not parsed:continue
                rec=teams.setdefault(target_name,{"players":{},"events":0,"explicit":0,"paths":set()});rec["events"]+=1;rec["explicit"]+=explicit;rec["paths"].add(path)
                for p in parsed:rec["players"][p["player_id"]]=p
        sql="""INSERT INTO player_team_context_snapshots(team_name,snapshot_hour,current_season,previous_season,expected_xi_strength,top11_strength,injury_impact,goalkeeper_injured,retained_minutes_share,starter_continuity,player_coverage,key_absences,source_meta) VALUES(%s,%s,2026,2025,%s,%s,0.0,FALSE,NULL,NULL,%s,'[]'::jsonb,%s) ON CONFLICT(team_name,snapshot_hour) DO UPDATE SET expected_xi_strength=EXCLUDED.expected_xi_strength,top11_strength=EXCLUDED.top11_strength,injury_impact=0.0,goalkeeper_injured=FALSE,retained_minutes_share=NULL,starter_continuity=NULL,player_coverage=EXCLUDED.player_coverage,key_absences='[]'::jsonb,source_meta=EXCLUDED.source_meta"""
        params=[];written=expected_ready=explicit_starter_teams=0
        for team,rec in teams.items():
            players=list(rec["players"].values());scores=v2.player_scores(players)
            if not players or not scores:continue
            ranked=sorted(players,key=lambda p:(float(p.get("starts") or 0),float(p.get("minutes") or 0),scores.get(p["player_id"],.5)),reverse=True);top=ranked[:11];topvals=[scores.get(p["player_id"],.5) for p in top];expected=sum(topvals)/len(topvals) if topvals else None;coverage=min(.75,len(players)/22.0*.75);explicit=int(rec["explicit"] or 0);expected_ready+=int(expected is not None);explicit_starter_teams+=int(explicit>=7);meta={"source":"espn-prematch-roster","roster_players":len(players),"explicit_starters":explicit,"events":rec["events"],"historical_continuity_available":False,"paths":sorted(rec["paths"])[:6]};params.append((team,hour,expected,expected,coverage,Jsonb(meta)));written+=1
        if params:
            with conn.cursor() as cur:cur.executemany(sql,params)
        result={"status":"success" if written>0 else "empty","prematch_events":len(rows),"events_with_team_groups":events_with_groups,"groups_seen":groups_seen,"teams_written":written,"expected_xi_ready":expected_ready,"explicit_starter_teams":explicit_starter_teams,"schema_signature":shape};print("ESPN_PREMATCH_PLAYER_BRIDGE_RESULT",json.dumps(result,separators=(",",":")),flush=True);return result
if __name__=="__main__":print(json.dumps(run_bridge(),indent=2))
