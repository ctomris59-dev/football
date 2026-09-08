#!/usr/bin/env python3
"""Optional Big Balls Sports Data importer for Big Five absences.

The endpoint is used as an additional injury/suspension source because ESPN's
team injury route currently returns empty football reports. We keep the raw
league payload and normalize whatever player/team/reason/status/date fields are
present. The source's own freshness metadata is persisted and must be checked by
callers before using the data in a pre-match model.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import psycopg
from psycopg.types.json import Jsonb
import requests

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
API_KEY = os.getenv("BBS_API_KEY", "").strip()
BASE_URL = os.getenv("BBS_BASE_URL", "https://api.bigballsdata.com").rstrip("/")
REQUEST_DELAY = float(os.getenv("BBS_REQUEST_DELAY_SECONDS", "0.35"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

LEAGUES = [
    ("epl", "Premier League"),
    ("la-liga", "La Liga"),
    ("serie-a", "Serie A"),
    ("bundesliga", "Bundesliga"),
    ("ligue-1", "Ligue 1"),
]

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bigballs-absence-importer")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bbs_absence_snapshots (
    league_slug TEXT NOT NULL,
    league_name TEXT NOT NULL,
    player_id TEXT,
    player_name TEXT,
    team_id TEXT,
    team_name TEXT,
    fixture_id TEXT,
    fixture_date DATE,
    status TEXT,
    reason TEXT,
    injury_type TEXT,
    return_date DATE,
    comment TEXT,
    source_as_of TIMESTAMPTZ,
    source_stale BOOLEAN,
    snapshot_hour TIMESTAMPTZ NOT NULL,
    raw JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (league_slug, snapshot_hour, player_id, fixture_id, player_name)
);
CREATE INDEX IF NOT EXISTS idx_bbs_absence_team ON bbs_absence_snapshots(league_slug, team_name, snapshot_hour DESC);
CREATE INDEX IF NOT EXISTS idx_bbs_absence_date ON bbs_absence_snapshots(fixture_date, snapshot_hour DESC);

CREATE TABLE IF NOT EXISTS bbs_absence_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    api_calls INTEGER NOT NULL DEFAULT 0,
    league_count INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL DEFAULT 0,
    stale_leagues INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def parse_date(value: Any):
    dt = parse_dt(value)
    return dt.date() if dt else None


def first(obj: Dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = obj.get(name)
        if value not in (None, ""):
            return value
    return None


def as_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("items", "injuries", "absences", "rows", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
        for key in ("items", "injuries", "absences", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


class BBSAbsenceImporter:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db:
            raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True)
        self.conn.execute(SCHEMA_SQL)
        self.session = requests.Session()
        self.api_calls = 0

    def close(self) -> None:
        self.conn.close()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not API_KEY:
            raise RuntimeError("BBS_API_KEY is not configured")
        time.sleep(REQUEST_DELAY)
        last: Optional[Exception] = None
        for attempt in range(4):
            try:
                r = self.session.get(
                    f"{BASE_URL}{path}",
                    params=params or {},
                    headers={"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"},
                    timeout=40,
                )
                self.api_calls += 1
                if r.status_code == 429:
                    time.sleep(3 * (attempt + 1)); continue
                if r.status_code >= 500:
                    time.sleep(min(12, 2 ** attempt)); continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:
                last = exc
                time.sleep(min(8, 2 ** attempt))
        raise RuntimeError(f"BBS request failed {path}: {last}")

    def store_row(self, league_slug: str, league_name: str, row: Dict[str, Any], snapshot_hour: datetime, meta: Dict[str, Any]) -> None:
        player = row.get("player") if isinstance(row.get("player"), dict) else {}
        team = row.get("team") if isinstance(row.get("team"), dict) else {}
        fixture = row.get("fixture") if isinstance(row.get("fixture"), dict) else {}
        player_id = str(first(player, ("id", "player_id")) or first(row, ("player_id", "playerId")) or "") or None
        player_name = str(first(player, ("name", "display_name")) or first(row, ("player_name", "playerName", "name")) or "") or None
        team_id = str(first(team, ("id", "team_id")) or first(row, ("team_id", "teamId")) or "") or None
        team_name = str(first(team, ("name", "display_name")) or first(row, ("team_name", "teamName")) or "") or None
        fixture_id = str(first(fixture, ("id", "fixture_id", "match_id")) or first(row, ("fixture_id", "fixtureId", "match_id", "matchId")) or "") or None
        fixture_date = parse_date(first(fixture, ("date", "fixture_date", "kickoff_utc")) or first(row, ("fixture_date", "match_date", "date", "kickoff_utc")))
        status = first(row, ("status", "availability_status", "designation"))
        reason = first(row, ("reason", "absence_reason", "description"))
        injury_type = first(row, ("injury_type", "injuryType", "type"))
        return_date = parse_date(first(row, ("return_date", "expected_return", "expectedReturn")))
        comment = first(row, ("comment", "note", "details"))
        as_of = parse_dt(meta.get("as_of") or meta.get("data_as_of") or meta.get("updated_at"))
        stale = bool(meta.get("stale")) if meta.get("stale") is not None else None
        self.conn.execute(
            """
            INSERT INTO bbs_absence_snapshots(
                league_slug,league_name,player_id,player_name,team_id,team_name,fixture_id,fixture_date,
                status,reason,injury_type,return_date,comment,source_as_of,source_stale,snapshot_hour,raw,fetched_at
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (league_slug,snapshot_hour,player_id,fixture_id,player_name) DO UPDATE SET
                team_id=EXCLUDED.team_id,team_name=EXCLUDED.team_name,fixture_date=EXCLUDED.fixture_date,
                status=EXCLUDED.status,reason=EXCLUDED.reason,injury_type=EXCLUDED.injury_type,
                return_date=EXCLUDED.return_date,comment=EXCLUDED.comment,source_as_of=EXCLUDED.source_as_of,
                source_stale=EXCLUDED.source_stale,raw=EXCLUDED.raw,fetched_at=NOW()
            """,
            (league_slug,league_name,player_id,player_name,team_id,team_name,fixture_id,fixture_date,status,reason,injury_type,return_date,comment,as_of,stale,snapshot_hour,Jsonb(row)),
        )

    def run(self) -> Dict[str, Any]:
        run_id = self.conn.execute("INSERT INTO bbs_absence_runs(status,message) VALUES('running','started') RETURNING id").fetchone()[0]
        if not API_KEY:
            self.conn.execute("UPDATE bbs_absence_runs SET finished_at=NOW(),status='not_configured',message='BBS_API_KEY is not configured' WHERE id=%s", (run_id,))
            result = {"status": "not_configured", "message": "Add BBS_API_KEY to enable Big Five injury/suspension snapshots."}
            log.info("BBS_ABSENCE_RESULT %s", json.dumps(result, separators=(",", ":")))
            return result
        snapshot_hour = utcnow().replace(minute=0, second=0, microsecond=0)
        total = 0; stale_leagues = 0; completed = 0
        try:
            for slug, name in LEAGUES:
                payload = self.get("/v1/injuries", {"league": slug})
                meta = payload.get("meta") if isinstance(payload, dict) and isinstance(payload.get("meta"), dict) else {}
                if meta.get("stale") is True:
                    stale_leagues += 1
                rows = as_rows(payload)
                for row in rows:
                    self.store_row(slug, name, row, snapshot_hour, meta)
                total += len(rows); completed += 1
            self.conn.execute(
                "UPDATE bbs_absence_runs SET finished_at=NOW(),status='success',api_calls=%s,league_count=%s,row_count=%s,stale_leagues=%s,message='ok' WHERE id=%s",
                (self.api_calls,completed,total,stale_leagues,run_id),
            )
            result = {"status":"success","api_calls":self.api_calls,"leagues":completed,"rows":total,"stale_leagues":stale_leagues}
            log.info("BBS_ABSENCE_RESULT %s", json.dumps(result,separators=(",",":")))
            return result
        except Exception as exc:
            self.conn.execute("UPDATE bbs_absence_runs SET finished_at=NOW(),status='failed',api_calls=%s,league_count=%s,row_count=%s,stale_leagues=%s,message=%s WHERE id=%s", (self.api_calls,completed,total,stale_leagues,str(exc)[:1000],run_id))
            raise


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    imp = BBSAbsenceImporter(database_url)
    try:
        return imp.run()
    finally:
        imp.close()

if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
