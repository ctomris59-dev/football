#!/usr/bin/env python3
"""Build pre-match context features for every current Big Five fixture.

No external API is used here. The builder derives schedule/rest/congestion from
our own ESPN fixture history and records whether each high-value data layer is
available/fresh enough to trust before prediction.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("match-context-builder")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS match_context_features (
    event_id TEXT PRIMARY KEY,
    league_slug TEXT NOT NULL,
    league_name TEXT NOT NULL,
    match_date TIMESTAMPTZ NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_rest_days DOUBLE PRECISION,
    away_rest_days DOUBLE PRECISION,
    home_matches_7d INTEGER NOT NULL DEFAULT 0,
    away_matches_7d INTEGER NOT NULL DEFAULT 0,
    home_matches_14d INTEGER NOT NULL DEFAULT 0,
    away_matches_14d INTEGER NOT NULL DEFAULT 0,
    home_matches_30d INTEGER NOT NULL DEFAULT 0,
    away_matches_30d INTEGER NOT NULL DEFAULT 0,
    home_short_rest BOOLEAN,
    away_short_rest BOOLEAN,
    prematch_lineup_or_roster BOOLEAN NOT NULL DEFAULT FALSE,
    prematch_injury_entries INTEGER NOT NULL DEFAULT 0,
    bbs_absence_rows INTEGER NOT NULL DEFAULT 0,
    bbs_source_stale BOOLEAN,
    espn_odds_present BOOLEAN NOT NULL DEFAULT FALSE,
    oddspapi_odds_present BOOLEAN NOT NULL DEFAULT FALSE,
    oddspapi_ou25_present BOOLEAN NOT NULL DEFAULT FALSE,
    oddspapi_btts_present BOOLEAN NOT NULL DEFAULT FALSE,
    oddspapi_corner85_present BOOLEAN NOT NULL DEFAULT FALSE,
    xg_history_present BOOLEAN NOT NULL DEFAULT FALSE,
    completeness_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    quality_flags JSONB NOT NULL,
    built_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_context_date ON match_context_features(match_date);
CREATE INDEX IF NOT EXISTS idx_context_quality ON match_context_features(completeness_score DESC, match_date);

CREATE TABLE IF NOT EXISTS match_context_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    fixture_count INTEGER NOT NULL DEFAULT 0,
    high_quality_count INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""


def canon(value: Any) -> str:
    s = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    aliases = {"man utd":"manchester united","man united":"manchester united","man city":"manchester city","psg":"paris saint germain","paris sg":"paris saint germain","inter":"inter milan","milan":"ac milan"}
    return aliases.get(s, s)


def similar(a: str, b: str) -> float:
    ca, cb = canon(a), canon(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    return SequenceMatcher(None, ca, cb).ratio()


class ContextBuilder:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db:
            raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True)
        self.conn.execute(SCHEMA_SQL)

    def close(self) -> None:
        self.conn.close()

    def completed_for_team(self, team: str, before: datetime) -> List[datetime]:
        rows = self.conn.execute(
            """SELECT match_date FROM espn_current_matches
               WHERE match_date < %s AND status IS NOT NULL
                 AND (home_team=%s OR away_team=%s)
               ORDER BY match_date DESC LIMIT 20""",
            (before, team, team),
        ).fetchall()
        return [r[0] for r in rows if r[0] is not None]

    def latest_prematch(self, event_id: str) -> Tuple[bool, int]:
        row = self.conn.execute(
            """SELECT lineup_entries,roster_entries,injury_entries FROM espn_prematch_snapshots
               WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1""", (event_id,)
        ).fetchone()
        if not row:
            return False, 0
        return bool((row[0] or 0) > 0 or (row[1] or 0) > 0), int(row[2] or 0)

    def espn_odds(self, event_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM espn_odds_snapshots WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1", (event_id,)).fetchone()
        return bool(row)

    def oddspapi_market_flags(self, home: str, away: str, match_date: datetime) -> Tuple[bool,bool,bool,bool]:
        fixtures = self.conn.execute(
            """SELECT fixture_id,home_team,away_team FROM oddspapi_fixture_snapshots
               WHERE start_time BETWEEN %s-INTERVAL '8 hours' AND %s+INTERVAL '8 hours'
               ORDER BY snapshot_hour DESC""", (match_date, match_date)
        ).fetchall()
        best = None; best_score = 0.0
        for fid, h, a in fixtures:
            score = similar(home, h or "") + similar(away, a or "")
            if score > best_score:
                best = str(fid); best_score = score
        if not best or best_score < 1.35:
            return False,False,False,False
        rows = self.conn.execute(
            "SELECT lower(COALESCE(market_name,'')),handicap,price FROM oddspapi_market_prices WHERE fixture_id=%s AND price IS NOT NULL", (best,)
        ).fetchall()
        any_odds = bool(rows); ou=btts=corner=False
        for name, line, _price in rows:
            if name == 'over under full time' and line is not None and abs(float(line)-2.5)<0.001: ou=True
            if 'both teams to score' in name: btts=True
            if 'corner' in name and line is not None and abs(float(line)-8.5)<0.001: corner=True
        return any_odds,ou,btts,corner

    def bbs_absences(self, league_name: str, home: str, away: str) -> Tuple[int, Optional[bool]]:
        try:
            rows = self.conn.execute(
                """SELECT team_name,source_stale FROM bbs_absence_snapshots
                   WHERE league_name=%s AND snapshot_hour=(SELECT MAX(snapshot_hour) FROM bbs_absence_snapshots WHERE league_name=%s)""",
                (league_name, league_name),
            ).fetchall()
        except Exception:
            return 0, None
        count=0; stale_values=[]
        for team, stale in rows:
            if similar(home, team or "") >= .72 or similar(away, team or "") >= .72:
                count += 1
                if stale is not None: stale_values.append(bool(stale))
        return count, (any(stale_values) if stale_values else None)

    def xg_available(self, league_name: str, home: str, away: str, before: datetime) -> bool:
        # Require at least 3 prior xG matches for each side, mirroring the model threshold.
        rows = self.conn.execute(
            """SELECT home_team,away_team FROM understat_matches
               WHERE league_name=%s AND is_result=TRUE AND match_date < %s
                 AND home_xg IS NOT NULL AND away_xg IS NOT NULL
               ORDER BY match_date DESC LIMIT 500""", (league_name, before.date())
        ).fetchall()
        h=a=0
        for ht,at in rows:
            if max(similar(home, ht), similar(home, at)) >= .78: h += 1
            if max(similar(away, ht), similar(away, at)) >= .78: a += 1
            if h>=3 and a>=3: return True
        return False

    def build(self) -> Dict[str, Any]:
        rid = self.conn.execute("INSERT INTO match_context_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        high=total=0
        try:
            upcoming = self.conn.execute(
                """SELECT event_id,league_slug,league_name,match_date,home_team,away_team
                   FROM espn_upcoming WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '6 hours'
                   ORDER BY match_date"""
            ).fetchall()
            for event_id,league_slug,league_name,match_date,home,away in upcoming:
                hhist=self.completed_for_team(home,match_date); ahist=self.completed_for_team(away,match_date)
                hrest=(match_date-hhist[0]).total_seconds()/86400 if hhist else None
                arest=(match_date-ahist[0]).total_seconds()/86400 if ahist else None
                def counts(hist):
                    return tuple(sum(1 for d in hist if (match_date-d).total_seconds() <= days*86400) for days in (7,14,30))
                h7,h14,h30=counts(hhist); a7,a14,a30=counts(ahist)
                lineup,inj_entries=self.latest_prematch(str(event_id)); espn_odds=self.espn_odds(str(event_id))
                odds,ou,btts,corner=self.oddspapi_market_flags(home,away,match_date)
                abs_rows,abs_stale=self.bbs_absences(league_name,home,away)
                xg=self.xg_available(league_name,home,away,match_date)
                flags=[]
                if hrest is not None and hrest < 3.0: flags.append('home_short_rest')
                if arest is not None and arest < 3.0: flags.append('away_short_rest')
                if not lineup: flags.append('lineup_not_yet_available')
                if abs_stale is True: flags.append('absence_source_stale')
                if not odds: flags.append('oddspapi_missing')
                if not corner: flags.append('corner85_market_missing')
                if not xg: flags.append('xg_history_insufficient')
                components=[xg, espn_odds or odds, lineup, ou, btts]
                score=sum(1 for x in components if x)/len(components)
                # Absence layer counts only when configured/fresh. Missing source doesn't masquerade as healthy.
                if abs_stale is False: score=min(1.0,score+0.10)
                if corner: score=min(1.0,score+0.10)
                if score>=0.80: high+=1
                self.conn.execute(
                    """INSERT INTO match_context_features(
                        event_id,league_slug,league_name,match_date,home_team,away_team,
                        home_rest_days,away_rest_days,home_matches_7d,away_matches_7d,home_matches_14d,away_matches_14d,
                        home_matches_30d,away_matches_30d,home_short_rest,away_short_rest,
                        prematch_lineup_or_roster,prematch_injury_entries,bbs_absence_rows,bbs_source_stale,
                        espn_odds_present,oddspapi_odds_present,oddspapi_ou25_present,oddspapi_btts_present,oddspapi_corner85_present,
                        xg_history_present,completeness_score,quality_flags,built_at,updated_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())
                    ON CONFLICT(event_id) DO UPDATE SET
                        match_date=EXCLUDED.match_date,home_rest_days=EXCLUDED.home_rest_days,away_rest_days=EXCLUDED.away_rest_days,
                        home_matches_7d=EXCLUDED.home_matches_7d,away_matches_7d=EXCLUDED.away_matches_7d,
                        home_matches_14d=EXCLUDED.home_matches_14d,away_matches_14d=EXCLUDED.away_matches_14d,
                        home_matches_30d=EXCLUDED.home_matches_30d,away_matches_30d=EXCLUDED.away_matches_30d,
                        home_short_rest=EXCLUDED.home_short_rest,away_short_rest=EXCLUDED.away_short_rest,
                        prematch_lineup_or_roster=EXCLUDED.prematch_lineup_or_roster,prematch_injury_entries=EXCLUDED.prematch_injury_entries,
                        bbs_absence_rows=EXCLUDED.bbs_absence_rows,bbs_source_stale=EXCLUDED.bbs_source_stale,
                        espn_odds_present=EXCLUDED.espn_odds_present,oddspapi_odds_present=EXCLUDED.oddspapi_odds_present,
                        oddspapi_ou25_present=EXCLUDED.oddspapi_ou25_present,oddspapi_btts_present=EXCLUDED.oddspapi_btts_present,
                        oddspapi_corner85_present=EXCLUDED.oddspapi_corner85_present,xg_history_present=EXCLUDED.xg_history_present,
                        completeness_score=EXCLUDED.completeness_score,quality_flags=EXCLUDED.quality_flags,updated_at=NOW()""",
                    (str(event_id),league_slug,league_name,match_date,home,away,hrest,arest,h7,a7,h14,a14,h30,a30,
                     bool(hrest is not None and hrest<3.0),bool(arest is not None and arest<3.0),lineup,inj_entries,abs_rows,abs_stale,
                     espn_odds,odds,ou,btts,corner,xg,score,Jsonb(flags)),
                )
                total += 1
            self.conn.execute("UPDATE match_context_runs SET finished_at=NOW(),status='success',fixture_count=%s,high_quality_count=%s,message='ok' WHERE id=%s", (total,high,rid))
            result={"status":"success","fixtures":total,"high_quality":high}
            log.info("MATCH_CONTEXT_RESULT %s",json.dumps(result,separators=(",",":")))
            return result
        except Exception as exc:
            self.conn.execute("UPDATE match_context_runs SET finished_at=NOW(),status='failed',fixture_count=%s,high_quality_count=%s,message=%s WHERE id=%s", (total,high,str(exc)[:1000],rid))
            raise


def run_build(database_url: Optional[str]=None) -> Dict[str, Any]:
    b=ContextBuilder(database_url)
    try:return b.build()
    finally:b.close()

if __name__=='__main__':
    print(json.dumps(run_build(),ensure_ascii=False,indent=2))
