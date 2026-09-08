#!/usr/bin/env python3
"""Optional Big Balls Sports Data availability/absence importer.

The soccer source is an *absence* feed, not a mandated forward-looking injury
report. We therefore preserve source freshness (meta.as_of/meta.stale) and never
silently promote a historical missed-match row into a confirmed current injury.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb
import requests

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
API_KEY = os.getenv("BBS_API_KEY", "").strip()
BASE_URL = os.getenv("BBS_BASE_URL", "https://api.bigballsdata.com").rstrip("/")
REQUEST_DELAY = float(os.getenv("BBS_REQUEST_DELAY_SECONDS", "0.35"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

LEAGUES: List[Tuple[str, str]] = [
    ("epl", "Premier League"),
    ("La Liga", "La Liga"),
    ("Serie A", "Serie A"),
    ("Bundesliga", "Bundesliga"),
    ("Ligue 1", "Ligue 1"),
]

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bbs-availability")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bbs_absence_snapshots (
    id BIGSERIAL PRIMARY KEY,
    league_key TEXT NOT NULL,
    league_name TEXT NOT NULL,
    snapshot_hour TIMESTAMPTZ NOT NULL,
    as_of TIMESTAMPTZ,
    stale BOOLEAN,
    player_id TEXT,
    player_name TEXT,
    team_id TEXT,
    team_name TEXT,
    fixture_id TEXT,
    fixture_date TIMESTAMPTZ,
    status TEXT,
    reason TEXT,
    absence_kind TEXT,
    injury_type TEXT,
    return_date DATE,
    source_updated_at TIMESTAMPTZ,
    raw JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (league_key, snapshot_hour, player_id, fixture_id, reason)
);
CREATE INDEX IF NOT EXISTS idx_bbs_absence_latest
    ON bbs_absence_snapshots(league_key, snapshot_hour DESC);
CREATE INDEX IF NOT EXISTS idx_bbs_absence_team
    ON bbs_absence_snapshots(team_name, fixture_date DESC);

CREATE TABLE IF NOT EXISTS bbs_availability_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    api_calls INTEGER NOT NULL DEFAULT 0,
    leagues_ok INTEGER NOT NULL DEFAULT 0,
    rows_stored INTEGER NOT NULL DEFAULT 0,
    newest_as_of TIMESTAMPTZ,
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
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00+00:00"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_date(value: Any):
    dt = parse_dt(value)
    return dt.date() if dt else None


def first(d: Dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if d.get(name) not in (None, ""):
            return d.get(name)
    return None


def nested_name(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return str(first(value, ("name", "display_name", "full_name", "short_name")) or "") or None
    return str(value) if value not in (None, "") else None


def nested_id(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        v = first(value, ("id", "player_id", "team_id", "uuid"))
        return str(v) if v not in (None, "") else None
    return None


def classify_reason(reason: Optional[str], status: Optional[str]) -> str:
    text = f"{reason or ''} {status or ''}".lower()
    if any(x in text for x in ("suspend", "red card", "yellow card", "cards", "ban")):
        return "suspension"
    if any(x in text for x in ("illness", "virus", "sick", "flu")):
        return "illness"
    if any(x in text for x in ("international duty", "national team")):
        return "international_duty"
    if any(x in text for x in ("loan", "transfer", "not in squad")):
        return "squad_other"
    injury_terms = (
        "injur", "hamstring", "knee", "ankle", "muscle", "groin", "calf", "thigh",
        "foot", "back", "shoulder", "hip", "achilles", "cruciate", "fracture", "knock",
        "concussion", "fitness", "adductor", "tendon",
    )
    if any(x in text for x in injury_terms):
        return "injury"
    return "other"


def rows_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("injuries", "absences", "items", "rows", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    for key in ("injuries", "absences", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


class BBSAvailabilityImporter:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db:
            raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True)
        self.conn.execute(SCHEMA_SQL)
        self.session = requests.Session()
        self.api_calls = 0
        self.last_call = 0.0

    def close(self) -> None:
        self.conn.close()

    def get_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not API_KEY:
            raise RuntimeError("BBS_API_KEY is not configured")
        elapsed = time.monotonic() - self.last_call
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        last: Optional[Exception] = None
        for attempt in range(4):
            try:
                r = self.session.get(
                    f"{BASE_URL}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    timeout=35,
                )
                self.last_call = time.monotonic()
                self.api_calls += 1
                if r.status_code == 429:
                    time.sleep(4 * (attempt + 1))
                    continue
                if r.status_code >= 500:
                    time.sleep(min(15, 2 ** attempt))
                    continue
                r.raise_for_status()
                data = r.json()
                return data if isinstance(data, dict) else {"data": data}
            except Exception as exc:
                last = exc
                time.sleep(min(10, 2 ** attempt))
        raise RuntimeError(f"BBS request failed: {path} {params}: {last}")

    def store_row(self, league_key: str, league_name: str, hour: datetime, as_of: Optional[datetime], stale: Optional[bool], row: Dict[str, Any]) -> None:
        player = row.get("player") if isinstance(row.get("player"), dict) else {}
        team = row.get("team") if isinstance(row.get("team"), dict) else {}
        fixture = row.get("fixture") if isinstance(row.get("fixture"), dict) else {}
        player_id = str(first(row, ("player_id", "playerId")) or nested_id(player) or "") or None
        player_name = str(first(row, ("player_name", "playerName", "name")) or nested_name(player) or "") or None
        team_id = str(first(row, ("team_id", "teamId")) or nested_id(team) or "") or None
        team_name = str(first(row, ("team_name", "teamName")) or nested_name(team) or "") or None
        fixture_id = str(first(row, ("fixture_id", "fixtureId", "match_id", "matchId")) or nested_id(fixture) or "") or None
        fixture_date = parse_dt(first(row, ("fixture_date", "fixtureDate", "match_date", "date")) or first(fixture, ("date", "start_time", "fixture_date")))
        status = first(row, ("status", "designation", "availability"))
        reason = first(row, ("reason", "comment", "description", "absence_reason"))
        injury_type = first(row, ("injury_type", "injuryType", "type"))
        return_date = parse_date(first(row, ("return_date", "returnDate", "expected_return")))
        updated = parse_dt(first(row, ("updated_at", "updatedAt", "source_updated_at")))
        kind = classify_reason(str(reason) if reason is not None else None, str(status) if status is not None else None)
        self.conn.execute(
            """
            INSERT INTO bbs_absence_snapshots(
                league_key,league_name,snapshot_hour,as_of,stale,
                player_id,player_name,team_id,team_name,fixture_id,fixture_date,
                status,reason,absence_kind,injury_type,return_date,source_updated_at,raw
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (league_key,snapshot_hour,player_id,fixture_id,reason) DO UPDATE SET
                as_of=EXCLUDED.as_of, stale=EXCLUDED.stale, player_name=EXCLUDED.player_name,
                team_id=EXCLUDED.team_id, team_name=EXCLUDED.team_name,
                fixture_date=EXCLUDED.fixture_date, status=EXCLUDED.status,
                absence_kind=EXCLUDED.absence_kind, injury_type=EXCLUDED.injury_type,
                return_date=EXCLUDED.return_date, source_updated_at=EXCLUDED.source_updated_at,
                raw=EXCLUDED.raw, fetched_at=NOW()
            """,
            (league_key, league_name, hour, as_of, stale, player_id, player_name, team_id, team_name, fixture_id, fixture_date,
             str(status) if status is not None else None, str(reason) if reason is not None else None, kind,
             str(injury_type) if injury_type is not None else None, return_date, updated, Jsonb(row)),
        )

    def run(self) -> Dict[str, Any]:
        run_id = self.conn.execute("INSERT INTO bbs_availability_runs(status,message) VALUES('running','started') RETURNING id").fetchone()[0]
        if not API_KEY:
            self.conn.execute("UPDATE bbs_availability_runs SET finished_at=NOW(),status='not_configured',message='BBS_API_KEY is not configured' WHERE id=%s", (run_id,))
            result = {"status": "not_configured", "message": "Add BBS_API_KEY to enable Big Five absence snapshots."}
            log.info("BBS_AVAILABILITY_RESULT %s", json.dumps(result, separators=(",", ":")))
            return result

        hour = utcnow().replace(minute=0, second=0, microsecond=0)
        total = ok = stale_count = 0
        newest: Optional[datetime] = None
        try:
            for league_key, league_name in LEAGUES:
                payload = self.get_json("/v1/injuries", {"league": league_key})
                meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                as_of = parse_dt(first(meta, ("as_of", "data_as_of", "updated_at")))
                stale_value = meta.get("stale")
                stale = bool(stale_value) if stale_value is not None else None
                if stale:
                    stale_count += 1
                if as_of and (newest is None or as_of > newest):
                    newest = as_of
                rows = rows_from_payload(payload)
                for row in rows:
                    self.store_row(league_key, league_name, hour, as_of, stale, row)
                total += len(rows)
                ok += 1
                log.info("BBS_AVAILABILITY_LEAGUE league=%s rows=%s as_of=%s stale=%s", league_name, len(rows), as_of, stale)

            self.conn.execute(
                """UPDATE bbs_availability_runs SET finished_at=NOW(),status='success',api_calls=%s,leagues_ok=%s,rows_stored=%s,newest_as_of=%s,stale_leagues=%s,message='ok' WHERE id=%s""",
                (self.api_calls, ok, total, newest, stale_count, run_id),
            )
            result = {"status": "success", "api_calls": self.api_calls, "leagues_ok": ok, "rows": total, "newest_as_of": newest.isoformat() if newest else None, "stale_leagues": stale_count}
            log.info("BBS_AVAILABILITY_RESULT %s", json.dumps(result, separators=(",", ":")))
            return result
        except Exception as exc:
            self.conn.execute("UPDATE bbs_availability_runs SET finished_at=NOW(),status='failed',api_calls=%s,leagues_ok=%s,rows_stored=%s,message=%s WHERE id=%s", (self.api_calls, ok, total, str(exc)[:1000], run_id))
            raise


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    imp = BBSAvailabilityImporter(database_url)
    try:
        return imp.run()
    finally:
        imp.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
