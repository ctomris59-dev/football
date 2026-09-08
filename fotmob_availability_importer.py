#!/usr/bin/env python3
"""Current Big Five injury/availability snapshots from FotMob's public web JSON.

This is an optional/fail-soft source. FotMob does not publish these web routes as a
contracted developer API, so raw injury objects and freshness timestamps are kept
and upstream failures never break the main refresh pipeline.

Observed current routes:
- /api/data/matches?date=YYYYMMDD
- /api/data/teams?id=<teamId>
Team squad members can expose `injured` / `injury` and injury.expectedReturn.
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
BASE = "https://www.fotmob.com/api/data"
LOOKAHEAD_DAYS = int(os.getenv("FOTMOB_AVAILABILITY_LOOKAHEAD_DAYS", "6"))
REQUEST_DELAY = float(os.getenv("FOTMOB_REQUEST_DELAY_SECONDS", "0.22"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("fotmob-availability")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fotmob_team_availability_snapshots (
    fotmob_team_id TEXT NOT NULL,
    snapshot_hour TIMESTAMPTZ NOT NULL,
    team_name TEXT,
    injury_count INTEGER NOT NULL DEFAULT 0,
    injured_players JSONB NOT NULL,
    raw_squad JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (fotmob_team_id, snapshot_hour)
);
CREATE INDEX IF NOT EXISTS idx_fotmob_team_availability_latest
    ON fotmob_team_availability_snapshots(fotmob_team_id, snapshot_hour DESC);

CREATE TABLE IF NOT EXISTS fotmob_fixture_availability_snapshots (
    espn_event_id TEXT NOT NULL,
    snapshot_hour TIMESTAMPTZ NOT NULL,
    match_date TIMESTAMPTZ NOT NULL,
    league_name TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    fotmob_match_id TEXT,
    home_fotmob_team_id TEXT,
    away_fotmob_team_id TEXT,
    home_injury_count INTEGER NOT NULL DEFAULT 0,
    away_injury_count INTEGER NOT NULL DEFAULT 0,
    home_injured_players JSONB NOT NULL,
    away_injured_players JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (espn_event_id, snapshot_hour)
);
CREATE INDEX IF NOT EXISTS idx_fotmob_fixture_availability_match
    ON fotmob_fixture_availability_snapshots(match_date, snapshot_hour DESC);

CREATE TABLE IF NOT EXISTS fotmob_availability_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    date_calls INTEGER NOT NULL DEFAULT 0,
    team_calls INTEGER NOT NULL DEFAULT 0,
    candidate_matches INTEGER NOT NULL DEFAULT 0,
    mapped_matches INTEGER NOT NULL DEFAULT 0,
    teams_mapped INTEGER NOT NULL DEFAULT 0,
    injured_players INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""

ALIASES = {
    "man utd": "manchester united", "man united": "manchester united", "man city": "manchester city",
    "nottm forest": "nottingham forest", "wolves": "wolverhampton wanderers",
    "spurs": "tottenham hotspur", "tottenham": "tottenham hotspur",
    "milan": "ac milan", "inter": "inter milan", "psg": "paris saint germain", "paris sg": "paris saint germain",
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


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fotmob_match_time(match: Dict[str, Any]) -> Optional[datetime]:
    return parse_dt((match.get("status") or {}).get("utcTime"))


def injury_members(team_payload: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], Any]:
    squad = team_payload.get("squad")
    groups = squad if isinstance(squad, list) else ((squad or {}).get("squad") if isinstance(squad, dict) else [])
    groups = groups if isinstance(groups, list) else []
    members: List[Dict[str, Any]] = []
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("members"), list):
            members.extend(x for x in group["members"] if isinstance(x, dict))
    injured: List[Dict[str, Any]] = []
    for member in members:
        injury = member.get("injury") if isinstance(member.get("injury"), dict) else None
        if not member.get("injured") and not injury:
            continue
        injured.append({
            "id": member.get("id"),
            "name": member.get("name"),
            "position": (member.get("role") or {}).get("fallback") if isinstance(member.get("role"), dict) else None,
            "expected_return": injury.get("expectedReturn") if injury else None,
            "injury": injury,
            "injured_flag": bool(member.get("injured")),
        })
    details = team_payload.get("details") if isinstance(team_payload.get("details"), dict) else {}
    return str(details.get("name") or ""), injured, groups


class FotMobAvailabilityImporter:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db:
            raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True)
        self.conn.execute(SCHEMA_SQL)
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "Mozilla/5.0 Chrome/152 Safari/537.36"})
        self.last_call = 0.0
        self.date_calls = 0
        self.team_calls = 0

    def close(self) -> None:
        self.conn.close()

    def get_json(self, path: str, *, team_call: bool = False) -> Dict[str, Any]:
        elapsed = time.monotonic() - self.last_call
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        r = self.session.get(BASE + path, timeout=30)
        self.last_call = time.monotonic()
        if team_call:
            self.team_calls += 1
        else:
            self.date_calls += 1
        if r.status_code in (403, 429):
            raise RuntimeError(f"FotMob blocked/rate-limited HTTP {r.status_code}")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {"data": data}

    def upcoming(self) -> List[Tuple[str, datetime, str, str, str]]:
        return list(self.conn.execute(
            """
            SELECT event_id,match_date,league_name,home_team,away_team
            FROM espn_upcoming
            WHERE is_current=TRUE
              AND match_date>=NOW()-INTERVAL '2 hours'
              AND match_date<=NOW()+(%s||' days')::interval
            ORDER BY match_date
            """,
            (LOOKAHEAD_DAYS,),
        ).fetchall())

    def daily_candidates(self, day: str) -> List[Dict[str, Any]]:
        payload = self.get_json(f"/matches?date={day.replace('-', '')}")
        out: List[Dict[str, Any]] = []
        for league in payload.get("leagues") or []:
            if not isinstance(league, dict):
                continue
            lname = str(league.get("name") or "")
            for match in league.get("matches") or []:
                if isinstance(match, dict):
                    out.append({**match, "_league_name": lname})
        return out

    def match_candidate(self, dt: datetime, home: str, away: str, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        best = None
        best_score = -1.0
        for row in rows:
            rd = fotmob_match_time(row)
            if not rd:
                continue
            hours = abs((rd - dt).total_seconds()) / 3600.0
            if hours > 6:
                continue
            h = (row.get("home") or {}).get("name") if isinstance(row.get("home"), dict) else None
            a = (row.get("away") or {}).get("name") if isinstance(row.get("away"), dict) else None
            sh, sa = sim(home, h), sim(away, a)
            if min(sh, sa) < 0.62:
                continue
            score = sh + sa - min(0.25, hours / 24.0)
            if score > best_score:
                best, best_score = row, score
        return best

    def run(self) -> Dict[str, Any]:
        rid = self.conn.execute("INSERT INTO fotmob_availability_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        candidates = mapped = teams_mapped = injured_total = 0
        try:
            upcoming = self.upcoming()
            by_day: Dict[str, List[Dict[str, Any]]] = {}
            for _eid, dt, _league, _home, _away in upcoming:
                day = dt.astimezone(timezone.utc).date().isoformat()
                if day not in by_day:
                    rows = self.daily_candidates(day)
                    by_day[day] = rows
                    candidates += len(rows)

            mapped_rows: List[Tuple[str, datetime, str, str, str, Dict[str, Any]]] = []
            team_ids: Dict[str, str] = {}
            for eid, dt, league, home, away in upcoming:
                day = dt.astimezone(timezone.utc).date().isoformat()
                match = self.match_candidate(dt, home, away, by_day.get(day, []))
                if not match:
                    continue
                mapped += 1
                mapped_rows.append((eid, dt, league, home, away, match))
                hobj = match.get("home") if isinstance(match.get("home"), dict) else {}
                aobj = match.get("away") if isinstance(match.get("away"), dict) else {}
                if hobj.get("id") is not None:
                    team_ids[str(hobj["id"])] = str(hobj.get("name") or home)
                if aobj.get("id") is not None:
                    team_ids[str(aobj["id"])] = str(aobj.get("name") or away)

            hour = utcnow().replace(minute=0, second=0, microsecond=0)
            team_avail: Dict[str, List[Dict[str, Any]]] = {}
            for team_id, fallback_name in team_ids.items():
                payload = self.get_json(f"/teams?id={team_id}", team_call=True)
                name, injured, raw_squad = injury_members(payload)
                team_avail[team_id] = injured
                injured_total += len(injured)
                teams_mapped += 1
                self.conn.execute(
                    """
                    INSERT INTO fotmob_team_availability_snapshots(fotmob_team_id,snapshot_hour,team_name,injury_count,injured_players,raw_squad)
                    VALUES(%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(fotmob_team_id,snapshot_hour) DO UPDATE SET
                      team_name=EXCLUDED.team_name,injury_count=EXCLUDED.injury_count,
                      injured_players=EXCLUDED.injured_players,raw_squad=EXCLUDED.raw_squad,fetched_at=NOW()
                    """,
                    (team_id, hour, name or fallback_name, len(injured), Jsonb(injured), Jsonb(raw_squad)),
                )

            for eid, dt, league, home, away, match in mapped_rows:
                hobj = match.get("home") if isinstance(match.get("home"), dict) else {}
                aobj = match.get("away") if isinstance(match.get("away"), dict) else {}
                hid = str(hobj.get("id")) if hobj.get("id") is not None else None
                aid = str(aobj.get("id")) if aobj.get("id") is not None else None
                hi = team_avail.get(hid or "", [])
                ai = team_avail.get(aid or "", [])
                self.conn.execute(
                    """
                    INSERT INTO fotmob_fixture_availability_snapshots(
                      espn_event_id,snapshot_hour,match_date,league_name,home_team,away_team,fotmob_match_id,
                      home_fotmob_team_id,away_fotmob_team_id,home_injury_count,away_injury_count,home_injured_players,away_injured_players)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(espn_event_id,snapshot_hour) DO UPDATE SET
                      fotmob_match_id=EXCLUDED.fotmob_match_id,home_fotmob_team_id=EXCLUDED.home_fotmob_team_id,
                      away_fotmob_team_id=EXCLUDED.away_fotmob_team_id,home_injury_count=EXCLUDED.home_injury_count,
                      away_injury_count=EXCLUDED.away_injury_count,home_injured_players=EXCLUDED.home_injured_players,
                      away_injured_players=EXCLUDED.away_injured_players,fetched_at=NOW()
                    """,
                    (eid, hour, dt, league, home, away, str(match.get("id") or ""), hid, aid, len(hi), len(ai), Jsonb(hi), Jsonb(ai)),
                )

            self.conn.execute(
                """UPDATE fotmob_availability_runs SET finished_at=NOW(),status='success',date_calls=%s,team_calls=%s,candidate_matches=%s,mapped_matches=%s,teams_mapped=%s,injured_players=%s,message='ok' WHERE id=%s""",
                (self.date_calls, self.team_calls, candidates, mapped, teams_mapped, injured_total, rid),
            )
            result = {"status":"success","date_calls":self.date_calls,"team_calls":self.team_calls,"candidates":candidates,"mapped":mapped,"teams":teams_mapped,"injured_players":injured_total}
            log.info("FOTMOB_AVAILABILITY_RESULT %s", json.dumps(result, separators=(",", ":")))
            return result
        except Exception as exc:
            self.conn.execute(
                "UPDATE fotmob_availability_runs SET finished_at=NOW(),status='failed',date_calls=%s,team_calls=%s,candidate_matches=%s,mapped_matches=%s,teams_mapped=%s,injured_players=%s,message=%s WHERE id=%s",
                (self.date_calls, self.team_calls, candidates, mapped, teams_mapped, injured_total, str(exc)[:1000], rid),
            )
            raise


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    imp = FotMobAvailabilityImporter(database_url)
    try:
        return imp.run()
    finally:
        imp.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2, default=str))
