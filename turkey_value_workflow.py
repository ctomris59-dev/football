#!/usr/bin/env python3
"""Turkey-first betting workflow.

Keeps two separate outputs for every production run:
1) HIGH_CONFIDENCE: model probability only.
2) HIGH_CONFIDENCE_VALUE: high confidence plus a validated Turkish price.

Turkish prices are intentionally fail-closed: a VALUE label is impossible unless a
real TR price has been captured. Opening prices are immutable once first seen.
"""
from __future__ import annotations
import json, os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import psycopg

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
HIGH_CONFIDENCE_MIN=float(os.getenv("HIGH_CONFIDENCE_MIN","0.70"))
VALUE_MIN_CONFIDENCE=float(os.getenv("VALUE_MIN_CONFIDENCE","0.65"))
VALUE_MIN_EDGE=float(os.getenv("VALUE_MIN_EDGE","0.015"))
VALUE_MIN_EV=float(os.getenv("VALUE_MIN_EV","0.02"))
TR_PRICE_MIN=float(os.getenv("TR_PRICE_MIN","1.01"))
TR_PRICE_MAX=float(os.getenv("TR_PRICE_MAX","5.00"))

DDL="""
CREATE TABLE IF NOT EXISTS turkey_odds_snapshots(
 id BIGSERIAL PRIMARY KEY,event_id TEXT NOT NULL,market TEXT NOT NULL,selection TEXT NOT NULL,
 source TEXT NOT NULL,price NUMERIC NOT NULL,fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 UNIQUE(event_id,market,selection,source,fetched_at));
CREATE TABLE IF NOT EXISTS turkey_opening_odds(
 event_id TEXT NOT NULL,market TEXT NOT NULL,selection TEXT NOT NULL,source TEXT NOT NULL,
 opening_price NUMERIC NOT NULL,first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(event_id,market,selection,source));
"""

def valid_price(price:Any)->bool:
 try:return TR_PRICE_MIN<=float(price)<=TR_PRICE_MAX
 except (TypeError,ValueError):return False

def store_price(conn,event_id:str,market:str,selection:str,source:str,price:float,at:Optional[datetime]=None)->bool:
 """Store a TR snapshot and freeze the first valid price as opening_price."""
 if not valid_price(price):return False
 at=at or datetime.now(timezone.utc);source=source.strip().lower()
 conn.execute("INSERT INTO turkey_odds_snapshots(event_id,market,selection,source,price,fetched_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",(event_id,market,selection,source,float(price),at))
 conn.execute("INSERT INTO turkey_opening_odds(event_id,market,selection,source,opening_price,first_seen_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id,market,selection,source) DO NOTHING",(event_id,market,selection,source,float(price),at))
 return True

def latest_tr_price(conn,event_id,market,selection):
 row=conn.execute("""SELECT source,price,fetched_at FROM turkey_odds_snapshots
 WHERE event_id=%s AND market=%s AND selection=%s AND price BETWEEN %s AND %s
 ORDER BY fetched_at DESC LIMIT 1""",(event_id,market,selection,TR_PRICE_MIN,TR_PRICE_MAX)).fetchone()
 return row

def build_lists(db:str=DATABASE_URL,run_id:Optional[int]=None,limit:int=10)->Dict[str,Any]:
 if not db:raise RuntimeError("Missing DATABASE_URL")
 with psycopg.connect(db,autocommit=True) as c:
  c.execute(DDL)
  if run_id is None:
   r=c.execute("SELECT MAX(run_id) FROM production_predictions").fetchone();run_id=int(r[0]) if r and r[0] else None
  if not run_id:return {"run_id":None,"high_confidence":[],"high_confidence_value":[]}
  rows=c.execute("""SELECT event_id,match_date,league_name,home_team,away_team,market,selection,
   model_probability,ranking_score,final_context_ready FROM production_predictions
   WHERE run_id=%s AND provisional_ready=TRUE ORDER BY model_probability DESC,ranking_score DESC""",(run_id,)).fetchall()
  high=[];value=[];seen_high=set();seen_value=set()
  for eid,dt,league,home,away,market,sel,p,rank,final in rows:
   p=float(p);base={"event_id":eid,"match_date":dt,"league":league,"home":home,"away":away,"market":market,"selection":sel,"confidence":p,"ranking":float(rank),"final":bool(final)}
   if p>=HIGH_CONFIDENCE_MIN and eid not in seen_high:
    high.append(base);seen_high.add(eid)
   tr=latest_tr_price(c,eid,market,sel)
   if p>=VALUE_MIN_CONFIDENCE and tr and eid not in seen_value:
    source,price,fetched=tr;price=float(price);market_p=1.0/price;edge=p-market_p;ev=p*price-1.0
    if edge>=VALUE_MIN_EDGE and ev>=VALUE_MIN_EV:
     item=dict(base);item.update({"tr_source":source,"tr_price":price,"tr_price_at":fetched,"market_implied_probability":market_p,"edge":edge,"ev":ev});value.append(item);seen_value.add(eid)
  high=sorted(high,key=lambda x:(x["confidence"],x["ranking"]),reverse=True)[:limit]
  value=sorted(value,key=lambda x:(x["confidence"],x["ev"],x["edge"],x["ranking"]),reverse=True)[:limit]
  return {"run_id":run_id,"generated_at":datetime.now(timezone.utc),"policy":{"high_confidence_min":HIGH_CONFIDENCE_MIN,"value_min_confidence":VALUE_MIN_CONFIDENCE,"value_min_edge":VALUE_MIN_EDGE,"value_min_ev":VALUE_MIN_EV,"turkey_price_required":True},"high_confidence":high,"high_confidence_value":value}

if __name__=="__main__":
 print(json.dumps(build_lists(),ensure_ascii=False,indent=2,default=str))
