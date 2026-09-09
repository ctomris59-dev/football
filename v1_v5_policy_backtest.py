#!/usr/bin/env python3
"""Leakage-safe V1 vs V5 ranking-policy validation.

Historically safe activation candidates:
- pressure/style signal from matches strictly before target fixture;
- squad continuity from previous-season starters and current-season lineups observed before target fixture.

Asian multi-line movement and Expected-XI injury impact remain diagnostic because historical Friday snapshots were not archived leakage-safely.
"""
from __future__ import annotations
import json, os, re, unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
import psycopg
from psycopg.types.json import Jsonb
from model_engine_v1 import best_market, predict_match

DATABASE_URL=os.getenv("DATABASE_URL","").strip();TRAIN=os.getenv("V5_BACKTEST_TRAIN_SEASON","2425");TEST=os.getenv("V5_BACKTEST_TEST_SEASON","2526")
VERSION="v1-v5-four-layer-safe-validation-v2";MIN_IMPROVEMENT=float(os.getenv("V5_MIN_HIT_RATE_IMPROVEMENT","0.005"))
SCHEMA="""
CREATE TABLE IF NOT EXISTS v5_policy_validation_runs(id BIGSERIAL PRIMARY KEY,started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),finished_at TIMESTAMPTZ,policy_version TEXT NOT NULL,train_season TEXT NOT NULL,test_season TEXT NOT NULL,status TEXT NOT NULL,matches_scored INTEGER NOT NULL DEFAULT 0,lineup_matches INTEGER NOT NULL DEFAULT 0,results JSONB,activation_mode TEXT,message TEXT);
CREATE TABLE IF NOT EXISTS policy_activation_registry(policy_key TEXT PRIMARY KEY,policy_version TEXT NOT NULL,active_mode TEXT NOT NULL,validated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),metrics JSONB NOT NULL DEFAULT '{}'::jsonb,reason TEXT);
"""
ALIASES={"man united":"manchester united","man utd":"manchester united","man city":"manchester city","nottm forest":"nottingham forest","wolves":"wolverhampton wanderers","newcastle":"newcastle united","spurs":"tottenham hotspur","tottenham":"tottenham hotspur","ath bilbao":"athletic club","athletic bilbao":"athletic club","betis":"real betis","sociedad":"real sociedad","valladolid":"real valladolid","vallecano":"rayo vallecano","celta":"celta vigo","espanol":"espanyol","milan":"ac milan","verona":"hellas verona","inter milan":"inter","dortmund":"borussia dortmund","mgladbach":"borussia monchengladbach","leverkusen":"bayer leverkusen","frankfurt":"eintracht frankfurt","paris sg":"paris saint germain","psg":"paris saint germain","st etienne":"saint etienne"}
def canon(v:Any)->str:
 s=unicodedata.normalize("NFKD",str(v or "")).encode("ascii","ignore").decode().lower().replace("'","");s=re.sub(r"\b(fc|cf|ssc|ac|club|football club|afc)\b"," ",s);s=re.sub(r"[^a-z0-9]+"," ",s).strip();s=re.sub(r"\s+"," ",s);return ALIASES.get(s,s)
def as_date(v:Any)->date:
 if isinstance(v,date) and not isinstance(v,datetime):return v
 if isinstance(v,datetime):return v.date()
 return date.fromisoformat(str(v)[:10])
def extract_lineup_objects(obj:Any)->List[Dict[str,Any]]:
 out=[]
 if isinstance(obj,dict):
  if isinstance(obj.get("team"),dict) and isinstance(obj.get("startXI"),list):out.append(obj)
  for v in obj.values():out.extend(extract_lineup_objects(v))
 elif isinstance(obj,list):
  for v in obj:out.extend(extract_lineup_objects(v))
 return out
def starter_names(lineup:Dict[str,Any])->List[str]:
 out=[]
 for item in lineup.get("startXI") or []:
  if not isinstance(item,dict):continue
  p=item.get("player") if isinstance(item.get("player"),dict) else item;name=p.get("name") if isinstance(p,dict) else None
  if name:out.append(canon(name))
 return [x for x in out if x]
def lineup_history(conn)->Tuple[Dict[str,set[str]],Dict[str,List[Tuple[date,List[str]]]],int]:
 prev_counts=defaultdict(Counter);current_events=defaultdict(list);used=0
 try:rows=conn.execute("""SELECT f.season,f.fixture_date,d.lineups FROM fixtures f JOIN fixture_details d ON d.fixture_id=f.fixture_id WHERE f.season IN (2024,2025) AND d.lineups IS NOT NULL AND f.status_short IN ('FT','AET','PEN') ORDER BY f.fixture_date""").fetchall()
 except Exception:return {},{},0
 for season,dt,raw in rows:
  for lu in extract_lineup_objects(raw):
   t=(lu.get("team") or {}).get("name");names=starter_names(lu)
   if not t or len(names)<7:continue
   used+=1;ct=canon(t);d=as_date(dt)
   if int(season)==2024:prev_counts[ct].update(names)
   else:current_events[ct].append((d,names))
 prev_top={t:set(x for x,_ in counts.most_common(11)) for t,counts in prev_counts.items() if counts}
 for t in current_events:current_events[t].sort(key=lambda x:x[0])
 return prev_top,current_events,used
def continuity_at(team:str,dt:date,prev_top,current_events)->Optional[float]:
 ct=canon(team);prev=prev_top.get(ct)
 if not prev:return None
 seen=set();matches=0
 for d,names in current_events.get(ct,[]):
  if d>=dt:break
  seen.update(names);matches+=1
 if matches<2 or len(seen)<11:return None
 return len(prev & seen)/len(prev)
def recent_team_matches(history,team,n=18):
 ct=canon(team);out=[]
 for m in reversed(history):
  if ct in (canon(m.get("home_team")),canon(m.get("away_team"))):out.append(m)
  if len(out)>=n:break
 return out
def env_total(rows,a,b):
 vals=[]
 for m in rows:
  x,y=m.get(a),m.get(b)
  if x is not None and y is not None:vals.append(float(x)+float(y))
 return sum(vals)/len(vals) if vals else None
def pressure_signals(history,home,away):
 hr,ar=recent_team_matches(history,home),recent_team_matches(history,away);hg,ag=env_total(hr,"home_goals","away_goals"),env_total(ar,"home_goals","away_goals");hc,ac=env_total(hr,"home_corners","away_corners"),env_total(ar,"home_corners","away_corners")
 goal=max(.55,min(1.45,((hg+ag)/2)/2.6)) if hg is not None and ag is not None else None;corner=max(.55,min(1.45,((hc+ac)/2)/9.5)) if hc is not None and ac is not None else None;return goal,corner
def pressure_factor(market,selection,goal,corner):
 sig=corner if market=="corners_over_8_5" else goal if market in ("over_2_5","btts") else None
 if sig is None:return 1.0
 over=("ÜST" in selection.upper() or "VAR" in selection.upper() or "OVER" in selection.upper())
 if over and sig<.88:return .985
 if not over and sig>1.12:return .985
 return 1.0
def continuity_factor(hc,ac):
 vals=[x for x in (hc,ac) if x is not None];return .985 if vals and min(vals)<.55 else 1.0
def no_vig_over(o,u):
 try:o=float(o);u=float(u)
 except Exception:return None
 if o<=1 or u<=1:return None
 a,b=1/o,1/u;return a/(a+b) if a+b else None
def market_factor(match,market,selection_yes,model_conf):
 if market!="over_2_5":return 1.0
 p=no_vig_over(match.get("odds_over_2_5"),match.get("odds_under_2_5"))
 if p is None:return 1.0
 selected=p if selection_yes else 1-p;gap=model_conf-selected
 if gap>.12:return .975
 if gap>.07:return .988
 return 1.0
def outcome(match,market):
 v=match.get(market);return None if v is None else bool(v)
def load_matches(conn):
 cur=conn.execute("""SELECT season_code,division,league_name,match_date,home_team,away_team,home_goals,away_goals,home_shots,away_shots,home_shots_on_target,away_shots_on_target,home_corners,away_corners,total_corners,over_2_5,btts,corners_over_8_5,odds_over_2_5,odds_under_2_5 FROM football_data_matches WHERE season_code IN (%s,%s) AND home_goals IS NOT NULL AND away_goals IS NOT NULL ORDER BY division,match_date,home_team,away_team""",(TRAIN,TEST));cols=[d.name for d in cur.description];return [dict(zip(cols,r)) for r in cur.fetchall()]
def summarize_weekly(weekly,score_key,split_date):
 picks=[];pre=[];post=[]
 for _week,cands in sorted(weekly.items()):
  ranked=sorted(cands,key=lambda x:(x[score_key],x["confidence"]),reverse=True)[:10];picks.extend(ranked);(post if min(x["date"] for x in ranked)>=split_date else pre).extend(ranked)
 def sm(rows):
  hits=sum(bool(x["correct"]) for x in rows);return {"picks":len(rows),"hits":hits,"hit_rate":round(hits/len(rows),4) if rows else None,"avg_confidence":round(sum(x["confidence"] for x in rows)/len(rows),4) if rows else None}
 return {**sm(picks),"first_half":sm(pre),"second_half":sm(post)}
def run_backtest(database_url:Optional[str]=None)->Dict[str,Any]:
 db=(database_url or DATABASE_URL).strip()
 if not db:raise RuntimeError("Missing DATABASE_URL")
 with psycopg.connect(db,autocommit=True) as c:
  c.execute(SCHEMA);rid=c.execute("INSERT INTO v5_policy_validation_runs(policy_version,train_season,test_season,status) VALUES(%s,%s,%s,'running') RETURNING id",(VERSION,TRAIN,TEST)).fetchone()[0]
  try:
   allm=load_matches(c);prev_top,current_events,lineup_used=lineup_history(c);test_dates=sorted({as_date(m["match_date"]) for m in allm if m["season_code"]==TEST});split=test_dates[len(test_dates)//2] if test_dates else date(2026,1,1);by_div=defaultdict(list)
   for m in allm:by_div[str(m["division"])].append(m)
   weekly=defaultdict(list);scored=cont_available=pressure_available=market_available=0
   for division,matches in by_div.items():
    history=[m for m in matches if m["season_code"]==TRAIN];tests=[m for m in matches if m["season_code"]==TEST];history.sort(key=lambda m:(m["match_date"],m["home_team"],m["away_team"]));tests.sort(key=lambda m:(m["match_date"],m["home_team"],m["away_team"]))
    for match in tests:
     pred=predict_match(history,match["home_team"],match["away_team"]);bm=best_market(pred);y=outcome(match,bm["market"])
     if y is None:history.append(match);continue
     scored+=1;dt=as_date(match["match_date"]);hc=continuity_at(match["home_team"],dt,prev_top,current_events);ac=continuity_at(match["away_team"],dt,prev_top,current_events);cont_available+=int(hc is not None or ac is not None);gp,cp=pressure_signals(history,match["home_team"],match["away_team"]);pressure_available+=int(gp is not None or cp is not None);mf=market_factor(match,bm["market"],bool(bm["selection_yes"]),float(bm["probability"]));market_available+=int(bm["market"]=="over_2_5" and no_vig_over(match.get("odds_over_2_5"),match.get("odds_under_2_5")) is not None);pf=pressure_factor(bm["market"],bm["selection"],gp,cp);cf=continuity_factor(hc,ac);base=float(bm["probability"])*(.75+.25*float(bm["data_quality"]));iso=dt.isocalendar();wk=f"{iso.year}-W{iso.week:02d}";weekly[wk].append({"date":dt,"confidence":float(bm["probability"]),"correct":bool(y)==bool(bm["selection_yes"]),"v1":base,"pressure":base*pf,"continuity":base*cf,"pressure_continuity":base*pf*cf,"market_stress":base*pf*cf*mf});history.append(match)
   variants={k:summarize_weekly(weekly,k,split) for k in ("v1","pressure","continuity","pressure_continuity","market_stress")};v1=variants["v1"];eligible=[]
   for mode in ("pressure","continuity","pressure_continuity"):
    x=variants[mode];full_gain=(x["hit_rate"] or 0)-(v1["hit_rate"] or 0);second_gain=(x["second_half"]["hit_rate"] or 0)-(v1["second_half"]["hit_rate"] or 0);first_gain=(x["first_half"]["hit_rate"] or 0)-(v1["first_half"]["hit_rate"] or 0)
    if x["picks"]>=300 and full_gain>=MIN_IMPROVEMENT and second_gain>=0 and first_gain>=-.015:eligible.append((mode,full_gain,second_gain))
   if eligible:eligible.sort(key=lambda z:(z[1],z[2]),reverse=True);active=eligible[0][0];reason=f"{active} beat V1 by {eligible[0][1]:.4f} full-season and did not regress second-half"
   else:active="v1_only";reason="No leakage-safe V5 subset cleared the improvement gate; four-layer extras remain shadow"
   results={"version":VERSION,"split_date":split.isoformat(),"matches_scored":scored,"lineup_objects":lineup_used,"coverage":{"continuity_matches":cont_available,"pressure_matches":pressure_available,"market_stress_matches":market_available},"variants":variants,"activation_mode":active,"unvalidated_for_activation":["expected_xi_injury_impact","asian_multiline_open_to_latest"],"note":"market_stress uses historical final/average O/U2.5 prices and is diagnostic only"}
   c.execute("UPDATE v5_policy_validation_runs SET finished_at=NOW(),status='success',matches_scored=%s,lineup_matches=%s,results=%s,activation_mode=%s,message=%s WHERE id=%s",(scored,lineup_used,Jsonb(results),active,reason,rid));c.execute("""INSERT INTO policy_activation_registry(policy_key,policy_version,active_mode,metrics,reason) VALUES('four-layer-v5',%s,%s,%s,%s) ON CONFLICT(policy_key) DO UPDATE SET policy_version=EXCLUDED.policy_version,active_mode=EXCLUDED.active_mode,validated_at=NOW(),metrics=EXCLUDED.metrics,reason=EXCLUDED.reason""",(VERSION,active,Jsonb(results),reason));print("V1_V5_POLICY_BACKTEST_RESULT",json.dumps(results,ensure_ascii=False,separators=(",",":")));return results
  except Exception as exc:c.execute("UPDATE v5_policy_validation_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:1000],rid));raise
def ensure_validation(database_url:Optional[str]=None)->Dict[str,Any]:
 db=(database_url or DATABASE_URL).strip()
 if not db:raise RuntimeError("Missing DATABASE_URL")
 with psycopg.connect(db,autocommit=True) as c:
  c.execute(SCHEMA);row=c.execute("SELECT activation_mode,results FROM v5_policy_validation_runs WHERE policy_version=%s AND status='success' ORDER BY id DESC LIMIT 1",(VERSION,)).fetchone()
  if row:return {"status":"fresh","activation_mode":row[0],"results":row[1]}
 res=run_backtest(db);return {"status":"ran","activation_mode":res["activation_mode"],"results":res}
if __name__=="__main__":print(json.dumps(run_backtest(),ensure_ascii=False,indent=2))
