#!/usr/bin/env python3
"""Compare raw vs score-state-adjusted V1 corner probabilities on 2025/26."""
from __future__ import annotations
import json, os
from typing import Any, Dict, Optional
import psycopg
from model_engine_v1 import predict_match

DATABASE_URL=os.getenv('DATABASE_URL','').strip()
VERSION='score-state-corners-v1'
SCHEMA_SQL='''
CREATE TABLE IF NOT EXISTS score_state_backtest_runs(
 id BIGSERIAL PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ,
 version TEXT NOT NULL, status TEXT NOT NULL, matches INTEGER NOT NULL DEFAULT 0,
 raw_brier DOUBLE PRECISION, adjusted_brier DOUBLE PRECISION, raw_accuracy DOUBLE PRECISION,
 adjusted_accuracy DOUBLE PRECISION, raw_high_n INTEGER, raw_high_hit DOUBLE PRECISION,
 adjusted_high_n INTEGER, adjusted_high_hit DOUBLE PRECISION, use_adjusted BOOLEAN NOT NULL DEFAULT FALSE, message TEXT
);
'''

def rowdict(r,adjusted=False):
    hc=r[9] if not adjusted or r[11] is None else r[11]; ac=r[10] if not adjusted or r[12] is None else r[12]
    return {'match_date':r[0],'home_team':r[1],'away_team':r[2],'home_goals':r[3],'away_goals':r[4],
            'home_shots_on_target':r[5],'away_shots_on_target':r[6],'home_corners':hc,'away_corners':ac}

def run_backtest(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db: raise RuntimeError('Missing DATABASE_URL')
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
        rid=conn.execute("INSERT INTO score_state_backtest_runs(version,status) VALUES(%s,'running') RETURNING id",(VERSION,)).fetchone()[0]
        try:
            leagues=[r[0] for r in conn.execute("SELECT DISTINCT league_name FROM football_data_matches WHERE season_code='2526'").fetchall()]
            n=0; rb=ab=0.0; rh=ah=0; rn=an=0; racc=aacc=0
            for league in leagues:
                rows=conn.execute('''SELECT f.match_date,f.home_team,f.away_team,f.home_goals,f.away_goals,
                      f.home_shots_on_target,f.away_shots_on_target,f.over_2_5,f.btts,f.home_corners,f.away_corners,
                      s.adjusted_home_corners,s.adjusted_away_corners,f.corners_over_8_5,f.season_code
                    FROM football_data_matches f LEFT JOIN score_state_adjusted_matches s
                      ON s.season_code=f.season_code AND s.division=f.division AND s.match_date=f.match_date
                      AND s.home_team=f.home_team AND s.away_team=f.away_team
                    WHERE f.league_name=%s AND f.season_code IN ('2324','2425','2526')
                    AND f.home_goals IS NOT NULL AND f.away_goals IS NOT NULL ORDER BY f.match_date''',(league,)).fetchall()
                raw_hist=[]; adj_hist=[]
                for r in rows:
                    if r[14]=='2526':
                        if not raw_hist: continue
                        pr=predict_match(raw_hist,r[1],r[2]); pa=predict_match(adj_hist,r[1],r[2])
                        y=1 if r[13] else 0; p1=pr.p_corners_over_8_5; p2=pa.p_corners_over_8_5
                        rb+=(p1-y)**2; ab+=(p2-y)**2; n+=1
                        racc+=int((p1>=.5)==bool(y)); aacc+=int((p2>=.5)==bool(y))
                        c1=max(p1,1-p1); c2=max(p2,1-p2)
                        if c1>=.65: rn+=1; rh+=int((p1>=.5)==bool(y))
                        if c2>=.65: an+=1; ah+=int((p2>=.5)==bool(y))
                    raw_hist.append(rowdict(r,False)); adj_hist.append(rowdict(r,True))
            raw_b=rb/n if n else None; adj_b=ab/n if n else None
            use=bool(n and adj_b is not None and raw_b is not None and adj_b < raw_b)
            vals=(n,raw_b,adj_b,racc/n if n else None,aacc/n if n else None,rn,rh/rn if rn else None,an,ah/an if an else None,use)
            conn.execute('''UPDATE score_state_backtest_runs SET finished_at=NOW(),status='success',matches=%s,raw_brier=%s,
              adjusted_brier=%s,raw_accuracy=%s,adjusted_accuracy=%s,raw_high_n=%s,raw_high_hit=%s,
              adjusted_high_n=%s,adjusted_high_hit=%s,use_adjusted=%s,message='enable only if adjusted Brier improves' WHERE id=%s''',(*vals,rid))
            result={'status':'success','matches':n,'raw_brier':raw_b,'adjusted_brier':adj_b,'raw_accuracy':vals[3],
                    'adjusted_accuracy':vals[4],'raw_high_n':rn,'raw_high_hit':vals[6],'adjusted_high_n':an,
                    'adjusted_high_hit':vals[8],'use_adjusted':use}
            print('SCORE_STATE_BACKTEST_RESULT',json.dumps(result,separators=(',',':'))); return result
        except Exception as exc:
            conn.execute("UPDATE score_state_backtest_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:1000],rid)); raise

if __name__=='__main__': print(json.dumps(run_backtest(),ensure_ascii=False,indent=2))
