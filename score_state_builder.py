#!/usr/bin/env python3
"""Create leakage-safe coarse score-state adjusted historical corner counts.

For each season, adjustment parameters come only from the previous season in the
same league. Half-time lead/draw/trail is used because Football-Data supplies HT
scores consistently; full-time red-card counts are stored as diagnostics but are
not used to adjust corners because red-card timing is unavailable.
"""
from __future__ import annotations
import json, os
from collections import defaultdict
from typing import Any, Dict, Optional
import psycopg

DATABASE_URL=os.getenv('DATABASE_URL','').strip()
SCHEMA_SQL='''
CREATE TABLE IF NOT EXISTS score_state_adjusted_matches(
 season_code TEXT NOT NULL,
 division TEXT NOT NULL,
 league_name TEXT NOT NULL,
 match_date DATE NOT NULL,
 home_team TEXT NOT NULL,
 away_team TEXT NOT NULL,
 ht_state TEXT,
 home_red INTEGER,
 away_red INTEGER,
 home_corners INTEGER,
 away_corners INTEGER,
 adjusted_home_corners DOUBLE PRECISION,
 adjusted_away_corners DOUBLE PRECISION,
 baseline_season TEXT,
 state_sample INTEGER,
 adjustment_applied BOOLEAN NOT NULL DEFAULT FALSE,
 built_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(season_code,division,match_date,home_team,away_team)
);
CREATE TABLE IF NOT EXISTS score_state_runs(
 id BIGSERIAL PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL,
 rows_built INTEGER NOT NULL DEFAULT 0,
 rows_adjusted INTEGER NOT NULL DEFAULT 0,
 message TEXT
);
'''

def state(hh,ha):
    if hh is None or ha is None: return None
    return 'lead' if hh>ha else ('trail' if hh<ha else 'draw')

def build(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db: raise RuntimeError('Missing DATABASE_URL')
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
        rid=conn.execute("INSERT INTO score_state_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        rows=conn.execute('''SELECT season_code,division,league_name,match_date,home_team,away_team,
                                    ht_home_goals,ht_away_goals,home_red,away_red,home_corners,away_corners
                             FROM football_data_matches WHERE home_corners IS NOT NULL AND away_corners IS NOT NULL
                             ORDER BY league_name,season_code,match_date''').fetchall()
        by_ls=defaultdict(list)
        for r in rows: by_ls[(r[2],r[0])].append(r)
        seasons=sorted({r[0] for r in rows})
        prev={seasons[i]:seasons[i-1] for i in range(1,len(seasons))}
        built=adjusted=0
        try:
            stats={}
            for (league,season),rs in by_ls.items():
                overall_h=sum(float(r[10]) for r in rs)/len(rs); overall_a=sum(float(r[11]) for r in rs)/len(rs)
                groups=defaultdict(list)
                for r in rs:
                    st=state(r[6],r[7])
                    if st: groups[st].append((float(r[10]),float(r[11])))
                stats[(league,season)]={'overall':(overall_h,overall_a),'groups':{
                    k:(sum(x for x,_ in v)/len(v),sum(y for _,y in v)/len(v),len(v)) for k,v in groups.items()}}
            for r in rows:
                season,div,league,dt,home,away,hh,ha,hr,ar,hc,ac=r; st=state(hh,ha)
                base=prev.get(season); ah=float(hc); aa=float(ac); n=0; applied=False
                if base and st and (league,base) in stats:
                    s=stats[(league,base)]; g=s['groups'].get(st)
                    if g and g[2]>=20:
                        oh,oa=s['overall']; sh,sa,n=g
                        ah=max(0.0,float(hc)-sh+oh); aa=max(0.0,float(ac)-sa+oa); applied=True; adjusted+=1
                conn.execute('''INSERT INTO score_state_adjusted_matches(
                    season_code,division,league_name,match_date,home_team,away_team,ht_state,home_red,away_red,
                    home_corners,away_corners,adjusted_home_corners,adjusted_away_corners,baseline_season,state_sample,adjustment_applied,built_at)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                  ON CONFLICT(season_code,division,match_date,home_team,away_team) DO UPDATE SET
                    ht_state=EXCLUDED.ht_state,home_red=EXCLUDED.home_red,away_red=EXCLUDED.away_red,
                    home_corners=EXCLUDED.home_corners,away_corners=EXCLUDED.away_corners,
                    adjusted_home_corners=EXCLUDED.adjusted_home_corners,adjusted_away_corners=EXCLUDED.adjusted_away_corners,
                    baseline_season=EXCLUDED.baseline_season,state_sample=EXCLUDED.state_sample,
                    adjustment_applied=EXCLUDED.adjustment_applied,built_at=NOW()''',
                  (season,div,league,dt,home,away,st,hr,ar,hc,ac,ah,aa,base,n,applied)); built+=1
            conn.execute("UPDATE score_state_runs SET finished_at=NOW(),status='success',rows_built=%s,rows_adjusted=%s,message='HT state; prior-season baselines' WHERE id=%s",(built,adjusted,rid))
            result={'status':'success','rows_built':built,'rows_adjusted':adjusted}
            print('SCORE_STATE_RESULT',json.dumps(result,separators=(',',':'))); return result
        except Exception as exc:
            conn.execute("UPDATE score_state_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:1000],rid)); raise

if __name__=='__main__': print(json.dumps(build(),ensure_ascii=False,indent=2))
