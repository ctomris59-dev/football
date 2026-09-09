#!/usr/bin/env python3
"""OddsPapi v4: production markets plus Asian total-line snapshots from same requests."""
from __future__ import annotations
import json
from typing import Any, Dict, Optional, Tuple
from psycopg.types.json import Jsonb

import oddspapi_allbooks_importer_v3 as base
from oddspapi_importer import parse_dt, to_float, to_int

SCHEMA = """
CREATE TABLE IF NOT EXISTS asian_market_prices(
 fixture_id TEXT NOT NULL,
 snapshot_hour TIMESTAMPTZ NOT NULL,
 bookmaker TEXT NOT NULL,
 market_id INTEGER NOT NULL,
 market_name TEXT,
 family TEXT NOT NULL,
 line DOUBLE PRECISION NOT NULL,
 side TEXT NOT NULL,
 price DOUBLE PRECISION NOT NULL,
 main_line BOOLEAN,
 bookmaker_changed_at TIMESTAMPTZ,
 changed_at TIMESTAMPTZ,
 raw JSONB,
 fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(fixture_id,snapshot_hour,bookmaker,market_id,side)
);
CREATE INDEX IF NOT EXISTS idx_asian_market_fixture ON asian_market_prices(fixture_id,snapshot_hour DESC,family,line);
"""

GOAL_LINES={2.25,2.5,2.75}
CORNER_LINES={8.0,8.5,9.0}

def extended_kind(name: Any, handicap: Any) -> Optional[Tuple[str,float]]:
    n=str(name or "").lower()
    try: line=round(float(handicap),2)
    except Exception:return None
    if "corner" in n and line in CORNER_LINES:return ("corners",line)
    if line in GOAL_LINES and ("total" in n or "over under" in n or "goal" in n):return ("goals",line)
    return None

def side_from_name(name: Any) -> Optional[str]:
    n=str(name or "").lower()
    if "over" in n:return "over"
    if "under" in n:return "under"
    return None

class Importer(base.MultiRequestAllBooksImporter):
    def __init__(self,database_url=None):
        super().__init__(database_url); self.conn.execute(SCHEMA)
    def store_target_prices(self,fixture,fixture_meta,selected,catalog,snapshot_hour,start,end):
        n,books=super().store_target_prices(fixture,fixture_meta,selected,catalog,snapshot_hour,start,end)
        fid=str(fixture.get("fixtureId") or "")
        tid=to_int(fixture.get("tournamentId"))
        st=parse_dt(fixture.get("startTime"))
        if not fid or tid not in selected or st is None or st<start or st>end:return n,books
        bookodds=fixture.get("bookmakerOdds") or {}
        if not isinstance(bookodds,dict):return n,books
        for bookmaker,book in bookodds.items():
            markets=(book or {}).get("markets") if isinstance(book,dict) else None
            if not isinstance(markets,dict):continue
            for mk,mdata in markets.items():
                mid=to_int(mk)
                if mid is None or not isinstance(mdata,dict):continue
                mm=catalog.get(mid,{})
                kind=extended_kind(mm.get("marketName"),mm.get("handicap"))
                if not kind:continue
                family,line=kind
                names=self.market_outcome_names(mm)
                outcomes=mdata.get("outcomes") or {}
                if not isinstance(outcomes,dict):continue
                for ok,odata in outcomes.items():
                    oid=to_int(ok)
                    oname=names.get(oid) if oid is not None else None
                    side=side_from_name(oname)
                    if not side or not isinstance(odata,dict):continue
                    players=odata.get("players") or {}
                    rows=list(players.values()) if isinstance(players,dict) else (players if isinstance(players,list) else [])
                    opts=[x for x in rows if isinstance(x,dict)]
                    if not opts:continue
                    opts.sort(key=lambda x:(bool(x.get("active")),bool(x.get("mainLine"))),reverse=True)
                    po=opts[0]; price=to_float(po.get("price"))
                    if price is None or price<=1.001:continue
                    self.conn.execute("""INSERT INTO asian_market_prices(fixture_id,snapshot_hour,bookmaker,market_id,market_name,family,line,side,price,main_line,bookmaker_changed_at,changed_at,raw)
                      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT(fixture_id,snapshot_hour,bookmaker,market_id,side) DO UPDATE SET price=EXCLUDED.price,main_line=EXCLUDED.main_line,
                      bookmaker_changed_at=EXCLUDED.bookmaker_changed_at,changed_at=EXCLUDED.changed_at,raw=EXCLUDED.raw,fetched_at=NOW()""",
                      (fid,snapshot_hour,str(bookmaker),mid,mm.get("marketName"),family,line,side,price,
                       bool(po.get("mainLine")) if po.get("mainLine") is not None else None,
                       parse_dt(po.get("bookmakerChangedAt")),parse_dt(po.get("changedAt")),Jsonb(po)))
        return n,books

def run_import(database_url=None)->Dict[str,Any]:
    x=Importer(database_url)
    try:
        res=x.run(); res["extended_asian_lines"]=True
        print("ODDSPAPI_ALLBOOKS_V4_RESULT",json.dumps(res,separators=(",",":"))); return res
    finally:x.close()

if __name__=="__main__":print(json.dumps(run_import(),ensure_ascii=False,indent=2))
