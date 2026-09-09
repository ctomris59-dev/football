#!/usr/bin/env python3
"""Populate DB-v3 player cache from the archived API-Football season_players table.

This is a provider-independent bridge: `season_players.raw.statistics[]` already stores
team, appearances, starts and minutes for the historical 2024/25 and 2025/26 seasons.
We convert those rows to the compact player cache consumed by player_context_db_v3.
No live API calls are made here.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any, Dict, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

import understat_player_continuity_v2 as v2

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
SEASON_MAP={2025:2026,2024:2025}  # collector season -> DB-v3 semantic season


def f(v:Any)->float:
    try:return float(v or 0)
    except Exception:return 0.0


def run_bridge(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(v2.SCHEMA)
        source_rows=usable_stats=written=0
        by_season=defaultdict(lambda:{"source":0,"usable":0,"written":0,"teams":set()})
        sql="""INSERT INTO understat_player_seasons(
               season,team_name,team_slug,player_id,player_name,games,starts,minutes,goals,xg,assists,xa,xgchain,xgbuildup,raw)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(season,team_slug,player_id) DO UPDATE SET
                 team_name=EXCLUDED.team_name,player_name=EXCLUDED.player_name,games=EXCLUDED.games,
                 starts=EXCLUDED.starts,minutes=EXCLUDED.minutes,goals=EXCLUDED.goals,xg=EXCLUDED.xg,
                 assists=EXCLUDED.assists,xa=EXCLUDED.xa,xgchain=EXCLUDED.xgchain,xgbuildup=EXCLUDED.xgbuildup,
                 raw=EXCLUDED.raw,fetched_at=NOW()"""
        params=[]
        for collector_season,target_season in SEASON_MAP.items():
            try:
                rows=conn.execute("SELECT player_id,player_name,raw FROM season_players WHERE season=%s",(collector_season,)).fetchall()
            except Exception:
                rows=[]
            by_season[target_season]["source"]=len(rows);source_rows+=len(rows)
            for player_id,player_name,raw in rows:
                if not isinstance(raw,dict):continue
                stats=raw.get("statistics") or []
                if not isinstance(stats,list):continue
                for st in stats:
                    if not isinstance(st,dict):continue
                    team=st.get("team") or {};team_name=team.get("name") if isinstance(team,dict) else None
                    games=st.get("games") or {};goals=st.get("goals") or {};expected=st.get("expected") or {}
                    if not team_name or not isinstance(games,dict):continue
                    appearances=f(games.get("appearences") if "appearences" in games else games.get("appearances"))
                    starts=f(games.get("lineups"));minutes=f(games.get("minutes"))
                    if appearances<=0 and starts<=0 and minutes<=0:continue
                    usable_stats+=1;by_season[target_season]["usable"]+=1;by_season[target_season]["teams"].add(str(team_name))
                    raw_proxy={"source":"season_players-db-bridge","collector_season":collector_season,"statistics":st}
                    params.append((target_season,str(team_name),v2.team_slug(str(team_name)),str(player_id),player_name,
                                   appearances,starts,minutes,f(goals.get("total")) if isinstance(goals,dict) else 0.0,
                                   f(expected.get("goals")) if isinstance(expected,dict) else 0.0,
                                   f(goals.get("assists")) if isinstance(goals,dict) else 0.0,0.0,0.0,0.0,Jsonb(raw_proxy)))
        if params:
            with conn.cursor() as cur:cur.executemany(sql,params)
            written=len(params)
            for p in params:by_season[int(p[0])]["written"]+=1
        result={"status":"success" if written>0 else "empty","source_rows":source_rows,"usable_stats":usable_stats,"written":written,
                "seasons":{str(k):{"source":v["source"],"usable":v["usable"],"written":v["written"],"teams":len(v["teams"])} for k,v in by_season.items()}}
        print("SEASON_PLAYERS_CONTEXT_BRIDGE_RESULT",json.dumps(result,separators=(",",":")),flush=True)
        return result

if __name__=="__main__":print(json.dumps(run_bridge(),indent=2))
