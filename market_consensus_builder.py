#!/usr/bin/env python3
"""Build per-bookmaker no-vig consensus and sharp-market references.

Never combines opposite sides from different bookmakers when removing margin.
"""
from __future__ import annotations

import json
import os
import re
import statistics
from collections import defaultdict
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

from oddspapi_allbooks_importer import market_kind

DATABASE_URL=os.getenv('DATABASE_URL','').strip()
SHARP_PRIORITY=[x.strip().lower() for x in os.getenv('SHARP_BOOKMAKERS','pinnacle,betfair_ex_eu,betfair').split(',') if x.strip()]

SCHEMA_SQL='''
CREATE TABLE IF NOT EXISTS market_consensus_snapshots(
 fixture_id TEXT NOT NULL,
 snapshot_hour TIMESTAMPTZ NOT NULL,
 market TEXT NOT NULL,
 bookmaker_count INTEGER NOT NULL,
 consensus_p_yes DOUBLE PRECISION NOT NULL,
 mean_p_yes DOUBLE PRECISION NOT NULL,
 dispersion DOUBLE PRECISION,
 sharp_bookmaker TEXT,
 sharp_p_yes DOUBLE PRECISION,
 best_price_yes DOUBLE PRECISION,
 best_price_no DOUBLE PRECISION,
 median_price_yes DOUBLE PRECISION,
 median_price_no DOUBLE PRECISION,
 raw_bookmakers JSONB NOT NULL,
 built_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(fixture_id,snapshot_hour,market)
);
CREATE INDEX IF NOT EXISTS idx_market_consensus_latest ON market_consensus_snapshots(fixture_id,market,snapshot_hour DESC);
CREATE TABLE IF NOT EXISTS market_consensus_runs(
 id BIGSERIAL PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL,
 fixtures INTEGER NOT NULL DEFAULT 0,
 market_rows INTEGER NOT NULL DEFAULT 0,
 median_bookmakers DOUBLE PRECISION,
 sharp_rows INTEGER NOT NULL DEFAULT 0,
 message TEXT
);
'''

def outcome_side(market:str,outcome:Any)->Optional[bool]:
    s=str(outcome or '').lower().strip()
    if market in ('over_2_5','corners_over_8_5'):
        if 'over' in s: return True
        if 'under' in s: return False
    elif market=='btts':
        if re.search(r'\byes\b',s) or s in {'1','true'}: return True
        if re.search(r'\bno\b',s) or s in {'0','false'}: return False
    return None

def no_vig(yes:float,no:float)->Optional[float]:
    if yes<=1.001 or no<=1.001: return None
    a,b=1/yes,1/no; t=a+b
    return a/t if t>0 else None

def build(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db: raise RuntimeError('Missing DATABASE_URL')
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
        rid=conn.execute("INSERT INTO market_consensus_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        fixtures=set(); markets=sharp_rows=0; counts=[]
        try:
            fixture_hours=conn.execute('''SELECT fixture_id,MAX(snapshot_hour) FROM oddspapi_market_prices
                                          WHERE fetched_at>=NOW()-INTERVAL '8 days' GROUP BY fixture_id''').fetchall()
            for fixture_id,hour in fixture_hours:
                rows=conn.execute('''SELECT bookmaker,market_name,handicap,outcome_name,price,active,main_line
                                     FROM oddspapi_market_prices WHERE fixture_id=%s AND snapshot_hour=%s
                                     AND price IS NOT NULL AND price>1.001 AND COALESCE(active,TRUE)=TRUE''',(fixture_id,hour)).fetchall()
                by=defaultdict(lambda:defaultdict(dict))
                for book,name,line,outcome,price,active,main in rows:
                    market=market_kind(name,line)
                    if not market: continue
                    side=outcome_side(market,outcome)
                    if side is None: continue
                    p=float(price)
                    cur=by[market][str(book).lower()].get(side)
                    if cur is None or p>cur: by[market][str(book).lower()][side]=p
                for market,books in by.items():
                    valid=[]
                    for book,sides in books.items():
                        if True not in sides or False not in sides: continue
                        pv=no_vig(sides[True],sides[False])
                        if pv is None: continue
                        valid.append({'bookmaker':book,'p_yes':pv,'price_yes':sides[True],'price_no':sides[False]})
                    if not valid: continue
                    probs=[x['p_yes'] for x in valid]; py=statistics.median(probs); mean=statistics.fmean(probs)
                    dispersion=statistics.pstdev(probs) if len(probs)>1 else 0.0
                    sharp=None
                    for want in SHARP_PRIORITY:
                        sharp=next((x for x in valid if x['bookmaker']==want),None)
                        if sharp: break
                    yes_prices=[x['price_yes'] for x in valid]; no_prices=[x['price_no'] for x in valid]
                    conn.execute('''INSERT INTO market_consensus_snapshots(
                         fixture_id,snapshot_hour,market,bookmaker_count,consensus_p_yes,mean_p_yes,dispersion,
                         sharp_bookmaker,sharp_p_yes,best_price_yes,best_price_no,median_price_yes,median_price_no,raw_bookmakers,built_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT(fixture_id,snapshot_hour,market) DO UPDATE SET bookmaker_count=EXCLUDED.bookmaker_count,
                         consensus_p_yes=EXCLUDED.consensus_p_yes,mean_p_yes=EXCLUDED.mean_p_yes,dispersion=EXCLUDED.dispersion,
                         sharp_bookmaker=EXCLUDED.sharp_bookmaker,sharp_p_yes=EXCLUDED.sharp_p_yes,
                         best_price_yes=EXCLUDED.best_price_yes,best_price_no=EXCLUDED.best_price_no,
                         median_price_yes=EXCLUDED.median_price_yes,median_price_no=EXCLUDED.median_price_no,
                         raw_bookmakers=EXCLUDED.raw_bookmakers,built_at=NOW()''',
                      (fixture_id,hour,market,len(valid),py,mean,dispersion,
                       sharp['bookmaker'] if sharp else None,sharp['p_yes'] if sharp else None,max(yes_prices),max(no_prices),
                       statistics.median(yes_prices),statistics.median(no_prices),Jsonb(valid)))
                    markets+=1; counts.append(len(valid)); fixtures.add(str(fixture_id)); sharp_rows+=int(sharp is not None)
            med=statistics.median(counts) if counts else 0.0
            conn.execute('''UPDATE market_consensus_runs SET finished_at=NOW(),status='success',fixtures=%s,market_rows=%s,
                            median_bookmakers=%s,sharp_rows=%s,message='per-book no-vig; no cross-book margin removal' WHERE id=%s''',
                         (len(fixtures),markets,med,sharp_rows,rid))
            result={'status':'success','fixtures':len(fixtures),'market_rows':markets,'median_bookmakers':med,'sharp_rows':sharp_rows}
            print('MARKET_CONSENSUS_RESULT',json.dumps(result,separators=(',',':'))); return result
        except Exception as exc:
            conn.execute("UPDATE market_consensus_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:1000],rid)); raise

if __name__=='__main__': print(json.dumps(build(),ensure_ascii=False,indent=2))
