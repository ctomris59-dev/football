#!/usr/bin/env python3
"""Merge historical absence signals and match-specific Sofascore availability into prematch context."""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import psycopg

DATABASE_URL=os.getenv("DATABASE_URL","").strip()

ALTER_SQL="""
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS sofascore_confirmed BOOLEAN;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS sofascore_home_missing INTEGER;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS sofascore_away_missing INTEGER;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS sofascore_snapshot_age_hours DOUBLE PRECISION;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS match_specific_availability_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS availability_confirmed_current BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prematch_feature_snapshots ADD COLUMN IF NOT EXISTS availability_source TEXT;
"""


def run_enrich(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    updated=matched=confirmed=0
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(ALTER_SQL)
        rows=conn.execute("""SELECT DISTINCT ON(event_id) event_id,snapshot_hour,availability_as_of,availability_stale FROM prematch_feature_snapshots ORDER BY event_id,snapshot_hour DESC""").fetchall()
        now=datetime.now(timezone.utc)
        for event_id,hour,bbs_asof,bbs_stale in rows:
            sofa=conn.execute("""SELECT snapshot_hour,confirmed,home_missing_count,away_missing_count FROM sofascore_availability_snapshots WHERE espn_event_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",(event_id,)).fetchone()
            sofa_present=bool(sofa)
            sofa_confirmed=bool(sofa[1]) if sofa else False
            sofa_age=round(max(0.0,(now-sofa[0]).total_seconds()/3600.0),2) if sofa and sofa[0] else None
            bbs_fresh=bbs_asof is not None and bbs_stale is not True
            current_confirmed=sofa_confirmed and (sofa_age is None or sofa_age<=6.0)
            source=("sofascore_confirmed+bbs" if current_confirmed and bbs_fresh else "sofascore_confirmed" if current_confirmed else "sofascore_match_specific+bbs" if sofa_present and bbs_fresh else "sofascore_match_specific" if sofa_present else "bbs_recent_absence" if bbs_fresh else None)
            conn.execute("""UPDATE prematch_feature_snapshots SET sofascore_confirmed=%s,sofascore_home_missing=%s,sofascore_away_missing=%s,sofascore_snapshot_age_hours=%s,match_specific_availability_present=%s,availability_confirmed_current=%s,availability_source=%s,built_at=NOW() WHERE event_id=%s AND snapshot_hour=%s""",
                         (sofa_confirmed,sofa[2] if sofa else None,sofa[3] if sofa else None,sofa_age,sofa_present,current_confirmed,source,event_id,hour))
            updated+=1;matched+=int(sofa_present);confirmed+=int(current_confirmed)
    result={"status":"success","updated":updated,"match_specific":matched,"confirmed_current":confirmed}
    print("AVAILABILITY_ENRICH_RESULT",json.dumps(result,separators=(",",":")))
    return result

if __name__=="__main__":print(json.dumps(run_enrich(),indent=2))
