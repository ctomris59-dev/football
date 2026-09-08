#!/usr/bin/env python3
"""Collect current pre-match context for Big Five fixtures from ESPN public endpoints.

Collects team injury snapshots, event odds snapshots, and xG fields when ESPN's
already-stored match summaries expose them. Raw JSON is preserved for audit and
future parsers because schemas vary by league/provider.
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

from espn_current_importer import stats_map, find_stat

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
REQUEST_DELAY = float(os.getenv("ESPN_CONTEXT_REQUEST_DELAY_SECONDS", "0.10"))
LOOKAHEAD_DAYS = int(os.getenv("ESPN_CONTEXT_LOOKAHEAD_DAYS", "14"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
CORE_BASE = "https://sports.core.api.espn.com/v2/sports/soccer/leagues"
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("espn-context")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS espn_injury_snapshots (
 id BIGSERIAL PRIMARY KEY, league_slug TEXT NOT NULL, team_id TEXT NOT NULL, team_name TEXT,
 snapshot_hour TIMESTAMPTZ NOT NULL, injury_count INTEGER, raw JSONB NOT NULL,
 fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE (league_slug, team_id, snapshot_hour));
CREATE INDEX IF NOT EXISTS idx_espn_injury_latest ON espn_injury_snapshots(league_slug, team_id, snapshot_hour DESC);
CREATE TABLE IF NOT EXISTS espn_odds_snapshots (
 id BIGSERIAL PRIMARY KEY, event_id TEXT NOT NULL, league_slug TEXT NOT NULL, match_date TIMESTAMPTZ,
 snapshot_hour TIMESTAMPTZ NOT NULL, provider TEXT, over_under_line DOUBLE PRECISION,
 home_moneyline DOUBLE PRECISION, draw_moneyline DOUBLE PRECISION, away_moneyline DOUBLE PRECISION,
 normalized JSONB, raw JSONB NOT NULL, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE (event_id, snapshot_hour));
CREATE INDEX IF NOT EXISTS idx_espn_odds_event ON espn_odds_snapshots(event_id, snapshot_hour DESC);
CREATE TABLE IF NOT EXISTS espn_advanced_match_stats (
 event_id TEXT PRIMARY KEY, home_xg DOUBLE PRECISION, away_xg DOUBLE PRECISION,
 home_xg_source_key TEXT, away_xg_source_key TEXT, discovered_stats JSONB,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS espn_context_runs (
 id BIGSERIAL PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ,
 injury_teams INTEGER NOT NULL DEFAULT 0, odds_events INTEGER NOT NULL DEFAULT 0,
 xg_matches INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, message TEXT);
"""

def utcnow() -> datetime: return datetime.now(timezone.utc)

def flatten(obj: Any, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.update(flatten(v, p)) if isinstance(v, (dict, list)) else out.__setitem__(p, v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{prefix}[{i}]"
            out.update(flatten(v, p)) if isinstance(v, (dict, list)) else out.__setitem__(p, v)
    return out

def to_float(v: Any) -> Optional[float]:
    if v is None or v == "": return None
    try: return float(str(v).replace(",", "").replace("+", "").strip())
    except (TypeError, ValueError): return None

def first_key(flat: Dict[str, Any], needles: Iterable[str]) -> Tuple[Optional[str], Optional[Any]]:
    ns = [n.lower() for n in needles]
    for k, v in flat.items():
        if any(n in k.lower() for n in ns): return k, v
    return None, None

def count_injuries(payload: Dict[str, Any]) -> Optional[int]:
    for key in ("injuries", "items", "entries", "athletes"):
        value = payload.get(key)
        if isinstance(value, list): return len(value)
    return None

class ContextImporter:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db: raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True); self.conn.execute(SCHEMA_SQL)
        self.session = requests.Session()
    def close(self) -> None: self.conn.close()
    def get_json(self, url: str, retries: int = 3) -> Dict[str, Any]:
        last: Optional[Exception] = None
        for attempt in range(retries):
            try:
                if REQUEST_DELAY: time.sleep(REQUEST_DELAY)
                r = self.session.get(url, timeout=25)
                if r.status_code == 404: return {"_http_status": 404}
                if r.status_code >= 500: time.sleep(2 ** attempt); continue
                r.raise_for_status(); data = r.json(); return data if isinstance(data, dict) else {"data": data}
            except Exception as exc: last = exc; time.sleep(2 ** attempt)
        return {"_error": str(last)}
    def upcoming_rows(self) -> List[Tuple[str, str, datetime, str, str, str, str]]:
        return list(self.conn.execute("""
          SELECT event_id,league_slug,match_date,COALESCE(home_team_id,''),home_team,COALESCE(away_team_id,''),away_team
          FROM espn_upcoming WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '6 hours'
          AND match_date<=NOW()+(%s||' days')::interval ORDER BY match_date""", (LOOKAHEAD_DAYS,)).fetchall())
    def collect_injuries(self, rows) -> int:
        teams: Dict[Tuple[str,str],str] = {}
        for _e,l,_d,hid,hn,aid,an in rows:
            if hid: teams[(l,hid)] = hn
            if aid: teams[(l,aid)] = an
        hour = utcnow().replace(minute=0,second=0,microsecond=0); done=0
        for (league,team_id),team_name in sorted(teams.items()):
            payload=self.get_json(f"{SITE_BASE}/{league}/teams/{team_id}/injuries")
            self.conn.execute("""INSERT INTO espn_injury_snapshots(league_slug,team_id,team_name,snapshot_hour,injury_count,raw)
              VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(league_slug,team_id,snapshot_hour) DO UPDATE SET
              team_name=EXCLUDED.team_name,injury_count=EXCLUDED.injury_count,raw=EXCLUDED.raw,fetched_at=NOW()""",
              (league,team_id,team_name,hour,count_injuries(payload),Jsonb(payload))); done+=1
        return done
    def normalize_odds(self,payload:Dict[str,Any])->Dict[str,Any]:
        flat=flatten(payload); out:Dict[str,Any]={}
        for target,needles in {
          "over_under_line":("overunder","over_under","total.line","total.points"),
          "home_moneyline":("hometeamodds.moneyline","home.moneyline","homeodds"),
          "draw_moneyline":("drawodds.moneyline","draw.moneyline","drawodds"),
          "away_moneyline":("awayteamodds.moneyline","away.moneyline","awayodds"),
          "provider":("provider.name",)}.items():
            k,v=first_key(flat,needles)
            if k is not None: out[target]=v; out[target+"_key"]=k
        return out
    def collect_odds(self,rows)->int:
        hour=utcnow().replace(minute=0,second=0,microsecond=0); done=0
        for event_id,league,match_dt,_hid,_hn,_aid,_an in rows:
            payload=self.get_json(f"{CORE_BASE}/{league}/events/{event_id}/competitions/{event_id}/odds"); norm=self.normalize_odds(payload)
            self.conn.execute("""INSERT INTO espn_odds_snapshots(event_id,league_slug,match_date,snapshot_hour,provider,over_under_line,home_moneyline,draw_moneyline,away_moneyline,normalized,raw)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id,snapshot_hour) DO UPDATE SET
              provider=EXCLUDED.provider,over_under_line=EXCLUDED.over_under_line,home_moneyline=EXCLUDED.home_moneyline,
              draw_moneyline=EXCLUDED.draw_moneyline,away_moneyline=EXCLUDED.away_moneyline,normalized=EXCLUDED.normalized,raw=EXCLUDED.raw,fetched_at=NOW()""",
              (event_id,league,match_dt,hour,str(norm.get("provider")) if norm.get("provider") is not None else None,
               to_float(norm.get("over_under_line")),to_float(norm.get("home_moneyline")),to_float(norm.get("draw_moneyline")),
               to_float(norm.get("away_moneyline")),Jsonb(norm),Jsonb(payload))); done+=1
        return done
    def discover_xg(self)->int:
        done=0
        for event_id,hid,aid,summary in self.conn.execute("SELECT event_id,home_team_id,away_team_id,summary_raw FROM espn_current_matches WHERE summary_raw IS NOT NULL").fetchall():
            if not isinstance(summary,dict): continue
            mapped=stats_map(summary); hs=mapped.get(str(hid or ""),{}); aws=mapped.get(str(aid or ""),{})
            hxg=find_stat(hs,("expected goals","expectedgoals"," xg","xg "),float_value=True); axg=find_stat(aws,("expected goals","expectedgoals"," xg","xg "),float_value=True)
            hk,_=first_key(hs,("expected goals","expectedgoals"," xg","xg ")); ak,_=first_key(aws,("expected goals","expectedgoals"," xg","xg "))
            if hxg is None and axg is None: continue
            self.conn.execute("""INSERT INTO espn_advanced_match_stats(event_id,home_xg,away_xg,home_xg_source_key,away_xg_source_key,discovered_stats)
              VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id) DO UPDATE SET home_xg=EXCLUDED.home_xg,away_xg=EXCLUDED.away_xg,
              home_xg_source_key=EXCLUDED.home_xg_source_key,away_xg_source_key=EXCLUDED.away_xg_source_key,discovered_stats=EXCLUDED.discovered_stats,updated_at=NOW()""",
              (event_id,hxg,axg,hk,ak,Jsonb({"home":hs,"away":aws}))); done+=1
        return done
    def run(self)->Dict[str,int]:
        rid=self.conn.execute("INSERT INTO espn_context_runs(status) VALUES('running') RETURNING id").fetchone()[0]; injuries=odds=xg=0
        try:
            rows=self.upcoming_rows(); injuries=self.collect_injuries(rows); odds=self.collect_odds(rows); xg=self.discover_xg()
            self.conn.execute("UPDATE espn_context_runs SET finished_at=NOW(),injury_teams=%s,odds_events=%s,xg_matches=%s,status='success',message='ok' WHERE id=%s",(injuries,odds,xg,rid))
            result={"injury_teams":injuries,"odds_events":odds,"xg_matches":xg}; log.info("ESPN_CONTEXT_RESULT %s",json.dumps(result,separators=(",",":"))); return result
        except Exception as exc:
            self.conn.execute("UPDATE espn_context_runs SET finished_at=NOW(),injury_teams=%s,odds_events=%s,xg_matches=%s,status='failed',message=%s WHERE id=%s",(injuries,odds,xg,str(exc)[:1000],rid)); raise

def run_import(database_url:Optional[str]=None)->Dict[str,int]:
    imp=ContextImporter(database_url)
    try: return imp.run()
    finally: imp.close()

if __name__=="__main__": print(run_import())
