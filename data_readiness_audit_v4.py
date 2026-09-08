#!/usr/bin/env python3
"""Readiness audit v4.

Fixes two v3 issues:
- availability was double-counted in readiness_score after the enrichment step;
- data_readiness_runs kept the base final-context count instead of the strict current-lineup count.

It also treats stale bookmaker snapshots as unavailable for market readiness.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

from data_readiness_audit import run_audit as run_base

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ODDS_MAX_AGE_HOURS = float(os.getenv("READINESS_ODDS_MAX_AGE_HOURS", "12"))

ALTER_SQL = """
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS current_injury_report_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS match_specific_availability_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS availability_confirmed_current BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS availability_source TEXT;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS fotmob_home_injuries INTEGER;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS fotmob_away_injuries INTEGER;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS bbs_lineup_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS sofascore_confirmed BOOLEAN;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS odds_snapshot_age_hours DOUBLE PRECISION;
"""


def _set_blocker(blockers: list[str], name: str, present: bool) -> None:
    if present:
        if name not in blockers:
            blockers.append(name)
    else:
        blockers[:] = [x for x in blockers if x != name]


def run_audit(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")

    base = run_base(db)
    updated = injury_ready = match_specific = confirmed = final_ready = 0
    goals_ready = btts_ready = corners_ready = 0

    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(ALTER_SQL)
        latest_run_id = conn.execute("SELECT id FROM data_readiness_runs ORDER BY id DESC LIMIT 1").fetchone()
        latest_run_id = latest_run_id[0] if latest_run_id else None

        rows = conn.execute(
            """
            SELECT DISTINCT ON(r.event_id)
                r.event_id,r.snapshot_hour,r.schedule_complete,r.lineup_available,
                r.home_history_matches,r.away_history_matches,
                r.home_corner_matches,r.away_corner_matches,
                r.xg_home_matches,r.xg_away_matches,
                r.odds_ou25,r.odds_btts,r.odds_corner85,r.blockers,
                p.odds_snapshot_age_hours,
                p.current_injury_report_present,p.match_specific_availability_present,
                p.availability_confirmed_current,p.availability_source,
                p.fotmob_home_injuries,p.fotmob_away_injuries,
                p.bbs_lineup_present,p.sofascore_confirmed
            FROM prediction_readiness_snapshots r
            LEFT JOIN LATERAL (
                SELECT odds_snapshot_age_hours,current_injury_report_present,
                       match_specific_availability_present,availability_confirmed_current,
                       availability_source,fotmob_home_injuries,fotmob_away_injuries,
                       bbs_lineup_present,sofascore_confirmed
                FROM prematch_feature_snapshots p
                WHERE p.event_id=r.event_id
                ORDER BY p.snapshot_hour DESC LIMIT 1
            ) p ON TRUE
            ORDER BY r.event_id,r.snapshot_hour DESC
            """
        ).fetchall()

        for row in rows:
            (
                event_id,hour,schedule_complete,lineup_available,
                home_hist,away_hist,home_corner,away_corner,xg_home,xg_away,
                has_ou,has_btts,has_corner,blockers,odds_age,
                current_injury_present,match_specific_present,confirmed_current,source,
                home_inj,away_inj,bbs_present,sofa_confirmed,
            ) = row

            current_injury_present = bool(current_injury_present)
            match_specific_present = bool(match_specific_present)
            confirmed_current = bool(confirmed_current)
            availability_present = bool(current_injury_present or match_specific_present)
            odds_fresh = odds_age is not None and float(odds_age) <= ODDS_MAX_AGE_HOURS
            ou_fresh = bool(has_ou) and odds_fresh
            btts_fresh = bool(has_btts) and odds_fresh
            corner_fresh = bool(has_corner) and odds_fresh

            match_history_ok = int(home_hist or 0) >= 10 and int(away_hist or 0) >= 10
            xg_ok = int(xg_home or 0) >= 3 and int(xg_away or 0) >= 3
            corner_history_ok = int(home_corner or 0) >= 10 and int(away_corner or 0) >= 10
            schedule_ok = bool(schedule_complete)
            lineup_signal = bool(lineup_available or match_specific_present)

            goals = bool(match_history_ok and xg_ok and ou_fresh)
            btts = bool(match_history_ok and xg_ok and btts_fresh)
            corners = bool(corner_history_ok and corner_fresh)
            final_context = bool(schedule_ok and current_injury_present and confirmed_current)

            bl = list(blockers or [])
            _set_blocker(bl, "fresh_availability_missing", not availability_present)
            _set_blocker(bl, "current_injury_report_missing", not current_injury_present)
            _set_blocker(bl, "match_lineup_not_confirmed_current", not confirmed_current)
            _set_blocker(bl, "odds_snapshot_stale", bool((has_ou or has_btts or has_corner) and not odds_fresh))

            components = [
                match_history_ok,
                xg_ok,
                corner_history_ok,
                schedule_ok,
                lineup_signal,
                current_injury_present,
                ou_fresh,
                btts_fresh,
                corner_fresh,
            ]
            score = round(sum(int(bool(x)) for x in components) / len(components), 4)

            conn.execute(
                """
                UPDATE prediction_readiness_snapshots SET
                    availability_present=%s,
                    availability_stale=%s,
                    current_injury_report_present=%s,
                    match_specific_availability_present=%s,
                    availability_confirmed_current=%s,
                    availability_source=%s,
                    fotmob_home_injuries=%s,
                    fotmob_away_injuries=%s,
                    bbs_lineup_present=%s,
                    sofascore_confirmed=%s,
                    odds_snapshot_age_hours=%s,
                    goals_provisional_ready=%s,
                    btts_provisional_ready=%s,
                    corners_provisional_ready=%s,
                    final_context_ready=%s,
                    readiness_score=%s,
                    blockers=%s,
                    built_at=NOW()
                WHERE event_id=%s AND snapshot_hour=%s
                """,
                (
                    availability_present, False if current_injury_present else None,
                    current_injury_present,match_specific_present,confirmed_current,source,
                    home_inj,away_inj,bool(bbs_present),sofa_confirmed,odds_age,
                    goals,btts,corners,final_context,score,Jsonb(bl),event_id,hour,
                ),
            )

            updated += 1
            injury_ready += int(current_injury_present)
            match_specific += int(match_specific_present)
            confirmed += int(confirmed_current)
            goals_ready += int(goals)
            btts_ready += int(btts)
            corners_ready += int(corners)
            final_ready += int(final_context)

        if latest_run_id is not None:
            conn.execute(
                """UPDATE data_readiness_runs SET
                       goals_ready=%s,btts_ready=%s,corners_ready=%s,final_context_ready=%s,
                       message='v4 freshness/availability corrected'
                   WHERE id=%s""",
                (goals_ready,btts_ready,corners_ready,final_ready,latest_run_id),
            )

    result = {
        **base,
        "version": "readiness-v4",
        "v4_updated": updated,
        "current_injury_reports": injury_ready,
        "match_specific_availability": match_specific,
        "confirmed_current_lineups": confirmed,
        "goals_ready": goals_ready,
        "btts_ready": btts_ready,
        "corners_ready": corners_ready,
        "final_context_ready": final_ready,
        "odds_max_age_hours": ODDS_MAX_AGE_HOURS,
    }
    print("DATA_READINESS_V4_RESULT", json.dumps(result, separators=(",", ":")))
    return result


if __name__ == "__main__":
    print(json.dumps(run_audit(), ensure_ascii=False, indent=2))
