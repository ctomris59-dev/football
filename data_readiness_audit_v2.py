#!/usr/bin/env python3
"""Availability-aware readiness audit v2.

Runs the existing market/history audit, then upgrades availability readiness with
match-specific Sofascore missing-player/confirmed-lineup snapshots.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import psycopg
from psycopg.types.json import Jsonb
from data_readiness_audit import run_audit as run_base

ALTER_SQL="""
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS match_specific_availability_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS availability_confirmed_current BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS sofascore_confirmed BOOLEAN;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS availability_source TEXT;
"""


def run_audit(database_url:Optional[str]=None)->Dict[str,Any]:
    base=run_base(database_url)
    db=database_url
    if not db:raise RuntimeError("Missing DATABASE_URL")
    updated=match_specific=confirmed=0
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(ALTER_SQL)
        rows=conn.execute("""SELECT DISTINCT ON(r.event_id) r.event_id,r.snapshot_hour,r.schedule_complete,r.lineup_available,r.availability_present,r.availability_stale,r.readiness_score,r.blockers,p.match_specific_availability_present,p.availability_confirmed_current,p.availability_source,p.sofascore_confirmed FROM prediction_readiness_snapshots r LEFT JOIN LATERAL (SELECT match_specific_availability_present,availability_confirmed_current,availability_source,sofascore_confirmed FROM prematch_feature_snapshots p WHERE p.event_id=r.event_id ORDER BY p.snapshot_hour DESC LIMIT 1) p ON TRUE ORDER BY r.event_id,r.snapshot_hour DESC""").fetchall()
        for event_id,hour,schedule,lineup,bbs_present,bbs_stale,score,blockers,sofa_present,confirmed_current,source,sofa_confirmed in rows:
            union_present=bool(bbs_present) or bool(sofa_present)
            effective_stale=False if sofa_present else bbs_stale
            final_context=bool(schedule) and bool(lineup) and union_present
            bl=list(blockers or [])
            if union_present:
                bl=[x for x in bl if x!="fresh_availability_missing"]
            if not confirmed_current and "availability_not_confirmed_current" not in bl:
                bl.append("availability_not_confirmed_current")
            if confirmed_current:
                bl=[x for x in bl if x!="availability_not_confirmed_current"]
            # Base score contains one availability component out of nine. Replace only that component.
            old_avail=bool(bbs_present)
            adjusted=float(score or 0.0)+(1.0/9.0 if union_present and not old_avail else 0.0)
            adjusted=min(1.0,round(adjusted,4))
            conn.execute("""UPDATE prediction_readiness_snapshots SET availability_present=%s,availability_stale=%s,match_specific_availability_present=%s,availability_confirmed_current=%s,sofascore_confirmed=%s,availability_source=%s,final_context_ready=%s,readiness_score=%s,blockers=%s,built_at=NOW() WHERE event_id=%s AND snapshot_hour=%s""",
                         (union_present,effective_stale,bool(sofa_present),bool(confirmed_current),sofa_confirmed,source,final_context,adjusted,Jsonb(bl),event_id,hour))
            updated+=1;match_specific+=int(bool(sofa_present));confirmed+=int(bool(confirmed_current))
    result={**base,"v2_updated":updated,"match_specific_availability":match_specific,"confirmed_current":confirmed}
    print("DATA_READINESS_V2_RESULT",json.dumps(result,separators=(",",":")))
    return result

if __name__=="__main__":
    import os
    print(json.dumps(run_audit(os.getenv("DATABASE_URL","")),ensure_ascii=False,indent=2))
