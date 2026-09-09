#!/usr/bin/env python3
"""Build current player context from audited real sources.

Priority:
1) static ESPN 2024/25 + 2025/26 explicit-XI archive (real starts, leakage-safe);
2) targeted second-tier exact-XI history for promoted Big-Five clubs;
3) FBref 2025/26 Starts+Minutes only when the real previous-season cache is incomplete;
4) FotMob previous-season deep stats only as an optional fallback;
5) resilient current ESPN roster bridge;
6) data-informed expected-XI proxy.

Legacy DB-v3 is never allowed to replace a missing current roster with previous-only
context. No source failure may turn a neutral placeholder into apparent real coverage.
"""
from __future__ import annotations
import json, os
from typing import Any, Dict, Optional
import psycopg

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
RUN_HISTORICAL_LINEUPS=os.getenv("PLAYER_CONTEXT_HISTORICAL_LINEUPS","true").lower() in {"1","true","yes"}
PREVIOUS_TEAM_TARGET=int(os.getenv("PLAYER_CONTEXT_PREVIOUS_TEAM_TARGET","85"))


def previous_cache_state(db:str)->Dict[str,Any]:
    try:
        with psycopg.connect(db) as c:
            row=c.execute("""SELECT COUNT(DISTINCT team_slug),COUNT(*),
              COUNT(*) FILTER(WHERE COALESCE(starts,0)>0),COUNT(*) FILTER(WHERE COALESCE(minutes,0)>0)
              FROM understat_player_seasons WHERE season=2025 AND (COALESCE(starts,0)>0 OR COALESCE(minutes,0)>0)""").fetchone()
            espn=c.execute("""SELECT COUNT(DISTINCT team_slug),COUNT(*) FROM understat_player_seasons
              WHERE season=2025 AND COALESCE(starts,0)>0 AND COALESCE(raw->>'source','')='espn-historical-lineups'""").fetchone()
        return {"teams":int(row[0] or 0),"rows":int(row[1] or 0),"start_rows":int(row[2] or 0),"minute_rows":int(row[3] or 0),"espn_exact_start_teams":int(espn[0] or 0),"espn_exact_start_rows":int(espn[1] or 0)}
    except Exception as exc:
        return {"teams":0,"rows":0,"start_rows":0,"minute_rows":0,"espn_exact_start_teams":0,"espn_exact_start_rows":0,"error":str(exc)[:300]}


def run(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")

    try:
        from season_players_context_bridge import run_bridge as seed
        seed_res=seed(db)
    except Exception as exc:
        seed_res={"status":"failed_optional","error":str(exc)[:400]}

    if RUN_HISTORICAL_LINEUPS:
        try:
            from espn_historical_lineups_backfill_v2 import run_import as historical_lineups
            historical_res=historical_lineups(db)
        except Exception as exc:
            historical_res={"status":"failed_optional","error":str(exc)[:500]}
        try:
            from espn_promoted_continuity_backfill import run_import as promoted_backfill
            promoted_res=promoted_backfill(db)
        except Exception as exc:
            promoted_res={"status":"failed_optional","error":str(exc)[:500]}
    else:
        historical_res={"status":"disabled"};promoted_res={"status":"disabled"}

    cache_before=previous_cache_state(db)
    # Real ESPN exact starts are sufficient for continuity/expected-XI ranking. FBref
    # minutes enrich quality but are no longer a single point of failure.
    if int(cache_before.get("teams") or 0)>=PREVIOUS_TEAM_TARGET:
        fbref_res={"status":"skipped","reason":"real previous-season cache sufficient","cache":cache_before}
        previous_res={"status":"skipped","reason":"real previous-season cache sufficient","cache":cache_before}
    else:
        try:
            from fbref_previous_season_players import run_import as fbref
            fbref_res=fbref(db)
        except Exception as exc:
            fbref_res={"status":"failed_optional","error":str(exc)[:500]}
        cache_mid=previous_cache_state(db)
        if int(cache_mid.get("teams") or 0)<PREVIOUS_TEAM_TARGET:
            try:
                from fotmob_previous_season_players import run_import as previous_players
                previous_res=previous_players(db)
            except Exception as exc:
                previous_res={"status":"failed_optional","error":str(exc)[:500]}
        else:
            previous_res={"status":"skipped","reason":"previous-season cache sufficient after FBref","cache":cache_mid}

    cache_after=previous_cache_state(db)
    try:
        from espn_team_roster_player_bridge_v2 import run_bridge as roster
        roster_res=roster(db)
    except Exception as exc:
        roster_res={"status":"failed","error":str(exc)[:700]}
        print("PLAYER_CONTEXT_ROSTER_ERROR",json.dumps(roster_res,separators=(",",":")),flush=True)

    if int(roster_res.get("contexts_written") or 0)>0 and int(roster_res.get("teams_with_players") or 0)>0:
        try:
            from expected_xi_usage_builder import run_build as expected_xi
            expected_res=expected_xi(db)
        except Exception as exc:
            expected_res={"status":"failed_optional","error":str(exc)[:500]}
        result={"status":"success","source":"espn-current-roster+real-previous-activity","seed":seed_res,"historical_lineups":historical_res,"promoted_history":promoted_res,"previous_cache_before":cache_before,"fbref_previous":fbref_res,"previous_fallback":previous_res,"previous_cache_after":cache_after,"roster":roster_res,"expected_xi":expected_res}
        print("PLAYER_CONTEXT_ORCHESTRATOR_RESULT",json.dumps(result,separators=(",",":")),flush=True)
        return result

    try:
        from espn_prematch_player_bridge import run_bridge as prematch
        prematch_res=prematch(db)
    except Exception as exc:
        prematch_res={"status":"failed_optional","error":str(exc)[:500]}
    if int(prematch_res.get("teams_written") or 0)>0 and int(prematch_res.get("expected_xi_ready") or 0)>0:
        result={"status":"success","source":"espn-prematch","seed":seed_res,"historical_lineups":historical_res,"promoted_history":promoted_res,"previous_cache":cache_after,"prematch":prematch_res,"roster":roster_res}
        print("PLAYER_CONTEXT_ORCHESTRATOR_RESULT",json.dumps(result,separators=(",",":")),flush=True)
        return result
    raise RuntimeError(f"No real current roster/player source available; roster={roster_res}, prematch={prematch_res}")

if __name__=="__main__":print(json.dumps(run(),indent=2))
