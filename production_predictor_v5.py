#!/usr/bin/env python3
"""Production v5 wrapper: four new data layers may only reduce ranking uncertainty.

Displayed V1 model probabilities remain unchanged until archived calibration proves
that Expected-XI, Asian lines, pressure proxy or continuity improve probability estimates.
"""
from __future__ import annotations
import json
from datetime import date, datetime
from typing import Any, Dict, Optional
import psycopg
from psycopg.types.json import Jsonb as PsyJsonb
import production_predictor_v4 as v4
import production_predictor_v3 as v3
MODEL_VERSION="production-poisson-form-v1-advanced-v5";POLICY_VERSION="expected-xi-asian-pressure-continuity-shadow-v5"
def _default(v):
 if isinstance(v,(date,datetime)):return v.isoformat()
 return str(v)
def _safe(v):return PsyJsonb(v,dumps=lambda x:json.dumps(x,default=_default,separators=(",",":")))
def penalty_factor(ctx: Dict[str,Any], market: str, selection: str, model_p: float) -> float:
 factor=1.0;cov=float(ctx.get("total_coverage") or 0)
 if cov<.35:factor*=.985
 hi=max(float(ctx.get("home_injury_impact") or 0),float(ctx.get("away_injury_impact") or 0))
 if hi>=.20:factor*=.985
 if ctx.get("home_goalkeeper_injured") or ctx.get("away_goalkeeper_injured"):factor*=.99
 conts=[x for x in (ctx.get("home_starter_continuity"),ctx.get("away_starter_continuity")) if x is not None]
 if conts and min(float(x) for x in conts)<.55:factor*=.985
 retained=[x for x in (ctx.get("home_retained_minutes_share"),ctx.get("away_retained_minutes_share")) if x is not None]
 if retained and min(float(x) for x in retained)<.55:factor*=.985
 books=int(ctx.get("asian_bookmakers") or 0);asian=ctx.get("asian_goal_p_over_2_5") if market=="over_2_5" else (ctx.get("asian_corner_p_over_8_5") if market=="corners_over_8_5" else None)
 if asian is not None and books>=2:
  pa=float(asian)
  if "ALT" in str(selection).upper() or "UNDER" in str(selection).upper():pa=1-pa
  if model_p-pa>.12:factor*=.975
  elif model_p-pa>.07:factor*=.988
 pressure=ctx.get("goal_pressure_signal") if market in ("over_2_5","btts") else (ctx.get("corner_pressure_signal") if market=="corners_over_8_5" else None)
 if pressure is not None:
  ps=float(pressure);over=("ÜST" in str(selection).upper() or "VAR" in str(selection).upper() or "OVER" in str(selection).upper())
  if over and ps<.88:factor*=.985
  if not over and ps>1.12:factor*=.985
 return max(.80,min(1.0,factor))
def _ctx(conn,event_id):
 try:r=conn.execute("""SELECT home_injury_impact,away_injury_impact,home_goalkeeper_injured,away_goalkeeper_injured,home_retained_minutes_share,away_retained_minutes_share,home_starter_continuity,away_starter_continuity,goal_pressure_signal,corner_pressure_signal,asian_goal_p_over_2_5,asian_corner_p_over_8_5,asian_bookmakers,total_coverage,context FROM advanced_fixture_context_v4 WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",(event_id,)).fetchone()
 except Exception:return {}
 if not r:return {}
 keys=["home_injury_impact","away_injury_impact","home_goalkeeper_injured","away_goalkeeper_injured","home_retained_minutes_share","away_retained_minutes_share","home_starter_continuity","away_starter_continuity","goal_pressure_signal","corner_pressure_signal","asian_goal_p_over_2_5","asian_corner_p_over_8_5","asian_bookmakers","total_coverage","context"];return dict(zip(keys,r))
def rerank(db:str,run_id:int)->Dict[str,Any]:
 with psycopg.connect(db,autocommit=True) as c:
  rows=c.execute("SELECT event_id,market,selection,model_probability,ranking_score,model_details FROM production_predictions WHERE run_id=%s AND provisional_ready=TRUE",(run_id,)).fetchall()
  for eid,market,sel,p,rank,details in rows:
   ctx=_ctx(c,eid);fac=penalty_factor(ctx,str(market),str(sel),float(p));md=dict(details or {}) if isinstance(details,dict) else {};md["four_layer_context"]=(ctx.get("context") or {});md["v5_shadow_penalty_factor"]=round(fac,6);c.execute("UPDATE production_predictions SET ranking_score=%s,model_details=%s WHERE run_id=%s AND event_id=%s AND market=%s",(round(float(rank)*fac,6),_safe(md),run_id,eid,market))
  cand=c.execute("SELECT event_id,market,ranking_score,model_probability FROM production_predictions WHERE run_id=%s AND provisional_ready=TRUE AND model_probability>=%s AND market_price IS NOT NULL AND market_price>=%s ORDER BY ranking_score DESC,model_probability DESC",(run_id,v3.core.PREDICTION_MIN_CONFIDENCE,v3.core.PREDICTION_MIN_PRICE)).fetchall();best={}
  for eid,m,r,p in cand:
   if eid not in best:best[eid]=(m,float(r),float(p))
  ordered=sorted(best.items(),key=lambda kv:(kv[1][1],kv[1][2]),reverse=True)[:10];c.execute("UPDATE production_predictions SET top10_rank=NULL WHERE run_id=%s",(run_id,))
  for i,(eid,(m,_r,_p)) in enumerate(ordered,1):c.execute("UPDATE production_predictions SET top10_rank=%s WHERE run_id=%s AND event_id=%s AND market=%s",(i,run_id,eid,m))
  c.execute("UPDATE production_prediction_runs SET model_version=%s,policy_version=%s,candidate_matches=%s,top10_count=%s,message=%s WHERE id=%s",(MODEL_VERSION,POLICY_VERSION,len(best),len(ordered),"v5 shadow context: new layers can only reduce ranking; displayed probabilities unchanged",run_id));top=[]
  for rank,(eid,(m,_r,_p)) in enumerate(ordered,1):
   row=c.execute("SELECT match_date,league_name,home_team,away_team,selection,model_probability,ranking_score,final_context_ready FROM production_predictions WHERE run_id=%s AND event_id=%s AND market=%s",(run_id,eid,m)).fetchone()
   if row:top.append({"rank":rank,"event_id":eid,"market":m,"match_date":row[0],"league":row[1],"home":row[2],"away":row[3],"selection":row[4],"confidence":float(row[5]),"ranking":float(row[6]),"final":bool(row[7])})
  return {"candidate_matches":len(best),"top10_count":len(ordered),"top10":top}
def run_predictions(database_url:Optional[str]=None,**kwargs)->Dict[str,Any]:
 db=(database_url or v3.core.DATABASE_URL).strip()
 if not db:raise RuntimeError("Missing DATABASE_URL")
 res=v4.run_predictions(db,**kwargs);rr=rerank(db,int(res["run_id"]));res.update(rr);res["model_version"]=MODEL_VERSION;res["policy_version"]=POLICY_VERSION;res["four_layer_shadow"]=True;return res
if __name__=="__main__":print(json.dumps(run_predictions(),ensure_ascii=False,indent=2,default=_default))
