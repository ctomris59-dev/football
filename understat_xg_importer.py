#!/usr/bin/env python3
"""Low-rate Understat xG importer for the Big Five leagues.

Uses Understat's JSON AJAX league endpoint used by open-source Understat clients:
    https://understat.com/getLeagueData/{league}/{season}
with X-Requested-With: XMLHttpRequest.

Only 15 league-season requests by default (5 leagues x 3 seasons). Raw payloads
are preserved. Season 2026 means 2026/27 in Understat's start-year convention.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb
import requests

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
UNDERSTAT_SEASONS = [int(x.strip()) for x in os.getenv("UNDERSTAT_SEASONS", "2024,2025,2026").split(",") if x.strip()]
UNDERSTAT_DELAY = float(os.getenv("UNDERSTAT_REQUEST_DELAY_SECONDS", "1.0"))
UNDERSTAT_REFRESH_HOURS = float(os.getenv("UNDERSTAT_REFRESH_HOURS", "6"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

LEAGUES: List[Tuple[str, str]] = [
    ("EPL", "Premier League"),
    ("La_Liga", "La Liga"),
    ("Serie_A", "Serie A"),
    ("Bundesliga", "Bundesliga"),
    ("Ligue_1", "Ligue 1"),
]
BASE = "https://understat.com"
HEADERS = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("understat-xg")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS understat_matches (
    match_id TEXT PRIMARY KEY,
    league_code TEXT NOT NULL,
    league_name TEXT NOT NULL,
    season INTEGER NOT NULL,
    match_date TIMESTAMPTZ,
    home_team_id TEXT,
    home_team TEXT,
    away_team_id TEXT,
    away_team TEXT,
    home_goals INTEGER,
    away_goals INTEGER,
    home_xg DOUBLE PRECISION,
    away_xg DOUBLE PRECISION,
    forecast_home DOUBLE PRECISION,
    forecast_draw DOUBLE PRECISION,
    forecast_away DOUBLE PRECISION,
    is_result BOOLEAN,
    raw JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_understat_league_season_date ON understat_matches(league_code, season, match_date);
CREATE TABLE IF NOT EXISTS understat_team_seasons (
    league_code TEXT NOT NULL,
    league_name TEXT NOT NULL,
    season INTEGER NOT NULL,
    team_id TEXT NOT NULL,
    team_name TEXT,
    raw JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (league_code, season, team_id)
);
CREATE TABLE IF NOT EXISTS understat_source_state (
    source_key TEXT PRIMARY KEY,
    last_success_at TIMESTAMPTZ,
    match_rows INTEGER NOT NULL DEFAULT 0,
    team_rows INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS understat_import_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    match_rows INTEGER NOT NULL DEFAULT 0,
    xg_rows INTEGER NOT NULL DEFAULT 0,
    team_rows INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""


def to_float(v: Any) -> Optional[float]:
    try: return float(v) if v not in (None, "") else None
    except (TypeError, ValueError): return None

def to_int(v: Any) -> Optional[int]:
    try: return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError): return None

def parse_dt(v: Any) -> Optional[datetime]:
    if not v: return None
    s = str(v).strip()
    for parser in (
        lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")),
        lambda x: datetime.strptime(x, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
    ):
        try: return parser(s)
        except ValueError: pass
    return None

def side(obj: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(obj, dict): return None, None
    return (str(obj.get("id")) if obj.get("id") is not None else None, obj.get("title") or obj.get("name") or obj.get("short_title"))

class UnderstatImporter:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db: raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True); self.conn.execute(SCHEMA_SQL)
        self.session = requests.Session(); self.requests = 0
    def close(self) -> None: self.conn.close()
    def fresh(self, key: str, season: int) -> bool:
        row = self.conn.execute("SELECT last_success_at FROM understat_source_state WHERE source_key=%s", (key,)).fetchone()
        if not row or not row[0]: return False
        hours = UNDERSTAT_REFRESH_HOURS if season == max(UNDERSTAT_SEASONS) else 24 * 365 * 10
        return row[0] >= datetime.now(timezone.utc) - __import__('datetime').timedelta(hours=hours)
    def get_league(self, league: str, season: int) -> Dict[str, Any]:
        if UNDERSTAT_DELAY: time.sleep(UNDERSTAT_DELAY)
        self.requests += 1
        url = f"{BASE}/getLeagueData/{league}/{season}"
        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                r = self.session.get(url, headers=HEADERS, timeout=35)
                if r.status_code == 429: time.sleep(5 * (attempt + 1)); continue
                if r.status_code >= 500: time.sleep(2 ** attempt); continue
                r.raise_for_status(); data = r.json()
                if not isinstance(data, dict): raise RuntimeError("Understat response is not an object")
                return data
            except Exception as exc:
                last = exc; time.sleep(2 ** attempt)
        raise RuntimeError(f"Understat failed {league} {season}: {last}")
    def store_match(self, league: str, league_name: str, season: int, m: Dict[str, Any]) -> bool:
        mid = str(m.get("id") or "");
        if not mid: return False
        hid, hn = side(m.get("h")); aid, an = side(m.get("a"))
        goals = m.get("goals") or {}; xg = m.get("xG") or {}; fc = m.get("forecast") or {}
        hxg, axg = to_float(xg.get("h")), to_float(xg.get("a"))
        self.conn.execute("""
          INSERT INTO understat_matches(match_id,league_code,league_name,season,match_date,home_team_id,home_team,away_team_id,away_team,
          home_goals,away_goals,home_xg,away_xg,forecast_home,forecast_draw,forecast_away,is_result,raw)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT(match_id) DO UPDATE SET league_code=EXCLUDED.league_code,league_name=EXCLUDED.league_name,season=EXCLUDED.season,
          match_date=EXCLUDED.match_date,home_team_id=EXCLUDED.home_team_id,home_team=EXCLUDED.home_team,away_team_id=EXCLUDED.away_team_id,
          away_team=EXCLUDED.away_team,home_goals=EXCLUDED.home_goals,away_goals=EXCLUDED.away_goals,home_xg=EXCLUDED.home_xg,away_xg=EXCLUDED.away_xg,
          forecast_home=EXCLUDED.forecast_home,forecast_draw=EXCLUDED.forecast_draw,forecast_away=EXCLUDED.forecast_away,is_result=EXCLUDED.is_result,
          raw=EXCLUDED.raw,updated_at=NOW()""",
          (mid,league,league_name,season,parse_dt(m.get("datetime")),hid,hn,aid,an,to_int(goals.get("h")),to_int(goals.get("a")),
           hxg,axg,to_float(fc.get("w")),to_float(fc.get("d")),to_float(fc.get("l")),bool(m.get("isResult")),Jsonb(m)))
        return hxg is not None and axg is not None
    def store_teams(self, league: str, league_name: str, season: int, teams: Any) -> int:
        items = teams.items() if isinstance(teams, dict) else enumerate(teams if isinstance(teams, list) else [])
        n=0
        for fallback_id, t in items:
            if not isinstance(t,dict): continue
            tid=str(t.get("id") if t.get("id") is not None else fallback_id); name=t.get("title") or t.get("name")
            self.conn.execute("""INSERT INTO understat_team_seasons(league_code,league_name,season,team_id,team_name,raw)
              VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(league_code,season,team_id) DO UPDATE SET team_name=EXCLUDED.team_name,raw=EXCLUDED.raw,updated_at=NOW()""",
              (league,league_name,season,tid,name,Jsonb(t))); n+=1
        return n
    def run(self) -> Dict[str,int]:
        rid=self.conn.execute("INSERT INTO understat_import_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        matches=xg_rows=teams_total=0
        try:
            for season in UNDERSTAT_SEASONS:
                for league,league_name in LEAGUES:
                    key=f"{league}:{season}"
                    if self.fresh(key,season):
                        row=self.conn.execute("SELECT match_rows,team_rows FROM understat_source_state WHERE source_key=%s",(key,)).fetchone(); matches+=int(row[0] or 0); teams_total+=int(row[1] or 0); continue
                    log.info("Understat xG: %s %s",league_name,season)
                    data=self.get_league(league,season); dates=data.get("dates") or []; teams=data.get("teams") or {}
                    local_matches=local_xg=0
                    for m in dates:
                        if isinstance(m,dict):
                            local_matches+=1
                            if self.store_match(league,league_name,season,m): local_xg+=1
                    local_teams=self.store_teams(league,league_name,season,teams)
                    self.conn.execute("""INSERT INTO understat_source_state(source_key,last_success_at,match_rows,team_rows,message)
                      VALUES(%s,NOW(),%s,%s,'ok') ON CONFLICT(source_key) DO UPDATE SET last_success_at=NOW(),match_rows=EXCLUDED.match_rows,team_rows=EXCLUDED.team_rows,message='ok',updated_at=NOW()""",
                      (key,local_matches,local_teams)); matches+=local_matches; xg_rows+=local_xg; teams_total+=local_teams
                    log.info("Understat stored %s %s: matches=%s xg=%s teams=%s",league_name,season,local_matches,local_xg,local_teams)
            self.conn.execute("UPDATE understat_import_runs SET finished_at=NOW(),status='success',requests=%s,match_rows=%s,xg_rows=%s,team_rows=%s,message='ok' WHERE id=%s",(self.requests,matches,xg_rows,teams_total,rid))
            result={"requests":self.requests,"match_rows":matches,"xg_rows":xg_rows,"team_rows":teams_total}; log.info("UNDERSTAT_RESULT %s",json.dumps(result,separators=(",",":"))); return result
        except Exception as exc:
            self.conn.execute("UPDATE understat_import_runs SET finished_at=NOW(),status='failed',requests=%s,match_rows=%s,xg_rows=%s,team_rows=%s,message=%s WHERE id=%s",(self.requests,matches,xg_rows,teams_total,str(exc)[:1000],rid)); raise

def run_import(database_url:Optional[str]=None)->Dict[str,int]:
    imp=UnderstatImporter(database_url)
    try: return imp.run()
    finally: imp.close()

if __name__=="__main__": print(json.dumps(run_import(),ensure_ascii=False,indent=2))
