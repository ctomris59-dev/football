#!/usr/bin/env python3
"""Cache real 2025/26 Big Five player minutes from FotMob deep-season stats.

Purpose: fill the only material current player-context gap left by API-Football's
Free-plan season restriction. FotMob DeepStat rows expose `minutesPlayed` and
`matchesPlayed`. We persist those real values and deliberately do NOT invent
starting-XI counts: `starts` remains 0. Downstream continuity can therefore use
previous-season top-minute players as a transparent proxy while exact historical
starter continuity remains unavailable unless archived lineups exist.

FotMob web JSON is not a contracted API, so this importer is fail-soft, low-call,
raw-payload preserving, and never controls V1/V5 activation.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg
import requests
from psycopg.types.json import Jsonb

import understat_player_continuity_v2 as v2

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TARGET_SEASON_NAME = os.getenv("FOTMOB_PREVIOUS_SEASON_NAME", "2025/2026")
CONTEXT_SEASON = int(os.getenv("FOTMOB_PREVIOUS_CONTEXT_SEASON", "2025"))
REFRESH_HOURS = float(os.getenv("FOTMOB_PREVIOUS_PLAYERS_REFRESH_HOURS", "168"))
REQUEST_DELAY = float(os.getenv("FOTMOB_PREVIOUS_PLAYERS_REQUEST_DELAY_SECONDS", "0.35"))
TIMEOUT = float(os.getenv("FOTMOB_PREVIOUS_PLAYERS_TIMEOUT_SECONDS", "20"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# FotMob's stable league ids, also reflected in its public league URLs.
LEAGUES: List[Tuple[int, str, str]] = [
    (47, "Premier League", "ENG"),
    (87, "La Liga", "ESP"),
    (55, "Serie A", "ITA"),
    (54, "Bundesliga", "GER"),
    (53, "Ligue 1", "FRA"),
]
# `minutes_played` is probed first. Current FotMob clients expose minutesPlayed
# on DeepStat rows even when another sort is selected, so rating/goals are safe
# fallbacks and their rows are unioned by player id.
STAT_PROBES = tuple(x.strip() for x in os.getenv(
    "FOTMOB_PREVIOUS_PLAYER_STAT_PROBES", "minutes_played,rating,goals"
).split(",") if x.strip())
BASES = ("https://www.fotmob.com/api/data", "https://www.fotmob.com/api")

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("fotmob-previous-season-players")

SCHEMA = """
CREATE TABLE IF NOT EXISTS fotmob_player_season_stats(
    season INTEGER NOT NULL,
    league_id INTEGER NOT NULL,
    league_name TEXT NOT NULL,
    team_id TEXT,
    team_name TEXT,
    player_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    matches_played DOUBLE PRECISION,
    minutes_played DOUBLE PRECISION,
    stat_value DOUBLE PRECISION,
    stat_probe TEXT,
    raw JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(season,league_id,player_id)
);
CREATE INDEX IF NOT EXISTS idx_fotmob_player_season_team
    ON fotmob_player_season_stats(season,team_name,minutes_played DESC);
CREATE TABLE IF NOT EXISTS fotmob_previous_player_runs(
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    api_calls INTEGER NOT NULL DEFAULT 0,
    leagues_ok INTEGER NOT NULL DEFAULT 0,
    teams_seen INTEGER NOT NULL DEFAULT 0,
    players_seen INTEGER NOT NULL DEFAULT 0,
    rows_written INTEGER NOT NULL DEFAULT 0,
    errors JSONB NOT NULL DEFAULT '{}'::jsonb,
    message TEXT
);
"""


def fnum(v: Any) -> Optional[float]:
    try:
        if v in (None, ""):
            return None
        return float(str(v).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


def first(d: Dict[str, Any], keys: Iterable[str]) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d.get(k)
    return None


def season_internal_id(payload: Dict[str, Any], wanted: str) -> Optional[str]:
    for item in payload.get("seasons") or []:
        if not isinstance(item, dict):
            continue
        name = str(first(item, ("name", "season", "label")) or "")
        if name == wanted:
            sid = first(item, ("id", "seasonId", "season_id"))
            if sid is not None:
                return str(sid)
    return None


def parse_rows(payload: Dict[str, Any], probe: str) -> List[Dict[str, Any]]:
    data = payload.get("statsData")
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        participant = row.get("participant") if isinstance(row.get("participant"), dict) else {}
        pid = first(row, ("participantId", "playerId", "id")) or first(participant, ("id", "participantId", "playerId"))
        name = first(row, ("participantName", "playerName", "name")) or first(participant, ("name", "participantName"))
        team_id = first(row, ("teamId", "team_id")) or first(participant, ("teamId", "team_id"))
        team_name = first(row, ("teamName", "team_name")) or first(participant, ("teamName", "team_name"))
        minutes = fnum(first(row, ("minutesPlayed", "minutes_played", "minutes")))
        matches = fnum(first(row, ("matchesPlayed", "matches_played", "matches", "appearances")))
        stat_value = fnum(first(row, ("statValue", "stat_value", "value")))
        if pid is None or not name:
            continue
        # A DeepStat without team identity cannot safely support retention mapping.
        if not team_name:
            continue
        out.append({
            "player_id": str(pid), "player_name": str(name),
            "team_id": str(team_id) if team_id is not None else None,
            "team_name": str(team_name), "minutes": minutes, "matches": matches,
            "stat_value": stat_value, "probe": probe, "raw": row,
        })
    return out


class Importer:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db:
            raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True)
        self.conn.execute(v2.SCHEMA)
        self.conn.execute(SCHEMA)
        self.s = requests.Session()
        self.s.headers.update({
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36",
        })
        self.last = 0.0
        self.calls = 0

    def close(self) -> None:
        self.conn.close()

    def fresh(self) -> bool:
        if REFRESH_HOURS <= 0:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM fotmob_previous_player_runs WHERE status='success' AND players_seen>0 AND finished_at>=NOW()-(%s||' hours')::interval LIMIT 1",
            (REFRESH_HOURS,),
        ).fetchone()
        return bool(row)

    def get_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        errors = []
        for base in BASES:
            elapsed = time.monotonic() - self.last
            if elapsed < REQUEST_DELAY:
                time.sleep(REQUEST_DELAY - elapsed)
            try:
                r = self.s.get(base + path, params=params, timeout=(4.0, TIMEOUT))
                self.last = time.monotonic(); self.calls += 1
                if r.status_code in (403, 429, 404):
                    errors.append(f"{base.rsplit('/',1)[-1]}:{r.status_code}")
                    continue
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict):
                    return data
                errors.append("non-dict")
            except Exception as exc:
                errors.append(f"{type(exc).__name__}:{str(exc)[:100]}")
        raise RuntimeError(";".join(errors)[:500] or "FotMob request failed")

    def deepstats(self, league_id: int, country: str, probe: str) -> Tuple[List[Dict[str, Any]], str]:
        # FotMob may accept the human season label directly; if it returns no rows,
        # use the internal season id supplied in the response's seasons mapping.
        params = {"id": league_id, "season": TARGET_SEASON_NAME, "type": "players", "stat": probe}
        payload = self.get_json("/leagueseasondeepstats", params)
        rows = parse_rows(payload, probe)
        resolved = TARGET_SEASON_NAME
        if not rows:
            sid = season_internal_id(payload, TARGET_SEASON_NAME)
            if not sid:
                league_payload = self.get_json("/leagues", {"id": league_id, "season": TARGET_SEASON_NAME, "ccode3": country})
                sid = season_internal_id(league_payload, TARGET_SEASON_NAME)
            if sid:
                resolved = sid
                payload = self.get_json("/leagueseasondeepstats", {"id": league_id, "season": sid, "type": "players", "stat": probe})
                rows = parse_rows(payload, probe)
        return rows, resolved

    def run(self) -> Dict[str, Any]:
        if self.fresh():
            row = self.conn.execute(
                "SELECT players_seen,rows_written,leagues_ok,teams_seen FROM fotmob_previous_player_runs WHERE status='success' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            result = {"status":"fresh_skip","players":int(row[0]),"rows_written":int(row[1]),"leagues_ok":int(row[2]),"teams":int(row[3])}
            print("FOTMOB_PREVIOUS_PLAYERS_RESULT", json.dumps(result, separators=(",", ":")), flush=True)
            return result

        rid = self.conn.execute("INSERT INTO fotmob_previous_player_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        errors: Dict[str, str] = {}
        all_players: Dict[Tuple[int, str], Dict[str, Any]] = {}
        league_ok = 0
        try:
            for league_id, league_name, country in LEAGUES:
                league_rows: Dict[str, Dict[str, Any]] = {}
                resolved_seen = []
                for probe in STAT_PROBES:
                    try:
                        rows, resolved = self.deepstats(league_id, country, probe)
                        resolved_seen.append(f"{probe}:{resolved}:{len(rows)}")
                        for row in rows:
                            existing = league_rows.get(row["player_id"])
                            if existing is None:
                                league_rows[row["player_id"]] = row
                            else:
                                # Prefer whichever probe exposes real minutes/matches.
                                if existing.get("minutes") is None and row.get("minutes") is not None:
                                    existing["minutes"] = row["minutes"]
                                if existing.get("matches") is None and row.get("matches") is not None:
                                    existing["matches"] = row["matches"]
                                existing.setdefault("raw_probes", {})[probe] = row.get("raw")
                    except Exception as exc:
                        errors[f"{league_name}:{probe}"] = str(exc)[:300]
                if league_rows:
                    league_ok += 1
                    for pid, row in league_rows.items():
                        row["league_id"] = league_id; row["league_name"] = league_name
                        row["resolved"] = resolved_seen
                        all_players[(league_id, pid)] = row
                else:
                    errors[f"{league_name}:all"] = errors.get(f"{league_name}:{STAT_PROBES[-1] if STAT_PROBES else 'none'}", "no rows")

            params_stats = []
            params_cache = []
            teams = set()
            for (league_id, _pid), row in all_players.items():
                team = str(row["team_name"]); teams.add((league_id, v2.canon(team)))
                minutes = float(row.get("minutes") or 0.0)
                matches = float(row.get("matches") or 0.0)
                raw = dict(row.get("raw") or {})
                raw.update({
                    "source":"fotmob-deepstats", "season_name":TARGET_SEASON_NAME,
                    "stat_probe":row.get("probe"), "resolved_probes":row.get("resolved", []),
                    "minutes_derived_continuity":True, "exact_starts_available":False,
                })
                params_stats.append((
                    CONTEXT_SEASON, league_id, row["league_name"], row.get("team_id"), team,
                    row["player_id"], row["player_name"], matches, minutes,
                    row.get("stat_value"), row.get("probe"), Jsonb(raw),
                ))
                params_cache.append((
                    CONTEXT_SEASON, team, v2.team_slug(team), f"fotmob:{row['player_id']}", row["player_name"],
                    matches, 0.0, minutes, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, Jsonb(raw),
                ))

            if params_stats:
                sql_stats = """INSERT INTO fotmob_player_season_stats(season,league_id,league_name,team_id,team_name,player_id,player_name,matches_played,minutes_played,stat_value,stat_probe,raw)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(season,league_id,player_id) DO UPDATE SET team_id=EXCLUDED.team_id,team_name=EXCLUDED.team_name,
                    player_name=EXCLUDED.player_name,matches_played=EXCLUDED.matches_played,minutes_played=EXCLUDED.minutes_played,
                    stat_value=EXCLUDED.stat_value,stat_probe=EXCLUDED.stat_probe,raw=EXCLUDED.raw,fetched_at=NOW()"""
                sql_cache = """INSERT INTO understat_player_seasons(season,team_name,team_slug,player_id,player_name,games,starts,minutes,goals,xg,assists,xa,xgchain,xgbuildup,raw)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(season,team_slug,player_id) DO UPDATE SET team_name=EXCLUDED.team_name,player_name=EXCLUDED.player_name,
                    games=EXCLUDED.games,starts=EXCLUDED.starts,minutes=EXCLUDED.minutes,raw=EXCLUDED.raw,fetched_at=NOW()"""
                with self.conn.cursor() as cur:
                    cur.executemany(sql_stats, params_stats)
                    cur.executemany(sql_cache, params_cache)

            status = "success" if all_players else "empty"
            result = {
                "status":status, "season":TARGET_SEASON_NAME, "context_season":CONTEXT_SEASON,
                "api_calls":self.calls, "leagues_ok":league_ok, "teams":len(teams),
                "players":len(all_players), "rows_written":len(params_stats), "errors":errors,
            }
            self.conn.execute(
                """UPDATE fotmob_previous_player_runs SET finished_at=NOW(),status=%s,api_calls=%s,leagues_ok=%s,teams_seen=%s,
                   players_seen=%s,rows_written=%s,errors=%s,message=%s WHERE id=%s""",
                (status,self.calls,league_ok,len(teams),len(all_players),len(params_stats),Jsonb(errors),
                 json.dumps({"season":TARGET_SEASON_NAME},separators=(",",":")),rid),
            )
            print("FOTMOB_PREVIOUS_PLAYERS_RESULT", json.dumps(result, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            self.conn.execute(
                "UPDATE fotmob_previous_player_runs SET finished_at=NOW(),status='failed',api_calls=%s,errors=%s,message=%s WHERE id=%s",
                (self.calls, Jsonb(errors), str(exc)[:1000], rid),
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
