#!/usr/bin/env python3
"""Quota-safe Expected-XI/player-impact + squad-continuity builder v2.

Instead of scraping one Understat team page per team (slow and brittle), fetches
one league page per Big-Five league/season and groups league-level playersData by
team. Current + previous season therefore need at most 10 HTTP requests.

The output schema is compatible with advanced_context_v4_builder. Current
injuries come from the latest FotMob availability snapshot. New layers remain
shadow/ranking context until policy validation activates them.
"""
from __future__ import annotations

import codecs
import json
import os
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb
import requests

import understat_player_continuity as legacy

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BASE = "https://understat.com"
CURRENT_SEASON = int(os.getenv("PLAYER_CONTEXT_CURRENT_SEASON", "2026"))
PREVIOUS_SEASON = CURRENT_SEASON - 1
LOOKAHEAD_DAYS = int(os.getenv("PLAYER_CONTEXT_LOOKAHEAD_DAYS", "8"))
REQUEST_DELAY = float(os.getenv("PLAYER_CONTEXT_REQUEST_DELAY_SECONDS", "0.55"))
HTTP_TIMEOUT = float(os.getenv("PLAYER_CONTEXT_HTTP_TIMEOUT_SECONDS", "25"))
LEAGUES = [
    ("EPL", "Premier League"),
    ("La_Liga", "La Liga"),
    ("Serie_A", "Serie A"),
    ("Bundesliga", "Bundesliga"),
    ("Ligue_1", "Ligue 1"),
]

SCHEMA = legacy.SCHEMA + """
CREATE TABLE IF NOT EXISTS understat_league_player_runs(
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  season INTEGER,
  league_code TEXT,
  league_name TEXT,
  http_status INTEGER,
  player_rows INTEGER NOT NULL DEFAULT 0,
  team_rows INTEGER NOT NULL DEFAULT 0,
  message TEXT
);
"""

ALIASES = {
    "man utd": "manchester united",
    "man united": "manchester united",
    "man city": "manchester city",
    "nottm forest": "nottingham forest",
    "wolves": "wolverhampton wanderers",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "milan": "ac milan",
    "paris sg": "paris saint germain",
    "psg": "paris saint germain",
    "mgladbach": "borussia monchengladbach",
    "borussia m gladbach": "borussia monchengladbach",
    "ath bilbao": "athletic club",
    "athletic bilbao": "athletic club",
    "inter milan": "inter",
}

def canon(v: Any) -> str:
    s = unicodedata.normalize("NFKD", str(v or "")).encode("ascii", "ignore").decode().lower().replace("'", "")
    s = re.sub(r"\b(fc|cf|ssc|ac|club|football club|afc)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return ALIASES.get(s, s)

def team_slug(team: str) -> str:
    return canon(team).replace(" ", "_")

def _decode_js_string(raw: str) -> Optional[Any]:
    for decoder in (
        lambda x: codecs.decode(x, "unicode_escape"),
        lambda x: bytes(x, "utf-8").decode("unicode_escape"),
    ):
        try:
            return json.loads(decoder(raw))
        except Exception:
            pass
    return None

def extract_players_data(html: str) -> List[Dict[str, Any]]:
    patterns = [
        r"var\s+playersData\s*=\s*JSON\.parse\('(?P<data>(?:\\.|[^'])*)'\)",
        r"playersData\s*=\s*JSON\.parse\('(?P<data>(?:\\.|[^'])*)'\)",
        r"JSON\.parse\('(?P<data>(?:\\.|[^'])*)'\)\s*;\s*var\s+playersData",
    ]
    for pat in patterns:
        m = re.search(pat, html, re.S)
        if not m:
            continue
        obj = _decode_js_string(m.group("data"))
        if isinstance(obj, dict) and isinstance(obj.get("players"), list):
            obj = obj["players"]
        if isinstance(obj, list):
            return [x for x in obj if isinstance(x, dict)]
        if isinstance(obj, dict):
            return [x for x in obj.values() if isinstance(x, dict)]
    return []

def team_title(row: Dict[str, Any]) -> Optional[str]:
    for k in ("team_title", "teamTitle", "team_name", "teamName"):
        if row.get(k):
            return str(row[k])
    team = row.get("team")
    if isinstance(team, dict):
        for k in ("title", "name"):
            if team.get(k):
                return str(team[k])
    return None

def normalized_player(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    p = legacy.normalize_player(row)
    if not p:
        return None
    p["team_title"] = team_title(row)
    return p

def match_team(want: str, available: Dict[str, Tuple[str, List[Dict[str, Any]]]]) -> Optional[Tuple[str, List[Dict[str, Any]], float]]:
    cw = canon(want)
    if cw in available:
        label, rows = available[cw]
        return label, rows, 1.0
    best = None
    for key, (label, rows) in available.items():
        score = SequenceMatcher(None, cw, key).ratio()
        wa, wb = set(cw.split()), set(key.split())
        if wa and wb and (wa & wb):
            score = max(score, 0.72 + 0.20 * len(wa & wb) / max(len(wa), len(wb)))
        if best is None or score > best[2]:
            best = (label, rows, score)
    return best if best and best[2] >= 0.72 else None

def injury_names(conn, team: str) -> Tuple[set[str], List[Dict[str, Any]]]:
    return legacy.injury_names(conn, team)

def player_scores(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    return legacy.player_scores(rows)

class Importer:
    def __init__(self, database_url: Optional[str] = None):
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db:
            raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True)
        self.conn.execute(SCHEMA)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 Chrome/152 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        })
        self.http_calls = 0
        self.last_call = 0.0

    def close(self):
        self.conn.close()

    def fetch_league(self, code: str, league_name: str, season: int) -> Dict[str, Tuple[str, List[Dict[str, Any]]]]:
        rid = self.conn.execute(
            "INSERT INTO understat_league_player_runs(status,season,league_code,league_name) VALUES('running',%s,%s,%s) RETURNING id",
            (season, code, league_name),
        ).fetchone()[0]
        wait = REQUEST_DELAY - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        status = None
        try:
            r = self.session.get(f"{BASE}/league/{code}/{season}", timeout=HTTP_TIMEOUT)
            self.last_call = time.monotonic()
            self.http_calls += 1
            status = int(r.status_code)
            if r.status_code != 200:
                raise RuntimeError(f"Understat league HTTP {r.status_code}: {code}/{season}")
            raw_rows = extract_players_data(r.text)
            if not raw_rows:
                raise RuntimeError(f"Understat playersData empty: {code}/{season}")
            grouped: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {}
            buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            labels: Dict[str, str] = {}
            for raw in raw_rows:
                label = team_title(raw)
                p = normalized_player(raw)
                if not label or not p:
                    continue
                key = canon(label)
                labels[key] = label
                buckets[key].append(p)
            if not buckets:
                raise RuntimeError(f"Understat player rows have no team_title: {code}/{season}")
            for key, rows in buckets.items():
                label = labels[key]
                slug = team_slug(label)
                self.conn.execute("DELETE FROM understat_player_seasons WHERE season=%s AND team_slug=%s", (season, slug))
                for p in rows:
                    self.conn.execute(
                        """INSERT INTO understat_player_seasons(
                           season,team_name,team_slug,player_id,player_name,games,starts,minutes,goals,xg,assists,xa,xgchain,xgbuildup,raw)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (season, label, slug, p["player_id"], p["player_name"], p["games"], p["starts"], p["minutes"],
                         p["goals"], p["xg"], p["assists"], p["xa"], p["xgchain"], p["xgbuildup"], Jsonb(p["raw"])),
                    )
                self.conn.execute(
                    """INSERT INTO understat_player_team_state(season,team_name,team_slug,player_rows,status,message)
                       VALUES(%s,%s,%s,%s,'success','league-page-v2')
                       ON CONFLICT(season,team_slug) DO UPDATE SET team_name=EXCLUDED.team_name,player_rows=EXCLUDED.player_rows,
                         status='success',message='league-page-v2',fetched_at=NOW()""",
                    (season, label, slug, len(rows)),
                )
                grouped[key] = (label, rows)
            self.conn.execute(
                """UPDATE understat_league_player_runs SET finished_at=NOW(),status='success',http_status=%s,
                   player_rows=%s,team_rows=%s,message='playersData league-page-v2' WHERE id=%s""",
                (status, sum(len(v[1]) for v in grouped.values()), len(grouped), rid),
            )
            return grouped
        except Exception as exc:
            self.conn.execute(
                "UPDATE understat_league_player_runs SET finished_at=NOW(),status='failed',http_status=%s,message=%s WHERE id=%s",
                (status, str(exc)[:700], rid),
            )
            raise

    def build_context(self, team: str, cur: List[Dict[str, Any]], prev: List[Dict[str, Any]], *, current_label: Optional[str], previous_label: Optional[str]) -> Dict[str, Any]:
        injured_names, injury_objs = injury_names(self.conn, team)
        cur_scores, prev_scores = player_scores(cur), player_scores(prev)
        cur_by = {canon(r.get("player_name")): r for r in cur if r.get("player_name")}
        prev_by = {canon(r.get("player_name")): r for r in prev if r.get("player_name")}
        all_names = set(cur_by) | set(prev_by)
        cur_games = max([float(r.get("games") or 0) for r in cur] or [0.0])
        current_weight = max(0.20, min(0.72, 0.20 + cur_games / 20.0 * 0.52))
        merged = []
        for name in all_names:
            cr, pr = cur_by.get(name), prev_by.get(name)
            cs = cur_scores.get((cr or {}).get("player_id", "")) if cr else None
            ps = prev_scores.get((pr or {}).get("player_id", "")) if pr else None
            if cr is not None and pr is None:
                score = current_weight * (cs if cs is not None else 0.5) + (1-current_weight) * 0.45
            elif pr is not None and cr is None:
                score = current_weight * 0.35 + (1-current_weight) * (ps if ps is not None else 0.5)
            else:
                score = current_weight * (cs if cs is not None else 0.5) + (1-current_weight) * (ps if ps is not None else 0.5)
            row = cr or pr or {}
            merged.append({
                "name": name,
                "label": row.get("player_name"),
                "score": float(score),
                "minutes": float(row.get("minutes") or 0),
                "starts": float(row.get("starts") or 0),
                "injured": name in injured_names,
            })
        top = sorted(merged, key=lambda x: (x["starts"], x["minutes"], x["score"]), reverse=True)[:11]
        denom = sum(x["score"] for x in top) or 1.0
        injury_impact = min(0.55, sum(x["score"] for x in merged if x["injured"]) / denom)
        available = sorted((x for x in merged if not x["injured"]), key=lambda x: (x["starts"], x["minutes"], x["score"]), reverse=True)[:11]
        expected = sum(x["score"] for x in available) / len(available) if available else None
        top11 = sum(x["score"] for x in top) / len(top) if top else None

        prev_total = sum(float(r.get("minutes") or 0) for r in prev)
        retained = sum(float(r.get("minutes") or 0) for n, r in prev_by.items() if n in cur_by)
        retained_share = retained / prev_total if prev_total > 0 else None
        prev_starters = {
            n for n, r in sorted(
                prev_by.items(),
                key=lambda kv: (float(kv[1].get("starts") or 0), float(kv[1].get("minutes") or 0)),
                reverse=True,
            )[:11]
        }
        starter_continuity = len(prev_starters & set(cur_by)) / len(prev_starters) if prev_starters else None
        gk = any(
            "goal" in str(x.get("position") or "").lower() or str(x.get("position") or "").lower() == "gk"
            for x in injury_objs
        )
        key_abs = sorted((x for x in merged if x["injured"]), key=lambda x: x["score"], reverse=True)[:6]
        if prev:
            coverage = min(1.0, 0.60 * min(1.0, len(cur) / 18.0) + 0.40 * min(1.0, len(prev) / 18.0))
        else:
            coverage = min(0.65, 0.65 * min(1.0, len(cur) / 18.0))
        return {
            "expected": expected,
            "top11": top11,
            "impact": injury_impact,
            "gk": gk,
            "retained": retained_share,
            "starter_continuity": starter_continuity,
            "coverage": coverage,
            "key_absences": [{"name": x["label"], "importance": round(x["score"], 4)} for x in key_abs],
            "meta": {
                "source": "understat-league-pages-v2",
                "current_players": len(cur),
                "previous_players": len(prev),
                "current_weight": round(current_weight, 3),
                "understat_current_team": current_label,
                "understat_previous_team": previous_label,
                "current_only": bool(cur and not prev),
            },
        }

    def run(self) -> Dict[str, Any]:
        rid = self.conn.execute("INSERT INTO player_context_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        upcoming = self.conn.execute(
            """SELECT DISTINCT league_name,home_team,away_team FROM espn_upcoming
               WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours'
                 AND match_date<=NOW()+(%s||' days')::interval""",
            (LOOKAHEAD_DAYS,),
        ).fetchall()
        by_league: Dict[str, set[str]] = defaultdict(set)
        for league, home, away in upcoming:
            by_league[str(league)].update((str(home), str(away)))
        league_code = {name: code for code, name in LEAGUES}
        caches: Dict[Tuple[str, int], Dict[str, Tuple[str, List[Dict[str, Any]]]]] = {}
        errors: Dict[str, str] = {}
        for league in by_league:
            code = league_code.get(league)
            if not code:
                continue
            for season in (CURRENT_SEASON, PREVIOUS_SEASON):
                try:
                    caches[(league, season)] = self.fetch_league(code, league, season)
                except Exception as exc:
                    caches[(league, season)] = {}
                    errors[f"{league}:{season}"] = str(exc)[:300]

        hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        teams = current = previous = mapped = current_only = 0
        coverages: List[float] = []
        try:
            for league, names in by_league.items():
                cur_avail = caches.get((league, CURRENT_SEASON), {})
                prev_avail = caches.get((league, PREVIOUS_SEASON), {})
                for team in sorted(names):
                    teams += 1
                    cm = match_team(team, cur_avail)
                    pm = match_team(team, prev_avail)
                    cur = cm[1] if cm else []
                    prev = pm[1] if pm else []
                    current += int(bool(cur))
                    previous += int(bool(prev))
                    mapped += int(bool(cur or prev))
                    current_only += int(bool(cur and not prev))
                    ctx = self.build_context(
                        team, cur, prev,
                        current_label=cm[0] if cm else None,
                        previous_label=pm[0] if pm else None,
                    )
                    coverages.append(float(ctx["coverage"] or 0))
                    meta = dict(ctx["meta"])
                    meta["current_match_score"] = round(cm[2], 4) if cm else None
                    meta["previous_match_score"] = round(pm[2], 4) if pm else None
                    self.conn.execute(
                        """INSERT INTO player_team_context_snapshots(
                           team_name,snapshot_hour,current_season,previous_season,expected_xi_strength,top11_strength,
                           injury_impact,goalkeeper_injured,retained_minutes_share,starter_continuity,player_coverage,key_absences,source_meta)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(team_name,snapshot_hour) DO UPDATE SET
                             current_season=EXCLUDED.current_season,previous_season=EXCLUDED.previous_season,
                             expected_xi_strength=EXCLUDED.expected_xi_strength,top11_strength=EXCLUDED.top11_strength,
                             injury_impact=EXCLUDED.injury_impact,goalkeeper_injured=EXCLUDED.goalkeeper_injured,
                             retained_minutes_share=EXCLUDED.retained_minutes_share,starter_continuity=EXCLUDED.starter_continuity,
                             player_coverage=EXCLUDED.player_coverage,key_absences=EXCLUDED.key_absences,source_meta=EXCLUDED.source_meta""",
                        (team, hour, CURRENT_SEASON, PREVIOUS_SEASON, ctx["expected"], ctx["top11"], ctx["impact"], ctx["gk"],
                         ctx["retained"], ctx["starter_continuity"], ctx["coverage"], Jsonb(ctx["key_absences"]), Jsonb(meta)),
                    )
            msg = {
                "source": "understat-league-pages-v2",
                "mapped": mapped,
                "current_only": current_only,
                "avg_coverage": round(sum(coverages)/len(coverages), 4) if coverages else 0,
                "errors": errors,
            }
            self.conn.execute(
                """UPDATE player_context_runs SET finished_at=NOW(),status=%s,teams=%s,teams_with_current=%s,
                   teams_with_previous=%s,http_calls=%s,message=%s WHERE id=%s""",
                ("success" if current else "failed", teams, current, previous, self.http_calls,
                 json.dumps(msg, separators=(",", ":")), rid),
            )
            result = {
                "status": "success" if current else "failed",
                "teams": teams,
                "mapped": mapped,
                "current": current,
                "previous": previous,
                "current_only": current_only,
                "http_calls": self.http_calls,
                "avg_coverage": msg["avg_coverage"],
                "errors": errors,
            }
            print("PLAYER_CONTEXT_V2_RESULT", json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return result
        except Exception as exc:
            self.conn.execute(
                "UPDATE player_context_runs SET finished_at=NOW(),status='failed',http_calls=%s,message=%s WHERE id=%s",
                (self.http_calls, str(exc)[:700], rid),
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
