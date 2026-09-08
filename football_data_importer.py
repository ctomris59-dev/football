#!/usr/bin/env python3
"""
Football-Data.co.uk importer for the Big Five leagues.

Downloads free CSV files for:
- 2024/25
- 2025/26
- 2026/27

and the weekly upcoming fixtures CSV.

Stores normalized match statistics plus the raw row in PostgreSQL.
Primary modeling targets are materialized directly:
- over_2_5
- btts
- corners_over_8_5

The importer is idempotent and safe to rerun.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import time
from datetime import date, datetime, time as dt_time, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb
import requests

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
FD_BASE_URL = os.getenv("FOOTBALL_DATA_BASE_URL", "https://www.football-data.co.uk").rstrip("/")
FD_SEASONS = [x.strip() for x in os.getenv("FOOTBALL_DATA_SEASONS", "2425,2526,2627").split(",") if x.strip()]
FD_REFRESH_HOURS = float(os.getenv("FOOTBALL_DATA_REFRESH_HOURS", "6"))
FD_FIXTURES_REFRESH_HOURS = float(os.getenv("FOOTBALL_DATA_FIXTURES_REFRESH_HOURS", "2"))
FD_REQUEST_DELAY = float(os.getenv("FOOTBALL_DATA_REQUEST_DELAY_SECONDS", "1.5"))
FD_FORCE_REFRESH = os.getenv("FOOTBALL_DATA_FORCE_REFRESH", "false").lower() in {"1", "true", "yes"}
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

LEAGUES: List[Tuple[str, str]] = [
    ("E0", "Premier League"),
    ("SP1", "La Liga"),
    ("I1", "Serie A"),
    ("D1", "Bundesliga"),
    ("F1", "Ligue 1"),
]

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("football-data-importer")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS football_data_matches (
    season_code TEXT NOT NULL,
    season_start INTEGER NOT NULL,
    division TEXT NOT NULL,
    league_name TEXT NOT NULL,
    match_date DATE NOT NULL,
    kickoff_time TIME,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,

    home_goals INTEGER,
    away_goals INTEGER,
    full_time_result TEXT,
    ht_home_goals INTEGER,
    ht_away_goals INTEGER,
    half_time_result TEXT,

    home_shots INTEGER,
    away_shots INTEGER,
    home_shots_on_target INTEGER,
    away_shots_on_target INTEGER,
    home_fouls INTEGER,
    away_fouls INTEGER,
    home_corners INTEGER,
    away_corners INTEGER,
    total_corners INTEGER,
    home_yellow INTEGER,
    away_yellow INTEGER,
    home_red INTEGER,
    away_red INTEGER,

    over_2_5 BOOLEAN,
    btts BOOLEAN,
    corners_over_8_5 BOOLEAN,

    odds_home DOUBLE PRECISION,
    odds_draw DOUBLE PRECISION,
    odds_away DOUBLE PRECISION,
    odds_over_2_5 DOUBLE PRECISION,
    odds_under_2_5 DOUBLE PRECISION,

    raw JSONB NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (season_code, division, match_date, home_team, away_team)
);

CREATE INDEX IF NOT EXISTS idx_fd_matches_league_season
    ON football_data_matches(division, season_start);
CREATE INDEX IF NOT EXISTS idx_fd_matches_date
    ON football_data_matches(match_date);
CREATE INDEX IF NOT EXISTS idx_fd_matches_targets
    ON football_data_matches(over_2_5, btts, corners_over_8_5);

CREATE TABLE IF NOT EXISTS football_data_upcoming (
    division TEXT NOT NULL,
    league_name TEXT,
    match_date DATE NOT NULL,
    kickoff_time TIME,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,

    odds_home DOUBLE PRECISION,
    odds_draw DOUBLE PRECISION,
    odds_away DOUBLE PRECISION,
    odds_over_2_5 DOUBLE PRECISION,
    odds_under_2_5 DOUBLE PRECISION,

    raw JSONB NOT NULL,
    source_url TEXT NOT NULL,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (division, match_date, home_team, away_team)
);

CREATE INDEX IF NOT EXISTS idx_fd_upcoming_date
    ON football_data_upcoming(match_date);
CREATE INDEX IF NOT EXISTS idx_fd_upcoming_current
    ON football_data_upcoming(is_current, match_date);

CREATE TABLE IF NOT EXISTS football_data_source_state (
    source_key TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    last_success_at TIMESTAMPTZ,
    row_count INTEGER,
    status TEXT,
    message TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS football_data_import_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    historical_rows INTEGER NOT NULL DEFAULT 0,
    upcoming_rows INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def season_start_from_code(code: str) -> int:
    return 2000 + int(code[:2])


def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def to_int(value: Any) -> Optional[int]:
    text = clean_text(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> Optional[float]:
    text = clean_text(value)
    if text is None:
        return None
    try:
        return float(text.replace(",", "."))
    except (TypeError, ValueError):
        return None


def first_float(row: Dict[str, Any], names: Iterable[str]) -> Optional[float]:
    for name in names:
        if name in row:
            value = to_float(row.get(name))
            if value is not None:
                return value
    return None


def parse_date(value: Any) -> Optional[date]:
    text = clean_text(value)
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def parse_time(value: Any) -> Optional[dt_time]:
    text = clean_text(value)
    if not text:
        return None
    for fmt in ("%H:%M", "%H.%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    return None


def decode_csv(content: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


class FootballDataImporter:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = (database_url or DATABASE_URL).strip()
        if not self.database_url:
            raise RuntimeError("Missing DATABASE_URL environment variable.")

        self.conn = psycopg.connect(self.database_url, autocommit=True)
        self.conn.execute(SCHEMA_SQL)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; FootballDatasetCollector/1.0; "
                    "+https://github.com/ctomris59-dev/football)"
                ),
                "Accept": "text/csv,text/plain,*/*",
            }
        )

    def close(self) -> None:
        self.conn.close()

    def recently_succeeded(self, key: str, hours: float) -> bool:
        if FD_FORCE_REFRESH:
            return False
        row = self.conn.execute(
            """
            SELECT last_success_at
            FROM football_data_source_state
            WHERE source_key=%s AND status='success'
            """,
            (key,),
        ).fetchone()
        if not row or not row[0]:
            return False
        return row[0] >= utcnow() - timedelta(hours=hours)

    def set_state(
        self,
        key: str,
        url: str,
        status: str,
        *,
        row_count: Optional[int] = None,
        message: str = "",
    ) -> None:
        success_at = utcnow() if status == "success" else None
        self.conn.execute(
            """
            INSERT INTO football_data_source_state(
                source_key, source_url, last_success_at, row_count, status, message, updated_at
            )
            VALUES (%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (source_key) DO UPDATE SET
                source_url=EXCLUDED.source_url,
                last_success_at=COALESCE(EXCLUDED.last_success_at, football_data_source_state.last_success_at),
                row_count=EXCLUDED.row_count,
                status=EXCLUDED.status,
                message=EXCLUDED.message,
                updated_at=NOW()
            """,
            (key, url, success_at, row_count, status, message[:1000]),
        )

    def fetch_csv(self, url: str, retries: int = 5) -> List[Dict[str, str]]:
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            if FD_REQUEST_DELAY > 0:
                time.sleep(FD_REQUEST_DELAY)
            try:
                resp = self.session.get(url, timeout=45)
                if resp.status_code == 404:
                    raise FileNotFoundError(f"CSV not found: {url}")
                if resp.status_code == 429:
                    wait = min(120, 30 * (attempt + 1))
                    log.warning("Football-Data rate limited; retry in %ss: %s", wait, url)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    wait = min(60, 5 * (2 ** attempt))
                    log.warning("Football-Data HTTP %s; retry in %ss", resp.status_code, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()

                text = decode_csv(resp.content)
                reader = csv.DictReader(io.StringIO(text))
                rows: List[Dict[str, str]] = []
                for raw in reader:
                    row = {
                        str(k).strip().lstrip("\ufeff"): (v.strip() if isinstance(v, str) else v)
                        for k, v in raw.items()
                        if k is not None and str(k).strip()
                    }
                    if row:
                        rows.append(row)
                return rows
            except FileNotFoundError:
                raise
            except Exception as exc:
                last_error = exc
                wait = min(60, 3 * (2 ** attempt))
                log.warning(
                    "Football-Data request failed (%s), attempt %s/%s; retry in %ss",
                    exc,
                    attempt + 1,
                    retries,
                    wait,
                )
                time.sleep(wait)
        raise RuntimeError(f"Football-Data request failed after retries: {url}: {last_error}")

    def upsert_match(
        self,
        row: Dict[str, str],
        season_code: str,
        division: str,
        league_name: str,
        url: str,
    ) -> bool:
        match_date = parse_date(row.get("Date"))
        home_team = clean_text(row.get("HomeTeam"))
        away_team = clean_text(row.get("AwayTeam"))
        if not match_date or not home_team or not away_team:
            return False

        home_goals = to_int(row.get("FTHG"))
        away_goals = to_int(row.get("FTAG"))
        if home_goals is None or away_goals is None:
            return False

        home_corners = to_int(row.get("HC"))
        away_corners = to_int(row.get("AC"))
        total_corners = None
        if home_corners is not None and away_corners is not None:
            total_corners = home_corners + away_corners

        total_goals = home_goals + away_goals
        over_2_5 = total_goals >= 3
        btts = home_goals >= 1 and away_goals >= 1
        corners_over_8_5 = total_corners >= 9 if total_corners is not None else None

        odds_home = first_float(row, ("AvgH", "B365H", "PSH", "WHH", "BWH"))
        odds_draw = first_float(row, ("AvgD", "B365D", "PSD", "WHD", "BWD"))
        odds_away = first_float(row, ("AvgA", "B365A", "PSA", "WHA", "BWA"))
        odds_over_2_5 = first_float(
            row,
            ("Avg>2.5", "B365>2.5", "P>2.5", "PC>2.5", "Max>2.5"),
        )
        odds_under_2_5 = first_float(
            row,
            ("Avg<2.5", "B365<2.5", "P<2.5", "PC<2.5", "Max<2.5"),
        )

        self.conn.execute(
            """
            INSERT INTO football_data_matches(
                season_code, season_start, division, league_name,
                match_date, kickoff_time, home_team, away_team,
                home_goals, away_goals, full_time_result,
                ht_home_goals, ht_away_goals, half_time_result,
                home_shots, away_shots, home_shots_on_target, away_shots_on_target,
                home_fouls, away_fouls,
                home_corners, away_corners, total_corners,
                home_yellow, away_yellow, home_red, away_red,
                over_2_5, btts, corners_over_8_5,
                odds_home, odds_draw, odds_away, odds_over_2_5, odds_under_2_5,
                raw, source_url, fetched_at, updated_at
            )
            VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,
                %s,%s,%s,%s,%s,
                %s,%s,NOW(),NOW()
            )
            ON CONFLICT (season_code, division, match_date, home_team, away_team)
            DO UPDATE SET
                league_name=EXCLUDED.league_name,
                kickoff_time=EXCLUDED.kickoff_time,
                home_goals=EXCLUDED.home_goals,
                away_goals=EXCLUDED.away_goals,
                full_time_result=EXCLUDED.full_time_result,
                ht_home_goals=EXCLUDED.ht_home_goals,
                ht_away_goals=EXCLUDED.ht_away_goals,
                half_time_result=EXCLUDED.half_time_result,
                home_shots=EXCLUDED.home_shots,
                away_shots=EXCLUDED.away_shots,
                home_shots_on_target=EXCLUDED.home_shots_on_target,
                away_shots_on_target=EXCLUDED.away_shots_on_target,
                home_fouls=EXCLUDED.home_fouls,
                away_fouls=EXCLUDED.away_fouls,
                home_corners=EXCLUDED.home_corners,
                away_corners=EXCLUDED.away_corners,
                total_corners=EXCLUDED.total_corners,
                home_yellow=EXCLUDED.home_yellow,
                away_yellow=EXCLUDED.away_yellow,
                home_red=EXCLUDED.home_red,
                away_red=EXCLUDED.away_red,
                over_2_5=EXCLUDED.over_2_5,
                btts=EXCLUDED.btts,
                corners_over_8_5=EXCLUDED.corners_over_8_5,
                odds_home=EXCLUDED.odds_home,
                odds_draw=EXCLUDED.odds_draw,
                odds_away=EXCLUDED.odds_away,
                odds_over_2_5=EXCLUDED.odds_over_2_5,
                odds_under_2_5=EXCLUDED.odds_under_2_5,
                raw=EXCLUDED.raw,
                source_url=EXCLUDED.source_url,
                fetched_at=NOW(),
                updated_at=NOW()
            """,
            (
                season_code,
                season_start_from_code(season_code),
                division,
                league_name,
                match_date,
                parse_time(row.get("Time")),
                home_team,
                away_team,
                home_goals,
                away_goals,
                clean_text(row.get("FTR")),
                to_int(row.get("HTHG")),
                to_int(row.get("HTAG")),
                clean_text(row.get("HTR")),
                to_int(row.get("HS")),
                to_int(row.get("AS")),
                to_int(row.get("HST")),
                to_int(row.get("AST")),
                to_int(row.get("HF")),
                to_int(row.get("AF")),
                home_corners,
                away_corners,
                total_corners,
                to_int(row.get("HY")),
                to_int(row.get("AY")),
                to_int(row.get("HR")),
                to_int(row.get("AR")),
                over_2_5,
                btts,
                corners_over_8_5,
                odds_home,
                odds_draw,
                odds_away,
                odds_over_2_5,
                odds_under_2_5,
                Jsonb(row),
                url,
            ),
        )
        return True

    def import_historical_source(
        self,
        season_code: str,
        division: str,
        league_name: str,
    ) -> int:
        key = f"history:{season_code}:{division}"
        is_current = season_code == max(FD_SEASONS)
        refresh_hours = FD_REFRESH_HOURS if is_current else 24 * 365 * 20

        url = f"{FD_BASE_URL}/mmz4281/{season_code}/{division}.csv"
        if self.recently_succeeded(key, refresh_hours):
            row = self.conn.execute(
                "SELECT row_count FROM football_data_source_state WHERE source_key=%s",
                (key,),
            ).fetchone()
            return int(row[0] or 0) if row else 0

        log.info("Football-Data: %s %s", league_name, season_code)
        try:
            rows = self.fetch_csv(url)
            stored = 0
            for row in rows:
                if self.upsert_match(row, season_code, division, league_name, url):
                    stored += 1
            self.set_state(key, url, "success", row_count=stored)
            log.info("Football-Data stored: %s %s -> %s matches", league_name, season_code, stored)
            return stored
        except FileNotFoundError as exc:
            log.warning("%s", exc)
            self.set_state(key, url, "not_found", row_count=0, message=str(exc))
            return 0
        except Exception as exc:
            self.set_state(key, url, "failed", row_count=0, message=str(exc))
            raise

    def upsert_upcoming(self, row: Dict[str, str], url: str) -> bool:
        division = clean_text(row.get("Div"))
        if not division:
            return False
        league_map = dict(LEAGUES)
        if division not in league_map:
            return False

        match_date = parse_date(row.get("Date"))
        home_team = clean_text(row.get("HomeTeam"))
        away_team = clean_text(row.get("AwayTeam"))
        if not match_date or not home_team or not away_team:
            return False

        odds_home = first_float(row, ("AvgH", "B365H", "PSH", "WHH", "BWH"))
        odds_draw = first_float(row, ("AvgD", "B365D", "PSD", "WHD", "BWD"))
        odds_away = first_float(row, ("AvgA", "B365A", "PSA", "WHA", "BWA"))
        odds_over_2_5 = first_float(
            row,
            ("Avg>2.5", "B365>2.5", "P>2.5", "PC>2.5", "Max>2.5"),
        )
        odds_under_2_5 = first_float(
            row,
            ("Avg<2.5", "B365<2.5", "P<2.5", "PC<2.5", "Max<2.5"),
        )

        self.conn.execute(
            """
            INSERT INTO football_data_upcoming(
                division, league_name, match_date, kickoff_time,
                home_team, away_team,
                odds_home, odds_draw, odds_away, odds_over_2_5, odds_under_2_5,
                raw, source_url, is_current, fetched_at, updated_at
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,NOW(),NOW())
            ON CONFLICT (division, match_date, home_team, away_team)
            DO UPDATE SET
                league_name=EXCLUDED.league_name,
                kickoff_time=EXCLUDED.kickoff_time,
                odds_home=EXCLUDED.odds_home,
                odds_draw=EXCLUDED.odds_draw,
                odds_away=EXCLUDED.odds_away,
                odds_over_2_5=EXCLUDED.odds_over_2_5,
                odds_under_2_5=EXCLUDED.odds_under_2_5,
                raw=EXCLUDED.raw,
                source_url=EXCLUDED.source_url,
                is_current=TRUE,
                fetched_at=NOW(),
                updated_at=NOW()
            """,
            (
                division,
                league_map[division],
                match_date,
                parse_time(row.get("Time")),
                home_team,
                away_team,
                odds_home,
                odds_draw,
                odds_away,
                odds_over_2_5,
                odds_under_2_5,
                Jsonb(row),
                url,
            ),
        )
        return True

    def import_upcoming(self) -> int:
        key = "fixtures:main"
        url = f"{FD_BASE_URL}/fixtures.csv"
        if self.recently_succeeded(key, FD_FIXTURES_REFRESH_HOURS):
            row = self.conn.execute(
                "SELECT row_count FROM football_data_source_state WHERE source_key=%s",
                (key,),
            ).fetchone()
            return int(row[0] or 0) if row else 0

        log.info("Football-Data: weekly upcoming fixtures")
        rows = self.fetch_csv(url)
        self.conn.execute("UPDATE football_data_upcoming SET is_current=FALSE WHERE is_current=TRUE")
        stored = 0
        for row in rows:
            if self.upsert_upcoming(row, url):
                stored += 1
        self.set_state(key, url, "success", row_count=stored)
        log.info("Football-Data upcoming stored: %s", stored)
        return stored

    def run(self) -> Dict[str, int]:
        run_id = self.conn.execute(
            """
            INSERT INTO football_data_import_runs(status)
            VALUES ('running')
            RETURNING id
            """
        ).fetchone()[0]

        historical_rows = 0
        upcoming_rows = 0
        try:
            for season_code in FD_SEASONS:
                for division, league_name in LEAGUES:
                    historical_rows += self.import_historical_source(
                        season_code, division, league_name
                    )

            try:
                upcoming_rows = self.import_upcoming()
            except Exception:
                log.exception("Football-Data upcoming import failed")

            self.conn.execute(
                """
                UPDATE football_data_import_runs
                SET finished_at=NOW(), status='success',
                    historical_rows=%s, upcoming_rows=%s,
                    message=%s
                WHERE id=%s
                """,
                (
                    historical_rows,
                    upcoming_rows,
                    "Football-Data import completed.",
                    run_id,
                ),
            )
            return {
                "historical_rows": historical_rows,
                "upcoming_rows": upcoming_rows,
            }
        except Exception as exc:
            self.conn.execute(
                """
                UPDATE football_data_import_runs
                SET finished_at=NOW(), status='failed',
                    historical_rows=%s, upcoming_rows=%s,
                    message=%s
                WHERE id=%s
                """,
                (historical_rows, upcoming_rows, str(exc)[:1000], run_id),
            )
            raise


def run_import(database_url: Optional[str] = None) -> Dict[str, int]:
    importer = FootballDataImporter(database_url)
    try:
        result = importer.run()
        log.info("Football-Data import summary: %s", result)
        return result
    finally:
        importer.close()


def main() -> None:
    run_import()


if __name__ == "__main__":
    main()
