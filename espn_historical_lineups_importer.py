#!/usr/bin/env python3
"""Resume-safe free ESPN historical starting-XI backfill for the Big Five.

Uses ESPN's public scoreboards + match summary endpoints to archive explicit starter
flags from 2025/26. It never infers a starter: a player is stored as a starter only
when ESPN marks the entry with an explicit starter/isStarter boolean or equivalent
starter role. Aggregated real starts are written into the shared player-season cache
with a source-specific player id so existing FotMob minutes are not overwritten.

The importer is bounded per run and resume-safe. Subsequent runs continue only events
whose summary/lineup has not yet been successfully archived.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg
import requests
from psycopg.types.json import Jsonb

import understat_player_continuity_v2 as v2

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SEASON = int(os.getenv("ESPN_HISTORICAL_LINEUP_SEASON", "2025"))
START_DATE = os.getenv("ESPN_HISTORICAL_LINEUP_START", "2025-08-01")
END_DATE = os.getenv("ESPN_HISTORICAL_LINEUP_END", "2026-06-15")
MAX_SUMMARIES = int(os.getenv("ESPN_HISTORICAL_MAX_SUMMARIES_PER_RUN", "350"))
REQUEST_DELAY = float(os.getenv("ESPN_HISTORICAL_REQUEST_DELAY_SECONDS", "0.08"))
TIMEOUT = float(os.getenv("ESPN_HISTORICAL_TIMEOUT_SECONDS", "25"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

LEAGUES: List[Tuple[str, str]] = [
    ("eng.1", "Premier League"), ("esp.1", "La Liga"), ("ita.1", "Serie A"),
    ("ger.1", "Bundesliga"), ("fra.1", "Ligue 1"),
]
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("espn-historical-lineups")

SCHEMA = """
CREATE TABLE IF NOT EXISTS espn_historical_events(
    event_id TEXT PRIMARY KEY, season INTEGER NOT NULL, league_slug TEXT NOT NULL, league_name TEXT NOT NULL,
    match_date TIMESTAMPTZ, home_team_id TEXT, home_team TEXT, away_team_id TEXT, away_team TEXT,
    completed BOOLEAN, summary_status TEXT NOT NULL DEFAULT 'pending', home_starters INTEGER NOT NULL DEFAULT 0,
    away_starters INTEGER NOT NULL DEFAULT 0, scoreboard_raw JSONB NOT NULL, summary_raw JSONB,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS idx_espn_hist_event_status ON espn_historical_events(season,summary_status,match_date);
CREATE TABLE IF NOT EXISTS espn_historical_lineup_players(
    event_id TEXT NOT NULL REFERENCES espn_historical_events(event_id) ON DELETE CASCADE, team_id TEXT NOT NULL,
    team_name TEXT NOT NULL, player_id TEXT NOT NULL, player_name TEXT NOT NULL, starter BOOLEAN NOT NULL,
    source_path TEXT, raw JSONB NOT NULL, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(event_id,team_id,player_id));
CREATE INDEX IF NOT EXISTS idx_espn_hist_lineup_team ON espn_historical_lineup_players(team_name,starter,event_id);
CREATE TABLE IF NOT EXISTS espn_historical_lineup_runs(
    id BIGSERIAL PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ,
    status TEXT NOT NULL, scoreboard_calls INTEGER NOT NULL DEFAULT 0, summary_calls INTEGER NOT NULL DEFAULT 0,
    events_seen INTEGER NOT NULL DEFAULT 0, events_pending INTEGER NOT NULL DEFAULT 0,
    events_with_both_lineups INTEGER NOT NULL DEFAULT 0, starter_rows INTEGER NOT NULL DEFAULT 0,
    teams_with_starts INTEGER NOT NULL DEFAULT 0, errors JSONB NOT NULL DEFAULT '{}'::jsonb, message TEXT);
"""


def dt(v: Any) -> Optional[datetime]:
    if not v: return None
    try:
        x = datetime.fromisoformat(str(v).replace("Z", "+00:00")); return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    except Exception: return None


def team_name(comp: Dict[str, Any]) -> str:
    t = comp.get("team") if isinstance(comp.get("team"), dict) else {}
    return str(t.get("displayName") or t.get("shortDisplayName") or t.get("name") or "").strip()


def event_pair(event: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    comps = event.get("competitions") if isinstance(event.get("competitions"), list) else []
    comp = comps[0] if comps and isinstance(comps[0], dict) else {}
    cs = comp.get("competitors") if isinstance(comp.get("competitors"), list) else []
    home = next((x for x in cs if isinstance(x, dict) and x.get("homeAway") == "home"), None)
    away = next((x for x in cs if isinstance(x, dict) and x.get("homeAway") == "away"), None)
    return (home, away) if home and away else None


def explicit_starter(entry: Dict[str, Any]) -> Optional[bool]:
    for k in ("starter", "isStarter", "starting", "isStarting"):
        if k in entry:
            v = entry.get(k)
            if isinstance(v, bool): return v
            if isinstance(v, (int, float)): return bool(v)
            if isinstance(v, str) and v.strip().lower() in {"true","false","yes","no","1","0"}:
                return v.strip().lower() in {"true","yes","1"}
    for k in ("role", "type", "status"):
        v = entry.get(k)
        if isinstance(v, str):
            z = v.lower().strip()
            if z in {"starter","starting xi","starting","start"}: return True
            if z in {"substitute","bench","replacement","sub"}: return False
    pos = entry.get("position")
    if isinstance(pos, dict):
        z = str(pos.get("name") or pos.get("displayName") or pos.get("abbreviation") or "").lower()
        if z in {"starter","starting xi","starting"}: return True
        if z in {"replacement","substitute","bench"}: return False
    return None


def athlete_obj(entry: Dict[str, Any]) -> Dict[str, Any]:
    for k in ("athlete", "player"):
        if isinstance(entry.get(k), dict): return entry[k]
    return entry


def player_identity(entry: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    a = athlete_obj(entry); pid = a.get("id") or a.get("uid") or a.get("guid")
    name = a.get("displayName") or a.get("fullName") or a.get("shortName") or a.get("name")
    if name and explicit_starter(entry) is not None: return str(pid or v2.canon(name)), str(name).strip()
    return None, None


def iter_team_containers(summary: Dict[str, Any]) -> Iterable[Tuple[str, str, List[Any], str]]:
    rosters = summary.get("rosters")
    if isinstance(rosters, list):
        for i, group in enumerate(rosters):
            if not isinstance(group, dict): continue
            t = group.get("team") if isinstance(group.get("team"), dict) else {}
            tid = str(t.get("id") or group.get("teamId") or "")
            tname = str(t.get("displayName") or t.get("shortDisplayName") or t.get("name") or group.get("teamName") or "")
            entries = group.get("roster") or group.get("athletes") or group.get("players") or []
            if tid and tname and isinstance(entries, list): yield tid, tname, entries, f"rosters[{i}]"
    box = summary.get("boxscore") if isinstance(summary.get("boxscore"), dict) else {}
    players = box.get("players")
    if isinstance(players, list):
        for i, group in enumerate(players):
            if not isinstance(group, dict): continue
            t = group.get("team") if isinstance(group.get("team"), dict) else {}
            tid = str(t.get("id") or group.get("teamId") or "")
            tname = str(t.get("displayName") or t.get("shortDisplayName") or t.get("name") or group.get("teamName") or "")
            entries: List[Any] = []
            for key in ("athletes", "players", "roster"):
                if isinstance(group.get(key), list): entries.extend(group[key])
            stats = group.get("statistics")
            if isinstance(stats, list):
                for s in stats:
                    if isinstance(s, dict) and isinstance(s.get("athletes"), list): entries.extend(s["athletes"])
            if tid and tname and entries: yield tid, tname, entries, f"boxscore.players[{i}]"


def extract_starters(summary: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for tid, tname, entries, path in iter_team_containers(summary):
        slot = out.setdefault(tid, {"team_id": tid, "team_name": tname, "players": {}, "paths": []}); slot["paths"].append(path)
        for raw in entries:
            if not isinstance(raw, dict) or explicit_starter(raw) is not True: continue
            pid, name = player_identity(raw)
            if pid and name: slot["players"][pid] = {"player_id": pid, "player_name": name, "raw": raw, "source_path": path}
    return out


class Importer:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db: raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True); self.conn.execute(v2.SCHEMA); self.conn.execute(SCHEMA)
        self.s = requests.Session(); self.s.headers.update({"Accept":"application/json","User-Agent":"python-requests/2.x"})
        self.scoreboard_calls = 0; self.summary_calls = 0; self.last_call = 0.0

    def close(self) -> None: self.conn.close()

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None, retries: int = 4) -> Dict[str, Any]:
        last: Optional[Exception] = None
        for attempt in range(retries):
            elapsed = time.monotonic() - self.last_call
            if elapsed < REQUEST_DELAY: time.sleep(REQUEST_DELAY - elapsed)
            try:
                r = self.s.get(url, params=params or {}, timeout=(5.0, TIMEOUT)); self.last_call = time.monotonic()
                if r.status_code == 429: time.sleep(min(20, 2 + attempt * 3)); continue
                if r.status_code >= 500: time.sleep(min(15, 2 ** attempt)); continue
                r.raise_for_status(); x = r.json(); return x if isinstance(x, dict) else {"data": x}
            except Exception as exc:
                last = exc; time.sleep(min(10, 2 ** attempt))
        raise RuntimeError(str(last or "ESPN request failed"))

    def month_windows(self) -> Iterable[Tuple[date, date]]:
        start = date.fromisoformat(START_DATE); end = date.fromisoformat(END_DATE); cur = start
        while cur <= end:
            nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1); yield cur, min(end, nxt - timedelta(days=1)); cur = nxt

    def discover_events(self) -> int:
        seen = 0
        for league, lname in LEAGUES:
            for start, end in self.month_windows():
                payload = self.get_json(f"{SITE_BASE}/{league}/scoreboard", {"dates":f"{start:%Y%m%d}-{end:%Y%m%d}","limit":200}); self.scoreboard_calls += 1
                events = payload.get("events") if isinstance(payload.get("events"), list) else []
                for event in events:
                    if not isinstance(event, dict): continue
                    eid = str(event.get("id") or ""); pair = event_pair(event); match_dt = dt(event.get("date"))
                    status_obj = ((event.get("status") or {}).get("type") or {}) if isinstance(event.get("status"), dict) else {}
                    if not eid or not pair or not match_dt or not bool(status_obj.get("completed")): continue
                    home, away = pair
                    hid = str(home.get("id") or (home.get("team") or {}).get("id") or ""); aid = str(away.get("id") or (away.get("team") or {}).get("id") or "")
                    self.conn.execute("""INSERT INTO espn_historical_events(event_id,season,league_slug,league_name,match_date,home_team_id,home_team,away_team_id,away_team,completed,scoreboard_raw)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s) ON CONFLICT(event_id) DO UPDATE SET league_slug=EXCLUDED.league_slug,
                        league_name=EXCLUDED.league_name,match_date=EXCLUDED.match_date,home_team_id=EXCLUDED.home_team_id,home_team=EXCLUDED.home_team,
                        away_team_id=EXCLUDED.away_team_id,away_team=EXCLUDED.away_team,completed=TRUE,scoreboard_raw=EXCLUDED.scoreboard_raw,updated_at=NOW()""",
                        (eid, SEASON, league, lname, match_dt, hid or None, team_name(home), aid or None, team_name(away), Jsonb(event)))
                    seen += 1
        return seen

    def backfill_summaries(self) -> Tuple[int, int, Dict[str, str]]:
        rows = self.conn.execute("""SELECT event_id,league_slug,home_team_id,away_team_id FROM espn_historical_events
            WHERE season=%s AND completed=TRUE AND summary_status<>'success' ORDER BY match_date LIMIT %s""", (SEASON, MAX_SUMMARIES)).fetchall()
        both = starter_rows = 0; errors: Dict[str, str] = {}
        for eid, league, hid, aid in rows:
            try:
                summary = self.get_json(f"{SITE_BASE}/{league}/summary", {"event":str(eid)}); self.summary_calls += 1
                groups = extract_starters(summary); hc = len((groups.get(str(hid)) or {}).get("players", {})) if hid else 0; ac = len((groups.get(str(aid)) or {}).get("players", {})) if aid else 0
                valid = hc >= 10 and ac >= 10; self.conn.execute("DELETE FROM espn_historical_lineup_players WHERE event_id=%s", (str(eid),))
                for tid, group in groups.items():
                    for p in group["players"].values():
                        self.conn.execute("""INSERT INTO espn_historical_lineup_players(event_id,team_id,team_name,player_id,player_name,starter,source_path,raw)
                            VALUES(%s,%s,%s,%s,%s,TRUE,%s,%s) ON CONFLICT(event_id,team_id,player_id) DO UPDATE SET player_name=EXCLUDED.player_name,
                            starter=TRUE,source_path=EXCLUDED.source_path,raw=EXCLUDED.raw,fetched_at=NOW()""",
                            (str(eid), tid, group["team_name"], p["player_id"], p["player_name"], p["source_path"], Jsonb(p["raw"]))); starter_rows += 1
                self.conn.execute("UPDATE espn_historical_events SET summary_status=%s,home_starters=%s,away_starters=%s,summary_raw=%s,updated_at=NOW() WHERE event_id=%s",
                                  ("success" if valid else "no_complete_lineup", hc, ac, Jsonb(summary), str(eid))); both += int(valid)
            except Exception as exc:
                errors[str(eid)] = str(exc)[:240]; self.conn.execute("UPDATE espn_historical_events SET summary_status='failed',updated_at=NOW() WHERE event_id=%s", (str(eid),))
        return both, starter_rows, errors

    def aggregate_cache(self) -> int:
        rows = self.conn.execute("""SELECT team_name,player_id,player_name,COUNT(*)::float AS starts FROM espn_historical_lineup_players p
            JOIN espn_historical_events e USING(event_id) WHERE e.season=%s AND p.starter=TRUE AND e.summary_status='success'
            GROUP BY team_name,player_id,player_name""", (SEASON,)).fetchall(); teams = set()
        for team, pid, name, starts in rows:
            teams.add(v2.canon(team)); raw = {"source":"espn-historical-lineups","exact_starts_available":True,"season":SEASON,"aggregation":"count-explicit-starter-flags","starts":float(starts or 0)}
            self.conn.execute("""INSERT INTO understat_player_seasons(season,team_name,team_slug,player_id,player_name,games,starts,minutes,goals,xg,assists,xa,xgchain,xgbuildup,raw)
                VALUES(%s,%s,%s,%s,%s,%s,%s,0,0,0,0,0,0,0,%s) ON CONFLICT(season,team_slug,player_id) DO UPDATE SET
                team_name=EXCLUDED.team_name,player_name=EXCLUDED.player_name,games=GREATEST(understat_player_seasons.games,EXCLUDED.games),
                starts=GREATEST(understat_player_seasons.starts,EXCLUDED.starts),raw=COALESCE(understat_player_seasons.raw,'{}'::jsonb)||EXCLUDED.raw,fetched_at=NOW()""",
                (SEASON, str(team), v2.team_slug(str(team)), f"espn:{pid}", str(name), float(starts or 0), float(starts or 0), Jsonb(raw)))
        return len(teams)

    def run(self) -> Dict[str, Any]:
        rid = self.conn.execute("INSERT INTO espn_historical_lineup_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        try:
            events = self.discover_events(); pending_before = self.conn.execute("SELECT COUNT(*) FROM espn_historical_events WHERE season=%s AND completed=TRUE AND summary_status<>'success'", (SEASON,)).fetchone()[0]
            both, starter_rows, errors = self.backfill_summaries(); teams = self.aggregate_cache()
            total_both = self.conn.execute("SELECT COUNT(*) FROM espn_historical_events WHERE season=%s AND summary_status='success'", (SEASON,)).fetchone()[0]
            total_events = self.conn.execute("SELECT COUNT(*) FROM espn_historical_events WHERE season=%s AND completed=TRUE", (SEASON,)).fetchone()[0]
            pending_after = self.conn.execute("SELECT COUNT(*) FROM espn_historical_events WHERE season=%s AND completed=TRUE AND summary_status<>'success'", (SEASON,)).fetchone()[0]
            coverage = round(float(total_both or 0) / float(total_events or 1), 4); status = "success" if total_events else "empty"
            result = {"status":status,"season":SEASON,"events_seen_this_run":events,"events_total":int(total_events),"summary_calls":self.summary_calls,
                      "pending_before":int(pending_before),"pending_after":int(pending_after),"both_lineups_this_run":both,"both_lineups_total":int(total_both),
                      "both_lineup_coverage":coverage,"starter_rows_this_run":starter_rows,"teams_with_starts":teams,"errors":errors}
            self.conn.execute("""UPDATE espn_historical_lineup_runs SET finished_at=NOW(),status=%s,scoreboard_calls=%s,summary_calls=%s,events_seen=%s,
                events_pending=%s,events_with_both_lineups=%s,starter_rows=%s,teams_with_starts=%s,errors=%s,message=%s WHERE id=%s""",
                (status,self.scoreboard_calls,self.summary_calls,int(total_events),int(pending_after),int(total_both),starter_rows,teams,Jsonb(errors),
                 json.dumps({"coverage":coverage,"pending_before":int(pending_before)},separators=(",",":"))[:1000],rid))
            print("ESPN_HISTORICAL_LINEUPS_RESULT", json.dumps(result, separators=(",", ":")), flush=True); return result
        except Exception as exc:
            self.conn.execute("UPDATE espn_historical_lineup_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s", (str(exc)[:1000], rid)); raise


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    i = Importer(database_url)
    try: return i.run()
    finally: i.close()


if __name__ == "__main__": print(json.dumps(run_import(), ensure_ascii=False, indent=2))
