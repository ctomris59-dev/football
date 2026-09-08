#!/usr/bin/env python3
"""BBS documented Big Five scheduled-match and stored-lineup snapshot importer."""
from __future__ import annotations
import json, logging, os, re, time, unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional
import psycopg
from psycopg.types.json import Jsonb
import requests

DATABASE_URL=os.getenv("DATABASE_URL","").strip();API_KEY=os.getenv("BBS_API_KEY","").strip();BASE="https://api.bigballsdata.com"
LOOKAHEAD_DAYS=int(os.getenv("BBS_LINEUP_LOOKAHEAD_DAYS","6"));REQUEST_DELAY=float(os.getenv("BBS_LINEUP_REQUEST_DELAY_SECONDS","0.20"));LOG_LEVEL=os.getenv("LOG_LEVEL","INFO").upper()
LEAGUES=[("epl","Premier League"),("laliga","La Liga"),("seriea","Serie A"),("bundesliga","Bundesliga"),("ligue1","Ligue 1")]
logging.basicConfig(level=getattr(logging,LOG_LEVEL,logging.INFO),format="%(asctime)s | %(levelname)s | %(message)s");log=logging.getLogger("bbs-lineups")
SCHEMA="""
CREATE TABLE IF NOT EXISTS bbs_lineup_snapshots(espn_event_id TEXT NOT NULL,bbs_match_id TEXT NOT NULL,snapshot_hour TIMESTAMPTZ NOT NULL,match_date TIMESTAMPTZ NOT NULL,league_name TEXT NOT NULL,home_team TEXT NOT NULL,away_team TEXT NOT NULL,home_starters INTEGER,away_starters INTEGER,home_bench INTEGER,away_bench INTEGER,explicit_confirmed BOOLEAN,raw JSONB NOT NULL,fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(espn_event_id,snapshot_hour));
CREATE INDEX IF NOT EXISTS idx_bbs_lineup_match ON bbs_lineup_snapshots(match_date,snapshot_hour DESC);
CREATE TABLE IF NOT EXISTS bbs_lineup_runs(id BIGSERIAL PRIMARY KEY,started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),finished_at TIMESTAMPTZ,status TEXT NOT NULL,api_calls INTEGER NOT NULL DEFAULT 0,league_calls INTEGER NOT NULL DEFAULT 0,candidate_matches INTEGER NOT NULL DEFAULT 0,mapped_matches INTEGER NOT NULL DEFAULT 0,lineup_calls INTEGER NOT NULL DEFAULT 0,lineups_present INTEGER NOT NULL DEFAULT 0,message TEXT);
"""
ALIASES={"man utd":"manchester united","man united":"manchester united","man city":"manchester city","nottm forest":"nottingham forest","wolves":"wolverhampton wanderers","spurs":"tottenham hotspur","tottenham":"tottenham hotspur","milan":"ac milan","inter":"inter milan","psg":"paris saint germain","paris sg":"paris saint germain","ath bilbao":"athletic club","athletic bilbao":"athletic club","mgladbach":"borussia monchengladbach","borussia m gladbach":"borussia monchengladbach"}
def canon(v:Any)->str:
 s=unicodedata.normalize("NFKD",str(v or "")).encode("ascii","ignore").decode().lower().replace("'","");s=re.sub(r"\b(fc|cf|ssc|ac|club|football club)\b"," ",s);s=re.sub(r"[^a-z0-9]+"," ",s).strip();s=re.sub(r"\s+"," ",s);return ALIASES.get(s,s)
def sim(a:Any,b:Any)->float:
 a,b=canon(a),canon(b);return 0.0 if not a or not b else (1.0 if a==b else SequenceMatcher(None,a,b).ratio())
def parse_dt(v:Any)->Optional[datetime]:
 if not v:return None
 if isinstance(v,(int,float)):
  try:return datetime.fromtimestamp(float(v)/1000.0 if float(v)>1e11 else float(v),tz=timezone.utc)
  except Exception:return None
 try:
  d=datetime.fromisoformat(str(v).replace("Z","+00:00"));return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
 except Exception:return None
def first(d:Dict[str,Any],keys)->Any:
 for k in keys:
  if d.get(k) not in (None,""):return d.get(k)
 return None
def nested_name(v:Any)->str:
 if isinstance(v,dict):return str(first(v,("name","display_name","short_name")) or "")
 return str(v or "")
def rows_from_matches(payload:Dict[str,Any])->List[Dict[str,Any]]:
 data=payload.get("data")
 if isinstance(data,list):return [x for x in data if isinstance(x,dict)]
 if isinstance(data,dict):
  for k in ("matches","items","results","rows"):
   v=data.get(k)
   if isinstance(v,list):return [x for x in v if isinstance(x,dict)]
 return []
def count_role(obj:Any,needles)->int:
 total=0
 if isinstance(obj,dict):
  for k,v in obj.items():
   if isinstance(v,list) and any(n in str(k).lower() for n in needles):total+=len(v)
   total+=count_role(v,needles)
 elif isinstance(obj,list):
  for x in obj:total+=count_role(x,needles)
 return total
def find_bool(obj:Any,keys)->Optional[bool]:
 if isinstance(obj,dict):
  for k,v in obj.items():
   if str(k).lower() in keys and isinstance(v,bool):return v
   r=find_bool(v,keys)
   if r is not None:return r
 elif isinstance(obj,list):
  for x in obj:
   r=find_bool(x,keys)
   if r is not None:return r
 return None
class Importer:
 def __init__(self,db:Optional[str]=None):
  self.db=(db or DATABASE_URL).strip()
  if not self.db:raise RuntimeError("Missing DATABASE_URL")
  if not API_KEY:raise RuntimeError("BBS_API_KEY is not configured")
  self.conn=psycopg.connect(self.db,autocommit=True);self.conn.execute(SCHEMA);self.s=requests.Session();self.calls=0;self.last=0.0
 def close(self):self.conn.close()
 def get(self,path:str,params:Optional[Dict[str,Any]]=None)->Dict[str,Any]:
  e=time.monotonic()-self.last
  if e<REQUEST_DELAY:time.sleep(REQUEST_DELAY-e)
  r=self.s.get(BASE+path,params=params,headers={"Authorization":f"Bearer {API_KEY}"},timeout=30);self.last=time.monotonic();self.calls+=1
  if r.status_code==404:return {"_http_status":404}
  r.raise_for_status();d=r.json();return d if isinstance(d,dict) else {"data":d}
 def upcoming(self):
  return list(self.conn.execute("SELECT event_id,match_date,league_name,home_team,away_team FROM espn_upcoming WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+(%s||' days')::interval ORDER BY match_date",(LOOKAHEAD_DAYS,)).fetchall())
 def match_row(self,dt,home,away,rows):
  best=None;score=-1.0
  for r in rows:
   h=nested_name(first(r,("home","home_team","homeTeam")));a=nested_name(first(r,("away","away_team","awayTeam")))
   rd=parse_dt(first(r,("kickoff_utc","kickoffUtc","start_time","startTime","kickoff","date","match_date")))
   if not rd:continue
   hours=abs((rd-dt).total_seconds())/3600.0
   if hours>8:continue
   sh,sa=sim(home,h),sim(away,a)
   if min(sh,sa)<.60:continue
   s=sh+sa-min(.25,hours/24.0)
   if s>score:best,score=r,s
  return best
 def run(self)->Dict[str,Any]:
  rid=self.conn.execute("INSERT INTO bbs_lineup_runs(status) VALUES('running') RETURNING id").fetchone()[0];league_calls=candidates=mapped=lineup_calls=present=0
  try:
   upcoming=self.upcoming();by_league={}
   for key,name in LEAGUES:
    p=self.get("/v1/matches",{"sport":"football","league":key,"limit":200});league_calls+=1;rows=rows_from_matches(p);by_league[name]=rows;candidates+=len(rows)
   hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)
   for eid,dt,league,home,away in upcoming:
    row=self.match_row(dt,home,away,by_league.get(league,[]))
    if not row:continue
    mid=str(first(row,("id","match_id","matchId")) or "")
    if not mid:continue
    mapped+=1;raw=self.get(f"/v1/stored/matches/{mid}/lineups");lineup_calls+=1
    if raw.get("_http_status")==404:continue
    starters=count_role(raw,("starting","starters","startingxi","starting_xi"));bench=count_role(raw,("bench","substitutes","subs"));data=raw.get("data") if isinstance(raw.get("data"),dict) else raw
    hobj=data.get("home") if isinstance(data,dict) else None;aobj=data.get("away") if isinstance(data,dict) else None
    hs=count_role(hobj,("starting","starters","startingxi","starting_xi")) if hobj is not None else None;ass=count_role(aobj,("starting","starters","startingxi","starting_xi")) if aobj is not None else None;hb=count_role(hobj,("bench","substitutes","subs")) if hobj is not None else None;ab=count_role(aobj,("bench","substitutes","subs")) if aobj is not None else None
    if hs is None and starters:hs=starters//2
    if ass is None and starters:ass=starters-hs
    if hb is None and bench:hb=bench//2
    if ab is None and bench:ab=bench-hb
    explicit=find_bool(raw,{"confirmed","isconfirmed","is_confirmed","lineupsconfirmed"});has=bool((hs or 0)+(ass or 0)+(hb or 0)+(ab or 0));present+=int(has)
    self.conn.execute("INSERT INTO bbs_lineup_snapshots(espn_event_id,bbs_match_id,snapshot_hour,match_date,league_name,home_team,away_team,home_starters,away_starters,home_bench,away_bench,explicit_confirmed,raw) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(espn_event_id,snapshot_hour) DO UPDATE SET bbs_match_id=EXCLUDED.bbs_match_id,home_starters=EXCLUDED.home_starters,away_starters=EXCLUDED.away_starters,home_bench=EXCLUDED.home_bench,away_bench=EXCLUDED.away_bench,explicit_confirmed=EXCLUDED.explicit_confirmed,raw=EXCLUDED.raw,fetched_at=NOW()",(eid,mid,hour,dt,league,home,away,hs,ass,hb,ab,explicit,Jsonb(raw)))
   self.conn.execute("UPDATE bbs_lineup_runs SET finished_at=NOW(),status='success',api_calls=%s,league_calls=%s,candidate_matches=%s,mapped_matches=%s,lineup_calls=%s,lineups_present=%s,message='ok' WHERE id=%s",(self.calls,league_calls,candidates,mapped,lineup_calls,present,rid));res={"status":"success","api_calls":self.calls,"candidate_matches":candidates,"mapped":mapped,"lineup_calls":lineup_calls,"lineups_present":present};log.info("BBS_LINEUPS_RESULT %s",json.dumps(res,separators=(",",":")));return res
  except Exception as exc:
   self.conn.execute("UPDATE bbs_lineup_runs SET finished_at=NOW(),status='failed',api_calls=%s,league_calls=%s,candidate_matches=%s,mapped_matches=%s,lineup_calls=%s,lineups_present=%s,message=%s WHERE id=%s",(self.calls,league_calls,candidates,mapped,lineup_calls,present,str(exc)[:1000],rid));raise
def run_import(database_url:Optional[str]=None)->Dict[str,Any]:
 x=Importer(database_url)
 try:return x.run()
 finally:x.close()
if __name__=="__main__":print(json.dumps(run_import(),ensure_ascii=False,indent=2))
