#!/usr/bin/env python3
"""Leakage-safe exploratory hybrid policy backtest.

Uses the xG-aware model for Over 2.5, the non-xG fallback for BTTS, and the
unchanged corner model. Fixed gates:
- corners confidence >= 0.60
- Over 2.5 confidence >= 0.60 (xG-aware)
- BTTS confidence >= 0.65 (non-xG fallback)

Each match contributes at most one selection. Ten highest policy scores per ISO
week are selected. This is an exploratory policy check on 2025/26, not an
independent profitability test.
"""
from __future__ import annotations
import json, logging, os, re, unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
import psycopg
from psycopg.types.json import Jsonb
from model_engine import predict_match
DATABASE_URL=os.getenv("DATABASE_URL","").strip(); TRAIN=os.getenv("BACKTEST_TRAIN_SEASON","2425"); TEST=os.getenv("BACKTEST_TEST_SEASON","2526"); MODEL_VERSION="hybrid-market-policy-v1"; LOG_LEVEL=os.getenv("LOG_LEVEL","INFO").upper()
logging.basicConfig(level=getattr(logging,LOG_LEVEL,logging.INFO),format="%(asctime)s | %(levelname)s | %(message)s"); log=logging.getLogger("hybrid-policy")
ALIASES={"man united":"manchester united","man city":"manchester city","nottm forest":"nottingham forest","wolves":"wolverhampton wanderers","newcastle":"newcastle united","ath bilbao":"athletic club","betis":"real betis","sociedad":"real sociedad","valladolid":"real valladolid","vallecano":"rayo vallecano","celta":"celta vigo","espanol":"espanyol","milan":"ac milan","verona":"hellas verona","dortmund":"borussia dortmund","mgladbach":"borussia m gladbach","leverkusen":"bayer leverkusen","frankfurt":"eintracht frankfurt","paris sg":"paris saint germain","st etienne":"saint etienne"}
SCHEMA="""CREATE TABLE IF NOT EXISTS model_policy_backtest_runs(id BIGSERIAL PRIMARY KEY,started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),finished_at TIMESTAMPTZ,policy_version TEXT NOT NULL,train_season TEXT NOT NULL,test_season TEXT NOT NULL,matches_scored INTEGER NOT NULL DEFAULT 0,candidate_picks INTEGER NOT NULL DEFAULT 0,metrics JSONB,status TEXT NOT NULL,message TEXT);"""
def canon(s:Any)->str:
    s=unicodedata.normalize("NFKD",str(s or "")).encode("ascii","ignore").decode().lower().replace("'",""); s=re.sub(r"[^a-z0-9]+"," ",s).strip(); return ALIASES.get(s,s)
def load(conn)->List[Dict[str,Any]]:
    cur=conn.execute("""SELECT season_code,division,league_name,match_date,home_team,away_team,home_goals,away_goals,home_shots,away_shots,home_shots_on_target,away_shots_on_target,home_corners,away_corners,total_corners,over_2_5,btts,corners_over_8_5 FROM football_data_matches WHERE season_code IN (%s,%s) AND home_goals IS NOT NULL AND away_goals IS NOT NULL ORDER BY division,match_date,home_team,away_team""",(TRAIN,TEST)); cols=[d.name for d in cur.description]; rows=[dict(zip(cols,r)) for r in cur.fetchall()]
    urows=conn.execute("""SELECT league_name,season,match_date::date,home_team,away_team,home_xg,away_xg FROM understat_matches WHERE season IN (2024,2025) AND is_result=TRUE AND home_xg IS NOT NULL AND away_xg IS NOT NULL""").fetchall(); by_key=defaultdict(list)
    for league,season,dt,h,a,hxg,axg in urows:by_key[(str(league),int(season),dt)].append((h,a,float(hxg),float(axg)))
    matched=0
    for m in rows:
        season=2024 if m["season_code"]=="2425" else 2025; cands=by_key.get((str(m["league_name"]),season,m["match_date"]),[]); ch,ca=canon(m["home_team"]),canon(m["away_team"]); best=None; score_best=0.0
        for uh,ua,hxg,axg in cands:
            sh=SequenceMatcher(None,ch,canon(uh)).ratio(); sa=SequenceMatcher(None,ca,canon(ua)).ratio(); score=sh+sa
            if min(sh,sa)>=.55 and score>score_best:best=(hxg,axg);score_best=score
        if best is not None and score_best>=1.35:m["home_xg"],m["away_xg"]=best;matched+=1
        else:m["home_xg"]=m["away_xg"]=None
    log.info("HYBRID_XG_JOIN matched=%s/%s",matched,len(rows));return rows
def no_xg(m:Dict[str,Any])->Dict[str,Any]:c=dict(m);c["home_xg"]=None;c["away_xg"]=None;return c
def conf_yes(p:float)->Tuple[float,bool]:return (p if p>=.5 else 1-p,p>=.5)
def choose(px,pb)->Optional[Dict[str,Any]]:
    choices=[];cconf,cyes=conf_yes(px.p_corners_over_8_5)
    if cconf>=.60:choices.append({"market":"corners_over_8_5","selection_yes":cyes,"confidence":cconf,"score":cconf+.020})
    gconf,gyes=conf_yes(px.p_over_2_5)
    if gconf>=.60:choices.append({"market":"over_2_5","selection_yes":gyes,"confidence":gconf,"score":gconf+.010})
    bconf,byes=conf_yes(pb.p_btts)
    if bconf>=.65:choices.append({"market":"btts","selection_yes":byes,"confidence":bconf,"score":bconf})
    return max(choices,key=lambda x:x["score"]) if choices else None
def outcome(m,market):v=m.get(market);return None if v is None else bool(v)
def run_backtest(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(SCHEMA);rid=conn.execute("INSERT INTO model_policy_backtest_runs(policy_version,train_season,test_season,status) VALUES(%s,%s,%s,'running') RETURNING id",(MODEL_VERSION,TRAIN,TEST)).fetchone()[0]
        try:
            rows=load(conn);bydiv=defaultdict(list)
            for m in rows:bydiv[str(m["division"])].append(m)
            weeks=defaultdict(list);scored=candidates=0
            for div,ms in bydiv.items():
                hist=[m for m in ms if m["season_code"]==TRAIN];tests=[m for m in ms if m["season_code"]==TEST];hist.sort(key=lambda m:(m["match_date"],m["home_team"],m["away_team"]));tests.sort(key=lambda m:(m["match_date"],m["home_team"],m["away_team"]));hist_base=[no_xg(m) for m in hist]
                for m in tests:
                    px=predict_match(hist,m["home_team"],m["away_team"]);pb=predict_match(hist_base,m["home_team"],m["away_team"]);scored+=1;pick=choose(px,pb)
                    if pick:
                        y=outcome(m,pick["market"])
                        if y is not None:
                            candidates+=1;year,week,_=m["match_date"].isocalendar();pick.update({"division":div,"match_date":str(m["match_date"]),"home_team":m["home_team"],"away_team":m["away_team"],"correct":y==pick["selection_yes"],"xg_used":bool(px.xg_used)});weeks[f"{year}-W{week:02d}"].append(pick)
                    hist.append(m);hist_base.append(no_xg(m))
            selected=[];per_week={};market_counts=defaultdict(lambda:{"n":0,"hits":0})
            for wk,cands in sorted(weeks.items()):
                top=sorted(cands,key=lambda x:x["score"],reverse=True)[:10];hits=sum(int(x["correct"]) for x in top);per_week[wk]={"n":len(top),"hits":hits,"hit_rate":round(hits/len(top),4) if top else None,"avg_confidence":round(sum(x["confidence"] for x in top)/len(top),4) if top else None}
                for x in top:market_counts[x["market"]]["n"]+=1;market_counts[x["market"]]["hits"]+=int(x["correct"])
                selected.extend(top)
            hits=sum(int(x["correct"]) for x in selected);by_market={m:{"n":d["n"],"hits":d["hits"],"hit_rate":round(d["hits"]/d["n"],4) if d["n"] else None} for m,d in market_counts.items()};metrics={"weeks":len(per_week),"picks":len(selected),"hits":hits,"hit_rate":round(hits/len(selected),4) if selected else None,"candidate_picks":candidates,"avg_confidence":round(sum(x["confidence"] for x in selected)/len(selected),4) if selected else None,"by_market":by_market,"by_week":per_week,"note":"Exploratory policy check; thresholds informed by earlier 2025/26 diagnostics."}
            conn.execute("UPDATE model_policy_backtest_runs SET finished_at=NOW(),matches_scored=%s,candidate_picks=%s,metrics=%s,status='success',message='ok' WHERE id=%s",(scored,candidates,Jsonb(metrics),rid));result={"policy_version":MODEL_VERSION,"matches_scored":scored,**metrics};log.info("HYBRID_POLICY_RESULT %s",json.dumps(result,ensure_ascii=False,separators=(",",":")));return result
        except Exception as exc:
            conn.execute("UPDATE model_policy_backtest_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc),rid));raise
if __name__=="__main__":print(json.dumps(run_backtest(),ensure_ascii=False,indent=2))
