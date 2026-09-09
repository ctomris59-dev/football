#!/usr/bin/env python3
"""Build the DB-v3 player context interface from the strongest persisted source.

Order:
1) seed historical player cache from season_players when present;
2) build current Expected-XI context from persisted ESPN prematch rosters/lineups;
3) if ESPN yields no current teams, fall back to the legacy DB-v3 builder.

A successful result requires real current player context rows. Historical continuity is
never fabricated; sources that cannot support it leave those fields NULL.
"""
from __future__ import annotations
import json, os
from typing import Any, Dict, Optional
import psycopg

DATABASE_URL=os.getenv("DATABASE_URL","").strip()


def run(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    from season_players_context_bridge import run_bridge as seed
    from espn_prematch_player_bridge import run_bridge as espn
    seed_res=seed(db)
    espn_res=espn(db)
    if int(espn_res.get("teams_written") or 0)>0 and int(espn_res.get("expected_xi_ready") or 0)>0:
        result={"status":"success","source":"espn-prematch-db-v3","seed":seed_res,"espn":espn_res}
        print("PLAYER_CONTEXT_ORCHESTRATOR_RESULT",json.dumps(result,separators=(",",":")),flush=True)
        return result
    from player_context_db_v3 import run_import
    db_res=run_import(db)
    result={"status":"success","source":"db-v3-fallback","seed":seed_res,"espn":espn_res,"db_v3":db_res}
    print("PLAYER_CONTEXT_ORCHESTRATOR_RESULT",json.dumps(result,separators=(",",":")),flush=True)
    return result

if __name__=="__main__":print(json.dumps(run(),indent=2))
