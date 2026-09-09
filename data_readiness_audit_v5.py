#!/usr/bin/env python3
"""Readiness v5: preserve v4 truth rules, report only active 8-day horizon.

Confirmed XI remains mandatory for final_context_ready. The fix is denominator/scope,
not a relaxation of evidence. Near-kickoff lineup coverage is reported against only
fixtures within the lineup publication window.
"""
from __future__ import annotations
import json, os
from typing import Any, Dict, Optional
import psycopg
import data_readiness_audit_v4 as v4
DATABASE_URL=os.getenv("DATABASE_URL","").strip();HORIZON_DAYS=int(os.getenv("READINESS_HORIZON_DAYS","8"));LINEUP_ELIGIBLE_HOURS=float(os.getenv("READINESS_LINEUP_ELIGIBLE_HOURS","3"))
def run_audit(database_url:Optional[str]=None)->Dict[str,Any]:
 db=(database_url or DATABASE_URL).strip()
 if not db:raise RuntimeError("Missing DATABASE_URL")
 base=v4.run_audit(db)
 with psycopg.connect(db) as c:
  row=c.execute("""WITH active AS (SELECT event_id,match_date FROM espn_upcoming WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+(%s||' days')::interval), latest AS (
    SELECT DISTINCT ON(r.event_id) r.*,a.match_date AS active_match_date FROM prediction_readiness_snapshots r JOIN active a ON a.event_id=r.event_id ORDER BY r.event_id,r.snapshot_hour DESC)
    SELECT COUNT(*),COUNT(*) FILTER(WHERE current_injury_report_present),COUNT(*) FILTER(WHERE match_specific_availability_present),COUNT(*) FILTER(WHERE availability_confirmed_current),COUNT(*) FILTER(WHERE goals_provisional_ready),COUNT(*) FILTER(WHERE btts_provisional_ready),COUNT(*) FILTER(WHERE corners_provisional_ready),COUNT(*) FILTER(WHERE final_context_ready),COUNT(*) FILTER(WHERE active_match_date<=NOW()+(%s||' hours')::interval),COUNT(*) FILTER(WHERE active_match_date<=NOW()+(%s||' hours')::interval AND availability_confirmed_current) FROM latest""",(HORIZON_DAYS,LINEUP_ELIGIBLE_HOURS,LINEUP_ELIGIBLE_HOURS)).fetchone()
  total,inj,ms,conf,goals,btts,corners,final,eligible,eligible_conf=[int(x or 0) for x in row]
 def rate(n,d):return round(n/d,4) if d else None
 res={"status":"success","version":"readiness-v5-active-horizon","fixtures":total,"horizon_days":HORIZON_DAYS,"current_injury_reports":{"n":inj,"rate":rate(inj,total)},"match_specific_availability":{"n":ms,"rate":rate(ms,total)},"confirmed_current_lineups":{"n":conf,"rate":rate(conf,total)},"lineup_eligible":{"fixtures":eligible,"confirmed":eligible_conf,"rate":rate(eligible_conf,eligible),"window_hours":LINEUP_ELIGIBLE_HOURS},"goals_ready":{"n":goals,"rate":rate(goals,total)},"btts_ready":{"n":btts,"rate":rate(btts,total)},"corners_ready":{"n":corners,"rate":rate(corners,total)},"final_context_ready":{"n":final,"rate":rate(final,total)},"base_v4":{"fixtures_unscoped":base.get("fixtures")}}
 print("DATA_READINESS_V5_RESULT",json.dumps(res,separators=(",",":")),flush=True);return res
if __name__=="__main__":print(json.dumps(run_audit(),indent=2))
