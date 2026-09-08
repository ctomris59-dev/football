#!/usr/bin/env python3
"""Unified current player-availability layer for pre-match features.

Source semantics are kept separate:
- FotMob: current squad injury flags / expected return dates (primary current injury signal)
- BBS: match-specific stored starting XI + bench when published
- Sofascore: optional match-specific missingPlayers + confirmed flag
- ESPN: existing pre-match roster/lineup presence remains in prematch_feature_snapshots

No missing source is interpreted as 'healthy'.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
CURRENT_INJURY_MAX_AGE_HOURS = float(os.getenv("CURRENT_INJURY_MAX_AGE_HOURS", "18"))
MATCH_LINEUP_MAX_AGE_HOURS = float(os.getenv("MATCH_LINEUP_MAX_AGE_HOURS", "6"))

ALTER_SQL = """
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS fotmob_home_injuries INTEGER;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS fotmob_away_injuries INTEGER;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS fotmob_home_injured_players JSONB;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS fotmob_away_injured_players JSONB;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS fotmob_snapshot_age_hours DOUBLE PRECISION;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS current_injury_report_present BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS bbs_lineup_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS bbs_lineup_confirmed BOOLEAN;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS bbs_home_starters INTEGER;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS bbs_away_starters INTEGER;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS bbs_snapshot_age_hours DOUBLE PRECISION;

ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS sofascore_confirmed BOOLEAN;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS sofascore_home_missing INTEGER;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS sofascore_away_missing INTEGER;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS sofascore_snapshot_age_hours DOUBLE PRECISION;

ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS match_specific_availability_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS availability_confirmed_current BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS availability_source TEXT;
"""


def age_hours(dt: Optional[datetime], now: datetime) -> Optional[float]:
    if not dt:
        return None
    return round(max(0.0, (now - dt).total_seconds() / 3600.0), 2)


def run_enrich(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    now = datetime.now(timezone.utc)
    updated = current_injury = match_specific = confirmed = 0

    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(ALTER_SQL)
        rows = conn.execute(
            """
            SELECT DISTINCT ON(event_id) event_id,snapshot_hour
            FROM prematch_feature_snapshots
            ORDER BY event_id,snapshot_hour DESC
            """
        ).fetchall()

        for event_id, hour in rows:
            try:
                fm = conn.execute(
                    """
                    SELECT snapshot_hour,home_injury_count,away_injury_count,home_injured_players,away_injured_players
                    FROM fotmob_fixture_availability_snapshots
                    WHERE espn_event_id=%s ORDER BY snapshot_hour DESC LIMIT 1
                    """,
                    (event_id,),
                ).fetchone()
            except Exception:
                fm = None

            try:
                bbs = conn.execute(
                    """
                    SELECT snapshot_hour,home_starters,away_starters,home_bench,away_bench,explicit_confirmed
                    FROM bbs_lineup_snapshots
                    WHERE espn_event_id=%s ORDER BY snapshot_hour DESC LIMIT 1
                    """,
                    (event_id,),
                ).fetchone()
            except Exception:
                bbs = None

            try:
                sofa = conn.execute(
                    """
                    SELECT snapshot_hour,confirmed,home_missing_count,away_missing_count
                    FROM sofascore_availability_snapshots
                    WHERE espn_event_id=%s ORDER BY snapshot_hour DESC LIMIT 1
                    """,
                    (event_id,),
                ).fetchone()
            except Exception:
                sofa = None

            fm_age = age_hours(fm[0], now) if fm else None
            bbs_age = age_hours(bbs[0], now) if bbs else None
            sofa_age = age_hours(sofa[0], now) if sofa else None

            current_injury_present = bool(fm and fm_age is not None and fm_age <= CURRENT_INJURY_MAX_AGE_HOURS)
            bbs_present = bool(
                bbs and bbs_age is not None and bbs_age <= MATCH_LINEUP_MAX_AGE_HOURS
                and ((bbs[1] or 0) > 0 or (bbs[2] or 0) > 0 or (bbs[3] or 0) > 0 or (bbs[4] or 0) > 0)
            )
            sofa_present = bool(sofa and sofa_age is not None and sofa_age <= MATCH_LINEUP_MAX_AGE_HOURS)
            sofa_confirmed = bool(sofa[1]) if sofa else False
            bbs_confirmed = bool(bbs[5]) if bbs and bbs[5] is not None else False
            lineup_confirmed = bool((sofa_present and sofa_confirmed) or (bbs_present and bbs_confirmed))
            match_specific_present = bool(sofa_present or bbs_present)

            sources = []
            if current_injury_present:
                sources.append("fotmob_current_injury")
            if bbs_present:
                sources.append("bbs_match_lineup")
            if sofa_present:
                sources.append("sofascore_match_specific")
            source = "+".join(sources) if sources else None

            conn.execute(
                """
                UPDATE prematch_feature_snapshots SET
                    fotmob_home_injuries=%s,fotmob_away_injuries=%s,
                    fotmob_home_injured_players=%s,fotmob_away_injured_players=%s,
                    fotmob_snapshot_age_hours=%s,current_injury_report_present=%s,
                    bbs_lineup_present=%s,bbs_lineup_confirmed=%s,bbs_home_starters=%s,bbs_away_starters=%s,bbs_snapshot_age_hours=%s,
                    sofascore_confirmed=%s,sofascore_home_missing=%s,sofascore_away_missing=%s,sofascore_snapshot_age_hours=%s,
                    match_specific_availability_present=%s,availability_confirmed_current=%s,availability_source=%s,
                    availability_as_of=%s,availability_stale=%s,built_at=NOW()
                WHERE event_id=%s AND snapshot_hour=%s
                """,
                (
                    fm[1] if fm else None, fm[2] if fm else None,
                    fm[3] if fm else None, fm[4] if fm else None,
                    fm_age, current_injury_present,
                    bbs_present, bbs[5] if bbs else None, bbs[1] if bbs else None, bbs[2] if bbs else None, bbs_age,
                    sofa[1] if sofa else None, sofa[2] if sofa else None, sofa[3] if sofa else None, sofa_age,
                    match_specific_present, lineup_confirmed, source,
                    fm[0] if fm else None, False if current_injury_present else None,
                    event_id, hour,
                ),
            )
            updated += 1
            current_injury += int(current_injury_present)
            match_specific += int(match_specific_present)
            confirmed += int(lineup_confirmed)

    result = {
        "status": "success",
        "updated": updated,
        "current_injury_reports": current_injury,
        "match_specific": match_specific,
        "confirmed_lineups": confirmed,
    }
    print("AVAILABILITY_ENRICH_V2_RESULT", json.dumps(result, separators=(",", ":")))
    return result


if __name__ == "__main__":
    print(json.dumps(run_enrich(), ensure_ascii=False, indent=2))
