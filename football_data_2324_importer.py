#!/usr/bin/env python3
"""One-time 2023/24 Big Five importer from a public Football-Data mirror.

The mirror normalizes Football-Data.co.uk fields into readable column names.
We map those fields back to the existing football_data_matches schema through
FootballDataImporter.upsert_match, so downstream model code remains unchanged.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from typing import Any, Dict, Optional

import requests

from football_data_importer import FootballDataImporter

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SOURCE_URL = os.getenv(
    "FOOTBALL_DATA_2324_URL",
    "https://raw.githubusercontent.com/vibedatascience/footballdatacouk_leagues_games_results_big5/refs/heads/main/footballdatacouk_leagues_games_results_2000_2024.csv",
)
SEASON_LABEL = "2023/24"
SEASON_CODE = "2324"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("football-data-2324")

LEAGUE_TO_DIV = {
    "Premier League": "E0",
    "La Liga": "SP1",
    "Serie A": "I1",
    "Bundesliga": "D1",
    "Ligue 1": "F1",
}

FIELD_MAP = {
    "MatchDate": "Date",
    "HomeTeam": "HomeTeam",
    "AwayTeam": "AwayTeam",
    "FullTimeHomeGoals": "FTHG",
    "FullTimeAwayGoals": "FTAG",
    "FullTimeResult": "FTR",
    "HalfTimeHomeGoals": "HTHG",
    "HalfTimeAwayGoals": "HTAG",
    "HalfTimeResult": "HTR",
    "HomeShots": "HS",
    "AwayShots": "AS",
    "HomeShotsOnTarget": "HST",
    "AwayShotsOnTarget": "AST",
    "HomeFouls": "HF",
    "AwayFouls": "AF",
    "HomeCorners": "HC",
    "AwayCorners": "AC",
    "HomeYellowCards": "HY",
    "AwayYellowCards": "AY",
    "HomeRedCards": "HR",
    "AwayRedCards": "AR",
}


def clean(v: Any) -> str:
    if v is None:
        return ""
    text = str(v).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    imp = FootballDataImporter(database_url or DATABASE_URL)
    try:
        state_key = "validation:2324:big5-normalized-mirror"
        existing = imp.conn.execute(
            "SELECT status,row_count FROM football_data_source_state WHERE source_key=%s",
            (state_key,),
        ).fetchone()
        if existing and existing[0] == "success" and int(existing[1] or 0) > 1500:
            result = {"status": "fresh", "rows": int(existing[1])}
            log.info("FD2324_RESULT %s", json.dumps(result, separators=(",", ":")))
            return result

        log.info("Downloading 2023/24 validation source: %s", SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=120)
        response.raise_for_status()
        text = response.content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        counts = {k: 0 for k in LEAGUE_TO_DIV}
        for src in reader:
            if clean(src.get("Season")) != SEASON_LABEL:
                continue
            league = clean(src.get("League"))
            division = LEAGUE_TO_DIV.get(league)
            if not division:
                continue
            row: Dict[str, str] = {}
            for source_name, fd_name in FIELD_MAP.items():
                row[fd_name] = clean(src.get(source_name))
            if imp.upsert_match(row, SEASON_CODE, division, league, SOURCE_URL):
                counts[league] += 1

        total = sum(counts.values())
        if total < 1500:
            raise RuntimeError(f"2023/24 mirror coverage unexpectedly low: total={total} by_league={counts}")
        imp.set_state(state_key, SOURCE_URL, "success", row_count=total, message=json.dumps(counts, separators=(",", ":")))
        result = {"status": "success", "rows": total, "by_league": counts}
        log.info("FD2324_RESULT %s", json.dumps(result, separators=(",", ":")))
        return result
    except Exception as exc:
        try:
            imp.set_state("validation:2324:big5-normalized-mirror", SOURCE_URL, "failed", row_count=0, message=str(exc))
        except Exception:
            pass
        raise
    finally:
        imp.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
