#!/usr/bin/env python3
"""Complete free ESPN exact-XI archive for 2024/25 + 2025/26.

2025/26 is used as a production-continuity validator/fallback. 2024/25 plus only
pre-match-observed 2025/26 lineups make the V5 continuity backtest leakage-safe.
Completed static seasons are skipped once >=95% of discovered matches have both
explicit lineups, avoiding repeated provider calls.
"""
from __future__ import annotations
import json, os
from typing import Any, Dict, Optional
import psycopg
import espn_historical_lineups_importer as base

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
MAX_SUMMARIES=int(os.getenv("ESPN_HISTORICAL_V2_MAX_SUMMARIES","2200"))
TARGET_COVERAGE=float(os.getenv("ESPN_HISTORICAL_TARGET_COVERAGE","0.95"))
SEASONS={
  2024:("2024-08-01","2025-06-15"),
  2025:("2025-08-01","2026-06-15"),
}

def state(db:str,season:int)->Dict[str,Any]:
    try:
        with psycopg.connect(db) as c:
            row=c.execute("""SELECT COUNT(*),COUNT(*) FILTER(WHERE summary_status='success'),COUNT(*) FILTER(WHERE summary_status<>'success')
              FROM espn_historical_events WHERE season=%s""",(season,)).fetchone()
        total,ok,pending=map(int,row);return {"events":total,"success":ok,"pending":pending,"coverage":round(ok/total,4) if total else 0.0}
    except Exception:return {"events":0,"success":0,"pending":0,"coverage":0.0}

def complete(s:Dict[str,Any])->bool:
    # Big-Five full season is ~1,750 matches. Keep a conservative floor to avoid
    # treating a truncated scoreboard discovery as complete.
    return int(s.get("events") or 0)>=1600 and float(s.get("coverage") or 0)>=TARGET_COVERAGE

def run_import(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    out={};overall="success"
    for season,(start,end) in SEASONS.items():
        before=state(db,season)
        if complete(before):out[str(season)]={"status":"complete_skip","before":before};continue
        old=(base.SEASON,base.START_DATE,base.END_DATE,base.MAX_SUMMARIES)
        try:
            base.SEASON=season;base.START_DATE=start;base.END_DATE=end;base.MAX_SUMMARIES=MAX_SUMMARIES
            result=base.run_import(db);after=state(db,season);out[str(season)]={"status":"ran","before":before,"result":result,"after":after}
            if not complete(after):overall="partial"
        except Exception as exc:
            out[str(season)]={"status":"failed_optional","before":before,"error":str(exc)[:600],"after":state(db,season)};overall="partial"
        finally:base.SEASON,base.START_DATE,base.END_DATE,base.MAX_SUMMARIES=old
    res={"status":overall,"target_coverage":TARGET_COVERAGE,"seasons":out};print("ESPN_HISTORICAL_LINEUPS_V2_RESULT",json.dumps(res,separators=(",",":")),flush=True);return res
if __name__=="__main__":print(json.dumps(run_import(),indent=2))
