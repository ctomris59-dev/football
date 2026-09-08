#!/usr/bin/env python3
"""Build one auditable pre-match feature snapshot per upcoming Big Five fixture.

No external requests are made here. It combines already-collected ESPN, OddsPapi
and optional BBS availability data. Schedule features are explicitly labelled
league-only because ESPN current storage currently covers domestic league games.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LOOKAHEAD_DAYS = int(os.getenv("PREMATCH_CONTEXT_LOOKAHEAD_DAYS", "14"))
ABSENCE_RECENT_DAYS = int(os.getenv("PREMATCH_ABSENCE_RECENT_DAYS", "21"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("prematch-context-builder")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS prematch_feature_snapshots (
    event_id TEXT NOT NULL,
    snapshot_hour TIMESTAMPTZ NOT NULL,
    league_slug TEXT NOT NULL,
    league_name TEXT NOT NULL,
    match_date TIMESTAMPTZ NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,

    home_days_rest DOUBLE PRECISION,
    away_days_rest DOUBLE PRECISION,
    home_matches_last_7d INTEGER NOT NULL DEFAULT 0,
    away_matches_last_7d INTEGER NOT NULL DEFAULT 0,
    home_matches_last_14d INTEGER NOT NULL DEFAULT 0,
    away_matches_last_14d INTEGER NOT NULL DEFAULT 0,
    schedule_scope TEXT NOT NULL DEFAULT 'domestic_league_only',

    lineup_entries INTEGER,
    roster_entries INTEGER,
    prematch_snapshot_age_hours DOUBLE PRECISION,

    oddspapi_fixture_id TEXT,
    odds_snapshot_age_hours DOUBLE PRECISION,
    odds_price_rows INTEGER NOT NULL DEFAULT 0,
    has_ou25 BOOLEAN NOT NULL DEFAULT FALSE,
    has_btts BOOLEAN NOT NULL DEFAULT FALSE,
    has_corner85 BOOLEAN NOT NULL DEFAULT FALSE,
    odds_movement JSONB,

    home_recent_absence_players INTEGER,
    away_recent_absence_players INTEGER,
    home_recent_injury_players INTEGER,
    away_recent_injury_players INTEGER,
    home_recent_suspension_players INTEGER,
    away_recent_suspension_players INTEGER,
    availability_as_of TIMESTAMPTZ,
    availability_stale BOOLEAN,
    availability_semantics TEXT NOT NULL DEFAULT 'recent_absence_signal_not_confirmed_current_injury',

    data_quality JSONB NOT NULL,
    built_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (event_id, snapshot_hour)
);
CREATE INDEX IF NOT EXISTS idx_prematch_context_matchdate
    ON prematch_feature_snapshots(match_date, snapshot_hour DESC);

CREATE TABLE IF NOT EXISTS prematch_context_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    upcoming_matches INTEGER NOT NULL DEFAULT 0,
    odds_matched INTEGER NOT NULL DEFAULT 0,
    availability_matched INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""

ALIASES = {
    "man utd": "manchester united", "man united": "manchester united",
    "man city": "manchester city", "nottm forest": "nottingham forest",
    "wolves": "wolverhampton wanderers", "newcastle": "newcastle united",
    "spurs": "tottenham hotspur", "tottenham": "tottenham hotspur",
    "ath bilbao": "athletic club", "athletic bilbao": "athletic club",
    "real sociedad": "real sociedad", "sociedad": "real sociedad",
    "betis": "real betis", "celta": "celta vigo", "espanol": "espanyol",
    "milan": "ac milan", "inter": "inter milan", "verona": "hellas verona",
    "dortmund": "borussia dortmund", "leverkusen": "bayer leverkusen",
    "mgladbach": "borussia monchengladbach", "borussia m gladbach": "borussia monchengladbach",
    "frankfurt": "eintracht frankfurt", "psg": "paris saint germain",
    "paris sg": "paris saint germain", "st etienne": "saint etienne",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canon(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    text = text.replace("'", "")
    text = re.sub(r"\b(fc|cf|ssc|ac|calcio|club|football club)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    text = re.sub(r"\s+", " ", text)
    return ALIASES.get(text, text)


def similarity(a: str, b: str) -> float:
    ca, cb = canon(a), canon(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    return SequenceMatcher(None, ca, cb).ratio()


def age_hours(dt: Optional[datetime], now: datetime) -> Optional[float]:
    if not dt:
        return None
    return round(max(0.0, (now - dt).total_seconds() / 3600.0), 2)


class PrematchContextBuilder:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db:
            raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True)
        self.conn.execute(SCHEMA_SQL)
        self.now = utcnow()
        self.hour = self.now.replace(minute=0, second=0, microsecond=0)

    def close(self) -> None:
        self.conn.close()

    def upcoming(self) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT event_id,league_slug,league_name,match_date,home_team,away_team
            FROM espn_upcoming
            WHERE is_current=TRUE
              AND match_date >= NOW()-INTERVAL '2 hours'
              AND match_date <= NOW()+(%s||' days')::interval
            ORDER BY match_date
            """,
            (LOOKAHEAD_DAYS,),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def schedule(self, team: str, fixture_dt: datetime) -> Dict[str, Any]:
        rows = self.conn.execute(
            """
            SELECT match_date
            FROM espn_current_matches
            WHERE match_date < %s
              AND (lower(home_team)=lower(%s) OR lower(away_team)=lower(%s))
            ORDER BY match_date DESC
            LIMIT 12
            """,
            (fixture_dt, team, team),
        ).fetchall()
        dates = [r[0] for r in rows if r[0]]
        rest = ((fixture_dt - dates[0]).total_seconds() / 86400.0) if dates else None
        seven = sum(1 for d in dates if fixture_dt - timedelta(days=7) <= d < fixture_dt)
        fourteen = sum(1 for d in dates if fixture_dt - timedelta(days=14) <= d < fixture_dt)
        return {"days_rest": round(rest, 2) if rest is not None else None, "last7": seven, "last14": fourteen}

    def latest_prematch(self, event_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT snapshot_hour,lineup_entries,roster_entries
            FROM espn_prematch_snapshots
            WHERE event_id=%s
            ORDER BY snapshot_hour DESC LIMIT 1
            """,
            (event_id,),
        ).fetchone()
        if not row:
            return {}
        return {"snapshot": row[0], "lineup": row[1], "roster": row[2]}

    def odds_fixture_candidates(self, league_name: str, fixture_dt: datetime) -> List[Tuple[Any, ...]]:
        try:
            return self.conn.execute(
                """
                SELECT DISTINCT ON (fixture_id)
                    fixture_id,league_name,start_time,home_team,away_team,snapshot_hour
                FROM oddspapi_fixture_snapshots
                WHERE start_time BETWEEN %s-INTERVAL '8 hours' AND %s+INTERVAL '8 hours'
                  AND lower(COALESCE(league_name,''))=lower(%s)
                ORDER BY fixture_id,snapshot_hour DESC
                """,
                (fixture_dt, fixture_dt, league_name),
            ).fetchall()
        except Exception:
            return []

    def match_odds_fixture(self, league_name: str, fixture_dt: datetime, home: str, away: str) -> Optional[Tuple[Any, ...]]:
        best = None
        best_score = 0.0
        for row in self.odds_fixture_candidates(league_name, fixture_dt):
            _fid, _ln, start, oh, oa, _snap = row
            sh, sa = similarity(home, oh or ""), similarity(away, oa or "")
            time_penalty = min(1.0, abs((start - fixture_dt).total_seconds()) / (8 * 3600)) if start else 1.0
            score = sh + sa - 0.15 * time_penalty
            if min(sh, sa) >= 0.55 and score > best_score:
                best, best_score = row, score
        return best

    def odds_summary(self, fixture_id: Optional[str]) -> Dict[str, Any]:
        if not fixture_id:
            return {}
        try:
            latest = self.conn.execute(
                "SELECT MAX(snapshot_hour) FROM oddspapi_market_prices WHERE fixture_id=%s",
                (fixture_id,),
            ).fetchone()[0]
            if not latest:
                return {}
            rows = self.conn.execute(
                """
                SELECT market_name,handicap,outcome_name,price,snapshot_hour
                FROM oddspapi_market_prices
                WHERE fixture_id=%s AND snapshot_hour=%s AND price IS NOT NULL
                """,
                (fixture_id, latest),
            ).fetchall()
            has_ou = has_btts = has_corner = False
            for name, handicap, outcome, price, snap in rows:
                n = str(name or "").lower()
                if "over under full time" in n and handicap is not None and abs(float(handicap)-2.5) < 0.01:
                    has_ou = True
                if "both teams to score" in n:
                    has_btts = True
                if "corner" in n and handicap is not None and abs(float(handicap)-8.5) < 0.01:
                    has_corner = True

            movement: Dict[str, Any] = {}
            first_snap = self.conn.execute(
                "SELECT MIN(snapshot_hour) FROM oddspapi_market_prices WHERE fixture_id=%s",
                (fixture_id,),
            ).fetchone()[0]
            if first_snap and first_snap != latest:
                movement = {"first_snapshot": first_snap.isoformat(), "latest_snapshot": latest.isoformat(), "hours": round((latest-first_snap).total_seconds()/3600, 2)}
            return {"snapshot": latest, "rows": len(rows), "ou25": has_ou, "btts": has_btts, "corner85": has_corner, "movement": movement}
        except Exception:
            return {}

    def latest_absence_rows(self) -> Tuple[Optional[datetime], Optional[bool], List[Tuple[Any, ...]]]:
        try:
            latest = self.conn.execute("SELECT MAX(snapshot_hour) FROM bbs_absence_snapshots").fetchone()[0]
            if not latest:
                return None, None, []
            meta = self.conn.execute(
                "SELECT MAX(as_of), bool_or(COALESCE(stale,FALSE)) FROM bbs_absence_snapshots WHERE snapshot_hour=%s",
                (latest,),
            ).fetchone()
            rows = self.conn.execute(
                """
                SELECT player_id,player_name,team_name,fixture_date,absence_kind
                FROM bbs_absence_snapshots
                WHERE snapshot_hour=%s
                  AND fixture_date >= NOW()-(%s||' days')::interval
                """,
                (latest, ABSENCE_RECENT_DAYS),
            ).fetchall()
            return meta[0], meta[1], rows
        except Exception:
            return None, None, []

    def team_absence_counts(self, team: str, rows: List[Tuple[Any, ...]]) -> Dict[str, int]:
        selected = []
        for r in rows:
            _pid, _pn, tname, _fd, _kind = r
            if similarity(team, tname or "") >= 0.72:
                selected.append(r)
        # One player can have multiple missed fixtures; count distinct players by kind.
        all_players = {str(r[0] or r[1]) for r in selected}
        injury = {str(r[0] or r[1]) for r in selected if r[4] == "injury"}
        suspension = {str(r[0] or r[1]) for r in selected if r[4] == "suspension"}
        return {"all": len(all_players), "injury": len(injury), "suspension": len(suspension)}

    def build(self) -> Dict[str, Any]:
        run_id = self.conn.execute("INSERT INTO prematch_context_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        odds_matched = availability_matched = 0
        try:
            matches = self.upcoming()
            avail_as_of, avail_stale, absence_rows = self.latest_absence_rows()
            for m in matches:
                home_sched = self.schedule(m["home_team"], m["match_date"])
                away_sched = self.schedule(m["away_team"], m["match_date"])
                pre = self.latest_prematch(m["event_id"])
                odds_match = self.match_odds_fixture(m["league_name"], m["match_date"], m["home_team"], m["away_team"])
                odds = self.odds_summary(str(odds_match[0])) if odds_match else {}
                if odds_match:
                    odds_matched += 1
                hc = self.team_absence_counts(m["home_team"], absence_rows)
                ac = self.team_absence_counts(m["away_team"], absence_rows)
                if hc["all"] or ac["all"]:
                    availability_matched += 1

                quality = {
                    "schedule_scope": "domestic_league_only",
                    "schedule_complete": home_sched["days_rest"] is not None and away_sched["days_rest"] is not None,
                    "prematch_roster_or_lineup": bool((pre.get("lineup") or 0) > 0 or (pre.get("roster") or 0) > 0),
                    "odds_matched": bool(odds_match),
                    "availability_source_present": bool(absence_rows),
                    "availability_is_confirmed_current": False,
                }

                self.conn.execute(
                    """
                    INSERT INTO prematch_feature_snapshots(
                        event_id,snapshot_hour,league_slug,league_name,match_date,home_team,away_team,
                        home_days_rest,away_days_rest,home_matches_last_7d,away_matches_last_7d,
                        home_matches_last_14d,away_matches_last_14d,
                        lineup_entries,roster_entries,prematch_snapshot_age_hours,
                        oddspapi_fixture_id,odds_snapshot_age_hours,odds_price_rows,has_ou25,has_btts,has_corner85,odds_movement,
                        home_recent_absence_players,away_recent_absence_players,
                        home_recent_injury_players,away_recent_injury_players,
                        home_recent_suspension_players,away_recent_suspension_players,
                        availability_as_of,availability_stale,data_quality,built_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT(event_id,snapshot_hour) DO UPDATE SET
                        home_days_rest=EXCLUDED.home_days_rest,away_days_rest=EXCLUDED.away_days_rest,
                        home_matches_last_7d=EXCLUDED.home_matches_last_7d,away_matches_last_7d=EXCLUDED.away_matches_last_7d,
                        home_matches_last_14d=EXCLUDED.home_matches_last_14d,away_matches_last_14d=EXCLUDED.away_matches_last_14d,
                        lineup_entries=EXCLUDED.lineup_entries,roster_entries=EXCLUDED.roster_entries,
                        prematch_snapshot_age_hours=EXCLUDED.prematch_snapshot_age_hours,
                        oddspapi_fixture_id=EXCLUDED.oddspapi_fixture_id,odds_snapshot_age_hours=EXCLUDED.odds_snapshot_age_hours,
                        odds_price_rows=EXCLUDED.odds_price_rows,has_ou25=EXCLUDED.has_ou25,has_btts=EXCLUDED.has_btts,has_corner85=EXCLUDED.has_corner85,
                        odds_movement=EXCLUDED.odds_movement,
                        home_recent_absence_players=EXCLUDED.home_recent_absence_players,away_recent_absence_players=EXCLUDED.away_recent_absence_players,
                        home_recent_injury_players=EXCLUDED.home_recent_injury_players,away_recent_injury_players=EXCLUDED.away_recent_injury_players,
                        home_recent_suspension_players=EXCLUDED.home_recent_suspension_players,away_recent_suspension_players=EXCLUDED.away_recent_suspension_players,
                        availability_as_of=EXCLUDED.availability_as_of,availability_stale=EXCLUDED.availability_stale,
                        data_quality=EXCLUDED.data_quality,built_at=NOW()
                    """,
                    (
                        m["event_id"],self.hour,m["league_slug"],m["league_name"],m["match_date"],m["home_team"],m["away_team"],
                        home_sched["days_rest"],away_sched["days_rest"],home_sched["last7"],away_sched["last7"],home_sched["last14"],away_sched["last14"],
                        pre.get("lineup"),pre.get("roster"),age_hours(pre.get("snapshot"), self.now),
                        str(odds_match[0]) if odds_match else None,age_hours(odds.get("snapshot"), self.now),int(odds.get("rows") or 0),
                        bool(odds.get("ou25")),bool(odds.get("btts")),bool(odds.get("corner85")),Jsonb(odds.get("movement") or {}),
                        hc["all"],ac["all"],hc["injury"],ac["injury"],hc["suspension"],ac["suspension"],
                        avail_as_of,avail_stale,Jsonb(quality),
                    ),
                )

            self.conn.execute("UPDATE prematch_context_runs SET finished_at=NOW(),status='success',upcoming_matches=%s,odds_matched=%s,availability_matched=%s,message='ok' WHERE id=%s", (len(matches), odds_matched, availability_matched, run_id))
            result = {"status":"success","upcoming":len(matches),"odds_matched":odds_matched,"availability_matched":availability_matched}
            log.info("PREMATCH_CONTEXT_RESULT %s", json.dumps(result, separators=(",", ":")))
            return result
        except Exception as exc:
            self.conn.execute("UPDATE prematch_context_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s", (str(exc)[:1000], run_id))
            raise


def run_build(database_url: Optional[str] = None) -> Dict[str, Any]:
    b = PrematchContextBuilder(database_url)
    try:
        return b.build()
    finally:
        b.close()


if __name__ == "__main__":
    print(json.dumps(run_build(), ensure_ascii=False, indent=2))
