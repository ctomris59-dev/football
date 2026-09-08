#!/usr/bin/env python3
"""Import Big Five second-tier match history for promotion-aware priors.

Sources: Football-Data.co.uk free CSVs.
Seasons: 2024/25 and 2025/26 by default.
Leagues: Championship, Segunda, Serie B, 2. Bundesliga, Ligue 2.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

from football_data_importer import (
    FootballDataImporter, parse_date, parse_time, to_int, season_start_from_code
)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SEASONS = [x.strip() for x in os.getenv("SECOND_TIER_SEASONS", "2425,2526").split(",") if x.strip()]
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

LEAGUES: List[Tuple[str, str, str]] = [
    ("E1", "Championship", "Premier League"),
    ("SP2", "Segunda Division", "La Liga"),
    ("I2", "Serie B", "Serie A"),
    ("D2", "2. Bundesliga", "Bundesliga"),
    ("F2", "Ligue 2", "Ligue 1"),
]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS second_tier_matches (
    season_code TEXT NOT NULL,
    season_start INTEGER NOT NULL,
    division TEXT NOT NULL,
    league_name TEXT NOT NULL,
    parent_league_name TEXT NOT NULL,
    match_date DATE NOT NULL,
    kickoff_time TIME,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_goals INTEGER,
    away_goals INTEGER,
    ht_home_goals INTEGER,
    ht_away_goals INTEGER,
    home_shots INTEGER,
    away_shots INTEGER,
    home_shots_on_target INTEGER,
    away_shots_on_target INTEGER,
    home_corners INTEGER,
    away_corners INTEGER,
    home_yellow INTEGER,
    away_yellow INTEGER,
    home_red INTEGER,
    away_red INTEGER,
    over_2_5 BOOLEAN,
    btts BOOLEAN,
    corners_over_8_5 BOOLEAN,
    raw JSONB NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(season_code, division, match_date, home_team, away_team)
);
CREATE INDEX IF NOT EXISTS idx_second_tier_parent_season
  ON second_tier_matches(parent_league_name, season_code, match_date);
CREATE INDEX IF NOT EXISTS idx_second_tier_team_home
  ON second_tier_matches(home_team, season_code);
CREATE INDEX IF NOT EXISTS idx_second_tier_team_away
  ON second_tier_matches(away_team, season_code);

CREATE TABLE IF NOT EXISTS second_tier_import_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    source_calls INTEGER NOT NULL DEFAULT 0,
    rows_stored INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("second-tier-importer")


class SecondTierImporter:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db:
            raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True)
        self.conn.execute(SCHEMA_SQL)
        self.fd = FootballDataImporter(self.db)
        self.calls = 0

    def close(self) -> None:
        self.fd.close()
        self.conn.close()

    def source_key(self, season: str, division: str) -> str:
        return f"second_tier:{season}:{division}"

    def cached(self, season: str, division: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT row_count FROM football_data_source_state WHERE source_key=%s AND status='success'",
            (self.source_key(season, division),)
        ).fetchone()
        return int(row[0] or 0) if row else None

    def set_state(self, season: str, division: str, url: str, status: str, rows: int, message: str = "") -> None:
        self.fd.set_state(self.source_key(season, division), url, status, row_count=rows, message=message)

    def store_row(self, row: Dict[str, Any], season: str, division: str, league: str, parent: str, url: str) -> bool:
        dt = parse_date(row.get("Date"))
        home = str(row.get("HomeTeam") or "").strip()
        away = str(row.get("AwayTeam") or "").strip()
        hg, ag = to_int(row.get("FTHG")), to_int(row.get("FTAG"))
        if not dt or not home or not away or hg is None or ag is None:
            return False

        hc, ac = to_int(row.get("HC")), to_int(row.get("AC"))
        self.conn.execute(
            """
            INSERT INTO second_tier_matches(
              season_code,season_start,division,league_name,parent_league_name,match_date,kickoff_time,
              home_team,away_team,home_goals,away_goals,ht_home_goals,ht_away_goals,
              home_shots,away_shots,home_shots_on_target,away_shots_on_target,
              home_corners,away_corners,home_yellow,away_yellow,home_red,away_red,
              over_2_5,btts,corners_over_8_5,raw,source_url,updated_at
            ) VALUES(
              %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
              %s,%s,%s,%s,%s,NOW()
            )
            ON CONFLICT(season_code,division,match_date,home_team,away_team) DO UPDATE SET
              home_goals=EXCLUDED.home_goals,away_goals=EXCLUDED.away_goals,
              ht_home_goals=EXCLUDED.ht_home_goals,ht_away_goals=EXCLUDED.ht_away_goals,
              home_shots=EXCLUDED.home_shots,away_shots=EXCLUDED.away_shots,
              home_shots_on_target=EXCLUDED.home_shots_on_target,away_shots_on_target=EXCLUDED.away_shots_on_target,
              home_corners=EXCLUDED.home_corners,away_corners=EXCLUDED.away_corners,
              home_yellow=EXCLUDED.home_yellow,away_yellow=EXCLUDED.away_yellow,
              home_red=EXCLUDED.home_red,away_red=EXCLUDED.away_red,
              over_2_5=EXCLUDED.over_2_5,btts=EXCLUDED.btts,corners_over_8_5=EXCLUDED.corners_over_8_5,
              raw=EXCLUDED.raw,source_url=EXCLUDED.source_url,updated_at=NOW()
            """,
            (
                season, season_start_from_code(season), division, league, parent, dt, parse_time(row.get("Time")),
                home, away, hg, ag, to_int(row.get("HTHG")), to_int(row.get("HTAG")),
                to_int(row.get("HS")), to_int(row.get("AS")), to_int(row.get("HST")), to_int(row.get("AST")),
                hc, ac, to_int(row.get("HY")), to_int(row.get("AY")), to_int(row.get("HR")), to_int(row.get("AR")),
                (hg + ag) > 2, (hg > 0 and ag > 0), ((hc + ac) > 8 if hc is not None and ac is not None else None),
                Jsonb(row), url,
            )
        )
        return True

    def run(self) -> Dict[str, Any]:
        run_id = self.conn.execute(
            "INSERT INTO second_tier_import_runs(status) VALUES('running') RETURNING id"
        ).fetchone()[0]
        total = 0
        details: Dict[str, int] = {}
        try:
            for season in SEASONS:
                for division, league, parent in LEAGUES:
                    key = f"{season}:{division}"
                    cached = self.cached(season, division)
                    if cached is not None:
                        details[key] = cached
                        total += cached
                        continue
                    url = f"https://www.football-data.co.uk/mmz4281/{season}/{division}.csv"
                    try:
                        rows = self.fd.fetch_csv(url, retries=3)
                        self.calls += 1
                        stored = sum(self.store_row(r, season, division, league, parent, url) for r in rows)
                        self.set_state(season, division, url, "success", stored, "second-tier import")
                        details[key] = stored
                        total += stored
                    except Exception as exc:
                        log.warning("Second-tier source failed %s: %s", key, exc)
                        self.set_state(season, division, url, "failed", 0, str(exc))
                        details[key] = 0
            self.conn.execute(
                "UPDATE second_tier_import_runs SET finished_at=NOW(),status='success',source_calls=%s,rows_stored=%s,message=%s WHERE id=%s",
                (self.calls, total, json.dumps(details, separators=(",", ":")), run_id),
            )
            result = {"status":"success","api_calls":self.calls,"rows":total,"by_source":details}
            log.info("SECOND_TIER_RESULT %s", json.dumps(result, separators=(",", ":")))
            return result
        except Exception as exc:
            self.conn.execute(
                "UPDATE second_tier_import_runs SET finished_at=NOW(),status='failed',source_calls=%s,rows_stored=%s,message=%s WHERE id=%s",
                (self.calls, total, str(exc)[:1000], run_id),
            )
            raise


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    imp = SecondTierImporter(database_url)
    try:
        return imp.run()
    finally:
        imp.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
