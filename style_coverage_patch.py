#!/usr/bin/env python3
"""Patch missing fixture style context without requiring a FotMob team id.

Uses stored top-flight match history by canonical team name. For a promoted club with
insufficient top-flight history, uses the existing validated promotion prior's
transferred_relative metrics. It never invents unavailable style fields.
"""
from __future__ import annotations
import json, os
from collections import defaultdict
from typing import Any, Dict, List, Optional
import psycopg
from psycopg.types.json import Jsonb
from internal_style_builder import canon, team_metrics, avg
from fixture_enrichment_builder import corner_style

DATABASE_URL=os.getenv("DATABASE_URL","").strip();MIN_MATCHES=int(os.getenv("STYLE_FALLBACK_MIN_MATCHES","3"))

def j(v):return v if isinstance(v,dict) else {}
def promotion(conn,team,league)->Optional[Dict[str,Any]]:
    ct=canon(team)
    try:rows=conn.execute("SELECT team_name,transferred_relative FROM promotion_priors WHERE target_season='2627' AND parent_league_name=%s",(league,)).fetchall()
    except Exception:return None
    for name,rel in rows:
        if canon(name)==ct and isinstance(rel,dict) and rel:return {"metrics":{},"relative":rel,"top11_strength":None,"player_coverage":0,"source":"promotion-prior"}
    return None

def run_patch(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as c:
        latest=c.execute("""SELECT DISTINCT ON (event_id) event_id,snapshot_hour,league_name,home_team,away_team,home_style,away_style
          FROM fixture_enrichment_snapshots WHERE match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+INTERVAL '8 days' ORDER BY event_id,snapshot_hour DESC""").fetchall()
        by_league=defaultdict(set)
        for _e,_s,l,h,a,_hs,_as in latest:by_league[str(l)].update([str(h),str(a)])
        styles={};history_ready=promotion_ready=0
        for league,teams in by_league.items():
            hist=c.execute("""SELECT match_date,home_team,away_team,home_goals,away_goals,home_shots_on_target,away_shots_on_target,home_corners,away_corners
              FROM football_data_matches WHERE league_name=%s AND season_code IN ('2425','2526') UNION ALL
              SELECT match_date::date,home_team,away_team,home_goals,away_goals,home_shots_on_target,away_shots_on_target,home_corners,away_corners
              FROM espn_current_matches WHERE league_name=%s ORDER BY 1""",(league,league)).fetchall()
            raw={t:team_metrics(hist,t) for t in teams};baselines={}
            for key in ("corner_taken_team","ontarget_scoring_att_team","goals_team"):
                vals=[float(m[key]) for m in raw.values() if m.get(key) is not None and int(m.get("matches") or 0)>=MIN_MATCHES];baselines[key]=avg(vals)
            for team,m in raw.items():
                style=None
                if int(m.get("matches") or 0)>=MIN_MATCHES:
                    rel={}
                    for key in ("corner_taken_team","ontarget_scoring_att_team","goals_team"):
                        v,b=m.get(key),baselines.get(key)
                        if v is not None and b not in (None,0):rel[key]=max(.4,min(2.5,float(v)/float(b)))
                    if rel:
                        style={"metrics":{k:v for k,v in m.items() if k!="matches" and v is not None},"relative":rel,"top11_strength":None,"player_coverage":0,"source":"internal-team-name-history","matches":int(m.get("matches") or 0)};history_ready+=1
                if not style:
                    style=promotion(c,team,league)
                    if style:promotion_ready+=1
                styles[(league,canon(team))]=style
        patched=both=0
        for eid,snap,league,home,away,hs,as_ in latest:
            h=j(hs) or styles.get((str(league),canon(home)));a=j(as_) or styles.get((str(league),canon(away)))
            if not h and not a:continue
            hc,ac=corner_style(h),corner_style(a);csi=(hc+ac)/2 if hc is not None and ac is not None else (hc if hc is not None else ac)
            c.execute("UPDATE fixture_enrichment_snapshots SET home_style=%s,away_style=%s,corner_style_index=%s,built_at=NOW() WHERE event_id=%s AND snapshot_hour=%s",(Jsonb(h) if h else None,Jsonb(a) if a else None,csi,eid,snap));patched+=1;both+=int(bool(h and a))
        res={"status":"success","fixtures":len(latest),"patched":patched,"both_style":both,"history_ready_teams":history_ready,"promotion_prior_teams":promotion_ready};print("STYLE_COVERAGE_PATCH_RESULT",json.dumps(res,separators=(",",":")),flush=True);return res
if __name__=="__main__":print(json.dumps(run_patch(),indent=2))
