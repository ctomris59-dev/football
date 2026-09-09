#!/usr/bin/env python3
"""Provider-independent Expected-XI/player-impact context from stored football data.

Primary current source is the already-cached FotMob squad/availability JSON. Player
importance is estimated conservatively from current squad statistics when exposed,
role/captain information, and current-season lineup evidence already stored in ESPN
summaries. No network calls are made here.

This module intentionally leaves previous-season retained-minutes null when a
leakage-safe previous roster cannot be reconstructed. It does calculate a current-XI
stability proxy from starts/appearances or observed prior lineups. All values remain
shadow unless the separate V1/V5 validation registry activates a safe subset.
"""
from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

import understat_player_continuity as legacy

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
LOOKAHEAD_DAYS=int(os.getenv("PLAYER_CONTEXT_LOOKAHEAD_DAYS","8"))
CURRENT_SEASON=int(os.getenv("PLAYER_CONTEXT_CURRENT_SEASON","2026"))
PREVIOUS_SEASON=CURRENT_SEASON-1
SCHEMA=legacy.SCHEMA+"""
CREATE TABLE IF NOT EXISTS db_player_context_runs(
 id BIGSERIAL PRIMARY KEY,started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),finished_at TIMESTAMPTZ,
 status TEXT NOT NULL,teams INTEGER NOT NULL DEFAULT 0,mapped_teams INTEGER NOT NULL DEFAULT 0,
 teams_with_stats INTEGER NOT NULL DEFAULT 0,teams_with_lineup_evidence INTEGER NOT NULL DEFAULT 0,
 message TEXT
);
"""
ALIASES={"man utd":"manchester united","man united":"manchester united","man city":"manchester city","nottm forest":"nottingham forest","wolves":"wolverhampton wanderers","spurs":"tottenham hotspur","tottenham":"tottenham hotspur","milan":"ac milan","inter milan":"inter","paris sg":"paris saint germain","psg":"paris saint germain","mgladbach":"borussia monchengladbach","borussia m gladbach":"borussia monchengladbach","ath bilbao":"athletic club","athletic bilbao":"athletic club"}
def canon(v:Any)->str:
 s=unicodedata.normalize("NFKD",str(v or "")).encode("ascii","ignore").decode().lower().replace("'","")
 s=re.sub(r"\b(fc|cf|ssc|ac|club|football club|afc)\b"," ",s);s=re.sub(r"[^a-z0-9]+"," ",s).strip();s=re.sub(r"\s+"," ",s);return ALIASES.get(s,s)
def f(v:Any)->Optional[float]:
 if isinstance(v,bool) or v in (None,"","-"):return None
 try:return float(str(v).replace("%","").replace(",","").strip())
 except Exception:return None
def walk_numbers(obj:Any,prefix:str="")->Dict[str,List[float]]:
 out=defaultdict(list)
 def rec(x,p):
  if isinstance(x,dict):
   for k,v in x.items():rec(v,f"{p}.{k}" if p else str(k))
  elif isinstance(x,list):
   for i,v in enumerate(x):rec(v,f"{p}[{i}]")
  else:
   val=f(x)
   if val is not None:out[p.lower()].append(val)
 rec(obj,prefix);return dict(out)
def first_metric(obj:Any,needles:Iterable[str])->Optional[float]:
 flat=walk_numbers(obj);ns=[n.lower().replace("_","") for n in needles]
 for k,vals in flat.items():
  nk=re.sub(r"[^a-z0-9]","",k)
  if any(n in nk for n in ns) and vals:return vals[0]
 return None
def role_of(m:Dict[str,Any])->str:
 role=m.get("role")
 if isinstance(role,dict):
  for k in ("fallback","name","key"):
   if role.get(k):return str(role[k])
 for k in ("position","positionLabel","pos"):
  if m.get(k):return str(m[k])
 return ""
def members_from_raw(raw:Any)->List[Dict[str,Any]]:
 groups=raw if isinstance(raw,list) else []
 out=[]
 for g in groups:
  if isinstance(g,dict) and isinstance(g.get("members"),list):out.extend(x for x in g["members"] if isinstance(x,dict))
 return out
def percentile(values:Dict[str,float])->Dict[str,float]:
 if not values:return {}
 arr=sorted(values.items(),key=lambda x:x[1]);n=len(arr);return {k:(i+.5)/n for i,(k,_v) in enumerate(arr)}
def player_strengths(members:List[Dict[str,Any]])->Tuple[List[Dict[str,Any]],float]:
 parsed=[]
 for m in members:
  pid=str(m.get("id") or m.get("playerId") or m.get("name") or "")
  name=str(m.get("name") or m.get("playerName") or "").strip()
  if not pid or not name:continue
  vals={
   "minutes":first_metric(m,("minutesplayed","minutes","time")),
   "starts":first_metric(m,("starts","started","startapps")),
   "apps":first_metric(m,("appearances","matchesplayed","games","apps")),
   "rating":first_metric(m,("rating","averagerating")),
   "goals":first_metric(m,("goals","goalstotal")),
   "assists":first_metric(m,("assists","goalassist")),
   "xg":first_metric(m,("expectedgoals","xg")),
   "xa":first_metric(m,("expectedassists","xa")),
  }
  parsed.append({"id":pid,"name":name,"canon":canon(name),"role":role_of(m),"injured":bool(m.get("injured") or isinstance(m.get("injury"),dict)),"captain":bool(m.get("isCaptain") or m.get("captain")),"metrics":vals,"raw":m})
 metric_p={}
 for key in ("minutes","starts","apps","rating","goals","assists","xg","xa"):
  metric_p[key]=percentile({p["id"]:float(p["metrics"][key]) for p in parsed if p["metrics"].get(key) is not None})
 weights={"minutes":.24,"starts":.19,"apps":.12,"rating":.18,"goals":.09,"assists":.06,"xg":.07,"xa":.05}
 available_metric_cells=sum(1 for p in parsed for v in p["metrics"].values() if v is not None);metric_coverage=available_metric_cells/max(1,len(parsed)*8)
 for p in parsed:
  comps=[(w,metric_p[k][p["id"]]) for k,w in weights.items() if p["id"] in metric_p[k]]
  base=sum(w*v for w,v in comps)/sum(w for w,_ in comps) if comps else .50
  if p["captain"]:base=min(1.0,base+.04)
  p["strength"]=max(.05,min(1.0,base))
 return parsed,metric_coverage
def collect_starters(obj:Any,target_team_id:Optional[str],target_team_name:str)->List[str]:
 target_id=str(target_team_id or "");ct=canon(target_team_name);found=[]
 def athlete_name(a):
  if not isinstance(a,dict):return None
  aa=a.get("athlete") if isinstance(a.get("athlete"),dict) else a
  return aa.get("displayName") or aa.get("fullName") or aa.get("name") if isinstance(aa,dict) else None
 def subtree(x,team_match=False):
  if isinstance(x,dict):
   tm=team_match
   team=x.get("team")
   if isinstance(team,dict):
    tid=str(team.get("id") or "");tn=canon(team.get("displayName") or team.get("name") or team.get("shortDisplayName"))
    if (target_id and tid==target_id) or (tn and tn==ct):tm=True
   athletes=x.get("athletes")
   if tm and isinstance(athletes,list):
    for a in athletes:
     if not isinstance(a,dict):continue
     starter=a.get("starter")
     if starter is None and isinstance(a.get("athlete"),dict):starter=a["athlete"].get("starter")
     if starter is True or str(starter).lower() in {"true","1","yes"}:
      n=athlete_name(a)
      if n:found.append(canon(n))
   for v in x.values():subtree(v,tm)
  elif isinstance(x,list):
   for v in x:subtree(v,team_match)
 subtree(obj,False);return [x for x in found if x]
def current_lineup_evidence(conn,team_name:str,team_id:Optional[str])->Tuple[Counter,int]:
 rows=conn.execute("""SELECT summary_raw FROM espn_current_matches WHERE status='post' AND (home_team=%s OR away_team=%s) AND summary_raw IS NOT NULL ORDER BY match_date DESC LIMIT 12""",(team_name,team_name)).fetchall()
 counts=Counter();matches=0
 for (raw,) in rows:
  names=collect_starters(raw,team_id,team_name)
  if len(set(names))>=7:counts.update(set(names));matches+=1
 return counts,matches
def source_team_rows(conn)->Dict[str,Dict[str,Any]]:
 rows=conn.execute("""SELECT DISTINCT ON (team_key) team_key,team_name,team_id,raw_squad,injured_players FROM (
  SELECT lower(home_team) team_key,home_team team_name,home_fotmob_team_id team_id,t.raw_squad,f.home_injured_players injured_players,f.snapshot_hour
  FROM fotmob_fixture_availability_snapshots f LEFT JOIN LATERAL (SELECT raw_squad FROM fotmob_team_availability_snapshots t WHERE t.fotmob_team_id=f.home_fotmob_team_id ORDER BY t.snapshot_hour DESC LIMIT 1)t ON TRUE
  UNION ALL
  SELECT lower(away_team),away_team,away_fotmob_team_id,t.raw_squad,f.away_injured_players,f.snapshot_hour
  FROM fotmob_fixture_availability_snapshots f LEFT JOIN LATERAL (SELECT raw_squad FROM fotmob_team_availability_snapshots t WHERE t.fotmob_team_id=f.away_fotmob_team_id ORDER BY t.snapshot_hour DESC LIMIT 1)t ON TRUE
 )q WHERE team_id IS NOT NULL ORDER BY team_key,snapshot_hour DESC""").fetchall()
 return {canon(name):{"team_name":name,"team_id":str(tid),"raw_squad":raw or [],"injured":inj or []} for _k,name,tid,raw,inj in rows}
def run_import(database_url:Optional[str]=None)->Dict[str,Any]:
 db=(database_url or DATABASE_URL).strip()
 if not db:raise RuntimeError("Missing DATABASE_URL")
 with psycopg.connect(db,autocommit=True) as c:
  c.execute(SCHEMA);rid=c.execute("INSERT INTO db_player_context_runs(status) VALUES('running') RETURNING id").fetchone()[0];hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)
  try:
   teams=sorted({str(x) for row in c.execute("""SELECT home_team,away_team FROM espn_upcoming WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+(%s||' days')::interval""",(LOOKAHEAD_DAYS,)).fetchall() for x in row});sources=source_team_rows(c);mapped=withstats=withline=0;covs=[]
   for team in teams:
    src=sources.get(canon(team));members=members_from_raw((src or {}).get("raw_squad"));players,metric_cov=player_strengths(members);line_counts,line_matches=current_lineup_evidence(c,team,(src or {}).get("team_id"));withline+=int(line_matches>0)
    if not players:
     continue
    mapped+=1;withstats+=int(metric_cov>0);injured={canon(x.get("name")) for x in ((src or {}).get("injured") or []) if isinstance(x,dict) and x.get("name")}
    for p in players:
     p["injured"]=p["injured"] or p["canon"] in injured
     if line_counts:
      freq=line_counts.get(p["canon"],0)/max(1,line_matches);p["strength"]=min(1.0,.78*p["strength"]+.22*freq)
    ranked=sorted(players,key=lambda p:(line_counts.get(p["canon"],0),p["metrics"].get("starts") or -1,p["metrics"].get("minutes") or -1,p["strength"]),reverse=True);top=ranked[:11];den=sum(p["strength"] for p in top) or 1.0;impact=min(.55,sum(p["strength"] for p in players if p["injured"])/den);avail=[p for p in ranked if not p["injured"]][:11];expected=sum(p["strength"] for p in avail)/len(avail) if avail else None;top11=sum(p["strength"] for p in top)/len(top) if top else None
    stability=None
    if line_matches>=2:
     top_names={p["canon"] for p in top};stability=sum(min(line_counts.get(n,0),line_matches) for n in top_names)/(11*line_matches)
    elif any(p["metrics"].get("starts") is not None for p in players):
     starts=sorted((float(p["metrics"].get("starts") or 0) for p in players),reverse=True);mx=max(starts or [0]);stability=(sum(starts[:11])/(11*mx)) if mx>0 else None
    gk=any(p["injured"] and ("goal" in p["role"].lower() or p["role"].lower() in {"gk","keeper"}) for p in players);key_abs=sorted((p for p in players if p["injured"]),key=lambda p:p["strength"],reverse=True)[:6]
    roster_cov=min(1.0,len(players)/20);coverage=min(.90,.55*roster_cov+.30*metric_cov+.15*min(1.0,line_matches/4));covs.append(coverage);meta={"source":"cached-fotmob-squad+espn-lineups-v3","players":len(players),"metric_coverage":round(metric_cov,4),"lineup_matches":line_matches,"retained_minutes_status":"unavailable_without_safe_previous_roster","stability_semantics":"current-XI stability, not prior-season retention"}
    c.execute("""INSERT INTO player_team_context_snapshots(team_name,snapshot_hour,current_season,previous_season,expected_xi_strength,top11_strength,injury_impact,goalkeeper_injured,retained_minutes_share,starter_continuity,player_coverage,key_absences,source_meta) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s) ON CONFLICT(team_name,snapshot_hour) DO UPDATE SET expected_xi_strength=EXCLUDED.expected_xi_strength,top11_strength=EXCLUDED.top11_strength,injury_impact=EXCLUDED.injury_impact,goalkeeper_injured=EXCLUDED.goalkeeper_injured,retained_minutes_share=NULL,starter_continuity=EXCLUDED.starter_continuity,player_coverage=EXCLUDED.player_coverage,key_absences=EXCLUDED.key_absences,source_meta=EXCLUDED.source_meta""",(team,hour,CURRENT_SEASON,PREVIOUS_SEASON,expected,top11,impact,gk,stability,coverage,Jsonb([{"name":p["name"],"importance":round(p["strength"],4)} for p in key_abs]),Jsonb(meta)))
   status="success" if mapped else "failed";msg={"source":"db-only-v3","avg_coverage":round(sum(covs)/len(covs),4) if covs else 0,"network_calls":0};c.execute("UPDATE db_player_context_runs SET finished_at=NOW(),status=%s,teams=%s,mapped_teams=%s,teams_with_stats=%s,teams_with_lineup_evidence=%s,message=%s WHERE id=%s",(status,len(teams),mapped,withstats,withline,json.dumps(msg,separators=(",",":")),rid));c.execute("""INSERT INTO player_context_runs(status,finished_at,teams,teams_with_current,teams_with_previous,http_calls,message) VALUES(%s,NOW(),%s,%s,0,0,%s)""",(status,len(teams),mapped,json.dumps(msg,separators=(",",":"))));res={"status":status,"teams":len(teams),"mapped":mapped,"teams_with_stats":withstats,"teams_with_lineup_evidence":withline,"avg_coverage":msg["avg_coverage"],"network_calls":0};print("PLAYER_CONTEXT_DB_V3_RESULT",json.dumps(res,separators=(",",":")));return res
  except Exception as exc:
   c.execute("UPDATE db_player_context_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:800],rid));raise
if __name__=="__main__":print(json.dumps(run_import(),indent=2))
