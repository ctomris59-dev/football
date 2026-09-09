#!/usr/bin/env python3
"""Build per-book no-vig Asian line consensus and opening-to-latest movement."""
from __future__ import annotations
import json, os, statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import psycopg
from psycopg.types.json import Jsonb

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
SCHEMA="""
CREATE TABLE IF NOT EXISTS asian_market_fixture_snapshots(
 fixture_id TEXT NOT NULL,
 snapshot_hour TIMESTAMPTZ NOT NULL,
 goal_lines JSONB NOT NULL DEFAULT '{}'::jsonb,
 corner_lines JSONB NOT NULL DEFAULT '{}'::jsonb,
 goal_p_over_2_5 DOUBLE PRECISION,
 corner_p_over_8_5 DOUBLE PRECISION,
 goal_open_to_latest_delta DOUBLE PRECISION,
 corner_open_to_latest_delta DOUBLE PRECISION,
 bookmaker_count INTEGER NOT NULL DEFAULT 0,
 built_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(fixture_id,snapshot_hour)
);
CREATE TABLE IF NOT EXISTS asian_market_feature_runs(
 id BIGSERIAL PRIMARY KEY,started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),finished_at TIMESTAMPTZ,status TEXT NOT NULL,
 fixtures INTEGER NOT NULL DEFAULT 0,lines INTEGER NOT NULL DEFAULT 0,max_bookmakers INTEGER NOT NULL DEFAULT 0,message TEXT
);
"""

def no_vig(over:float,under:float)->Optional[float]:
    if over<=1 or under<=1:return None
    a,b=1/over,1/under
    return a/(a+b) if a+b else None

def aggregate_line(rows:List[Tuple[str,str,float]])->Optional[Dict[str,Any]]:
    by={}
    for book,side,price in rows:
        by.setdefault(str(book),{})[str(side)]=float(price)
    vals=[]
    for book,p in by.items():
        if "over" in p and "under" in p:
            nv=no_vig(p["over"],p["under"])
            if nv is not None: vals.append((book,nv,p))
    if not vals:return None
    ps=[v[1] for v in vals]
    return {"p_over":statistics.median(ps),"dispersion":statistics.pstdev(ps) if len(ps)>1 else 0.0,
            "bookmakers":len(vals),"books":{b:{"p_over":p,"over":od["over"],"under":od["under"]} for b,p,od in vals}}

def run_build(database_url=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as c:
        c.execute(SCHEMA); rid=c.execute("INSERT INTO asian_market_feature_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        try:
            fixtures=[r[0] for r in c.execute("""SELECT DISTINCT fixture_id FROM asian_market_prices
              WHERE snapshot_hour>=NOW()-INTERVAL '10 days'""").fetchall()]
            nowh=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)
            nlines=maxbooks=0
            for fid in fixtures:
                latest=c.execute("SELECT MAX(snapshot_hour) FROM asian_market_prices WHERE fixture_id=%s",(fid,)).fetchone()[0]
                line_rows=c.execute("""SELECT family,line,bookmaker,side,price FROM asian_market_prices WHERE fixture_id=%s AND snapshot_hour=%s""",(fid,latest)).fetchall()
                grouped={}
                for fam,line,b,s,p in line_rows:grouped.setdefault((fam,float(line)),[]).append((b,s,float(p)))
                goals={}; corners={}; books=0
                for (fam,line),rows in grouped.items():
                    ag=aggregate_line(rows)
                    if not ag:continue
                    nlines+=1; books=max(books,int(ag["bookmakers"]))
                    (goals if fam=="goals" else corners)[str(line)]=ag
                maxbooks=max(maxbooks,books)
                def movement(fam,line):
                    snaps=c.execute("""SELECT snapshot_hour,bookmaker,side,price FROM asian_market_prices
                      WHERE fixture_id=%s AND family=%s AND line=%s ORDER BY snapshot_hour""",(fid,fam,line)).fetchall()
                    byh={}
                    for h,b,s,p in snaps:byh.setdefault(h,[]).append((b,s,float(p)))
                    vals=[(h,aggregate_line(rows)) for h,rows in sorted(byh.items())]
                    vals=[(h,x) for h,x in vals if x]
                    return (vals[-1][1]["p_over"]-vals[0][1]["p_over"]) if len(vals)>=2 else None
                gp=(goals.get("2.5") or {}).get("p_over")
                cp=(corners.get("8.5") or {}).get("p_over")
                c.execute("""INSERT INTO asian_market_fixture_snapshots(fixture_id,snapshot_hour,goal_lines,corner_lines,goal_p_over_2_5,corner_p_over_8_5,
                  goal_open_to_latest_delta,corner_open_to_latest_delta,bookmaker_count)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT(fixture_id,snapshot_hour) DO UPDATE SET goal_lines=EXCLUDED.goal_lines,corner_lines=EXCLUDED.corner_lines,
                  goal_p_over_2_5=EXCLUDED.goal_p_over_2_5,corner_p_over_8_5=EXCLUDED.corner_p_over_8_5,
                  goal_open_to_latest_delta=EXCLUDED.goal_open_to_latest_delta,corner_open_to_latest_delta=EXCLUDED.corner_open_to_latest_delta,
                  bookmaker_count=EXCLUDED.bookmaker_count,built_at=NOW()""",
                  (fid,nowh,Jsonb(goals),Jsonb(corners),gp,cp,movement("goals",2.5),movement("corners",8.5),books))
            c.execute("UPDATE asian_market_feature_runs SET finished_at=NOW(),status='success',fixtures=%s,lines=%s,max_bookmakers=%s,message='per-book no-vig' WHERE id=%s",(len(fixtures),nlines,maxbooks,rid))
            res={"status":"success","fixtures":len(fixtures),"lines":nlines,"max_bookmakers":maxbooks}
            print("ASIAN_MARKET_FEATURES_RESULT",json.dumps(res,separators=(",",":")));return res
        except Exception as exc:
            c.execute("UPDATE asian_market_feature_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:700],rid));raise

if __name__=="__main__":print(json.dumps(run_build(),indent=2))
