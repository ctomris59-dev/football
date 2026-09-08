#!/usr/bin/env python3
"""Fetch current Big Five player-strength and team-style aggregates from FotMob.

Fail-soft, no API key. Uses league season deep-stat tables instead of per-player
requests, keeping the request count bounded.
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb
import requests

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
BASE="https://www.fotmob.com/api/data"
SEASON_LABEL=os.getenv("FOTMOB_STRENGTH_SEASON","2026/2027")
REQUEST_DELAY=float(os.getenv("FOTMOB_STRENGTH_REQUEST_DELAY_SECONDS","0.55"))

LEAGUES=[
    (47,"Premier League"),(87,"La Liga"),(55,"Serie A"),(54,"Bundesliga"),(53,"Ligue 1")
]
PLAYER_STATS=["rating","expected_goals_per_90","expected_assists_per_90","goals_per_90","goal_assist"]
TEAM_STATS=["possession_percentage_team","accurate_cross_team","poss_won_att_3rd_team",
            "ontarget_scoring_att_team","corner_taken_team","expected_goals_team"]

SCHEMA_SQL="""
CREATE TABLE IF NOT EXISTS fotmob_player_strength_snapshots(
  snapshot_date DATE NOT NULL,
  league_name TEXT NOT NULL,
  league_id INTEGER NOT NULL,
  season_label TEXT NOT NULL,
  season_id TEXT,
  player_id TEXT NOT NULL,
  player_name TEXT,
  team_id TEXT,
  team_name TEXT,
  position TEXT,
  minutes_played DOUBLE PRECISION,
  matches_played DOUBLE PRECISION,
  metrics JSONB NOT NULL,
  percentiles JSONB NOT NULL,
  strength_score DOUBLE PRECISION,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(snapshot_date,league_id,player_id)
);
CREATE INDEX IF NOT EXISTS idx_fotmob_player_strength_team
  ON fotmob_player_strength_snapshots(snapshot_date,team_id,strength_score DESC);

CREATE TABLE IF NOT EXISTS fotmob_team_style_snapshots(
  snapshot_date DATE NOT NULL,
  league_name TEXT NOT NULL,
  league_id INTEGER NOT NULL,
  season_label TEXT NOT NULL,
  season_id TEXT,
  team_id TEXT NOT NULL,
  team_name TEXT,
  metrics JSONB NOT NULL,
  relative_metrics JSONB NOT NULL,
  top11_strength DOUBLE PRECISION,
  player_coverage INTEGER NOT NULL DEFAULT 0,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(snapshot_date,league_id,team_id)
);

CREATE TABLE IF NOT EXISTS fotmob_strength_runs(
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  http_calls INTEGER NOT NULL DEFAULT 0,
  leagues_ok INTEGER NOT NULL DEFAULT 0,
  player_rows INTEGER NOT NULL DEFAULT 0,
  team_rows INTEGER NOT NULL DEFAULT 0,
  message TEXT
);
"""

def f(v:Any)->Optional[float]:
    try:
        if v in (None,"","-"): return None
        if isinstance(v,dict):
            for k in ("value","num","statValue"):
                if k in v: return f(v[k])
            return None
        return float(v)
    except Exception: return None

def s(v:Any)->Optional[str]:
    if v in (None,""): return None
    return str(v)

def recursive_season_id(obj:Any,label:str)->Optional[str]:
    want=label.replace(" ","").lower()
    if isinstance(obj,dict):
        name=str(obj.get("name") or obj.get("seasonName") or obj.get("label") or obj.get("year") or "")
        if want in name.replace(" ","").lower():
            for k in ("id","seasonId","season_id"):
                if obj.get(k) is not None:
                    return str(obj[k])
        for v in obj.values():
            got=recursive_season_id(v,label)
            if got: return got
    elif isinstance(obj,list):
        for v in obj:
            got=recursive_season_id(v,label)
            if got: return got
    return None

def stats_rows(payload:Dict[str,Any])->List[Dict[str,Any]]:
    rows=payload.get("statsData")
    if isinstance(rows,list): return [x for x in rows if isinstance(x,dict)]
    for key in ("data","items","stats"):
        v=payload.get(key)
        if isinstance(v,list) and (not v or isinstance(v[0],dict)): return v
    return []

def player_id(row:Dict[str,Any])->Optional[str]:
    for k in ("participantId","playerId","id"):
        if row.get(k) is not None: return str(row[k])
    return None

def team_id(row:Dict[str,Any])->Optional[str]:
    for k in ("teamId","team_id"):
        if row.get(k) is not None: return str(row[k])
    return None

def name_of(row:Dict[str,Any])->Optional[str]:
    for k in ("participantName","playerName","name"):
        if row.get(k): return str(row[k])
    return None

def team_name_of(row:Dict[str,Any])->Optional[str]:
    for k in ("teamName","team_name"):
        if row.get(k): return str(row[k])
    return None

def percentile_map(vals:Dict[str,Optional[float]])->Dict[str,float]:
    finite=sorted(v for v in vals.values() if v is not None and math.isfinite(v))
    if not finite: return {}
    out={}; n=len(finite)
    for k,v in vals.items():
        if v is None or not math.isfinite(v): continue
        below=sum(1 for x in finite if x < v)
        equal=sum(1 for x in finite if x == v)
        out[k]=(below+0.5*equal)/n
    return out

class Importer:
    def __init__(self,database_url:Optional[str]=None):
        self.db=(database_url or DATABASE_URL).strip()
        if not self.db: raise RuntimeError("Missing DATABASE_URL")
        self.conn=psycopg.connect(self.db,autocommit=True)
        self.conn.execute(SCHEMA_SQL)
        self.ses=requests.Session()
        self.ses.headers.update({"Accept":"application/json","User-Agent":"Mozilla/5.0 Chrome/152 Safari/537.36"})
        self.calls=0; self.last=0.0
    def close(self): self.conn.close()
    def get(self,path:str)->Dict[str,Any]:
        wait=REQUEST_DELAY-(time.monotonic()-self.last)
        if wait>0: time.sleep(wait)
        r=self.ses.get(BASE+path,timeout=35)
        self.last=time.monotonic(); self.calls+=1
        if r.status_code in (403,429): raise RuntimeError(f"FotMob HTTP {r.status_code}")
        r.raise_for_status()
        d=r.json()
        return d if isinstance(d,dict) else {"data":d}

    def deep(self,league_id:int,season_id:str,typ:str,stat:str)->List[Dict[str,Any]]:
        return stats_rows(self.get(f"/leagueseasondeepstats?id={league_id}&season={season_id}&type={typ}&stat={stat}"))

    def run_league(self,league_id:int,league_name:str,snap:date)->Tuple[int,int]:
        overview=self.get(f"/leagues?id={league_id}&season={SEASON_LABEL.replace('/','%2F')}")
        season_id=recursive_season_id(overview,SEASON_LABEL)
        if not season_id:
            raise RuntimeError(f"FotMob season id not found for {league_name} {SEASON_LABEL}")

        players:Dict[str,Dict[str,Any]]={}
        for stat in PLAYER_STATS:
            for row in self.deep(league_id,season_id,"players",stat):
                pid=player_id(row)
                if not pid: continue
                p=players.setdefault(pid,{
                    "player_id":pid,"player_name":name_of(row),"team_id":team_id(row),"team_name":team_name_of(row),
                    "position":s(row.get("position") or row.get("positionLabel") or row.get("role")),
                    "minutes":f(row.get("minutesPlayed") or row.get("minutes_played")),
                    "matches":f(row.get("matchesPlayed") or row.get("matches_played")),
                    "metrics":{}
                })
                p["player_name"]=p["player_name"] or name_of(row)
                p["team_id"]=p["team_id"] or team_id(row); p["team_name"]=p["team_name"] or team_name_of(row)
                p["position"]=p["position"] or s(row.get("position") or row.get("positionLabel"))
                p["minutes"]=p["minutes"] if p["minutes"] is not None else f(row.get("minutesPlayed"))
                p["matches"]=p["matches"] if p["matches"] is not None else f(row.get("matchesPlayed"))
                p["metrics"][stat]=f(row.get("statValue"))

        pct_by_stat={stat:percentile_map({pid:p["metrics"].get(stat) for pid,p in players.items()}) for stat in PLAYER_STATS}
        min_pct=percentile_map({pid:p.get("minutes") for pid,p in players.items()})
        weights={"rating":0.40,"expected_goals_per_90":0.18,"expected_assists_per_90":0.14,"goals_per_90":0.10,"goal_assist":0.08,"minutes":0.10}
        for pid,p in players.items():
            comps=[]
            for stat,w in weights.items():
                val=min_pct.get(pid) if stat=="minutes" else pct_by_stat.get(stat,{}).get(pid)
                if val is not None: comps.append((w,val))
            p["strength"]=sum(w*v for w,v in comps)/sum(w for w,_ in comps) if comps else None
            p["percentiles"]={stat:pct_by_stat.get(stat,{}).get(pid) for stat in PLAYER_STATS if pid in pct_by_stat.get(stat,{})}
            if pid in min_pct: p["percentiles"]["minutes"]=min_pct[pid]

        teams:Dict[str,Dict[str,Any]]={}
        for stat in TEAM_STATS:
            for row in self.deep(league_id,season_id,"teams",stat):
                tid=team_id(row) or (str(row.get("participantId")) if row.get("participantId") is not None else None)
                if not tid: continue
                t=teams.setdefault(tid,{"team_id":tid,"team_name":team_name_of(row) or name_of(row),"metrics":{}})
                t["team_name"]=t["team_name"] or team_name_of(row) or name_of(row)
                t["metrics"][stat]=f(row.get("statValue"))

        medians={}
        for stat in TEAM_STATS:
            vals=sorted(t["metrics"].get(stat) for t in teams.values() if t["metrics"].get(stat) is not None)
            medians[stat]=(vals[len(vals)//2] if vals else None)
        for t in teams.values():
            t["relative"]={}
            for stat,v in t["metrics"].items():
                m=medians.get(stat)
                if v is not None and m not in (None,0):
                    t["relative"][stat]=max(0.4,min(2.5,v/m))

        by_team:Dict[str,List[Dict[str,Any]]]=defaultdict(list)
        for p in players.values():
            if p.get("team_id"): by_team[p["team_id"]].append(p)

        player_rows=0
        for p in players.values():
            self.conn.execute(
                """INSERT INTO fotmob_player_strength_snapshots(snapshot_date,league_name,league_id,season_label,season_id,
                   player_id,player_name,team_id,team_name,position,minutes_played,matches_played,metrics,percentiles,strength_score,fetched_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                   ON CONFLICT(snapshot_date,league_id,player_id) DO UPDATE SET player_name=EXCLUDED.player_name,
                     team_id=EXCLUDED.team_id,team_name=EXCLUDED.team_name,position=EXCLUDED.position,
                     minutes_played=EXCLUDED.minutes_played,matches_played=EXCLUDED.matches_played,
                     metrics=EXCLUDED.metrics,percentiles=EXCLUDED.percentiles,strength_score=EXCLUDED.strength_score,fetched_at=NOW()""",
                (snap,league_name,league_id,SEASON_LABEL,season_id,p["player_id"],p["player_name"],p["team_id"],p["team_name"],
                 p["position"],p["minutes"],p["matches"],Jsonb(p["metrics"]),Jsonb(p["percentiles"]),p["strength"])
            ); player_rows+=1

        team_rows=0
        for tid,t in teams.items():
            plist=sorted((p for p in by_team.get(tid,[]) if p.get("strength") is not None),
                         key=lambda p:(p.get("minutes") or 0,p.get("strength") or 0),reverse=True)[:11]
            top11=sum(p["strength"] for p in plist)/len(plist) if plist else None
            self.conn.execute(
                """INSERT INTO fotmob_team_style_snapshots(snapshot_date,league_name,league_id,season_label,season_id,
                   team_id,team_name,metrics,relative_metrics,top11_strength,player_coverage,fetched_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                   ON CONFLICT(snapshot_date,league_id,team_id) DO UPDATE SET team_name=EXCLUDED.team_name,
                     metrics=EXCLUDED.metrics,relative_metrics=EXCLUDED.relative_metrics,top11_strength=EXCLUDED.top11_strength,
                     player_coverage=EXCLUDED.player_coverage,fetched_at=NOW()""",
                (snap,league_name,league_id,SEASON_LABEL,season_id,tid,t["team_name"],Jsonb(t["metrics"]),Jsonb(t["relative"]),
                 top11,len(by_team.get(tid,[])))
            ); team_rows+=1
        return player_rows,team_rows

    def run(self)->Dict[str,Any]:
        rid=self.conn.execute("INSERT INTO fotmob_strength_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        snap=datetime.now(timezone.utc).date(); ok=pr=tr=0; errors={}
        for lid,lname in LEAGUES:
            try:
                a,b=self.run_league(lid,lname,snap); pr+=a; tr+=b; ok+=1
            except Exception as exc:
                errors[lname]=str(exc)[:300]
        status="success" if ok>=4 else ("partial" if ok else "failed")
        self.conn.execute(
            """UPDATE fotmob_strength_runs SET finished_at=NOW(),status=%s,http_calls=%s,leagues_ok=%s,
               player_rows=%s,team_rows=%s,message=%s WHERE id=%s""",
            (status,self.calls,ok,pr,tr,json.dumps(errors,separators=(",",":")),rid)
        )
        result={"status":status,"http_calls":self.calls,"leagues_ok":ok,"player_rows":pr,"team_rows":tr,"errors":errors}
        print("FOTMOB_STRENGTH_RESULT",json.dumps(result,separators=(",",":")))
        return result

def run_import(database_url:Optional[str]=None)->Dict[str,Any]:
    imp=Importer(database_url)
    try:return imp.run()
    finally:imp.close()

if __name__=="__main__":
    print(json.dumps(run_import(),ensure_ascii=False,indent=2))
