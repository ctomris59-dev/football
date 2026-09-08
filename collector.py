#!/usr/bin/env python3
"""
API-Football bulk historical collector for the Big Five leagues.

Design goals:
- No API key in source code.
- Resume-safe/idempotent: rerun without duplicating rows.
- Uses /fixtures?ids=... batching (max 20 fixture IDs) to minimize API calls.
- Stores everything persistently in PostgreSQL because Render Cron/One-Off disks are ephemeral.
- Falls back to endpoint-specific calls only when embedded data is unexpectedly missing.
- Stops cleanly before exhausting the daily quota, preserving progress.

Default seasons:
  2024 = 2024/25
  2025 = 2025/26

Default leagues:
  39  Premier League
  140 La Liga
  135 Serie A
  78  Bundesliga
  61  Ligue 1
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb
import requests

BASE_URL = os.getenv("API_FOOTBALL_BASE_URL", "https://v3.football.api-sports.io").rstrip("/")
API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

SEASONS = [int(x.strip()) for x in os.getenv("SEASONS", "2024,2025").split(",") if x.strip()]
LEAGUES_RAW = os.getenv(
    "LEAGUES",
    "39:Premier League,140:La Liga,135:Serie A,78:Bundesliga,61:Ligue 1",
)
LEAGUES: List[Tuple[int, str]] = []
for item in LEAGUES_RAW.split(","):
    league_id, name = item.split(":", 1)
    LEAGUES.append((int(league_id.strip()), name.strip()))

BATCH_SIZE = max(1, min(20, int(os.getenv("BATCH_SIZE", "20"))))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY_SECONDS", "0.25"))
DAILY_RESERVE = int(os.getenv("DAILY_REQUEST_RESERVE", "20"))
INCLUDE_SEASON_PLAYERS = os.getenv("INCLUDE_SEASON_PLAYERS", "true").lower() in {"1", "true", "yes"}
INCLUDE_INJURIES = os.getenv("INCLUDE_INJURIES", "true").lower() in {"1", "true", "yes"}
REFRESH_ACTIVE_SEASON = os.getenv("REFRESH_ACTIVE_SEASON", "false").lower() in {"1", "true", "yes"}
ACTIVE_SEASON = int(os.getenv("ACTIVE_SEASON", "2026"))
ENABLE_FALLBACK = os.getenv("ENABLE_FALLBACK", "true").lower() in {"1", "true", "yes"}
MAX_FALLBACK_REQUESTS = int(os.getenv("MAX_FALLBACK_REQUESTS", "500"))
ONLY_FINISHED_FOR_DETAILS = os.getenv("ONLY_FINISHED_FOR_DETAILS", "true").lower() in {"1", "true", "yes"}
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

FINISHED_STATUSES = {"FT", "AET", "PEN"}
EXPECTED_EMBEDDED_KEYS = ("events", "lineups", "statistics", "players")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("football-collector")


class QuotaStop(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def chunks(values: List[int], size: int) -> Iterable[List[int]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id UUID PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    seasons JSONB NOT NULL,
    leagues JSONB NOT NULL,
    api_calls INTEGER NOT NULL DEFAULT 0,
    message TEXT
);

CREATE TABLE IF NOT EXISTS collection_state (
    state_key TEXT PRIMARY KEY,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    meta JSONB
);

CREATE TABLE IF NOT EXISTS league_coverage (
    league_id INTEGER NOT NULL,
    league_name TEXT,
    season INTEGER NOT NULL,
    coverage JSONB,
    raw JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (league_id, season)
);

CREATE TABLE IF NOT EXISTS fixtures (
    fixture_id BIGINT PRIMARY KEY,
    league_id INTEGER NOT NULL,
    league_name TEXT,
    season INTEGER NOT NULL,
    fixture_date TIMESTAMPTZ,
    status_short TEXT,
    round TEXT,
    venue_id BIGINT,
    venue_name TEXT,
    home_team_id BIGINT,
    home_team_name TEXT,
    away_team_id BIGINT,
    away_team_name TEXT,
    home_goals INTEGER,
    away_goals INTEGER,
    raw JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_fixtures_league_season ON fixtures(league_id, season);
CREATE INDEX IF NOT EXISTS idx_fixtures_date ON fixtures(fixture_date);

CREATE TABLE IF NOT EXISTS fixture_details (
    fixture_id BIGINT PRIMARY KEY REFERENCES fixtures(fixture_id) ON DELETE CASCADE,
    events JSONB,
    lineups JSONB,
    statistics JSONB,
    players JSONB,
    raw JSONB NOT NULL,
    embedded_complete BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS season_players (
    league_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    player_id BIGINT NOT NULL,
    player_name TEXT,
    team_ids JSONB,
    raw JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (league_id, season, player_id)
);

CREATE TABLE IF NOT EXISTS injuries (
    fixture_id BIGINT NOT NULL,
    league_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    team_id BIGINT,
    team_name TEXT,
    player_id BIGINT,
    player_name TEXT,
    type TEXT,
    reason TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw JSONB NOT NULL,
    PRIMARY KEY (fixture_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_injuries_team ON injuries(team_id);
CREATE INDEX IF NOT EXISTS idx_injuries_league_season ON injuries(league_id, season);

CREATE TABLE IF NOT EXISTS api_call_log (
    id BIGSERIAL PRIMARY KEY,
    called_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID,
    endpoint TEXT NOT NULL,
    params JSONB,
    http_status INTEGER,
    api_results INTEGER,
    daily_remaining INTEGER,
    minute_remaining INTEGER,
    error TEXT
);
"""


class Collector:
    def __init__(self) -> None:
        if not API_KEY:
            raise SystemExit("Missing API_FOOTBALL_KEY environment variable.")
        if not DATABASE_URL:
            raise SystemExit("Missing DATABASE_URL environment variable.")

        self.session = requests.Session()
        self.session.headers.update({"x-apisports-key": API_KEY})
        self.conn = psycopg.connect(DATABASE_URL, autocommit=True)
        self.conn.execute(SCHEMA_SQL)
        self.run_id = uuid.uuid4()
        self.api_calls = 0
        self.fallback_calls = 0

        self.conn.execute(
            """
            INSERT INTO collection_runs(run_id, started_at, status, seasons, leagues)
            VALUES (%s, %s, 'running', %s, %s)
            """,
            (
                self.run_id,
                utcnow(),
                Jsonb(SEASONS),
                Jsonb([{"id": i, "name": n} for i, n in LEAGUES]),
            ),
        )

    def close_run(self, status: str, message: str = "") -> None:
        self.conn.execute(
            """
            UPDATE collection_runs
            SET finished_at=%s, status=%s, api_calls=%s, message=%s
            WHERE run_id=%s
            """,
            (utcnow(), status, self.api_calls, message, self.run_id),
        )

    def state_done(self, key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM collection_state WHERE state_key=%s", (key,)
        ).fetchone()
        return bool(row)

    @staticmethod
    def is_active_season(season: int) -> bool:
        return season == ACTIVE_SEASON

    def state_done_for_season(self, key: str, season: int) -> bool:
        """Historical seasons stay resume-locked; active season can be explicitly refreshed."""
        if REFRESH_ACTIVE_SEASON and self.is_active_season(season):
            return False
        return self.state_done(key)

    def mark_state(self, key: str, meta: Optional[dict] = None) -> None:
        self.conn.execute(
            """
            INSERT INTO collection_state(state_key, completed_at, meta)
            VALUES (%s, NOW(), %s)
            ON CONFLICT (state_key) DO UPDATE
            SET completed_at=EXCLUDED.completed_at, meta=EXCLUDED.meta
            """,
            (key, Jsonb(meta or {})),
        )

    @staticmethod
    def _header_int(headers: requests.structures.CaseInsensitiveDict, key: str) -> Optional[int]:
        value = headers.get(key)
        try:
            return int(value) if value is not None else None
        except (ValueError, TypeError):
            return None

    def api_get(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        fallback: bool = False,
        retries: int = 5,
    ) -> Dict[str, Any]:
        if fallback:
            if self.fallback_calls >= MAX_FALLBACK_REQUESTS:
                raise QuotaStop(
                    f"Fallback request cap ({MAX_FALLBACK_REQUESTS}) reached; progress is saved."
                )
            self.fallback_calls += 1

        url = f"{BASE_URL}/{endpoint.lstrip('/')}"
        last_error = None

        for attempt in range(retries):
            time.sleep(REQUEST_DELAY)
            try:
                resp = self.session.get(url, params=params or {}, timeout=45)
                self.api_calls += 1

                daily_remaining = self._header_int(resp.headers, "x-ratelimit-requests-remaining")
                minute_remaining = self._header_int(resp.headers, "X-RateLimit-Remaining")

                payload: Dict[str, Any]
                try:
                    payload = resp.json()
                except Exception:
                    payload = {}

                api_results = payload.get("results")
                errors = payload.get("errors")
                error_text = None
                if resp.status_code >= 400:
                    error_text = f"HTTP {resp.status_code}: {resp.text[:500]}"
                elif errors and errors != [] and errors != {}:
                    error_text = f"API errors: {errors}"

                self.conn.execute(
                    """
                    INSERT INTO api_call_log(
                        run_id, endpoint, params, http_status, api_results,
                        daily_remaining, minute_remaining, error
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        self.run_id,
                        endpoint,
                        Jsonb(params or {}),
                        resp.status_code,
                        api_results if isinstance(api_results, int) else None,
                        daily_remaining,
                        minute_remaining,
                        error_text,
                    ),
                )

                if daily_remaining is not None and daily_remaining <= DAILY_RESERVE:
                    raise QuotaStop(
                        f"Daily quota reserve reached ({daily_remaining} remaining). "
                        "Progress is saved; rerun after the quota resets."
                    )

                if resp.status_code == 429:
                    wait = 65
                    log.warning("Rate limited. Sleeping %ss before retry.", wait)
                    time.sleep(wait)
                    continue

                if resp.status_code in (499, 500, 502, 503, 504):
                    wait = min(60, 2 ** attempt * 3)
                    log.warning("Transient HTTP %s. Retry in %ss.", resp.status_code, wait)
                    time.sleep(wait)
                    continue

                resp.raise_for_status()

                if errors and errors != [] and errors != {}:
                    raise RuntimeError(f"API returned errors for {endpoint}: {errors}")

                return payload

            except QuotaStop:
                raise
            except Exception as exc:
                last_error = exc
                wait = min(60, 2 ** attempt * 3)
                log.warning("Request failed (%s), attempt %s/%s; retry in %ss",
                            exc, attempt + 1, retries, wait)
                time.sleep(wait)

        raise RuntimeError(f"API request failed after retries: {endpoint} {params}: {last_error}")

    def store_coverage(self, league_id: int, league_name: str, season: int) -> None:
        key = f"coverage:{league_id}:{season}"
        if self.state_done_for_season(key, season):
            return
        payload = self.api_get("leagues", {"id": league_id, "season": season})
        response = payload.get("response", [])
        coverage = None
        if response:
            seasons = response[0].get("seasons", [])
            matched = next((s for s in seasons if s.get("year") == season), None)
            if matched:
                coverage = matched.get("coverage")
        self.conn.execute(
            """
            INSERT INTO league_coverage(league_id, league_name, season, coverage, raw)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (league_id, season) DO UPDATE
            SET league_name=EXCLUDED.league_name,
                coverage=EXCLUDED.coverage,
                raw=EXCLUDED.raw,
                updated_at=NOW()
            """,
            (league_id, league_name, season, Jsonb(coverage), Jsonb(payload)),
        )
        self.mark_state(key, {"results": payload.get("results")})

    def store_fixture(self, item: Dict[str, Any], league_id: int, league_name: str, season: int) -> None:
        fixture = item.get("fixture", {})
        teams = item.get("teams", {})
        goals = item.get("goals", {})
        league = item.get("league", {})
        venue = fixture.get("venue") or {}
        status = fixture.get("status") or {}

        fixture_id = fixture.get("id")
        if fixture_id is None:
            return

        self.conn.execute(
            """
            INSERT INTO fixtures(
                fixture_id, league_id, league_name, season, fixture_date, status_short, round,
                venue_id, venue_name,
                home_team_id, home_team_name, away_team_id, away_team_name,
                home_goals, away_goals, raw
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (fixture_id) DO UPDATE SET
                league_id=EXCLUDED.league_id,
                league_name=EXCLUDED.league_name,
                season=EXCLUDED.season,
                fixture_date=EXCLUDED.fixture_date,
                status_short=EXCLUDED.status_short,
                round=EXCLUDED.round,
                venue_id=EXCLUDED.venue_id,
                venue_name=EXCLUDED.venue_name,
                home_team_id=EXCLUDED.home_team_id,
                home_team_name=EXCLUDED.home_team_name,
                away_team_id=EXCLUDED.away_team_id,
                away_team_name=EXCLUDED.away_team_name,
                home_goals=EXCLUDED.home_goals,
                away_goals=EXCLUDED.away_goals,
                raw=EXCLUDED.raw,
                updated_at=NOW()
            """,
            (
                fixture_id,
                league_id,
                league_name,
                season,
                fixture.get("date"),
                status.get("short"),
                league.get("round"),
                venue.get("id"),
                venue.get("name"),
                (teams.get("home") or {}).get("id"),
                (teams.get("home") or {}).get("name"),
                (teams.get("away") or {}).get("id"),
                (teams.get("away") or {}).get("name"),
                goals.get("home"),
                goals.get("away"),
                Jsonb(item),
            ),
        )

    def collect_fixture_list(self, league_id: int, league_name: str, season: int) -> List[int]:
        key = f"fixtures-list:{league_id}:{season}"
        if not self.state_done_for_season(key, season):
            log.info("Fixtures: %s %s%s", league_name, season, " [refresh]" if REFRESH_ACTIVE_SEASON and self.is_active_season(season) else "")
            payload = self.api_get("fixtures", {"league": league_id, "season": season})
            for item in payload.get("response", []):
                self.store_fixture(item, league_id, league_name, season)
            self.mark_state(key, {"results": payload.get("results")})
        rows = self.conn.execute(
            """
            SELECT fixture_id
            FROM fixtures
            WHERE league_id=%s AND season=%s
              AND (%s = FALSE OR status_short = ANY(%s))
            ORDER BY fixture_date, fixture_id
            """,
            (league_id, season, ONLY_FINISHED_FOR_DETAILS, list(FINISHED_STATUSES)),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def store_fixture_detail(self, item: Dict[str, Any]) -> None:
        fixture_id = (item.get("fixture") or {}).get("id")
        if fixture_id is None:
            return

        embedded = {k: item.get(k) for k in EXPECTED_EMBEDDED_KEYS}
        complete = all(v not in (None, [], {}) for v in embedded.values())

        self.conn.execute(
            """
            INSERT INTO fixture_details(
                fixture_id, events, lineups, statistics, players, raw, embedded_complete
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (fixture_id) DO UPDATE SET
                events=COALESCE(EXCLUDED.events, fixture_details.events),
                lineups=COALESCE(EXCLUDED.lineups, fixture_details.lineups),
                statistics=COALESCE(EXCLUDED.statistics, fixture_details.statistics),
                players=COALESCE(EXCLUDED.players, fixture_details.players),
                raw=EXCLUDED.raw,
                embedded_complete=EXCLUDED.embedded_complete,
                updated_at=NOW()
            """,
            (
                fixture_id,
                Jsonb(embedded["events"]),
                Jsonb(embedded["lineups"]),
                Jsonb(embedded["statistics"]),
                Jsonb(embedded["players"]),
                Jsonb(item),
                complete,
            ),
        )

    def merge_detail_field(self, fixture_id: int, field: str, value: Any) -> None:
        if field not in EXPECTED_EMBEDDED_KEYS:
            raise ValueError(field)
        self.conn.execute(
            f"""
            UPDATE fixture_details
            SET {field}=%s, updated_at=NOW()
            WHERE fixture_id=%s
            """,
            (Jsonb(value), fixture_id),
        )

    def ensure_detail_row(self, fixture_id: int) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM fixture_details WHERE fixture_id=%s", (fixture_id,)
        ).fetchone()
        if row:
            return
        base = self.conn.execute(
            "SELECT raw FROM fixtures WHERE fixture_id=%s", (fixture_id,)
        ).fetchone()
        if not base:
            return
        self.conn.execute(
            """
            INSERT INTO fixture_details(fixture_id, raw, embedded_complete)
            VALUES (%s,%s,FALSE)
            ON CONFLICT DO NOTHING
            """,
            (fixture_id, Jsonb(base[0])),
        )

    def fallback_missing(self, fixture_id: int) -> None:
        if not ENABLE_FALLBACK:
            return
        self.ensure_detail_row(fixture_id)
        row = self.conn.execute(
            "SELECT events, lineups, statistics, players FROM fixture_details WHERE fixture_id=%s",
            (fixture_id,),
        ).fetchone()
        if not row:
            return

        mapping = [
            ("events", "fixtures/events"),
            ("lineups", "fixtures/lineups"),
            ("statistics", "fixtures/statistics"),
            ("players", "fixtures/players"),
        ]
        current = {"events": row[0], "lineups": row[1], "statistics": row[2], "players": row[3]}

        for field, endpoint in mapping:
            if current[field] not in (None, [], {}):
                continue
            key = f"fallback:{field}:{fixture_id}"
            if self.state_done(key):
                continue
            payload = self.api_get(endpoint, {"fixture": fixture_id}, fallback=True)
            value = payload.get("response", [])
            self.merge_detail_field(fixture_id, field, value)
            self.mark_state(key, {"results": payload.get("results")})

    def collect_details(self, fixture_ids: List[int], league_id: int, season: int) -> None:
        missing = []
        for fid in fixture_ids:
            row = self.conn.execute(
                "SELECT 1 FROM fixture_details WHERE fixture_id=%s", (fid,)
            ).fetchone()
            if not row:
                missing.append(fid)

        log.info("Details %s/%s: %s missing fixtures", league_id, season, len(missing))

        for batch in chunks(missing, BATCH_SIZE):
            batch_key = f"details:{league_id}:{season}:{'-'.join(map(str, batch))}"
            if self.state_done(batch_key):
                continue
            payload = self.api_get("fixtures", {"ids": "-".join(map(str, batch))})
            returned = set()
            for item in payload.get("response", []):
                fid = (item.get("fixture") or {}).get("id")
                if fid is not None:
                    returned.add(int(fid))
                    self.store_fixture_detail(item)
            self.mark_state(batch_key, {"requested": batch, "returned": sorted(returned)})

        # Targeted fallback only after batched retrieval.
        if ENABLE_FALLBACK:
            for fid in fixture_ids:
                row = self.conn.execute(
                    """
                    SELECT events, lineups, statistics, players
                    FROM fixture_details WHERE fixture_id=%s
                    """,
                    (fid,),
                ).fetchone()
                if not row or any(v in (None, [], {}) for v in row):
                    self.fallback_missing(fid)

    def store_season_player(self, league_id: int, season: int, item: Dict[str, Any]) -> None:
        player = item.get("player") or {}
        stats = item.get("statistics") or []
        player_id = player.get("id")
        if player_id is None:
            return
        team_ids = sorted(
            {
                s.get("team", {}).get("id")
                for s in stats
                if (s.get("team") or {}).get("id") is not None
            }
        )
        self.conn.execute(
            """
            INSERT INTO season_players(league_id, season, player_id, player_name, team_ids, raw)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (league_id, season, player_id) DO UPDATE SET
                player_name=EXCLUDED.player_name,
                team_ids=EXCLUDED.team_ids,
                raw=EXCLUDED.raw,
                updated_at=NOW()
            """,
            (
                league_id,
                season,
                player_id,
                player.get("name"),
                Jsonb(team_ids),
                Jsonb(item),
            ),
        )

    def get_stored_coverage(self, league_id: int, season: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT coverage FROM league_coverage WHERE league_id=%s AND season=%s",
            (league_id, season),
        ).fetchone()
        return row[0] if row else None

    def store_injury(self, item: Dict[str, Any], league_id: int, season: int) -> None:
        player = item.get("player") or {}
        team = item.get("team") or {}
        fixture = item.get("fixture") or {}
        fixture_id = fixture.get("id")
        player_id = player.get("id")
        if fixture_id is None or player_id is None:
            return
        self.conn.execute(
            """
            INSERT INTO injuries(
                fixture_id, league_id, season, team_id, team_name,
                player_id, player_name, type, reason, raw
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (fixture_id, player_id) DO UPDATE SET
                team_id=EXCLUDED.team_id,
                team_name=EXCLUDED.team_name,
                player_name=EXCLUDED.player_name,
                type=EXCLUDED.type,
                reason=EXCLUDED.reason,
                fetched_at=NOW(),
                raw=EXCLUDED.raw
            """,
            (
                fixture_id, league_id, season,
                team.get("id"), team.get("name"),
                player_id, player.get("name"),
                player.get("type"), player.get("reason"),
                Jsonb(item),
            ),
        )

    def collect_injuries(self, league_id: int, league_name: str, season: int) -> None:
        if not INCLUDE_INJURIES:
            return
        key = f"injuries:{league_id}:{season}"
        if self.state_done_for_season(key, season):
            return

        coverage = self.get_stored_coverage(league_id, season)
        if coverage is not None and coverage.get("injuries") is False:
            log.info("Injuries: %s %s coverage=false, atlaniyor", league_name, season)
            self.mark_state(key, {"skipped": "coverage_false"})
            return

        refreshing = REFRESH_ACTIVE_SEASON and self.is_active_season(season)
        log.info("Injuries: %s %s%s", league_name, season, " [refresh]" if refreshing else "")
        payload = self.api_get("injuries", {"league": league_id, "season": season})

        # Active-season injuries are a current snapshot. Replace that league-season atomically
        # so recovered/suspended players do not remain forever as stale rows.
        if refreshing:
            with self.conn.transaction():
                self.conn.execute(
                    "DELETE FROM injuries WHERE league_id=%s AND season=%s",
                    (league_id, season),
                )
                for item in payload.get("response", []):
                    self.store_injury(item, league_id, season)
        else:
            for item in payload.get("response", []):
                self.store_injury(item, league_id, season)
        self.mark_state(key, {"results": payload.get("results"), "refreshed": refreshing})
        # NOT: API 'type' alani pratikte hep "Missing Fixture" degerini tasiyor;
        # Injury/Suspension ayrimi asil 'reason' metninde (orn. "Thigh Injury" vs "Suspended").
        # Model tarafinda bu ayrimi 'reason' uzerinden turetecegiz.

    def collect_season_players(self, league_id: int, league_name: str, season: int) -> None:
        if not INCLUDE_SEASON_PLAYERS:
            return
        page = 1
        while True:
            key = f"players:{league_id}:{season}:page:{page}"
            if self.state_done_for_season(key, season):
                # We still need the total page count. Read it from state meta.
                meta = self.conn.execute(
                    "SELECT meta FROM collection_state WHERE state_key=%s", (key,)
                ).fetchone()
                total = int((meta[0] or {}).get("total_pages", page))
                if page >= total:
                    break
                page += 1
                continue

            log.info("Players: %s %s page %s", league_name, season, page)
            payload = self.api_get(
                "players",
                {"league": league_id, "season": season, "page": page},
            )
            for item in payload.get("response", []):
                self.store_season_player(league_id, season, item)

            paging = payload.get("paging") or {}
            current = int(paging.get("current") or page)
            total = int(paging.get("total") or current)
            self.mark_state(
                key,
                {
                    "results": payload.get("results"),
                    "current_page": current,
                    "total_pages": total,
                },
            )
            if current >= total:
                break
            page = current + 1

    def print_summary(self) -> None:
        counts = {}
        for table in ("fixtures", "fixture_details", "injuries", "season_players", "league_coverage"):
            counts[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        missing = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM fixtures f
            LEFT JOIN fixture_details d ON d.fixture_id=f.fixture_id
            WHERE f.status_short = ANY(%s)
              AND (
                  d.fixture_id IS NULL OR
                  d.events IS NULL OR d.events='[]'::jsonb OR
                  d.lineups IS NULL OR d.lineups='[]'::jsonb OR
                  d.statistics IS NULL OR d.statistics='[]'::jsonb OR
                  d.players IS NULL OR d.players='[]'::jsonb
              )
            """,
            (list(FINISHED_STATUSES),),
        ).fetchone()[0]
        log.info("SUMMARY: %s | finished fixtures with missing detail fields=%s", counts, missing)

    def run(self) -> None:
        try:
            # 1) Coverage preflight + fixture calendars
            fixture_map: Dict[Tuple[int, int], List[int]] = {}
            for season in SEASONS:
                for league_id, league_name in LEAGUES:
                    self.store_coverage(league_id, league_name, season)
                    ids = self.collect_fixture_list(league_id, league_name, season)
                    fixture_map[(league_id, season)] = ids

            # 2) Batched fixture details: up to 20 fixture IDs/request
            for season in SEASONS:
                for league_id, league_name in LEAGUES:
                    self.collect_details(fixture_map[(league_id, season)], league_id, season)

            # 3) Injuries + suspensions: 1 call per league-season (cheap, coverage-gated)
            for season in SEASONS:
                for league_id, league_name in LEAGUES:
                    self.collect_injuries(league_id, league_name, season)

            # 4) Season-level player stats (paginated)
            for season in SEASONS:
                for league_id, league_name in LEAGUES:
                    self.collect_season_players(league_id, league_name, season)

            self.print_summary()
            self.close_run("success", "Collection completed.")
            log.info("Collection completed successfully. API calls this run: %s", self.api_calls)

        except QuotaStop as exc:
            log.warning("%s", exc)
            self.print_summary()
            self.close_run("paused_quota", str(exc))
            # Exit 0 intentionally: Render shows a clean run and rerunning resumes from DB.
            sys.exit(0)
        except Exception as exc:
            log.exception("Collection failed")
            self.close_run("failed", str(exc))
            raise


def main() -> None:
    collector = Collector()
    try:
        collector.run()
    finally:
        collector.conn.close()


if __name__ == "__main__":
    main()
