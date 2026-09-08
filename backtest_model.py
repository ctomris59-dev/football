#!/usr/bin/env python3
"""Leakage-safe rolling backtest for xG-aware Big Five model v2."""
from __future__ import annotations
import json, logging, math, os, re, unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple
import psycopg
from psycopg.types.json import Jsonb
from model_engine import Prediction, best_market, predict_match
DATABASE_URL=os.getenv("DATABASE_URL","").strip(); BACKTEST_TRAIN_SEASON=os.getenv("BACKTEST_TRAIN_SEASON","2425"); BACKTEST_TEST_SEASON=os.getenv("BACKTEST_TEST_SEASON","2526"); LOG_LEVEL=os.getenv("LOG_LEVEL","INFO").upper()
logging.basicConfig(level=getattr(logging,LOG_LEVEL,logging.INFO),format="%(asctime)s | %(levelname)s | %(message)s"); log=logging.getLogger("football-backtest")
SCHEMA_SQL="""CREATE TABLE IF NOT EXISTS model_backtest_runs(id BIGSERIAL PRIMARY KEY,started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),finished_at TIMESTAMPTZ,model_version TEXT NOT NULL,train_season TEXT NOT NULL,test_season TEXT NOT NULL,matches_scored INTEGER NOT NULL DEFAULT 0,metrics JSONB,market_metrics JSONB,top10_metrics JSONB,status TEXT NOT NULL,message TEXT);"""
MODEL_VERSION="poisson-form-xg-v2"; MARKETS=("over_2_5","btts","corners_over_8_5")
ALIASES={
 "man united":"manchester united","man city":"manchester city","nottm forest":"nottingham forest","wolves":"wolverhampton wanderers","newcastle":"newcastle united",
 "ath bilbao":"athletic club","betis":"real betis","sociedad":"real sociedad","valladolid":"real valladolid","vallecano":"rayo vallecano","celta":"celta vigo","espanol":"espanyol",
 "milan":"ac milan","verona":"hellas verona",
 "dortmund":"borussia dortmund","mgladbach":"borussia m gladbach","leverkusen":"bayer leverkusen","frankfurt":"eintracht frankfurt",
 "paris sg":"paris saint germain","st etienne":"saint etienne"
}
def canon(s:Any)->str:
    s=unicodedata.normalize("NFKD",str(s or "")).encode("ascii","ignore").decode().lower().replace("'","")
    s=re.sub(r"[^a-z0-9]+"," ",s).strip(); return ALIASES.get(s,s)
def _safe_log(x:float)->float:return math.log(min(1-1e-12,max(1e-12,x)))
def _metrics(items:Sequence[Tuple[float,int]])->Dict[str,Any]:
    if not items:return {"n":0}
    n=len(items); brier=sum((p-y)**2 for p,y in items)/n; ll=-sum(y*_safe_log(p)+(1-y)*_safe_log(1-p) for p,y in items)/n; acc=sum((p>=.5)==bool(y) for p,y in items)/n
    r={"n":n,"brier":round(brier,5),"logloss":round(ll,5),"accuracy_0_50":round(acc,4),"base_rate":round(sum(y for _,y in items)/n,4)}
    for t in (.55,.60,.65,.70):
        sel=[(p,y) for p,y in items if max(p,1-p)>=t]; r[f"confidence_{t:.2f}"]={"n":len(sel),"coverage":round(len(sel)/n,4),"hit_rate":round(sum((p>=.5)==bool(y) for p,y in sel)/len(sel),4) if sel else None}
    return r
def _outcome(m:Dict[str,Any],market:str)->Optional[int]:
    v=m.get(market); return None if v is None else int(bool(v))
def _prob(p:Prediction,m:str)->float:
    return p.p_over_2_5 if m=="over_2_5" else p.p_btts if m=="btts" else p.p_corners_over_8_5
def load_matches(conn)->List[Dict[str,Any]]:
    cur=conn.execute("""SELECT season_code,division,league_name,match_date,home_team,away_team,home_goals,away_goals,home_shots,away_shots,home_shots_on_target,away_shots_on_target,home_corners,away_corners,total_corners,over_2_5,btts,corners_over_8_5,odds_over_2_5,odds_under_2_5 FROM football_data_matches WHERE season_code IN (%s,%s) AND home_goals IS NOT NULL AND away_goals IS NOT NULL ORDER BY division,match_date,home_team,away_team""",(BACKTEST_TRAIN_SEASON,BACKTEST_TEST_SEASON)); cols=[d.name for d in cur.description]; rows=[dict(zip(cols,r)) for r in cur.fetchall()]
    uc=conn.execute("""SELECT league_name,season,match_date::date,home_team,away_team,home_xg,away_xg FROM understat_matches WHERE season IN (2024,2025) AND is_result=TRUE AND home_xg IS NOT NULL AND away_xg IS NOT NULL"""); urows=uc.fetchall(); by_key=defaultdict(list)
    for league,season,dt,home,away,hxg,axg in urows: by_key[(str(league),int(season),dt)].append((home,away,float(hxg),float(axg)))
    matched=0
    for m in rows:
        season=2024 if m["season_code"]=="2425" else 2025; cands=by_key.get((str(m["league_name"]),season,m["match_date"]),[]); best=None; bestscore=0.0
        ch,ca=canon(m["home_team"]),canon(m["away_team"])
        for uh,ua,hxg,axg in cands:
            sh=SequenceMatcher(None,ch,canon(uh)).ratio(); sa=SequenceMatcher(None,ca,canon(ua)).ratio(); score=sh+sa
            if min(sh,sa)>=.55 and score>bestscore: best=(hxg,axg); bestscore=score
        if best is not None and bestscore>=1.35: m["home_xg"],m["away_xg"]=best; matched+=1
        else: m["home_xg"]=m["away_xg"]=None
    log.info("XG_JOIN_COVERAGE matched=%s total=%s rate=%.4f",matched,len(rows),matched/max(1,len(rows))); return rows

def run_backtest(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(SCHEMA_SQL); rid=conn.execute("INSERT INTO model_backtest_runs(model_version,train_season,test_season,status) VALUES(%s,%s,%s,'running') RETURNING id",(MODEL_VERSION,BACKTEST_TRAIN_SEASON,BACKTEST_TEST_SEASON)).fetchone()[0]
        try:
            allm=load_matches(conn); by_div=defaultdict(list)
            for m in allm:by_div[str(m["division"])].append(m)
            per={m:[] for m in MARKETS}; weekly=defaultdict(list); scored=xg_used=0
            for division,matches in by_div.items():
                history=[m for m in matches if m["season_code"]==BACKTEST_TRAIN_SEASON]; tests=[m for m in matches if m["season_code"]==BACKTEST_TEST_SEASON]; history.sort(key=lambda m:(m["match_date"],m["home_team"],m["away_team"])); tests.sort(key=lambda m:(m["match_date"],m["home_team"],m["away_team"]))
                for match in tests:
                    pred=predict_match(history,match["home_team"],match["away_team"]); scored+=1; xg_used+=int(pred.xg_used)
                    for market in MARKETS:
                        y=_outcome(match,market)
                        if y is not None:per[market].append((_prob(pred,market),y))
                    bm=best_market(pred); yb=_outcome(match,bm["market"])
                    if yb is not None:
                        y,w,_=match["match_date"].isocalendar(); weekly[f"{y}-W{w:02d}"].append({"division":division,"match_date":str(match["match_date"]),"home_team":match["home_team"],"away_team":match["away_team"],"market":bm["market"],"selection_yes":bm["selection_yes"],"confidence":float(bm["probability"]),"data_quality":float(bm["data_quality"]),"xg_used":bool(pred.xg_used),"correct":bool(yb)==bool(bm["selection_yes"])})
                    history.append(match)
            mm={m:_metrics(v) for m,v in per.items()}; overall=_metrics([x for m in MARKETS for x in per[m]]); picks=[]; weeks={}
            for week,cands in sorted(weekly.items()):
                ranked=sorted(cands,key=lambda x:x["confidence"]*(.75+.25*x["data_quality"]),reverse=True)[:10]; hits=sum(x["correct"] for x in ranked); weeks[week]={"n":len(ranked),"hits":hits,"hit_rate":round(hits/len(ranked),4) if ranked else None,"avg_confidence":round(sum(x["confidence"] for x in ranked)/len(ranked),4) if ranked else None}; picks.extend(ranked)
            hits=sum(x["correct"] for x in picks); top={"weeks":len(weeks),"picks":len(picks),"hits":hits,"hit_rate":round(hits/len(picks),4) if picks else None,"avg_confidence":round(sum(x["confidence"] for x in picks)/len(picks),4) if picks else None,"xg_used_rate":round(xg_used/max(1,scored),4),"by_week":weeks}
            result={"model_version":MODEL_VERSION,"train_season":BACKTEST_TRAIN_SEASON,"test_season":BACKTEST_TEST_SEASON,"matches_scored":scored,"xg_used_matches":xg_used,"overall":overall,"markets":mm,"top10":top}
            conn.execute("UPDATE model_backtest_runs SET finished_at=NOW(),matches_scored=%s,metrics=%s,market_metrics=%s,top10_metrics=%s,status='success',message='ok' WHERE id=%s",(scored,Jsonb({**overall,"xg_used_matches":xg_used}),Jsonb(mm),Jsonb(top),rid)); log.info("BACKTEST_RESULT %s",json.dumps(result,ensure_ascii=False,separators=(",",":"))); return result
        except Exception as exc:
            conn.execute("UPDATE model_backtest_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc),rid)); raise
if __name__=="__main__":print(json.dumps(run_backtest(),ensure_ascii=False,indent=2))
