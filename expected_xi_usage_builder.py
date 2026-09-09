#!/usr/bin/env python3
"""Build a data-informed expected-XI proxy from real usage/history.

Inputs: current ESPN roster snapshots, explicit starters from already completed
2026/27 ESPN match summaries, final 2025/26 FBref Starts+Minutes, and current FotMob
injury names. This is an expected-XI proxy, never a confirmed lineup. Values remain
shadow-only until leakage-safe validation.
"""
from __future__ import annotations
import json, os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import psycopg
from psycopg.types.json import Jsonb
import understat_player_continuity_v2 as v2
import espn_team_roster_player_bridge as roster_base
from espn_historical_lineups_importer import extract_starters
DATABASE_URL=os.getenv("DATABASE_URL","").strip();MIN_KNOWN=int(os.getenv("EXPECTED_XI_MIN_KNOWN_PLAYERS","7"))

def pct(vals:Dict[str,float])->Dict[str,float]:
 s=sorted(vals.items(),key=lambda x:x[1]);n=len(s);return {k:(i+.5)/n for i,(k,_v) in enumerate(s)} if n else {}
def run_build(database_url:Optional[str]=None)->Dict[str,Any]:
 db=(database_url or DATABASE_URL).strip()
 if not db:raise RuntimeError("Missing DATABASE_URL")
 with psycopg.connect(db,autocommit=True) as c:
  # Global previous-season activity allows transferred players to contribute to XI ranking.
  prev={}
  try:
   for team,name,starts,mins in c.execute("SELECT team_name,player_name,starts,minutes FROM fbref_player_season_stats WHERE season=2025").fetchall():
    k=v2.canon(name);cur=prev.get(k)
    score=(float(starts or 0),float(mins or 0));
    if not cur or score>(cur["starts"],cur["minutes"]):prev[k]={"team":str(team),"starts":score[0],"minutes":score[1]}
  except Exception:pass
  # Explicit current-season starts from completed ESPN summaries already archived by current importer.
  current=defaultdict(Counter);matches=defaultdict(set)
  try:
   rows=c.execute("SELECT event_id,summary_raw FROM espn_current_matches WHERE summary_raw IS NOT NULL AND match_date>=DATE '2026-07-01'").fetchall()
   for eid,raw in rows:
    if not isinstance(raw,dict):continue
    for _tid,g in extract_starters(raw).items():
     team=str(g.get("team_name") or "");ct=v2.canon(team)
     for p in g.get("players",{}).values():current[ct][v2.canon(p.get("player_name"))]+=1
     if g.get("players"):matches[ct].add(str(eid))
  except Exception:pass
  targets=c.execute("""SELECT DISTINCT home_team FROM espn_upcoming WHERE is_current=TRUE AND match_date<=NOW()+INTERVAL '8 days'
    UNION SELECT DISTINCT away_team FROM espn_upcoming WHERE is_current=TRUE AND match_date<=NOW()+INTERVAL '8 days'""").fetchall()
  hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0);updated=data_ready=0
  for (team,) in targets:
   team=str(team);ct=v2.canon(team)
   snap=c.execute("""SELECT raw FROM espn_team_roster_snapshots WHERE team_name=%s ORDER BY snapshot_date DESC LIMIT 1""",(team,)).fetchone()
   if not snap or not isinstance(snap[0],dict):continue
   players=roster_base.extract_players(snap[0]);names=[roster_base.pname(p) for p in players if roster_base.pname(p)]
   if not names:continue
   injured,_objs=v2.injury_names(c,team)
   cur_counts={v2.canon(n):float(current[ct].get(v2.canon(n),0)) for n in names};prev_starts={v2.canon(n):float((prev.get(v2.canon(n)) or {}).get("starts",0)) for n in names};prev_minutes={v2.canon(n):float((prev.get(v2.canon(n)) or {}).get("minutes",0)) for n in names}
   pc,ps,pm=pct(cur_counts),pct(prev_starts),pct(prev_minutes);ranked=[]
   for n in names:
    k=v2.canon(n);known=cur_counts.get(k,0)>0 or prev_starts.get(k,0)>0 or prev_minutes.get(k,0)>0
    if not known:continue
    current_weight=.62 if len(matches[ct])>=2 else .40 if len(matches[ct])==1 else 0.0
    hist_weight=1-current_weight;score=current_weight*pc.get(k,.5)+hist_weight*(.55*ps.get(k,.5)+.45*pm.get(k,.5))
    ranked.append({"name":n,"key":k,"score":score,"current_starts":cur_counts.get(k,0),"previous_starts":prev_starts.get(k,0),"previous_minutes":prev_minutes.get(k,0),"injured":k in injured})
   available=sorted([x for x in ranked if not x["injured"]],key=lambda x:(x["current_starts"],x["score"],x["previous_starts"],x["previous_minutes"]),reverse=True)[:11]
   top_all=sorted(ranked,key=lambda x:(x["current_starts"],x["score"],x["previous_starts"],x["previous_minutes"]),reverse=True)[:11]
   informed=len(ranked)>=MIN_KNOWN and len(available)>=MIN_KN;expected=sum(x["score"] for x in available)/len(available) if informed else None;top11=sum(x["score"] for x in top_all)/len(top_all) if len(top_all)>=MIN_KNOWN else None
   row=c.execute("SELECT retained_minutes_share,starter_continuity,player_coverage,key_absences,source_meta FROM player_team_context_snapshots WHERE team_name=%s ORDER BY snapshot_hour DESC LIMIT 1",(team,)).fetchone()
   if not row:continue
   meta=row[4] if isinstance(row[4],dict) else {};meta={**meta,"expected_xi_data_informed":bool(informed),"expected_xi_method":"current-explicit-starts+fbref-previous-activity+current-injuries","current_lineup_matches":len(matches[ct]),"known_ranked_players":len(ranked),"expected_xi_names":[x["name"] for x in available],"confirmed_lineup":False,"neutral_strength_interface":not informed}
   c.execute("""UPDATE player_team_context_snapshots SET expected_xi_strength=%s,top11_strength=%s,source_meta=%s WHERE team_name=%s AND snapshot_hour=(SELECT MAX(snapshot_hour) FROM player_team_context_snapshots WHERE team_name=%s)""",(expected if informed else .50,top11 if top11 is not None else .50,Jsonb(meta),team,team));updated+=1;data_ready+=int(informed)
  res={"status":"success","teams_seen":len(targets),"updated":updated,"data_informed_expected_xi":data_ready,"current_usage_teams":sum(1 for x in matches.values() if x),"fbref_global_players":len(prev)};print("EXPECTED_XI_USAGE_RESULT",json.dumps(res,separators=(",",":")),flush=True);return res
if __name__=="__main__":print(json.dumps(run_build(),indent=2))
