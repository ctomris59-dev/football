#!/usr/bin/env python3
"""Build current player context from audited real sources.

Order: existing API cache -> FBref final 2025/26 Starts+Minutes -> optional FotMob
fallback only when FBref is incomplete -> resume ESPN explicit historical XI archive
-> resilient current ESPN roster bridge. Legacy DB-v3 is never allowed to replace a
missing current roster with previous-season-only context.
"""
from __future__ import annotations
import json, os
from typing import Any, Dict, Optional
DATABASE_URL=os.getenv("DATABASE_URL","").strip()
RUN_HISTORICAL_LINEUPS=os.getenv("PLAYER_CONTEXT_HISTORICAL_LINEUPS","true").lower() in {"1","true","yes"}

def run(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    try:
        from season_players_context_bridge import run_bridge as seed
        seed_res=seed(db)
    except Exception as exc:seed_res={"status":"failed_optional","error":str(exc)[:400]}

    try:
        from fbref_previous_season_players import run_import as fbref
        fbref_res=fbref(db)
    except Exception as exc:
        fbref_res={"status":"failed_optional","error":str(exc)[:500]}
    fbref_teams=int(fbref_res.get("teams") or 0)

    # Confirmed-empty on Render; only spend calls when FBref did not supply broad coverage.
    if fbref_teams<85:
        try:
            from fotmob_previous_season_players import run_import as previous_players
            previous_res=previous_players(db)
        except Exception as exc:previous_res={"status":"failed_optional","error":str(exc)[:500]}
    else:previous_res={"status":"skipped","reason":"FBref previous-season coverage sufficient","fbref_teams":fbref_teams}

    if RUN_HISTORICAL_LINEUPS:
        try:
            from espn_historical_lineups_importer import run_import as historical_lineups
            historical_res=historical_lineups(db)
        except Exception as exc:historical_res={"status":"failed_optional","error":str(exc)[:500]}
    else:historical_res={"status":"disabled"}

    try:
        from espn_team_roster_player_bridge_v2 import run_bridge as roster
        roster_res=roster(db)
    except Exception as exc:
        roster_res={"status":"failed","error":str(exc)[:700]}
        print("PLAYER_CONTEXT_ROSTER_ERROR",json.dumps(roster_res,separators=(",",":")),flush=True)
    if int(roster_res.get("contexts_written") or 0)>0 and int(roster_res.get("teams_with_players") or 0)>0:
        result={"status":"success","source":"espn-team-roster-v2+fbref","seed":seed_res,"fbref_previous":fbref_res,"previous_fallback":previous_res,"historical_lineups":historical_res,"roster":roster_res}
        print("PLAYER_CONTEXT_ORCHESTRATOR_RESULT",json.dumps(result,separators=(",",":")),flush=True);return result

    # Upcoming ESPN summaries often contain only team shells days before kickoff.
    try:
        from espn_prematch_player_bridge import run_bridge as prematch
        prematch_res=prematch(db)
    except Exception as exc:prematch_res={"status":"failed_optional","error":str(exc)[:500]}
    if int(prematch_res.get("teams_written") or 0)>0 and int(prematch_res.get("expected_xi_ready") or 0)>0:
        result={"status":"success","source":"espn-prematch","seed":seed_res,"fbref_previous":fbref_res,"historical_lineups":historical_res,"prematch":prematch_res,"roster":roster_res}
        print("PLAYER_CONTEXT_ORCHESTRATOR_RESULT",json.dumps(result,separators=(",",":")),flush=True);return result

    raise RuntimeError(f"No real current roster/player source available; roster={roster_res}, prematch={prematch_res}")
if __name__=="__main__":print(json.dumps(run(),indent=2))
