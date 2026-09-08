#!/usr/bin/env python3
"""Collect team schedule pages for clubs in upcoming Big Five fixtures.

This complements the domestic-league scoreboard. ESPN's team schedule endpoint
can expose a club's broader fixture context; every returned event is preserved
with competition metadata. The downstream builder uses it when available and
falls back to domestic-league history otherwise.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb
import requests

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
REQUEST_DELAY=float(os.getenv("ESPN_TEAM_SCHEDULE_DELAY_SECONDS","0.08"))
LOG_LEVEL=os.getenv("LOG_LEVEL","INFO").upper()
BASE="https://site.api.espn.com/apis/site/v2/sports/soccer"
logging.basicConfig(level=getattr(logging,LOG_LEVEL,logging.INFO),format="%(asctime)s | %(levelname)s | %(message)s")
log=logging.getLogger("espn-team-schedule")

SCHEMA="""
CREATE TABLE IF NOT EXISTS espn_team_schedule_events(
    league_slug TEXT NOT NULL,
    team_id TEXT NOT NULL,
    team_name TEXT,
    event_id TEXT NOT NULL,
    match_date TIMESTAMPTZ,
    competition_name TEXT,
    competition_slug TEXT,
    opponent_name TEXT,
    home_away TEXT,
    status TEXT,
    completed BOOLEAN,
    raw JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(team_id,event_id)
);
CREATE INDEX IF NOT EXISTS idx_espn_team_sched_team_date ON espn_team_schedule_events(team_id,match_date);

CREATE TABLE IF NOT EXISTS espn_team_schedule_runs(
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    teams INTEGER NOT NULL DEFAULT 0,
    events INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""

def parse_dt(v:Any)->Optional[datetime]:
    if not v:return None
    try:
        dt=datetime.fromisoformat(str(v).replace("Z","+00:00"));return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:return None

def as_events(payload:Dict[str,Any])->List[Dict[str,Any]]:
    for key in ("events","items","results"):
        v=payload.get(key)
        if isinstance(v,list):return [x for x in v if isinstance(x,dict)]
    return []

def team_name_from_comp(c:Dict[str,Any])->str:
    t=c.get("team") if isinstance(c.get("team"),dict) else {}
    return str(t.get("displayName") or t.get("shortDisplayName") or t.get("name") or "")

class Importer:
    def __init__(self,db:Optional[str]=None)->None:
        self.db=(db or DATABASE_URL).strip()
        if not self.db:raise RuntimeError("Missing DATABASE_URL")
        self.conn=psycopg.connect(self.db,autocommit=True);self.conn.execute(SCHEMA);self.session=requests.Session()
    def close(self):self.conn.close()
    def teams(self)->List[Tuple[str,str,str]]:
        rows=self.conn.execute("""SELECT DISTINCT league_slug,home_team_id,home_team FROM espn_upcoming WHERE is_current=TRUE AND home_team_id IS NOT NULL UNION SELECT DISTINCT league_slug,away_team_id,away_team FROM espn_upcoming WHERE is_current=TRUE AND away_team_id IS NOT NULL""").fetchall()
        return [(str(a),str(b),str(c)) for a,b,c in rows if b]
    def get(self,url:str)->Dict[str,Any]:
        last=None
        for attempt in range(3):
            try:
                if REQUEST_DELAY:time.sleep(REQUEST_DELAY)
                r=self.session.get(url,timeout=25)
                if r.status_code>=500:time.sleep(2**attempt);continue
                r.raise_for_status();j=r.json();return j if isinstance(j,dict) else {"data":j}
            except Exception as exc:last=exc;time.sleep(2**attempt)
        raise RuntimeError(f"ESPN team schedule failed: {last}")
    def store_event(self,league:str,team_id:str,team_name:str,e:Dict[str,Any])->bool:
        eid=str(e.get("id") or "")
        if not eid:return False
        dt=parse_dt(e.get("date"))
        comps=e.get("competitions") if isinstance(e.get("competitions"),list) else []
        comp=comps[0] if comps and isinstance(comps[0],dict) else {}
        competitors=comp.get("competitors") if isinstance(comp.get("competitors"),list) else []
        me=next((x for x in competitors if str((x.get("team") or {}).get("id") or "")==team_id),None)
        opp=next((x for x in competitors if str((x.get("team") or {}).get("id") or "")!=team_id),None)
        homeaway=str((me or {}).get("homeAway") or "") or None
        opp_name=team_name_from_comp(opp or {}) or None
        status_obj=(e.get("status") or {}).get("type") if isinstance(e.get("status"),dict) else {}
        status=str((status_obj or {}).get("name") or (status_obj or {}).get("description") or "") or None
        completed=bool((status_obj or {}).get("completed")) if isinstance(status_obj,dict) else None
        league_obj=e.get("league") if isinstance(e.get("league"),dict) else {}
        comp_name=str(league_obj.get("name") or league_obj.get("abbreviation") or "") or None
        comp_slug=str(league_obj.get("slug") or "") or None
        self.conn.execute("""INSERT INTO espn_team_schedule_events(league_slug,team_id,team_name,event_id,match_date,competition_name,competition_slug,opponent_name,home_away,status,completed,raw) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(team_id,event_id) DO UPDATE SET team_name=EXCLUDED.team_name,match_date=EXCLUDED.match_date,competition_name=EXCLUDED.competition_name,competition_slug=EXCLUDED.competition_slug,opponent_name=EXCLUDED.opponent_name,home_away=EXCLUDED.home_away,status=EXCLUDED.status,completed=EXCLUDED.completed,raw=EXCLUDED.raw,updated_at=NOW()""",(league,team_id,team_name,eid,dt,comp_name,comp_slug,opp_name,homeaway,status,completed,Jsonb(e)))
        return True
    def run(self)->Dict[str,Any]:
        rid=self.conn.execute("INSERT INTO espn_team_schedule_runs(status) VALUES('running') RETURNING id").fetchone()[0];teams=events=0
        try:
            for league,tid,tname in self.teams():
                payload=self.get(f"{BASE}/{league}/teams/{tid}/schedule")
                for e in as_events(payload):
                    events+=int(self.store_event(league,tid,tname,e))
                teams+=1
            self.conn.execute("UPDATE espn_team_schedule_runs SET finished_at=NOW(),status='success',teams=%s,events=%s,message='ok' WHERE id=%s",(teams,events,rid))
            result={"status":"success","teams":teams,"events":events};log.info("ESPN_TEAM_SCHEDULE_RESULT %s",json.dumps(result,separators=(",",":")));return result
        except Exception as exc:
            self.conn.execute("UPDATE espn_team_schedule_runs SET finished_at=NOW(),status='failed',teams=%s,events=%s,message=%s WHERE id=%s",(teams,events,str(exc)[:1000],rid));raise

def run_import(database_url:Optional[str]=None)->Dict[str,Any]:
    i=Importer(database_url)
    try:return i.run()
    finally:i.close()

if __name__=='__main__':print(json.dumps(run_import(),ensure_ascii=False,indent=2))
