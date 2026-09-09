#!/usr/bin/env python3
"""Free FBref 2025/26 Big-Five player Starts + Minutes cache.

Primary production-continuity source. Direct FBref HTML is attempted first; the free
Jina Reader rendering is a fail-soft fallback for Cloudflare/edge blocks. No value is
inferred: only rows with an explicit player, squad, Starts and Minutes field are
stored. League/team aggregate gates reject truncated or malformed responses.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import time
from collections import defaultdict
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

import psycopg
import requests
from psycopg.types.json import Jsonb

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
SEASON=int(os.getenv("FBREF_PREVIOUS_SEASON","2025"))
SEASON_LABEL=os.getenv("FBREF_PREVIOUS_SEASON_LABEL","2025-2026")
REFRESH_HOURS=float(os.getenv("FBREF_PREVIOUS_REFRESH_HOURS",str(24*30)))
TIMEOUT=float(os.getenv("FBREF_TIMEOUT_SECONDS","30"))
DELAY=float(os.getenv("FBREF_REQUEST_DELAY_SECONDS","0.7"))
USE_JINA=os.getenv("FBREF_JINA_FALLBACK","true").lower() in {"1","true","yes"}
LOG_LEVEL=os.getenv("LOG_LEVEL","INFO").upper()

LEAGUES=[
    (9,"Premier League"),(12,"La Liga"),(11,"Serie A"),(20,"Bundesliga"),(13,"Ligue 1"),
]
logging.basicConfig(level=getattr(logging,LOG_LEVEL,logging.INFO),format="%(asctime)s | %(levelname)s | %(message)s")
log=logging.getLogger("fbref-previous-season")

SCHEMA="""
CREATE TABLE IF NOT EXISTS fbref_player_season_stats(
 season INTEGER NOT NULL, competition TEXT NOT NULL, team_name TEXT NOT NULL,
 player_id TEXT NOT NULL, player_name TEXT NOT NULL, games DOUBLE PRECISION,
 starts DOUBLE PRECISION NOT NULL, minutes DOUBLE PRECISION NOT NULL,
 source_method TEXT NOT NULL, source_url TEXT NOT NULL, raw JSONB NOT NULL,
 fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(season,competition,team_name,player_id));
CREATE INDEX IF NOT EXISTS idx_fbref_player_season_team ON fbref_player_season_stats(season,team_name,minutes DESC);
CREATE TABLE IF NOT EXISTS fbref_previous_player_runs(
 id BIGSERIAL PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ,
 status TEXT NOT NULL, leagues_ok INTEGER NOT NULL DEFAULT 0, teams INTEGER NOT NULL DEFAULT 0,
 players INTEGER NOT NULL DEFAULT 0, direct_ok INTEGER NOT NULL DEFAULT 0, jina_ok INTEGER NOT NULL DEFAULT 0,
 errors JSONB NOT NULL DEFAULT '{}'::jsonb, message TEXT);
"""

def num(v:Any)->Optional[float]:
    s=str(v or "").replace(",","").strip()
    if not s or s in {"-","—"}:return None
    try:return float(s)
    except Exception:return None

def plain(v:Any)->str:
    s=html.unescape(str(v or ""));s=re.sub(r"!\[[^\]]*\]\([^)]*\)","",s);s=re.sub(r"\[([^\]]+)\]\([^)]*\)",r"\1",s)
    s=re.sub(r"<[^>]+>","",s);return re.sub(r"\s+"," ",s).strip()

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.in_row=False;self.in_cell=False;self.cell_stat="";self.cell_id="";self.buf=[];self.row={};self.rows=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=="tr":self.in_row=True;self.row={}
        elif self.in_row and tag in {"th","td"}:
            self.in_cell=True;self.buf=[];self.cell_stat=str(a.get("data-stat") or "");self.cell_id=str(a.get("data-append-csv") or "")
    def handle_data(self,data):
        if self.in_cell:self.buf.append(data)
    def handle_endtag(self,tag):
        if tag in {"th","td"} and self.in_cell:
            val=plain("".join(self.buf));
            if self.cell_stat:
                self.row[self.cell_stat]=val
                if self.cell_stat=="player" and self.cell_id:self.row["_player_id"]=self.cell_id
            self.in_cell=False
        elif tag=="tr" and self.in_row:
            if self.row:self.rows.append(dict(self.row))
            self.in_row=False

def normalize_row(r:Dict[str,Any])->Optional[Dict[str,Any]]:
    player=plain(r.get("player"));team=plain(r.get("team") or r.get("squad"));starts=num(r.get("games_starts") or r.get("starts"));minutes=num(r.get("minutes") or r.get("min"));games=num(r.get("games") or r.get("mp"))
    if not player or not team or starts is None or minutes is None:return None
    if starts<0 or minutes<0:return None
    pid=str(r.get("_player_id") or hashlib.sha1(f"{team}|{player}".encode()).hexdigest()[:16])
    return {"player_id":pid,"player_name":player,"team_name":team,"games":games,"starts":starts,"minutes":minutes,"raw":r}

def parse_html(text:str)->List[Dict[str,Any]]:
    # FBref frequently wraps tables in HTML comments. Exposing the comments to the
    # stdlib parser is safe here because we only consume data-stat cells.
    p=TableParser();p.feed(text.replace("<!--","").replace("-->",""));out=[];seen=set()
    for raw in p.rows:
        r=normalize_row(raw)
        if not r:continue
        key=(r["team_name"],r["player_id"])
        if key in seen:continue
        seen.add(key);out.append(r)
    return out

def parse_markdown(text:str)->List[Dict[str,Any]]:
    lines=[x.strip() for x in text.splitlines() if "|" in x]
    out=[];seen=set();header=None
    for line in lines:
        cells=[plain(x) for x in line.strip().strip("|").split("|")]
        low=[x.lower() for x in cells]
        if "player" in low and ("squad" in low or "team" in low) and "starts" in low and ("min" in low or "minutes" in low):
            header=low;continue
        if not header or set("-: ")>=set("".join(cells)):continue
        if len(cells)!=len(header):continue
        d=dict(zip(header,cells));raw={"player":d.get("player"),"team":d.get("squad") or d.get("team"),"games_starts":d.get("starts"),"minutes":d.get("min") or d.get("minutes"),"games":d.get("mp")}
        r=normalize_row(raw)
        if not r:continue
        key=(r["team_name"],r["player_id"])
        if key not in seen:seen.add(key);out.append(r)
    return out

def quality(rows:List[Dict[str,Any]])->Tuple[bool,Dict[str,Any]]:
    teams=defaultdict(list)
    for r in rows:teams[r["team_name"]].append(r)
    good=0
    for rs in teams.values():
        st=sum(float(x["starts"]) for x in rs);mins=sum(float(x["minutes"]) for x in rs)
        if 250<=st<=500 and mins>=15000:good+=1
    meta={"players":len(rows),"teams":len(teams),"quality_teams":good}
    return len(rows)>=250 and len(teams)>=17 and good>=17,meta

class Importer:
    def __init__(self,db:Optional[str]=None):
        self.db=(db or DATABASE_URL).strip()
        if not self.db:raise RuntimeError("Missing DATABASE_URL")
        self.conn=psycopg.connect(self.db,autocommit=True);self.conn.execute(SCHEMA)
        self.s=requests.Session();self.s.headers.update({"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36","Accept":"text/html,application/xhtml+xml"})
        self.last=0.0
    def close(self):self.conn.close()
    def fresh(self)->bool:
        row=self.conn.execute("SELECT COUNT(DISTINCT team_name),MAX(fetched_at) FROM fbref_player_season_stats WHERE season=%s",(SEASON,)).fetchone()
        if not row or int(row[0] or 0)<90 or not row[1]:return False
        age=(__import__('datetime').datetime.now(__import__('datetime').timezone.utc)-row[1]).total_seconds()/3600
        return age<=REFRESH_HOURS
    def get(self,url:str)->requests.Response:
        wait=DELAY-(time.monotonic()-self.last)
        if wait>0:time.sleep(wait)
        r=self.s.get(url,timeout=(5,TIMEOUT));self.last=time.monotonic();return r
    def league(self,comp:int,name:str)->Tuple[List[Dict[str,Any]],str,str,Dict[str,Any]]:
        url=f"https://fbref.com/en/comps/{comp}/{SEASON_LABEL}/playingtime/{SEASON_LABEL}-{name.replace(' ','-')}-Stats"
        direct_err=""
        try:
            r=self.get(url)
            if r.status_code==200:
                rows=parse_html(r.text);ok,q=quality(rows)
                if ok:return rows,"direct",url,q
                direct_err=f"quality:{q}"
            else:direct_err=f"HTTP {r.status_code}"
        except Exception as exc:direct_err=str(exc)[:180]
        if USE_JINA:
            try:
                jr=self.get("https://r.jina.ai/"+url)
                if jr.status_code==200:
                    rows=parse_markdown(jr.text);ok,q=quality(rows)
                    if ok:return rows,"jina",url,q
                    raise RuntimeError(f"Jina quality gate failed {q}; direct={direct_err}")
                raise RuntimeError(f"Jina HTTP {jr.status_code}; direct={direct_err}")
            except Exception as exc:raise RuntimeError(str(exc))
        raise RuntimeError(direct_err or "FBref unavailable")
    def run(self)->Dict[str,Any]:
        if self.fresh():
            row=self.conn.execute("SELECT COUNT(*),COUNT(DISTINCT team_name) FROM fbref_player_season_stats WHERE season=%s",(SEASON,)).fetchone();res={"status":"fresh_skip","season":SEASON,"players":int(row[0]),"teams":int(row[1])};print("FBREF_PREVIOUS_PLAYERS_RESULT",json.dumps(res,separators=(",",":")),flush=True);return res
        rid=self.conn.execute("INSERT INTO fbref_previous_player_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        errors={};leagues_ok=teams=players=direct_ok=jina_ok=0
        try:
            for comp,name in LEAGUES:
                try:
                    rows,method,url,q=self.league(comp,name)
                    self.conn.execute("DELETE FROM fbref_player_season_stats WHERE season=%s AND competition=%s",(SEASON,name))
                    for r in rows:
                        self.conn.execute("""INSERT INTO fbref_player_season_stats(season,competition,team_name,player_id,player_name,games,starts,minutes,source_method,source_url,raw)
                          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",(SEASON,name,r["team_name"],r["player_id"],r["player_name"],r["games"],r["starts"],r["minutes"],method,url,Jsonb(r["raw"])))
                    leagues_ok+=1;teams+=int(q["teams"]);players+=len(rows);direct_ok+=int(method=="direct");jina_ok+=int(method=="jina")
                except Exception as exc:errors[name]=str(exc)[:400]
            status="success" if leagues_ok==5 and teams>=90 else "partial" if leagues_ok else "failed"
            self.conn.execute("UPDATE fbref_previous_player_runs SET finished_at=NOW(),status=%s,leagues_ok=%s,teams=%s,players=%s,direct_ok=%s,jina_ok=%s,errors=%s,message=%s WHERE id=%s",(status,leagues_ok,teams,players,direct_ok,jina_ok,Jsonb(errors),"strict Starts+Minutes quality gates",rid))
            res={"status":status,"season":SEASON,"leagues_ok":leagues_ok,"teams":teams,"players":players,"direct_ok":direct_ok,"jina_ok":jina_ok,"errors":errors};print("FBREF_PREVIOUS_PLAYERS_RESULT",json.dumps(res,separators=(",",":")),flush=True)
            if not leagues_ok:raise RuntimeError(f"FBref previous-season source unavailable: {errors}")
            return res
        except Exception as exc:
            try:self.conn.execute("UPDATE fbref_previous_player_runs SET finished_at=NOW(),status='failed',errors=%s,message=%s WHERE id=%s",(Jsonb(errors),str(exc)[:700],rid))
            except Exception:pass
            raise

def run_import(database_url:Optional[str]=None)->Dict[str,Any]:
    i=Importer(database_url)
    try:return i.run()
    finally:i.close()

if __name__=="__main__":print(json.dumps(run_import(),ensure_ascii=False,indent=2))
