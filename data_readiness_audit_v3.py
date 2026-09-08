#!/usr/bin/env python3
"""Readiness audit v3 using current injury reports and match-specific lineup confirmation."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

from data_readiness_audit import run_audit as run_base

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

ALTER_SQL = """
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS current_injury_report_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS match_specific_availability_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS availability_confirmed_current BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS availability_source TEXT;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS fotmob_home_injuries INTEGER;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS fotmob_away_injuries INTEGER;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS bbs_lineup_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS sofascore_confirmed BOOLEAN;
"""


def run_audit(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    base = run_base(db)
    updated = injury_ready = match_specific = confirmed = final_ready = 0

    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(ALTER_SQL)
        rows = conn.execute(
            """
            SELECT DISTINCT ON(r.event_id)
                r.event_id,r.snapshot_hour,r.schedule_complete,r.lineup_available,
                r.readiness_score,r.blockers,
                p.current_injury_report_present,p.match_specific_availability_present,
                p.availability_confirmed_current,p.availability_source,
                p.fotmob_home_injuries,p.fotmob_away_injuries,
                p.bbs_lineup_present,p.sofascore_confirmed
            FROM prediction_readiness_snapshots r
            LEFT JOIN LATERAL (
                SELECT current_injury_report_present,match_specific_availability_present,
                       availability_confirmed_current,availability_source,
                       fotmob_home_injuries,fotmob_away_injuries,bbs_lineup_present,sofascore_confirmed
                FROM prematch_feature_snapshots p
                WHERE p.event_id=r.event_id
                ORDER BY p.snapshot_hour DESC LIMIT 1
            ) p ON TRUE
            ORDER BY r.event_id,r.snapshot_hour DESC
            """
        ).fetchall()

        for row in rows:
            (
                event_id,hour,schedule_complete,lineup_available,score,blockers,
                current_injury_present,match_specific_present,confirmed_current,source,
                home_inj,away_inj,bbs_present,sofa_confirmed,
            ) = row
            current_injury_present = bool(current_injury_present)
            match_specific_present = bool(match_specific_present)
            confirmed_current = bool(confirmed_current)
            availability_present = bool(current_injury_present or match_specific_present)

            bl = list(blockers or [])
            if availability_present:
                bl = [x for x in bl if x != "fresh_availability_missing"]
            elif "fresh_availability_missing" not in bl:
                bl.append("fresh_availability_missing")
            if not current_injury_present and "current_injury_report_missing" not in bl:
                bl.append("current_injury_report_missing")
            if current_injury_present:
                bl = [x for x in bl if x != "current_injury_report_missing"]
            if not confirmed_current and "match_lineup_not_confirmed_current" not in bl:
                bl.append("match_lineup_not_confirmed_current")
            if confirmed_current:
                bl = [x for x in bl if x != "match_lineup_not_confirmed_current"]

            # Existing score has 9 equal components and old availability was generally false.
            adjusted = float(score or 0.0)
            if availability_present:
                adjusted = min(1.0, adjusted + 1.0 / 9.0)
            adjusted = round(adjusted, 4)

            # 'Final' is intentionally strict. Provisional market readiness can be true days before kickoff,
            # but final context requires a fresh injury report plus a confirmed match-specific lineup.
            final_context = bool(schedule_complete and current_injury_present and confirmed_current)

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
                    final_context_ready=%s,
                    readiness_score=%s,
                    blockers=%s,
                    built_at=NOW()
                WHERE event_id=%s AND snapshot_hour=%s
                """,
                (
                    availability_present, False if current_injury_present else None,
                    current_injury_present,match_specific_present,confirmed_current,source,
                    home_inj,away_inj,bool(bbs_present),sofa_confirmed,
                    final_context,adjusted,Jsonb(bl),event_id,hour,
                ),
            )
            updated += 1
            injury_ready += int(current_injury_present)
            match_specific += int(match_specific_present)
            confirmed += int(confirmed_current)
            final_ready += int(final_context)

    result = {
        **base,
        "v3_updated": updated,
        "current_injury_reports": injury_ready,
        "match_specific_availability": match_specific,
        "confirmed_current_lineups": confirmed,
        "final_context_ready": final_ready,
    }
    print("DATA_READINESS_V3_RESULT", json.dumps(result, separators=(",", ":")))
    return result


if __name__ == "__main__":
    print(json.dumps(run_audit(), ensure_ascii=False, indent=2))
