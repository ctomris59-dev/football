#!/usr/bin/env python3
"""Deterministic internal Elo fallback built entirely from owned match history.

Populates the existing ClubElo-compatible lookup tables with INTERNAL::* rows so
fixture enrichment never depends on api.clubelo.com being reachable. External
ClubElo can still be enabled as an optional diagnostic source.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg

from clubelo_importer import SCHEMA_SQL as CLUBELO_SCHEMA

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
HOME_ADVANTAGE = float(os.getenv("INTERNAL_ELO_HOME_ADVANTAGE", "60"))
K_FACTOR = float(os.getenv("INTERNAL_ELO_K_FACTOR", "20"))
SEASON_CARRY = float(os.getenv("INTERNAL_ELO_SEASON_CARRY", "0.85"))
BASE_ELO = 1500.0

SCHEMA_SQL = CLUBELO_SCHEMA + """
CREATE TABLE IF NOT EXISTS internal_elo_runs(
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  matches INTEGER NOT NULL DEFAULT 0,
  teams INTEGER NOT NULL DEFAULT 0,
  leagues INTEGER NOT NULL DEFAULT 0,
  message TEXT
);
"""


def expected(home_elo: float, away_elo: float, home_advantage: float = HOME_ADVANTAGE) -> float:
    return 1.0 / (1.0 + 10.0 ** (-(home_elo + home_advantage - away_elo) / 400.0))


def result_score(hg: int, ag: int) -> float:
    return 1.0 if hg > ag else (0.0 if hg < ag else 0.5)


def load_matches(conn, league: str) -> List[Tuple[str, date, str, str, int, int]]:
    rows = list(conn.execute(
        """SELECT season_code,match_date,home_team,away_team,home_goals,away_goals
           FROM football_data_matches
           WHERE league_name=%s AND season_code IN ('2324','2425','2526')
             AND home_goals IS NOT NULL AND away_goals IS NOT NULL""",
        (league,),
    ).fetchall())
    rows.extend(
        ("2627", r[0].date() if isinstance(r[0], datetime) else r[0], r[1], r[2], r[3], r[4])
        for r in conn.execute(
            """SELECT match_date,home_team,away_team,home_goals,away_goals
               FROM espn_current_matches WHERE league_name=%s
                 AND home_goals IS NOT NULL AND away_goals IS NOT NULL""",
            (league,),
        ).fetchall()
    )
    clean = []
    for season, dt, home, away, hg, ag in rows:
        if not dt or not home or not away or hg is None or ag is None:
            continue
        clean.append((str(season), dt, str(home), str(away), int(hg), int(ag)))
    clean.sort(key=lambda x: (x[1], x[2], x[3]))
    return clean


def build(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    leagues = ["Premier League", "La Liga", "Serie A", "Bundesliga", "Ligue 1"]
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
        rid = conn.execute("INSERT INTO internal_elo_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        total_matches = 0
        all_teams = set()
        try:
            today = datetime.now(timezone.utc).date()
            for league in leagues:
                ratings: Dict[str, float] = defaultdict(lambda: BASE_ELO)
                counts: Dict[str, int] = defaultdict(int)
                last_date: Dict[str, date] = {}
                current_season: Optional[str] = None
                for season, dt, home, away, hg, ag in load_matches(conn, league):
                    if current_season is None:
                        current_season = season
                    elif season != current_season:
                        for team in list(ratings):
                            ratings[team] = BASE_ELO + (ratings[team] - BASE_ELO) * SEASON_CARRY
                        current_season = season
                    eh = expected(ratings[home], ratings[away])
                    actual = result_score(hg, ag)
                    delta = K_FACTOR * (actual - eh)
                    ratings[home] += delta
                    ratings[away] -= delta
                    counts[home] += 1; counts[away] += 1
                    last_date[home] = dt; last_date[away] = dt
                    total_matches += 1

                # Include upcoming teams even when they have no current-season result yet.
                for row in conn.execute(
                    """SELECT home_team FROM espn_upcoming WHERE league_name=%s AND is_current=TRUE
                       UNION SELECT away_team FROM espn_upcoming WHERE league_name=%s AND is_current=TRUE""",
                    (league, league),
                ).fetchall():
                    if row and row[0]:
                        _ = ratings[str(row[0])]

                for team, elo in ratings.items():
                    all_teams.add((league, team))
                    synthetic = f"INTERNAL::{league}::{team}"
                    fd = last_date.get(team, date(2026, 7, 1))
                    conn.execute(
                        """INSERT INTO clubelo_team_map(system_team,league_name,clubelo_club,country,confidence,mapped_at)
                           VALUES(%s,%s,%s,'INTERNAL',1.0,NOW())
                           ON CONFLICT(system_team,league_name) DO UPDATE SET clubelo_club=EXCLUDED.clubelo_club,
                             country='INTERNAL',confidence=1.0,mapped_at=NOW()""",
                        (team, league, synthetic),
                    )
                    conn.execute(
                        """INSERT INTO clubelo_history(clubelo_club,from_date,to_date,elo,rank,country,level,fetched_at)
                           VALUES(%s,%s,NULL,%s,NULL,'INTERNAL',1,NOW())
                           ON CONFLICT(clubelo_club,from_date) DO UPDATE SET to_date=NULL,elo=EXCLUDED.elo,
                             country='INTERNAL',level=1,fetched_at=NOW()""",
                        (synthetic, fd, float(elo)),
                    )
                    conn.execute(
                        """INSERT INTO clubelo_daily_snapshots(snapshot_date,club,country,level,elo,rank,from_date,to_date,fetched_at)
                           VALUES(%s,%s,'INTERNAL',1,%s,NULL,%s,NULL,NOW())
                           ON CONFLICT(snapshot_date,club) DO UPDATE SET country='INTERNAL',level=1,elo=EXCLUDED.elo,
                             from_date=EXCLUDED.from_date,to_date=NULL,fetched_at=NOW()""",
                        (today, synthetic, float(elo), fd),
                    )
            conn.execute(
                """UPDATE internal_elo_runs SET finished_at=NOW(),status='success',matches=%s,teams=%s,leagues=%s,
                   message=%s WHERE id=%s""",
                (total_matches, len(all_teams), len(leagues), json.dumps({"k": K_FACTOR, "home_adv": HOME_ADVANTAGE, "carry": SEASON_CARRY}), rid),
            )
            result = {"status": "success", "matches": total_matches, "teams": len(all_teams), "leagues": len(leagues)}
            print("INTERNAL_ELO_RESULT", json.dumps(result, separators=(",", ":")))
            return result
        except Exception as exc:
            conn.execute("UPDATE internal_elo_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s", (str(exc)[:1000], rid))
            raise


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
