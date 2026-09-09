#!/usr/bin/env python3
"""Near-kickoff FotMob lineup snapshots for Big Five fixtures.

This source is deliberately fail-soft. FotMob's public web JSON is useful but is
not a contracted API and matchDetails can be blocked in some environments. We
therefore:
- only call it close to kickoff;
- use the match ids already mapped by the working FotMob availability importer;
- preserve the raw payload;
- never infer `confirmed=True` merely because eleven names are visible (they may
  be predicted/last-XI data); confirmation requires an explicit boolean in the
  payload or a match that has already started/finished.
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

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BASE = "https://www.fotmob.com"
WINDOW_BEFORE_MINUTES = int(os.getenv("FOTMOB_LINEUP_BEFORE_MINUTES", "180"))
WINDOW_AFTER_MINUTES = int(os.getenv("FOTMOB_LINEUP_AFTER_MINUTES", "180"))
REQUEST_DELAY = float(os.getenv("FOTMOB_LINEUP_REQUEST_DELAY_SECONDS", "0.35"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("fotmob-lineups")

SCHEMA = """
CREATE TABLE IF NOT EXISTS fotmob_lineup_snapshots(
    espn_event_id TEXT NOT NULL,
    fotmob_match_id TEXT NOT NULL,
    snapshot_hour TIMESTAMPTZ NOT NULL,
    match_date TIMESTAMPTZ NOT NULL,
    league_name TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_starters INTEGER,
    away_starters INTEGER,
    home_bench INTEGER,
    away_bench INTEGER,
    explicit_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    route_used TEXT,
    raw JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(espn_event_id,snapshot_hour)
);
CREATE INDEX IF NOT EXISTS idx_fotmob_lineup_match
    ON fotmob_lineup_snapshots(match_date,snapshot_hour DESC);
CREATE TABLE IF NOT EXISTS fotmob_lineup_runs(
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    candidates INTEGER NOT NULL DEFAULT 0,
    api_calls INTEGER NOT NULL DEFAULT 0,
    responses INTEGER NOT NULL DEFAULT 0,
    lineups_present INTEGER NOT NULL DEFAULT 0,
    confirmed INTEGER NOT NULL DEFAULT 0,
    errors JSONB NOT NULL DEFAULT '{}'::jsonb,
    message TEXT
);
"""


def count_players(v: Any) -> int:
    """Count player-like dicts while avoiding repeated nested stat objects."""
    seen = set()
    total = 0

    def walk(x: Any) -> None:
        nonlocal total
        if isinstance(x, dict):
            # FotMob player records normally have an id + name/name object.
            pid = x.get("id") or x.get("playerId")
            name = x.get("name")
            if isinstance(name, dict):
                name = name.get("fullName") or name.get("name") or name.get("displayName")
            if pid is not None and name:
                key = str(pid)
                if key not in seen:
                    seen.add(key)
                    total += 1
                return
            for child in x.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(x, list):
            for child in x:
                walk(child)

    walk(v)
    return total


def find_bool(obj: Any, keys: Iterable[str]) -> Optional[bool]:
    wanted = {str(k).lower() for k in keys}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in wanted and isinstance(v, bool):
                return v
            if isinstance(v, (dict, list)):
                found = find_bool(v, wanted)
                if found is not None:
                    return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_bool(v, wanted)
            if found is not None:
                return found
    return None


def status_started(payload: Dict[str, Any]) -> bool:
    status = (payload.get("header") or {}).get("status") if isinstance(payload.get("header"), dict) else None
    if not isinstance(status, dict):
        return False
    return bool(status.get("started") or status.get("finished") or status.get("ongoing"))


def lineup_sides(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    lineup = content.get("lineup") if isinstance(content.get("lineup"), dict) else {}
    for key in ("lineup", "lineups"):
        val = lineup.get(key)
        if isinstance(val, list) and val:
            return [x for x in val if isinstance(x, dict)]
    # Some wrappers/new payloads expose explicit home/away blocks.
    out = []
    for key in ("homeTeam", "awayTeam", "home", "away"):
        val = lineup.get(key)
        if isinstance(val, dict):
            out.append(val)
    return out


def side_counts(side: Dict[str, Any]) -> Tuple[int, int]:
    starters_obj = side.get("players") or side.get("starters") or side.get("startingXI") or side.get("starting_xi")
    bench_obj = side.get("bench") or side.get("substitutes") or side.get("subs")
    return count_players(starters_obj), count_players(bench_obj)


class Importer:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db:
            raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True)
        self.conn.execute(SCHEMA)
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36",
        })
        self.last_call = 0.0
        self.calls = 0

    def close(self) -> None:
        self.conn.close()

    def candidates(self) -> List[Tuple[Any, ...]]:
        return list(self.conn.execute(
            """
            SELECT DISTINCT ON(f.espn_event_id)
                   f.espn_event_id,f.fotmob_match_id,f.match_date,f.league_name,
                   f.home_team,f.away_team,f.home_fotmob_team_id,f.away_fotmob_team_id
            FROM fotmob_fixture_availability_snapshots f
            WHERE f.match_date >= NOW()-(%s||' minutes')::interval
              AND f.match_date <= NOW()+(%s||' minutes')::interval
              AND COALESCE(f.fotmob_match_id,'') <> ''
            ORDER BY f.espn_event_id,f.snapshot_hour DESC
            """,
            (WINDOW_AFTER_MINUTES, WINDOW_BEFORE_MINUTES),
        ).fetchall())

    def get_json(self, match_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        errors = []
        for route in ("/api/data/matchDetails", "/api/matchDetails"):
            elapsed = time.monotonic() - self.last_call
            if elapsed < REQUEST_DELAY:
                time.sleep(REQUEST_DELAY - elapsed)
            try:
                r = self.session.get(BASE + route, params={"matchId": match_id}, timeout=20)
                self.last_call = time.monotonic()
                self.calls += 1
                if r.status_code in (403, 429):
                    errors.append(f"{route}:{r.status_code}")
                    continue
                if r.status_code == 404:
                    errors.append(f"{route}:404")
                    continue
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict):
                    return data, route, None
                errors.append(f"{route}:non-dict")
            except Exception as exc:
                errors.append(f"{route}:{type(exc).__name__}:{str(exc)[:120]}")
        return None, None, ";".join(errors)[:500]

    def run(self) -> Dict[str, Any]:
        rid = self.conn.execute("INSERT INTO fotmob_lineup_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        rows = self.candidates()
        responses = present = confirmed_n = 0
        errors: Dict[str, str] = {}
        hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        try:
            for eid, mid, dt, league, home, away, hid, aid in rows:
                payload, route, error = self.get_json(str(mid))
                if not payload:
                    if error:
                        errors[str(eid)] = error
                    continue
                responses += 1
                sides = lineup_sides(payload)
                home_side = away_side = None
                for side in sides:
                    sid = str(side.get("teamId") or side.get("team_id") or "")
                    name = str(side.get("teamName") or side.get("name") or "").lower()
                    if hid and sid == str(hid):
                        home_side = side
                    elif aid and sid == str(aid):
                        away_side = side
                    elif not home_side and str(home).lower() in name:
                        home_side = side
                    elif not away_side and str(away).lower() in name:
                        away_side = side
                if len(sides) >= 2:
                    home_side = home_side or sides[0]
                    away_side = away_side or sides[1]
                hs, hb = side_counts(home_side or {})
                ass, ab = side_counts(away_side or {})
                has_lineup = hs >= 7 and ass >= 7
                present += int(has_lineup)
                explicit = find_bool(payload, {"confirmed", "isconfirmed", "is_confirmed", "lineupconfirmed", "lineup_confirmed"})
                confirmed = bool(explicit is True or status_started(payload)) and has_lineup
                confirmed_n += int(confirmed)
                self.conn.execute(
                    """
                    INSERT INTO fotmob_lineup_snapshots(
                        espn_event_id,fotmob_match_id,snapshot_hour,match_date,league_name,home_team,away_team,
                        home_starters,away_starters,home_bench,away_bench,explicit_confirmed,route_used,raw)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(espn_event_id,snapshot_hour) DO UPDATE SET
                        fotmob_match_id=EXCLUDED.fotmob_match_id,home_starters=EXCLUDED.home_starters,
                        away_starters=EXCLUDED.away_starters,home_bench=EXCLUDED.home_bench,
                        away_bench=EXCLUDED.away_bench,explicit_confirmed=EXCLUDED.explicit_confirmed,
                        route_used=EXCLUDED.route_used,raw=EXCLUDED.raw,fetched_at=NOW()
                    """,
                    (eid, str(mid), hour, dt, league, home, away, hs, ass, hb, ab, confirmed, route, Jsonb(payload)),
                )
            status = "success"
            self.conn.execute(
                """UPDATE fotmob_lineup_runs SET finished_at=NOW(),status=%s,candidates=%s,api_calls=%s,
                   responses=%s,lineups_present=%s,confirmed=%s,errors=%s,message='ok' WHERE id=%s""",
                (status, len(rows), self.calls, responses, present, confirmed_n, Jsonb(errors), rid),
            )
            result = {"status":status,"candidates":len(rows),"api_calls":self.calls,"responses":responses,
                      "lineups_present":present,"confirmed":confirmed_n,"errors":errors}
            log.info("FOTMOB_LINEUPS_RESULT %s", json.dumps(result, separators=(",", ":")))
            return result
        except Exception as exc:
            self.conn.execute(
                "UPDATE fotmob_lineup_runs SET finished_at=NOW(),status='failed',candidates=%s,api_calls=%s,responses=%s,lineups_present=%s,confirmed=%s,errors=%s,message=%s WHERE id=%s",
                (len(rows), self.calls, responses, present, confirmed_n, Jsonb(errors), str(exc)[:1000], rid),
            )
            raise


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    imp = Importer(database_url)
    try:
        return imp.run()
    finally:
        imp.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
