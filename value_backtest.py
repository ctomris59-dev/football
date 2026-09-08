#!/usr/bin/env python3
"""Exploratory Over/Under 2.5 model-vs-market value backtest.

Uses Football-Data's stored average pre-match O/U prices as the historical
market benchmark and the leakage-safe rolling xG-aware model.

Important: this is not proof of realizable profit. Historical average prices may
not equal the exact price available at the user's decision timestamp. Results
are diagnostic for whether model probabilities add information beyond market
prices.
"""
from __future__ import annotations
import json, logging, os
from collections import defaultdict
from typing import Any, Dict, Optional
import psycopg
from psycopg.types.json import Jsonb
from backtest_model import load_matches, BACKTEST_TRAIN_SEASON, BACKTEST_TEST_SEASON
from model_engine import predict_match
DATABASE_URL=os.getenv("DATABASE_URL","").strip(); VERSION="ou25-market-edge-v1"; EDGES=(0.00,0.02,0.04,0.06,0.08,0.10)
logging.basicConfig(level=logging.INFO,format="%(asctime)s | %(levelname)s | %(message)s");log=logging.getLogger("value-backtest")
SCHEMA="""CREATE TABLE IF NOT EXISTS model_value_backtest_runs(id BIGSERIAL PRIMARY KEY,started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),finished_at TIMESTAMPTZ,version TEXT NOT NULL,train_season TEXT NOT NULL,test_season TEXT NOT NULL,matches_scored INTEGER NOT NULL DEFAULT 0,odds_matches INTEGER NOT NULL DEFAULT 0,metrics JSONB,status TEXT NOT NULL,message TEXT);"""
def f(v):
    try:
        x=float(v);return x if x>1.0 else None
    except (TypeError,ValueError):return None
def market_probs(oo,ou):
    oo=f(oo);ou=f(ou)
    if not oo or not ou:return None
    qo,qu=1/oo,1/ou;z=qo+qu
    return (qo/z,qu/z,oo,ou,z-1) if z>0 else None
def run_backtest(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(SCHEMA);rid=conn.execute("INSERT INTO model_value_backtest_runs(version,train_season,test_season,status) VALUES(%s,%s,%s,'running') RETURNING id",(VERSION,BACKTEST_TRAIN_SEASON,BACKTEST_TEST_SEASON)).fetchone()[0]
        try:
            rows=load_matches(conn);bydiv=defaultdict(list)
            for m in rows:bydiv[str(m["division"])].append(m)
            picks={e:[] for e in EDGES};scored=odds_matches=0;calibration=[]
            for div,ms in bydiv.items():
                hist=[m for m in ms if m["season_code"]==BACKTEST_TRAIN_SEASON];tests=[m for m in ms if m["season_code"]==BACKTEST_TEST_SEASON];hist.sort(key=lambda m:(m["match_date"],m["home_team"],m["away_team"]));tests.sort(key=lambda m:(m["match_date"],m["home_team"],m["away_team"]))
                for m in tests:
                    pred=predict_match(hist,m["home_team"],m["away_team"]);scored+=1;mp=market_probs(m.get("odds_over_2_5"),m.get("odds_under_2_5"))
                    if mp:
                        mo,mu,oo,ou,margin=mp;odds_matches+=1;p=pred.p_over_2_5;calibration.append({"model":p,"market":mo,"outcome":bool(m["over_2_5"])})
                        edge_over=p-mo;edge_under=(1-p)-mu;side="over" if edge_over>=edge_under else "under";edge=max(edge_over,edge_under);price=oo if side=="over" else ou;won=bool(m["over_2_5"]) if side=="over" else not bool(m["over_2_5"])
                        for gate in EDGES:
                            if edge>=gate:picks[gate].append({"won":won,"price":price,"edge":edge,"side":side,"margin":margin,"model_p":p,"market_p":mo,"division":div})
                    hist.append(m)
            metrics={}
            for gate,arr in picks.items():
                n=len(arr);wins=sum(x["won"] for x in arr);pnl=sum((x["price"]-1) if x["won"] else -1 for x in arr);over_n=sum(x["side"]=="over" for x in arr)
                metrics[f"edge_{gate:.2f}"]={"n":n,"wins":wins,"hit_rate":round(wins/n,4) if n else None,"flat_stake_profit_units":round(pnl,2),"roi":round(pnl/n,4) if n else None,"avg_model_edge":round(sum(x["edge"] for x in arr)/n,4) if n else None,"avg_odds":round(sum(x["price"] for x in arr)/n,3) if n else None,"over_share":round(over_n/n,4) if n else None}
            if calibration:
                model_brier=sum((x["model"]-int(x["outcome"]))**2 for x in calibration)/len(calibration);market_brier=sum((x["market"]-int(x["outcome"]))**2 for x in calibration)/len(calibration)
            else:model_brier=market_brier=None
            result={"version":VERSION,"matches_scored":scored,"odds_matches":odds_matches,"model_brier":round(model_brier,5) if model_brier is not None else None,"market_brier":round(market_brier,5) if market_brier is not None else None,"gates":metrics,"warning":"Diagnostic only; historical average pre-match odds are not guaranteed execution prices."}
            conn.execute("UPDATE model_value_backtest_runs SET finished_at=NOW(),matches_scored=%s,odds_matches=%s,metrics=%s,status='success',message='ok' WHERE id=%s",(scored,odds_matches,Jsonb(result),rid));log.info("VALUE_BACKTEST_RESULT %s",json.dumps(result,ensure_ascii=False,separators=(",",":")));return result
        except Exception as exc:
            conn.execute("UPDATE model_value_backtest_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc),rid));raise
if __name__=="__main__":print(json.dumps(run_backtest(),ensure_ascii=False,indent=2))
