#!/usr/bin/env python3
"""Optional pre-match availability confirmation from Sofascore public football endpoints.

Maps our ESPN upcoming fixtures to Sofascore daily scheduled events by kickoff/team names,
then stores lineups.missingPlayers and the confirmed flag. This is an optional redundancy
layer: any upstream/WAF failure is reported but must never break the main pipeline.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb
import requests

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LOOKAHEAD_DAYS = int(os.getenv("SOFASCORE_LOOKAHEAD_DAYS", "6"))
REQUEST_DELAY = float(os.getenv("SOFASCORE_REQUEST_DELAY_SECONDS", "0.18"))
BASE = "https://api.sofascore.com/api/v1"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("sofascore-availability")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sofascore_availability_snapshots (
    espn_event_id TEXT NOT NULL,
    sofa_event_id TEXT NOT NULL,
    snapshot_hour TIMESTAMPTZ NOT NULL,
    match_date TIMESTAMPTZ NOT NULL,
    league_name TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    confirmed BOOLEAN,
    home_missing_count INTEGER NOT NULL DEFAULT 0,
    away_missing_count INTEGER NOT NULL DEFAULT 0,
    home_missing JSONB NOT NULL,
    away_missing JSONB NOT NULL,
    raw JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (espn_event_id, snapshot_hour)
);
CREATE INDEX IF NOT EXISTS idx_sofascore_availability_match
    ON sofascore_availability_snapshots(match_date, snapshot_hour DESC);

CREATE TABLE IF NOT EXISTS sofascore_availability_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    dates_queried INTEGER NOT NULL DEFAULT 0,
    candidate_events INTEGER NOT NULL DEFAULT 0,
    mapped_events INTEGER NOT NULL DEFAULT 0,
    lineup_calls INTEGER NOT NULL DEFAULT 0,
    confirmed_lineups INTEGER NOT NULL DEFAULT 0,
    missing_players INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""

ALIASES = {
    "man utd": "manchester united", "man united": "manchester united", "man city": "manchester city",
    "nottm forest": "nottingham forest", "wolves": "wolverhampton wanderers", "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur", "milan": "ac milan", "inter": "inter milan",
    "psg": "paris saint germain", "paris sg": "paris saint germain",
    "ath bilbao": "athletic club", "athletic bilbao": "athletic club",
    "mgladbach": "borussia monchengladbach", "borussia m gladbach": "borussia monchengladbach",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canon(v: Any) -> str:
    s = unicodedata.normalize("NFKD", str(v or "")).encode("ascii", "ignore").decode().lower().replace("'", "")
    s = re.sub(r"\b(fc|cf|ssc|ac|club|football club)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return ALIASES.get(s, s)


def sim(a: Any, b: Any) -> float:
    a, b = canon(a), canon(b)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()


def team_name(event: Dict[str, Any], side: str) -> str:
    t = event.get(f"{side}Team") or {}
    return str(t.get("name") or t.get("shortName") or "")


def kickoff(event: Dict[str, Any]) -> Optional[datetime]:
    ts = event.get("startTimestamp")
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def missing_list(side: Any) -> List[Dict[str, Any]]:
    if not isinstance(side, dict):
        return []
    value = side.get("missingPlayers")
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    return []


class SofaAvailabilityImporter:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db:
            raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True)
        self.conn.execute(SCHEMA_SQL)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.sofascore.com/",
        })
        self.last_call = 0.0

    def close(self) -> None:
        self.conn.close()

    def get_json(self, url: str) -> Dict[str, Any]:
        elapsed = time.monotonic() - self.last_call
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        r = self.session.get(url, timeout=25)
        self.last_call = time.monotonic()
        if r.status_code in (403, 429):
            raise RuntimeError(f"Sofascore blocked/rate-limited HTTP {r.status_code}")
        if r.status_code == 404:
            return {"_http_status": 404}
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {"data": data}

    def upcoming(self) -> List[Tuple[str, datetime, str, str, str]]:
        return list(self.conn.execute(
            """
            SELECT event_id,match_date,league_name,home_team,away_team
            FROM espn_upcoming
            WHERE is_current=TRUE
              AND match_date >= NOW()-INTERVAL '2 hours'
              AND match_date <= NOW()+(%s||' days')::interval
            ORDER BY match_date
            """,
            (LOOKAHEAD_DAYS,),
        ).fetchall())

    def daily_events(self, day: str) -> List[Dict[str, Any]]:
        payload = self.get_json(f"{BASE}/sport/football/scheduled-events/{day}")
        events = payload.get("events")
        return [x for x in events if isinstance(x, dict)] if isinstance(events, list) else []

    def match_event(self, dt: datetime, home: str, away: str, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        best: Optional[Dict[str, Any]] = None
        best_score = -1.0
        for e in candidates:
            k = kickoff(e)
            if not k:
                continue
            hours = abs((k - dt).total_seconds()) / 3600.0
            if hours > 6:
                continue
            sh, sa = sim(home, team_name(e, "home")), sim(away, team_name(e, "away"))
            if min(sh, sa) < 0.62:
                continue
            score = sh + sa - min(0.25, hours / 24.0)
            if score > best_score:
                best, best_score = e, score
        return best

    def run(self) -> Dict[str, Any]:
        rid = self.conn.execute("INSERT INTO sofascore_availability_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        dates_queried = candidate_events = mapped = calls = confirmed_n = missing_n = 0
        try:
            rows = self.upcoming()
            by_day: Dict[str, List[Dict[str, Any]]] = {}
            for _eid, dt, _league, _home, _away in rows:
                key = dt.astimezone(timezone.utc).date().isoformat()
                if key not in by_day:
                    evs = self.daily_events(key)
                    by_day[key] = evs
                    dates_queried += 1
                    candidate_events += len(evs)

            hour = utcnow().replace(minute=0, second=0, microsecond=0)
            for espn_id, dt, league, home, away in rows:
                key = dt.astimezone(timezone.utc).date().isoformat()
                event = self.match_event(dt, home, away, by_day.get(key, []))
                if not event:
                    continue
                sofa_id = str(event.get("id") or "")
                if not sofa_id:
                    continue
                mapped += 1
                lineup = self.get_json(f"{BASE}/event/{sofa_id}/lineups")
                calls += 1
                if lineup.get("_http_status") == 404:
                    continue
                home_missing = missing_list(lineup.get("home"))
                away_missing = missing_list(lineup.get("away"))
                is_confirmed = bool(lineup.get("confirmed"))
                confirmed_n += int(is_confirmed)
                missing_n += len(home_missing) + len(away_missing)
                self.conn.execute(
                    """
                    INSERT INTO sofascore_availability_snapshots(
                        espn_event_id,sofa_event_id,snapshot_hour,match_date,league_name,home_team,away_team,
                        confirmed,home_missing_count,away_missing_count,home_missing,away_missing,raw
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(espn_event_id,snapshot_hour) DO UPDATE SET
                        sofa_event_id=EXCLUDED.sofa_event_id,confirmed=EXCLUDED.confirmed,
                        home_missing_count=EXCLUDED.home_missing_count,away_missing_count=EXCLUDED.away_missing_count,
                        home_missing=EXCLUDED.home_missing,away_missing=EXCLUDED.away_missing,raw=EXCLUDED.raw,fetched_at=NOW()
                    """,
                    (espn_id, sofa_id, hour, dt, league, home, away, is_confirmed,
                     len(home_missing), len(away_missing), Jsonb(home_missing), Jsonb(away_missing), Jsonb(lineup)),
                )

            self.conn.execute(
                """UPDATE sofascore_availability_runs SET finished_at=NOW(),status='success',dates_queried=%s,candidate_events=%s,mapped_events=%s,lineup_calls=%s,confirmed_lineups=%s,missing_players=%s,message='ok' WHERE id=%s""",
                (dates_queried, candidate_events, mapped, calls, confirmed_n, missing_n, rid),
            )
            result = {"status":"success","dates":dates_queried,"candidates":candidate_events,"mapped":mapped,"lineup_calls":calls,"confirmed":confirmed_n,"missing_players":missing_n}
            log.info("SOFASCORE_AVAILABILITY_RESULT %s", json.dumps(result, separators=(",", ":")))
            return result
        except Exception as exc:
            self.conn.execute("UPDATE sofascore_availability_runs SET finished_at=NOW(),status='failed',dates_queried=%s,candidate_events=%s,mapped_events=%s,lineup_calls=%s,confirmed_lineups=%s,missing_players=%s,message=%s WHERE id=%s",
                              (dates_queried,candidate_events,mapped,calls,confirmed_n,missing_n,str(exc)[:1000],rid))
            raise


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    imp = SofaAvailabilityImporter(database_url)
    try:
        return imp.run()
    finally:
        imp.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2, default=str))
