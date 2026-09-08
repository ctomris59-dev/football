#!/usr/bin/env python3
"""Quota-efficient all-bookmaker OddsPapi snapshots for three production markets.

One odds request returns every available bookmaker. Only O/U 2.5, BTTS and
corners O/U 8.5 are normalized, avoiding storage of unrelated markets.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from psycopg.types.json import Jsonb

from oddspapi_canonical_importer import CanonicalOddsPapiImporter
from oddspapi_importer import API_KEY, LOOKAHEAD_DAYS, fixture_objects, parse_dt, to_float, to_int, utcnow

SCHEMA_SQL="""
CREATE TABLE IF NOT EXISTS oddspapi_allbooks_runs(
 id BIGSERIAL PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL,
 api_calls INTEGER NOT NULL DEFAULT 0,
 fixtures INTEGER NOT NULL DEFAULT 0,
 bookmakers INTEGER NOT NULL DEFAULT 0,
 price_rows INTEGER NOT NULL DEFAULT 0,
 message TEXT
);
"""

def market_kind(name: Any, handicap: Any) -> Optional[str]:
    n=str(name or '').lower()
    try: line=float(handicap) if handicap is not None else None
    except Exception: line=None
    if 'both teams to score' in n: return 'btts'
    if 'corner' in n and line is not None and abs(line-8.5)<0.01: return 'corners_over_8_5'
    if line is not None and abs(line-2.5)<0.01 and ('over under' in n or 'total' in n or 'goal' in n): return 'over_2_5'
    return None

class AllBooksImporter(CanonicalOddsPapiImporter):
    def __init__(self,database_url:Optional[str]=None):
        super().__init__(database_url)
        self.conn.execute(SCHEMA_SQL)

    def store_target_prices(self,fixture:Dict[str,Any],fixture_meta:Optional[Dict[str,Any]],selected:Dict[int,Dict[str,Any]],
                            catalog:Dict[int,Dict[str,Any]],snapshot_hour:datetime,start:datetime,end:datetime):
        fixture_id=str(fixture.get('fixtureId') or '')
        if not fixture_id: return 0,set()
        start_time=parse_dt(fixture.get('startTime'))
        if start_time is None or start_time<start or start_time>end: return 0,set()
        tid=to_int(fixture.get('tournamentId'))
        if tid not in selected: return 0,set()
        books=fixture.get('bookmakerOdds') or {}
        if not isinstance(books,dict): return 0,set()
        meta=fixture_meta or {}
        league_name=selected[tid].get('_league_name')
        home=meta.get('participant1ShortName') or meta.get('participant1Name')
        away=meta.get('participant2ShortName') or meta.get('participant2Name')
        p1=to_int(meta.get('participant1Id') or fixture.get('participant1Id'))
        p2=to_int(meta.get('participant2Id') or fixture.get('participant2Id'))
        stored=0; seen=set()
        for bookmaker,book in books.items():
            if not isinstance(book,dict): continue
            markets=book.get('markets') or {}
            if not isinstance(markets,dict): continue
            target_market_ids=[]
            for mk in markets:
                mid=to_int(mk)
                if mid is None: continue
                mm=catalog.get(mid,{})
                if market_kind(mm.get('marketName'),mm.get('handicap')):
                    target_market_ids.append(mid)
            if not target_market_ids: continue
            bname=str(bookmaker)
            seen.add(bname)
            self.conn.execute('''INSERT INTO oddspapi_fixture_snapshots(
                fixture_id,snapshot_hour,tournament_id,league_name,start_time,home_team,away_team,
                participant1_id,participant2_id,bookmaker,has_odds,raw_fixture,raw_odds,fetched_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
              ON CONFLICT(fixture_id,snapshot_hour,bookmaker) DO UPDATE SET
                tournament_id=EXCLUDED.tournament_id,league_name=EXCLUDED.league_name,start_time=EXCLUDED.start_time,
                home_team=COALESCE(EXCLUDED.home_team,oddspapi_fixture_snapshots.home_team),
                away_team=COALESCE(EXCLUDED.away_team,oddspapi_fixture_snapshots.away_team),
                raw_fixture=COALESCE(EXCLUDED.raw_fixture,oddspapi_fixture_snapshots.raw_fixture),
                raw_odds=EXCLUDED.raw_odds,fetched_at=NOW()''',
              (fixture_id,snapshot_hour,tid,league_name,start_time,home,away,p1,p2,bname,True,Jsonb(meta) if meta else None,Jsonb(book)))
            for mid in target_market_ids:
                market_data=markets.get(str(mid),markets.get(mid))
                if not isinstance(market_data,dict): continue
                mm=catalog.get(mid,{})
                names=self.market_outcome_names(mm)
                outcomes=market_data.get('outcomes') or {}
                if not isinstance(outcomes,dict): continue
                for ok,odata in outcomes.items():
                    if not isinstance(odata,dict): continue
                    oid=to_int(ok)
                    if oid is None: continue
                    players=odata.get('players') or {}
                    rows=list(players.values()) if isinstance(players,dict) else (players if isinstance(players,list) else [])
                    opts=[x for x in rows if isinstance(x,dict)]
                    if not opts: continue
                    opts.sort(key=lambda x:(bool(x.get('active')),bool(x.get('mainLine'))),reverse=True)
                    po=opts[0]
                    price=to_float(po.get('price'))
                    if price is None or price<=1.001: continue
                    self.conn.execute('''INSERT INTO oddspapi_market_prices(
                       fixture_id,snapshot_hour,bookmaker,market_id,market_name,handicap,period,market_type,
                       outcome_id,outcome_name,bookmaker_outcome_id,price,active,main_line,
                       bookmaker_changed_at,changed_at,raw,fetched_at)
                     VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                     ON CONFLICT(fixture_id,snapshot_hour,bookmaker,market_id,outcome_id) DO UPDATE SET
                       market_name=EXCLUDED.market_name,handicap=EXCLUDED.handicap,outcome_name=EXCLUDED.outcome_name,
                       bookmaker_outcome_id=EXCLUDED.bookmaker_outcome_id,price=EXCLUDED.price,active=EXCLUDED.active,
                       main_line=EXCLUDED.main_line,bookmaker_changed_at=EXCLUDED.bookmaker_changed_at,
                       changed_at=EXCLUDED.changed_at,raw=EXCLUDED.raw,fetched_at=NOW()''',
                     (fixture_id,snapshot_hour,bname,mid,mm.get('marketName'),to_float(mm.get('handicap')),mm.get('period'),
                      mm.get('marketType'),oid,names.get(oid),po.get('bookmakerOutcomeId'),price,
                      bool(po.get('active')) if po.get('active') is not None else None,
                      bool(po.get('mainLine')) if po.get('mainLine') is not None else None,
                      parse_dt(po.get('bookmakerChangedAt')),parse_dt(po.get('changedAt')),Jsonb(po)))
                    stored+=1
        return stored,seen

    def run(self)->Dict[str,Any]:
        selected,catalog=self.cached_catalog()
        if len(selected)!=5 or len(catalog)<5:
            base=super().run()
            return {'status':'catalog_bootstrap','base':base}
        rid=self.conn.execute("INSERT INTO oddspapi_allbooks_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        if not API_KEY:
            self.conn.execute("UPDATE oddspapi_allbooks_runs SET finished_at=NOW(),status='not_configured',message='ODDSPAPI_API_KEY missing' WHERE id=%s",(rid,))
            return {'status':'not_configured'}
        snapshot=utcnow().replace(minute=0,second=0,microsecond=0)
        start=utcnow()-timedelta(hours=3); end=utcnow()+timedelta(days=LOOKAHEAD_DAYS)
        fixtures=0; prices=0; all_books=set()
        try:
            fixture_map=self.fetch_fixture_map(start,end,selected)
            tids=','.join(str(x) for x in sorted(selected))
            payload=self.api_get('/v4/odds-by-tournaments',{
                'tournamentIds':tids,'language':'en','verbosity':3,'oddsFormat':'decimal'
            })
            for fixture in fixture_objects(payload):
                n,books=self.store_target_prices(fixture,fixture_map.get(str(fixture.get('fixtureId') or '')),
                                                  selected,catalog,snapshot,start,end)
                if n: fixtures+=1; prices+=n; all_books.update(books)
            self.conn.execute('''UPDATE oddspapi_allbooks_runs SET finished_at=NOW(),status='success',api_calls=%s,
                                 fixtures=%s,bookmakers=%s,price_rows=%s,message=%s WHERE id=%s''',
                              (self.api_calls,fixtures,len(all_books),prices,json.dumps(sorted(all_books)),rid))
            result={'status':'success','api_calls':self.api_calls,'fixtures':fixtures,'bookmakers':len(all_books),'price_rows':prices}
            print('ODDSPAPI_ALLBOOKS_RESULT',json.dumps(result,separators=(',',':'))); return result
        except Exception as exc:
            self.conn.execute("UPDATE oddspapi_allbooks_runs SET finished_at=NOW(),status='failed',api_calls=%s,message=%s WHERE id=%s",
                              (self.api_calls,str(exc)[:1000],rid)); raise

def run_import(database_url:Optional[str]=None):
    imp=AllBooksImporter(database_url)
    try:return imp.run()
    finally:imp.close()

if __name__=='__main__': print(json.dumps(run_import(),ensure_ascii=False,indent=2))
