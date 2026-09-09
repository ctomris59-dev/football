#!/usr/bin/env python3
"""Audit live coverage of the four new pre-match context layers."""
from __future__ import annotations
import json, os
from typing import Any, Dict, Optional
import psycopg
from psycopg.types.json import Jsonb

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
SCHEMA="""
CREATE TABLE IF NOT EXISTS four_layer_coverage_runs(
 id BIGSERIAL PRIMARY KEY,started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),finished_at TIMESTAMPTZ,
 status TEXT NOT NULL,fixtures INTEGER NOT NULL DEFAULT 0,metrics JSONB,message TEXT
);
"""

def pct(n:int,d:int)->float:
    return round(n/d,4) if d else 0.0

def run_audit(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as c:
        c.execute(SCHEMA);rid=c.execute("INSERT INTO four_layer_coverage_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        try:
            rows=c.execute("""SELECT event_id,home_team,away_team FROM espn_upcoming
              WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+INTERVAL '8 days'""").fetchall()
            total=len(rows);player=continuity=pressure=asian_goal=asian_corner=asian_any=advanced=0
            both_player=both_cont=0;books=[];total_cov=[];player_cov=[];pressure_cov=[]
            for eid,h,a in rows:
                def pctx(team):
                    return c.execute("""SELECT expected_xi_strength,retained_minutes_share,starter_continuity,player_coverage
                      FROM player_team_context_snapshots WHERE team_name=%s ORDER BY snapshot_hour DESC LIMIT 1""",(team,)).fetchone()
                hp,ap=pctx(h),pctx(a)
                hpok=bool(hp and hp[0] is not None and float(hp[3] or 0)>0)
                apok=bool(ap and ap[0] is not None and float(ap[3] or 0)>0)
                player+=int(hpok or apok);both_player+=int(hpok and apok)
                hcont=bool(hp and (hp[1] is not None or hp[2] is not None))
                acont=bool(ap and (ap[1] is not None or ap[2] is not None))
                continuity+=int(hcont or acont);both_cont+=int(hcont and acont)
                if hp:player_cov.append(float(hp[3] or 0))
                if ap:player_cov.append(float(ap[3] or 0))
                pr=c.execute("""SELECT coverage FROM fixture_pressure_snapshots WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",(eid,)).fetchone()
                if pr and float(pr[0] or 0)>0:
                    pressure+=1;pressure_cov.append(float(pr[0] or 0))
                fm=c.execute("""SELECT oddspapi_fixture_id FROM prematch_feature_snapshots
                  WHERE event_id=%s AND oddspapi_fixture_id IS NOT NULL ORDER BY snapshot_hour DESC LIMIT 1""",(eid,)).fetchone()
                if fm:
                    ar=c.execute("""SELECT goal_p_over_2_5,corner_p_over_8_5,bookmaker_count
                      FROM asian_market_fixture_snapshots WHERE fixture_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",(str(fm[0]),)).fetchone()
                    if ar:
                        asian_any+=1;asian_goal+=int(ar[0] is not None);asian_corner+=int(ar[1] is not None);books.append(int(ar[2] or 0))
                ac=c.execute("""SELECT total_coverage FROM advanced_fixture_context_v4 WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",(eid,)).fetchone()
                if ac:
                    advanced+=1;total_cov.append(float(ac[0] or 0))
            metrics={
              "fixtures":total,
              "expected_xi_any":{"n":player,"rate":pct(player,total)},
              "expected_xi_both":{"n":both_player,"rate":pct(both_player,total)},
              "continuity_any":{"n":continuity,"rate":pct(continuity,total)},
              "continuity_both":{"n":both_cont,"rate":pct(both_cont,total)},
              "pressure":{"n":pressure,"rate":pct(pressure,total)},
              "asian_any":{"n":asian_any,"rate":pct(asian_any,total)},
              "asian_goal_2_5":{"n":asian_goal,"rate":pct(asian_goal,total)},
              "asian_corner_8_5":{"n":asian_corner,"rate":pct(asian_corner,total)},
              "advanced_context":{"n":advanced,"rate":pct(advanced,total)},
              "avg_player_coverage":round(sum(player_cov)/len(player_cov),4) if player_cov else 0,
              "avg_pressure_coverage":round(sum(pressure_cov)/len(pressure_cov),4) if pressure_cov else 0,
              "avg_total_coverage":round(sum(total_cov)/len(total_cov),4) if total_cov else 0,
              "median_asian_bookmakers":(sorted(books)[len(books)//2] if books else 0),
            }
            c.execute("UPDATE four_layer_coverage_runs SET finished_at=NOW(),status='success',fixtures=%s,metrics=%s,message='live coverage audit' WHERE id=%s",(total,Jsonb(metrics),rid))
            res={"status":"success",**metrics}
            print("FOUR_LAYER_COVERAGE_RESULT",json.dumps(res,separators=(",",":")));return res
        except Exception as exc:
            c.execute("UPDATE four_layer_coverage_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:700],rid));raise
if __name__=="__main__":print(json.dumps(run_audit(),indent=2))
