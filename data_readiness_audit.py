#!/usr/bin/env python3
"""Audit whether an upcoming fixture has enough data for each prediction market.

The audit deliberately distinguishes provisional analysis from final pre-kickoff
selection. Confirmed lineups are often unavailable days ahead, so their absence
reduces final readiness without blocking early model work.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

DATABASE_URL=os.getenv("DATABASE_URL","").strip()

SCHEMA="""
CREATE TABLE IF NOT EXISTS prediction_readiness_snapshots(
    event_id TEXT NOT NULL,
    snapshot_hour TIMESTAMPTZ NOT NULL,
    match_date TIMESTAMPTZ NOT NULL,
    league_name TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_history_matches INTEGER NOT NULL DEFAULT 0,
    away_history_matches INTEGER NOT NULL DEFAULT 0,
    home_corner_matches INTEGER NOT NULL DEFAULT 0,
    away_corner_matches INTEGER NOT NULL DEFAULT 0,
    xg_home_matches INTEGER NOT NULL DEFAULT 0,
    xg_away_matches INTEGER NOT NULL DEFAULT 0,
    schedule_complete BOOLEAN NOT NULL DEFAULT FALSE,
    lineup_available BOOLEAN NOT NULL DEFAULT FALSE,
    availability_present BOOLEAN NOT NULL DEFAULT FALSE,
    availability_stale BOOLEAN,
    odds_ou25 BOOLEAN NOT NULL DEFAULT FALSE,
    odds_btts BOOLEAN NOT NULL DEFAULT FALSE,
    odds_corner85 BOOLEAN NOT NULL DEFAULT FALSE,
    goals_provisional_ready BOOLEAN NOT NULL DEFAULT FALSE,
    btts_provisional_ready BOOLEAN NOT NULL DEFAULT FALSE,
    corners_provisional_ready BOOLEAN NOT NULL DEFAULT FALSE,
    final_context_ready BOOLEAN NOT NULL DEFAULT FALSE,
    readiness_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    blockers JSONB NOT NULL,
    built_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(event_id,snapshot_hour)
);
CREATE INDEX IF NOT EXISTS idx_prediction_readiness_date ON prediction_readiness_snapshots(match_date,snapshot_hour DESC);

CREATE TABLE IF NOT EXISTS data_readiness_runs(
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    fixtures INTEGER NOT NULL DEFAULT 0,
    goals_ready INTEGER NOT NULL DEFAULT 0,
    btts_ready INTEGER NOT NULL DEFAULT 0,
    corners_ready INTEGER NOT NULL DEFAULT 0,
    final_context_ready INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""

ALIASES={"man utd":"manchester united","man united":"manchester united","man city":"manchester city","nottm forest":"nottingham forest","wolves":"wolverhampton wanderers","spurs":"tottenham hotspur","milan":"ac milan","inter":"inter milan","psg":"paris saint germain","paris sg":"paris saint germain"}
def canon(v:Any)->str:
    s=unicodedata.normalize("NFKD",str(v or "")).encode("ascii","ignore").decode().lower().replace("'","")
    s=re.sub(r"\b(fc|cf|ssc|club|football club)\b"," ",s);s=re.sub(r"[^a-z0-9]+"," ",s).strip();s=re.sub(r"\s+"," ",s)
    return ALIASES.get(s,s)
def sim(a:Any,b:Any)->float:
    a,b=canon(a),canon(b)
    if not a or not b:return 0.0
    return 1.0 if a==b else SequenceMatcher(None,a,b).ratio()

class Audit:
    def __init__(self,db:Optional[str]=None):
        self.db=(db or DATABASE_URL).strip()
        if not self.db:raise RuntimeError("Missing DATABASE_URL")
        self.conn=psycopg.connect(self.db,autocommit=True);self.conn.execute(SCHEMA)
    def close(self):self.conn.close()
    def history_counts(self,team:str,league:str,before)->tuple[int,int]:
        rows=self.conn.execute("""SELECT home_team,away_team,total_corners FROM football_data_matches WHERE league_name=%s AND match_date < %s ORDER BY match_date DESC LIMIT 1000""",(league,before.date())).fetchall()
        n=c=0
        for h,a,tc in rows:
            if max(sim(team,h),sim(team,a))>=.78:
                n+=1;c+=int(tc is not None)
        # Add current-season completed rows not already in Football-Data mirror.
        rows2=self.conn.execute("""SELECT home_team,away_team,total_corners FROM espn_current_matches WHERE league_name=%s AND match_date < %s ORDER BY match_date DESC""",(league,before)).fetchall()
        for h,a,tc in rows2:
            if max(sim(team,h),sim(team,a))>=.78:
                n+=1;c+=int(tc is not None)
        return n,c
    def xg_count(self,team:str,league:str,before)->int:
        rows=self.conn.execute("""SELECT home_team,away_team FROM understat_matches WHERE league_name=%s AND match_date < %s AND is_result=TRUE AND home_xg IS NOT NULL AND away_xg IS NOT NULL ORDER BY match_date DESC LIMIT 500""",(league,before.date())).fetchall()
        return sum(1 for h,a in rows if max(sim(team,h),sim(team,a))>=.78)
    def run(self)->Dict[str,Any]:
        rid=self.conn.execute("INSERT INTO data_readiness_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        fixtures=gr=br=cr=fr=0
        try:
            rows=self.conn.execute("""SELECT DISTINCT ON(event_id) event_id,snapshot_hour,match_date,league_name,home_team,away_team,home_days_rest,away_days_rest,lineup_entries,roster_entries,has_ou25,has_btts,has_corner85,availability_as_of,availability_stale,data_quality FROM prematch_feature_snapshots ORDER BY event_id,snapshot_hour DESC""").fetchall()
            for event_id,hour,dt,league,home,away,hr,ar,lineup,roster,ou,btts,corner,avail_asof,avail_stale,q in rows:
                hn,hc=self.history_counts(home,league,dt);an,ac=self.history_counts(away,league,dt);hx=self.xg_count(home,league,dt);ax=self.xg_count(away,league,dt)
                schedule=hr is not None and ar is not None;lineup_ok=(lineup or 0)>0 or (roster or 0)>0;availability=avail_asof is not None and avail_stale is not True
                goals=hn>=10 and an>=10 and hx>=3 and ax>=3 and bool(ou)
                btts_r=hn>=10 and an>=10 and hx>=3 and ax>=3 and bool(btts)
                corners=hc>=10 and ac>=10 and bool(corner)
                final_context=schedule and lineup_ok and availability
                blockers=[]
                if hn<10 or an<10:blockers.append("insufficient_match_history")
                if hx<3 or ax<3:blockers.append("insufficient_xg_history")
                if hc<10 or ac<10:blockers.append("insufficient_corner_history")
                if not schedule:blockers.append("schedule_context_missing")
                if not lineup_ok:blockers.append("lineup_not_yet_available")
                if not availability:blockers.append("fresh_availability_missing")
                if not ou:blockers.append("ou25_odds_missing")
                if not btts:blockers.append("btts_odds_missing")
                if not corner:blockers.append("corner85_odds_missing")
                components=[hn>=10 and an>=10,hx>=3 and ax>=3,hc>=10 and ac>=10,schedule,lineup_ok,availability,bool(ou),bool(btts),bool(corner)]
                score=round(sum(bool(x) for x in components)/len(components),4)
                self.conn.execute("""INSERT INTO prediction_readiness_snapshots(event_id,snapshot_hour,match_date,league_name,home_team,away_team,home_history_matches,away_history_matches,home_corner_matches,away_corner_matches,xg_home_matches,xg_away_matches,schedule_complete,lineup_available,availability_present,availability_stale,odds_ou25,odds_btts,odds_corner85,goals_provisional_ready,btts_provisional_ready,corners_provisional_ready,final_context_ready,readiness_score,blockers) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id,snapshot_hour) DO UPDATE SET home_history_matches=EXCLUDED.home_history_matches,away_history_matches=EXCLUDED.away_history_matches,home_corner_matches=EXCLUDED.home_corner_matches,away_corner_matches=EXCLUDED.away_corner_matches,xg_home_matches=EXCLUDED.xg_home_matches,xg_away_matches=EXCLUDED.xg_away_matches,schedule_complete=EXCLUDED.schedule_complete,lineup_available=EXCLUDED.lineup_available,availability_present=EXCLUDED.availability_present,availability_stale=EXCLUDED.availability_stale,odds_ou25=EXCLUDED.odds_ou25,odds_btts=EXCLUDED.odds_btts,odds_corner85=EXCLUDED.odds_corner85,goals_provisional_ready=EXCLUDED.goals_provisional_ready,btts_provisional_ready=EXCLUDED.btts_provisional_ready,corners_provisional_ready=EXCLUDED.corners_provisional_ready,final_context_ready=EXCLUDED.final_context_ready,readiness_score=EXCLUDED.readiness_score,blockers=EXCLUDED.blockers,built_at=NOW()""",(event_id,hour,dt,league,home,away,hn,an,hc,ac,hx,ax,schedule,lineup_ok,availability,avail_stale,bool(ou),bool(btts),bool(corner),goals,btts_r,corners,final_context,score,Jsonb(blockers)))
                fixtures+=1;gr+=int(goals);br+=int(btts_r);cr+=int(corners);fr+=int(final_context)
            self.conn.execute("UPDATE data_readiness_runs SET finished_at=NOW(),status='success',fixtures=%s,goals_ready=%s,btts_ready=%s,corners_ready=%s,final_context_ready=%s,message='ok' WHERE id=%s",(fixtures,gr,br,cr,fr,rid))
            result={"status":"success","fixtures":fixtures,"goals_ready":gr,"btts_ready":br,"corners_ready":cr,"final_context_ready":fr};print("DATA_READINESS_RESULT",json.dumps(result,separators=(",",":")));return result
        except Exception as exc:
            self.conn.execute("UPDATE data_readiness_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:1000],rid));raise

def run_audit(database_url:Optional[str]=None)->Dict[str,Any]:
    a=Audit(database_url)
    try:return a.run()
    finally:a.close()
if __name__=='__main__':print(json.dumps(run_audit(),ensure_ascii=False,indent=2))
