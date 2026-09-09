#!/usr/bin/env python3
"""Build the DB-v3 player context interface from real player sources.

Order:
1) seed any API-Football historical player cache already present;
2) refresh/cache real 2025/26 FotMob player minutes when available;
3) resume the free ESPN 2025/26 explicit starting-XI archive;
4) fetch/archive current ESPN team rosters for upcoming teams;
5) use ESPN prematch player-bearing JSON as a secondary current source;
6) fall back to legacy DB-v3 only if the real current roster sources fail.

Historical continuity is never fabricated. Retained-minutes uses real prior-season
minutes; starter_continuity uses only explicit historical starter flags.
V5 activation remains owned by the leakage-safe backtest.
"""
from __future__ import annotations
import json, os
from typing import Any, Dict, Optional
DATABASE_URL=os.getenv("DATABASE_URL","").strip()
RUN_HISTORICAL_LINEUPS=os.getenv("PLAYER_CONTEXT_HISTORICAL_LINEUPS","true").lower() in {"1","true","yes"}

def run(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")

    from season_players_context_bridge import run_bridge as seed
    seed_res=seed(db)

    try:
        from fotmob_previous_season_players import run_import as previous_players
        previous_res=previous_players(db)
    except Exception as exc:
        previous_res={"status":"failed_optional","error":str(exc)[:500]}

    if RUN_HISTORICAL_LINEUPS:
        try:
            from espn_historical_lineups_importer import run_import as historical_lineups
            historical_res=historical_lineups(db)
        except Exception as exc:
            historical_res={"status":"failed_optional","error":str(exc)[:500]}
    else:
        historical_res={"status":"disabled"}

    # ESPN's current roster endpoint has proven reliable on Render and is the
    # preferred current roster source. Its bridge combines those current names
    # with real prior-season minutes and explicit historical starts cached above.
    try:
        from espn_team_roster_player_bridge import run_bridge as roster
        roster_res=roster(db)
    except Exception as exc:
        roster_res={"status":"failed","error":str(exc)[:500]}
    if int(roster_res.get("contexts_written") or 0)>0 and int(roster_res.get("teams_with_players") or 0)>0:
        result={"status":"success","source":"espn-team-roster-db-v3","seed":seed_res,"previous_players":previous_res,
                "historical_lineups":historical_res,"roster":roster_res}
        print("PLAYER_CONTEXT_ORCHESTRATOR_RESULT",json.dumps(result,separators=(",",":")),flush=True);return result

    # Secondary path: some ESPN prematch payloads can expose player-bearing
    # lineups/rosters. This remains conservative and fail-closed.
    from espn_prematch_player_bridge import run_bridge as prematch
    prematch_res=prematch(db)
    if int(prematch_res.get("teams_written") or 0)>0 and int(prematch_res.get("expected_xi_ready") or 0)>0:
        result={"status":"success","source":"espn-prematch-db-v3","seed":seed_res,"previous_players":previous_res,
                "historical_lineups":historical_res,"prematch":prematch_res,"roster":roster_res}
        print("PLAYER_CONTEXT_ORCHESTRATOR_RESULT",json.dumps(result,separators=(",",":")),flush=True);return result

    from player_context_db_v3 import run_import
    db_res=run_import(db)
    result={"status":"success","source":"db-v3-fallback","seed":seed_res,"previous_players":previous_res,
            "historical_lineups":historical_res,"prematch":prematch_res,"roster":roster_res,"db_v3":db_res}
    print("PLAYER_CONTEXT_ORCHESTRATOR_RESULT",json.dumps(result,separators=(",",":")),flush=True);return result
if __name__=="__main__":print(json.dumps(run(),indent=2))
