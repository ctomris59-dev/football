#!/usr/bin/env python3
"""Current-season Big Five importer using ESPN's public soccer endpoints.

No API key is required. The importer reads daily scoreboards for the five major
European leagues, stores upcoming fixtures, and enriches completed games from
the ESPN game-summary boxscore. The full raw payloads are preserved so model
features can be extended later without re-downloading old games.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb
import requests

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ESPN_LOOKBACK_DAYS = int(os.getenv("ESPN_LOOKBACK_DAYS", "45"))
ESPN_LOOKAHEAD_DAYS = int(os.getenv("ESPN_LOOKAHEAD_DAYS", "14"))
ESPN_REFRESH_HOURS = float(os.getenv("ESPN_REFRESH_HOURS", "2"))
ESPN_REQUEST_DELAY = float(os.getenv("ESPN_REQUEST_DELAY_SECONDS", "0.08"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

LEAGUES: List[Tuple[str, str]] = [
    ("eng.1", "Premier League"),
    ("esp.1", "La Liga"),
    ("ita.1", "Serie A"),
    ("ger.1", "Bundesliga"),
    ("fra.1", "Ligue 1"),
]

SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("espn-current-importer")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS espn_current_matches (
    event_id TEXT PRIMARY KEY,
    league_slug TEXT NOT NULL,
    league_name TEXT NOT NULL,
    match_date TIMESTAMPTZ NOT NULL,
    home_team_id TEXT,
    home_team TEXT NOT NULL,
    away_team_id TEXT,
    away_team TEXT NOT NULL,
    home_goals INTEGER,
    away_goals INTEGER,
    home_corners INTEGER,
    away_corners INTEGER,
    total_corners INTEGER,
    home_shots INTEGER,
    away_shots INTEGER,
    home_shots_on_target INTEGER,
    away_shots_on_target INTEGER,
    home_fouls INTEGER,
    away_fouls INTEGER,
    home_possession DOUBLE PRECISION,
    away_possession DOUBLE PRECISION,
    over_2_5 BOOLEAN,
    btts BOOLEAN,
    corners_over_8_5 BOOLEAN,
    status TEXT,
    scoreboard_raw JSONB NOT NULL,
    summary_raw JSONB,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_espn_current_league_date
    ON espn_current_matches(league_slug, match_date);
CREATE INDEX IF NOT EXISTS idx_espn_current_teams
    ON espn_current_matches(home_team, away_team);

CREATE TABLE IF NOT EXISTS espn_upcoming (
    event_id TEXT PRIMARY KEY,
    league_slug TEXT NOT NULL,
    league_name TEXT NOT NULL,
    match_date TIMESTAMPTZ NOT NULL,
    home_team_id TEXT,
    home_team TEXT NOT NULL,
    away_team_id TEXT,
    away_team TEXT NOT NULL,
    status TEXT,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    raw JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_espn_upcoming_date
    ON espn_upcoming(is_current, match_date);

CREATE TABLE IF NOT EXISTS espn_import_state (
    state_key TEXT PRIMARY KEY,
    last_success_at TIMESTAMPTZ,
    completed_matches INTEGER NOT NULL DEFAULT 0,
    upcoming_matches INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS espn_import_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    completed_matches INTEGER NOT NULL DEFAULT 0,
    upcoming_matches INTEGER NOT NULL DEFAULT 0,
    summary_calls INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace("%", "").strip()))
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def competitor_pair(event: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]]:
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    competition = competitions[0]
    competitors = competition.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    return home, away, competition


def display_team(comp: Dict[str, Any]) -> str:
    team = comp.get("team") or {}
    return (
        team.get("displayName")
        or team.get("shortDisplayName")
        or team.get("name")
        or "Unknown"
    )


def stats_map(summary: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for side in ((summary.get("boxscore") or {}).get("teams") or []):
        team_id = str((side.get("team") or {}).get("id") or "")
        if not team_id:
            continue
        mapped: Dict[str, Any] = {}
        for stat in side.get("statistics") or []:
            name = str(stat.get("name") or "").lower()
            label = str(stat.get("label") or stat.get("displayName") or "").lower()
            key = f"{name} {label}"
            value = stat.get("displayValue", stat.get("value"))
            mapped[key] = value
        out[team_id] = mapped
    return out


def find_stat(stats: Dict[str, Any], needles: Iterable[str], *, float_value: bool = False):
    normalized = [n.lower() for n in needles]
    for key, value in stats.items():
        if any(n in key for n in normalized):
            return to_float(value) if float_value else to_int(value)
    return None


class ESPNImporter:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = (database_url or DATABASE_URL).strip()
        if not self.database_url:
            raise RuntimeError("Missing DATABASE_URL environment variable.")
        self.conn = psycopg.connect(self.database_url, autocommit=True)
        self.conn.execute(SCHEMA_SQL)
        # Keep requests' normal Python user-agent. ESPN's public edge does not
        # require browser impersonation.
        self.session = requests.Session()
        self.summary_calls = 0

    def close(self) -> None:
        self.conn.close()

    def recently_succeeded(self) -> bool:
        row = self.conn.execute(
            "SELECT last_success_at FROM espn_import_state WHERE state_key='big5-current'"
        ).fetchone()
        if not row or not row[0]:
            return False
        return row[0] >= utcnow() - timedelta(hours=ESPN_REFRESH_HOURS)

    def get_json(self, url: str, params: Optional[Dict[str, str]] = None, retries: int = 4) -> Dict[str, Any]:
        last: Optional[Exception] = None
        for attempt in range(retries):
            if ESPN_REQUEST_DELAY:
                time.sleep(ESPN_REQUEST_DELAY)
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    time.sleep(min(30, 3 * (attempt + 1)))
                    continue
                if resp.status_code >= 500:
                    time.sleep(min(30, 2 ** attempt))
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last = exc
                time.sleep(min(20, 2 ** attempt))
        raise RuntimeError(f"ESPN request failed: {url}: {last}")

    def event_already_complete(self, event_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM espn_current_matches WHERE event_id=%s",
            (event_id,),
        ).fetchone()
        return bool(row)

    def fetch_summary(self, league_slug: str, event_id: str) -> Dict[str, Any]:
        self.summary_calls += 1
        return self.get_json(
            f"{SITE_BASE}/{league_slug}/summary",
            {"event": event_id},
        )

    def upsert_completed(self, league_slug: str, league_name: str, event: Dict[str, Any]) -> bool:
        event_id = str(event.get("id") or "")
        pair = competitor_pair(event)
        match_dt = parse_iso(event.get("date"))
        if not event_id or not pair or not match_dt:
            return False
        home, away, _competition = pair
        home_id = str(home.get("id") or (home.get("team") or {}).get("id") or "")
        away_id = str(away.get("id") or (away.get("team") or {}).get("id") or "")
        hg = to_int(home.get("score"))
        ag = to_int(away.get("score"))
        if hg is None or ag is None:
            return False

        summary: Dict[str, Any] = {}
        if not self.event_already_complete(event_id):
            try:
                summary = self.fetch_summary(league_slug, event_id)
            except Exception as exc:
                log.warning("ESPN summary unavailable %s: %s", event_id, exc)
        else:
            # Preserve existing detailed stats rather than spending another call.
            return True

        mapped = stats_map(summary)
        hs = mapped.get(home_id, {})
        aws = mapped.get(away_id, {})

        home_corners = find_stat(hs, ("corner kick", "corners", "cornerkicks"))
        away_corners = find_stat(aws, ("corner kick", "corners", "cornerkicks"))
        total_corners = (
            home_corners + away_corners
            if home_corners is not None and away_corners is not None
            else None
        )

        home_shots_on_target = find_stat(hs, ("shots on target", "shotsontarget"))
        away_shots_on_target = find_stat(aws, ("shots on target", "shotsontarget"))
        home_shots = find_stat(hs, ("total shots", "totalshots", "shots"))
        away_shots = find_stat(aws, ("total shots", "totalshots", "shots"))
        home_fouls = find_stat(hs, ("fouls committed", "foulscommitted", "fouls"))
        away_fouls = find_stat(aws, ("fouls committed", "foulscommitted", "fouls"))
        home_possession = find_stat(hs, ("possession",), float_value=True)
        away_possession = find_stat(aws, ("possession",), float_value=True)

        status = str(((event.get("status") or {}).get("type") or {}).get("detail") or "final")
        self.conn.execute(
            """
            INSERT INTO espn_current_matches(
                event_id, league_slug, league_name, match_date,
                home_team_id, home_team, away_team_id, away_team,
                home_goals, away_goals,
                home_corners, away_corners, total_corners,
                home_shots, away_shots, home_shots_on_target, away_shots_on_target,
                home_fouls, away_fouls, home_possession, away_possession,
                over_2_5, btts, corners_over_8_5, status,
                scoreboard_raw, summary_raw, fetched_at, updated_at
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,NOW(),NOW()
            )
            ON CONFLICT (event_id) DO UPDATE SET
                status=EXCLUDED.status,
                scoreboard_raw=EXCLUDED.scoreboard_raw,
                summary_raw=COALESCE(EXCLUDED.summary_raw, espn_current_matches.summary_raw),
                updated_at=NOW()
            """,
            (
                event_id, league_slug, league_name, match_dt,
                home_id or None, display_team(home), away_id or None, display_team(away),
                hg, ag,
                home_corners, away_corners, total_corners,
                home_shots, away_shots, home_shots_on_target, away_shots_on_target,
                home_fouls, away_fouls, home_possession, away_possession,
                hg + ag >= 3,
                hg >= 1 and ag >= 1,
                total_corners >= 9 if total_corners is not None else None,
                status,
                Jsonb(event),
                Jsonb(summary) if summary else None,
            ),
        )
        return True

    def upsert_upcoming(self, league_slug: str, league_name: str, event: Dict[str, Any]) -> bool:
        event_id = str(event.get("id") or "")
        pair = competitor_pair(event)
        match_dt = parse_iso(event.get("date"))
        if not event_id or not pair or not match_dt:
            return False
        home, away, _competition = pair
        home_id = str(home.get("id") or (home.get("team") or {}).get("id") or "")
        away_id = str(away.get("id") or (away.get("team") or {}).get("id") or "")
        status = str(((event.get("status") or {}).get("type") or {}).get("detail") or "scheduled")
        self.conn.execute(
            """
            INSERT INTO espn_upcoming(
                event_id, league_slug, league_name, match_date,
                home_team_id, home_team, away_team_id, away_team,
                status, is_current, raw, fetched_at, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,NOW(),NOW())
            ON CONFLICT (event_id) DO UPDATE SET
                league_slug=EXCLUDED.league_slug,
                league_name=EXCLUDED.league_name,
                match_date=EXCLUDED.match_date,
                home_team_id=EXCLUDED.home_team_id,
                home_team=EXCLUDED.home_team,
                away_team_id=EXCLUDED.away_team_id,
                away_team=EXCLUDED.away_team,
                status=EXCLUDED.status,
                is_current=TRUE,
                raw=EXCLUDED.raw,
                updated_at=NOW()
            """,
            (
                event_id, league_slug, league_name, match_dt,
                home_id or None, display_team(home), away_id or None, display_team(away),
                status, Jsonb(event),
            ),
        )
        return True

    def run(self) -> Dict[str, int]:
        if self.recently_succeeded():
            row = self.conn.execute(
                """
                SELECT completed_matches, upcoming_matches
                FROM espn_import_state WHERE state_key='big5-current'
                """
            ).fetchone()
            result = {
                "completed_matches": int(row[0] or 0) if row else 0,
                "upcoming_matches": int(row[1] or 0) if row else 0,
            }
            log.info("ESPN current import is fresh; skipping: %s", result)
            return result

        run_id = self.conn.execute(
            "INSERT INTO espn_import_runs(status) VALUES ('running') RETURNING id"
        ).fetchone()[0]

        completed = 0
        upcoming = 0
        try:
            self.conn.execute("UPDATE espn_upcoming SET is_current=FALSE WHERE is_current=TRUE")
            today = utcnow().date()
            start = today - timedelta(days=ESPN_LOOKBACK_DAYS)
            end = today + timedelta(days=ESPN_LOOKAHEAD_DAYS)

            for league_slug, league_name in LEAGUES:
                log.info("ESPN current season: %s", league_name)
                day = start
                seen: set[str] = set()
                while day <= end:
                    payload = self.get_json(
                        f"{SITE_BASE}/{league_slug}/scoreboard",
                        {"dates": day.strftime("%Y%m%d"), "limit": "100"},
                    )
                    for event in payload.get("events") or []:
                        event_id = str(event.get("id") or "")
                        if not event_id or event_id in seen:
                            continue
                        seen.add(event_id)
                        status_type = (event.get("status") or {}).get("type") or {}
                        completed_flag = bool(status_type.get("completed")) or status_type.get("state") == "post"
                        event_dt = parse_iso(event.get("date"))
                        if completed_flag:
                            if self.upsert_completed(league_slug, league_name, event):
                                completed += 1
                        elif event_dt and event_dt >= utcnow() - timedelta(hours=6):
                            if self.upsert_upcoming(league_slug, league_name, event):
                                upcoming += 1
                    day += timedelta(days=1)

            self.conn.execute(
                """
                INSERT INTO espn_import_state(
                    state_key, last_success_at, completed_matches, upcoming_matches, message, updated_at
                ) VALUES ('big5-current',NOW(),%s,%s,%s,NOW())
                ON CONFLICT (state_key) DO UPDATE SET
                    last_success_at=NOW(),
                    completed_matches=EXCLUDED.completed_matches,
                    upcoming_matches=EXCLUDED.upcoming_matches,
                    message=EXCLUDED.message,
                    updated_at=NOW()
                """,
                (completed, upcoming, "ESPN current-season import completed."),
            )
            self.conn.execute(
                """
                UPDATE espn_import_runs
                SET finished_at=NOW(), status='success', completed_matches=%s,
                    upcoming_matches=%s, summary_calls=%s, message=%s
                WHERE id=%s
                """,
                (completed, upcoming, self.summary_calls, "Completed", run_id),
            )
            result = {"completed_matches": completed, "upcoming_matches": upcoming}
            log.info("ESPN current import summary: %s, summary_calls=%s", result, self.summary_calls)
            return result
        except Exception as exc:
            self.conn.execute(
                """
                UPDATE espn_import_runs
                SET finished_at=NOW(), status='failed', completed_matches=%s,
                    upcoming_matches=%s, summary_calls=%s, message=%s
                WHERE id=%s
                """,
                (completed, upcoming, self.summary_calls, str(exc)[:1000], run_id),
            )
            raise


def run_import(database_url: Optional[str] = None) -> Dict[str, int]:
    importer = ESPNImporter(database_url)
    try:
        return importer.run()
    finally:
        importer.close()


def main() -> None:
    run_import()


if __name__ == "__main__":
    main()
