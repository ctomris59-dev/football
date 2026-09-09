#!/usr/bin/env python3
"""Build the DB-v3 player context interface from real player sources.

Order:
1) seed historical player cache from season_players when present;
2) reuse any real player-bearing ESPN prematch roster/lineup JSON;
3) fetch and archive current ESPN team rosters for upcoming teams;
4) fall back to legacy DB-v3 only if the ESPN roster source cannot produce players.

Historical continuity is never fabricated. Roster-only sources leave continuity NULL.
"""
from __future__ import annotations
import json, os
from typing import Any, Dict, Optional
DATABASE_URL=os.getenv("DATABASE_URL","").strip()

def run(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    from season_players_context_bridge import run_bridge as seed
    from espn_prematch_player_bridge import run_bridge as prematch
    seed_res=seed(db);prematch_res=prematch(db)
    if int(prematch_res.get("teams_written") or 0)>0 and int(prematch_res.get("expected_xi_ready") or 0)>0:
        result={"status":"success","source":"espn-prematch-db-v3","seed":seed_res,"prematch":prematch_res}
        print("PLAYER_CONTEXT_ORCHESTRATOR_RESULT",json.dumps(result,separators=(",",":")),flush=True);return result
    try:
        from espn_team_roster_player_bridge import run_bridge as roster
        roster_res=roster(db)
    except Exception as exc:
        roster_res={"status":"failed","error":str(exc)[:500]}
    if int(roster_res.get("contexts_written") or 0)>0 and int(roster_res.get("teams_with_players") or 0)>0:
        result={"status":"success","source":"espn-team-roster-db-v3","seed":seed_res,"prematch":prematch_res,"roster":roster_res}
        print("PLAYER_CONTEXT_ORCHESTRATOR_RESULT",json.dumps(result,separators=(",",":")),flush=True);return result
    from player_context_db_v3 import run_import
    db_res=run_import(db)
    result={"status":"success","source":"db-v3-fallback","seed":seed_res,"prematch":prematch_res,"roster":roster_res,"db_v3":db_res}
    print("PLAYER_CONTEXT_ORCHESTRATOR_RESULT",json.dumps(result,separators=(",",":")),flush=True);return result
if __name__=="__main__":print(json.dumps(run(),indent=2))
