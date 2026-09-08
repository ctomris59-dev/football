#!/usr/bin/env python3
"""Import ClubElo snapshots and histories for teams relevant to the Big Five.

No API key is required. The first run fetches full history only for newly mapped
clubs; subsequent weekly runs refresh the daily snapshot with minimal traffic.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

import psycopg
import requests

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
REQUEST_DELAY=float(os.getenv("CLUBELO_REQUEST_DELAY_SECONDS","0.30"))
HISTORY_REFRESH_DAYS=int(os.getenv("CLUBELO_HISTORY_REFRESH_DAYS","30"))

LEAGUE_COUNTRY={
    "Premier League":"ENG","La Liga":"ESP","Serie A":"ITA","Bundesliga":"GER","Ligue 1":"FRA"
}

SCHEMA_SQL="""
CREATE TABLE IF NOT EXISTS clubelo_daily_snapshots(
  snapshot_date DATE NOT NULL,
  club TEXT NOT NULL,
  country TEXT,
  level INTEGER,
  elo DOUBLE PRECISION,
  rank DOUBLE PRECISION,
  from_date DATE,
  to_date DATE,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(snapshot_date,club)
);
CREATE TABLE IF NOT EXISTS clubelo_team_map(
  system_team TEXT NOT NULL,
  league_name TEXT NOT NULL,
  clubelo_club TEXT NOT NULL,
  country TEXT,
  confidence DOUBLE PRECISION NOT NULL,
  mapped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(system_team,league_name)
);
CREATE TABLE IF NOT EXISTS clubelo_history(
  clubelo_club TEXT NOT NULL,
  from_date DATE NOT NULL,
  to_date DATE,
  elo DOUBLE PRECISION NOT NULL,
  rank DOUBLE PRECISION,
  country TEXT,
  level INTEGER,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(clubelo_club,from_date)
);
CREATE INDEX IF NOT EXISTS idx_clubelo_history_lookup ON clubelo_history(clubelo_club,from_date,to_date);
CREATE TABLE IF NOT EXISTS clubelo_import_runs(
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  snapshot_rows INTEGER NOT NULL DEFAULT 0,
  mapped_teams INTEGER NOT NULL DEFAULT 0,
  history_calls INTEGER NOT NULL DEFAULT 0,
  history_rows INTEGER NOT NULL DEFAULT 0,
  message TEXT
);
"""

ALIASES={
 "man utd":"man united","manchester united":"man united","man city":"man city",
 "nottm forest":"nottingham forest","wolves":"wolverhampton",
 "spurs":"tottenham","tottenham hotspur":"tottenham",
 "paris saint germain":"paris sg","psg":"paris sg",
 "inter milan":"inter","ac milan":"milan",
 "athletic club":"ath bilbao","athletic bilbao":"ath bilbao",
 "borussia monchengladbach":"m gladbach",
}

def canon(v:Any)->str:
    s=unicodedata.normalize("NFKD",str(v or "")).encode("ascii","ignore").decode().lower().replace("'","")
    s=re.sub(r"\b(fc|cf|ssc|ac|calcio|club|football club|afc)\b"," ",s)
    s=re.sub(r"[^a-z0-9]+"," ",s).strip()
    s=re.sub(r"\s+"," ",s)
    return ALIASES.get(s,s)

def parse_date(v:Any)->Optional[date]:
    s=str(v or "").strip()
    if not s or s.lower()=="none": return None
    try: return datetime.strptime(s[:10],"%Y-%m-%d").date()
    except Exception: return None

def to_float(v:Any)->Optional[float]:
    try:
        if v in (None,"","None"): return None
        return float(v)
    except Exception: return None

def to_int(v:Any)->Optional[int]:
    try:
        if v in (None,"","None"): return None
        return int(float(v))
    except Exception: return None

class ClubEloImporter:
    def __init__(self,database_url:Optional[str]=None):
        self.db=(database_url or DATABASE_URL).strip()
        if not self.db: raise RuntimeError("Missing DATABASE_URL")
        self.conn=psycopg.connect(self.db,autocommit=True)
        self.conn.execute(SCHEMA_SQL)
        self.s=requests.Session()
        self.s.headers.update({"User-Agent":"Mozilla/5.0 FootballAnalytics/1.0"})
        self.calls=0
    def close(self): self.conn.close()

    def get_csv(self,path:str)->List[Dict[str,str]]:
        last=None
        for scheme in ("https","http"):
            url=f"{scheme}://api.clubelo.com/{path}"
            try:
                time.sleep(REQUEST_DELAY)
                r=self.s.get(url,timeout=40)
                self.calls+=1
                r.raise_for_status()
                text=r.text
                if not text.lstrip().startswith(("Rank,Club","Club,Country","Rank,")):
                    raise RuntimeError(f"unexpected ClubElo payload: {text[:80]}")
                return list(csv.DictReader(io.StringIO(text)))
            except Exception as exc:
                last=exc
        raise RuntimeError(f"ClubElo request failed {path}: {last}")

    def snapshot(self)->Tuple[date,List[Dict[str,str]]]:
        today=datetime.now(timezone.utc).date()
        last=None
        for back in (0,1,2,3,7,14):
            d=today-timedelta(days=back)
            try:
                rows=self.get_csv(d.isoformat())
                if rows: return d,rows
            except Exception as exc: last=exc
        raise RuntimeError(f"No ClubElo daily snapshot: {last}")

    def target_teams(self)->List[Tuple[str,str,str]]:
        out={}
        for league,country in LEAGUE_COUNTRY.items():
            rows=self.conn.execute(
                """SELECT home_team FROM espn_current_matches WHERE league_name=%s
                   UNION SELECT away_team FROM espn_current_matches WHERE league_name=%s
                   UNION SELECT home_team FROM espn_upcoming WHERE league_name=%s AND is_current=TRUE
                   UNION SELECT away_team FROM espn_upcoming WHERE league_name=%s AND is_current=TRUE
                   UNION SELECT home_team FROM football_data_matches WHERE league_name=%s AND season_code='2526'
                   UNION SELECT away_team FROM football_data_matches WHERE league_name=%s AND season_code='2526'""",
                (league,league,league,league,league,league)
            ).fetchall()
            for r in rows:
                if r and r[0]: out[(str(r[0]),league)]=country
        return [(team,league,country) for (team,league),country in out.items()]

    def map_team(self,team:str,country:str,snapshot_rows:List[Dict[str,str]])->Optional[Tuple[str,float]]:
        ct=canon(team)
        best=None; best_score=0.0
        for row in snapshot_rows:
            rc=str(row.get("Country") or "")
            if rc and rc!=country: continue
            club=str(row.get("Club") or "")
            cc=canon(club)
            if not cc: continue
            score=1.0 if ct==cc else SequenceMatcher(None,ct,cc).ratio()
            if ct in cc or cc in ct: score=max(score,0.86)
            if score>best_score: best,best_score=club,score
        if best and best_score>=0.62: return best,best_score
        return None

    def history_fresh(self,club:str)->bool:
        row=self.conn.execute("SELECT MAX(fetched_at) FROM clubelo_history WHERE clubelo_club=%s",(club,)).fetchone()
        return bool(row and row[0] and row[0]>=datetime.now(timezone.utc)-timedelta(days=HISTORY_REFRESH_DAYS))

    def history_slug(self,club:str)->str:
        s=unicodedata.normalize("NFKD",club).encode("ascii","ignore").decode()
        return re.sub(r"[^A-Za-z0-9-]","",s)

    def run(self)->Dict[str,Any]:
        rid=self.conn.execute("INSERT INTO clubelo_import_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        snap_count=mapped=hist_calls=hist_rows=0
        try:
            snap_date,rows=self.snapshot()
            for row in rows:
                club=str(row.get("Club") or "").strip()
                if not club: continue
                self.conn.execute(
                    """INSERT INTO clubelo_daily_snapshots(snapshot_date,club,country,level,elo,rank,from_date,to_date,fetched_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT(snapshot_date,club) DO UPDATE SET country=EXCLUDED.country,level=EXCLUDED.level,
                         elo=EXCLUDED.elo,rank=EXCLUDED.rank,from_date=EXCLUDED.from_date,to_date=EXCLUDED.to_date,fetched_at=NOW()""",
                    (snap_date,club,row.get("Country"),to_int(row.get("Level")),to_float(row.get("Elo")),
                     to_float(row.get("Rank")),parse_date(row.get("From")),parse_date(row.get("To")))
                )
                snap_count+=1

            for team,league,country in self.target_teams():
                m=self.map_team(team,country,rows)
                if not m: continue
                club,confidence=m
                mapped+=1
                self.conn.execute(
                    """INSERT INTO clubelo_team_map(system_team,league_name,clubelo_club,country,confidence,mapped_at)
                       VALUES(%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT(system_team,league_name) DO UPDATE SET clubelo_club=EXCLUDED.clubelo_club,
                         country=EXCLUDED.country,confidence=EXCLUDED.confidence,mapped_at=NOW()""",
                    (team,league,club,country,confidence)
                )
                if confidence<0.68 or self.history_fresh(club): continue
                try:
                    hrows=self.get_csv(self.history_slug(club)); hist_calls+=1
                    for hr in hrows:
                        fd=parse_date(hr.get("From")); elo=to_float(hr.get("Elo"))
                        if not fd or elo is None: continue
                        self.conn.execute(
                            """INSERT INTO clubelo_history(clubelo_club,from_date,to_date,elo,rank,country,level,fetched_at)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,NOW())
                               ON CONFLICT(clubelo_club,from_date) DO UPDATE SET to_date=EXCLUDED.to_date,
                                 elo=EXCLUDED.elo,rank=EXCLUDED.rank,country=EXCLUDED.country,level=EXCLUDED.level,fetched_at=NOW()""",
                            (club,fd,parse_date(hr.get("To")),elo,to_float(hr.get("Rank")),
                             hr.get("Country") or country,to_int(hr.get("Level")))
                        ); hist_rows+=1
                except Exception:
                    continue

            self.conn.execute(
                """UPDATE clubelo_import_runs SET finished_at=NOW(),status='success',snapshot_rows=%s,mapped_teams=%s,
                   history_calls=%s,history_rows=%s,message=%s WHERE id=%s""",
                (snap_count,mapped,hist_calls,hist_rows,json.dumps({"snapshot_date":snap_date.isoformat(),"http_calls":self.calls}),rid)
            )
            result={"status":"success","snapshot_date":snap_date.isoformat(),"snapshot_rows":snap_count,
                    "mapped_teams":mapped,"history_calls":hist_calls,"history_rows":hist_rows,"http_calls":self.calls}
            print("CLUBELO_RESULT",json.dumps(result,separators=(",",":")))
            return result
        except Exception as exc:
            self.conn.execute("UPDATE clubelo_import_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                              (str(exc)[:1000],rid))
            raise

def run_import(database_url:Optional[str]=None)->Dict[str,Any]:
    imp=ClubEloImporter(database_url)
    try: return imp.run()
    finally: imp.close()

if __name__=="__main__":
    print(json.dumps(run_import(),ensure_ascii=False,indent=2))
