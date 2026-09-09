#!/usr/bin/env python3
"""Build a data-informed expected-XI proxy from real usage/history.

Inputs are all pre-match safe/current-source observations:
- current ESPN roster snapshots;
- explicit starters from completed 2026/27 ESPN summaries;
- previous-season real activity from FBref Starts+Minutes when reachable;
- otherwise the shared real cache, especially ESPN 2025/26 exact starter counts;
- current FotMob injury names.

This remains an expected-XI proxy, never a confirmed lineup. It never fabricates
minutes or starts and remains shadow-only until leakage-safe validation activates it.
"""
from __future__ import annotations
import json, os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import psycopg
from psycopg.types.json import Jsonb
import understat_player_continuity_v2 as v2
import espn_team_roster_player_bridge as roster_base
from espn_historical_lineups_importer import extract_starters

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
MIN_KNOWN=int(os.getenv("EXPECTED_XI_MIN_KNOWN_PLAYERS","7"))


def pct(vals:Dict[str,float])->Dict[str,float]:
    s=sorted(vals.items(),key=lambda x:x[1]);n=len(s)
    return {k:(i+.5)/n for i,(k,_v) in enumerate(s)} if n else {}


def previous_activity(c)->tuple[Dict[str,Dict[str,Any]],Dict[str,int]]:
    """Real 2025/26 activity by canonical player; FBref enriches but is not required."""
    prev:Dict[str,Dict[str,Any]]={};counts={"shared_rows":0,"fbref_rows":0,"espn_exact_start_rows":0}

    def add(team,name,starts,minutes,source,priority):
        k=v2.canon(name)
        if not k:return
        s=float(starts or 0);m=float(minutes or 0)
        if s<=0 and m<=0:return
        cur=prev.get(k)
        if not cur:
            prev[k]={"team":str(team),"starts":s,"minutes":m,"sources":{str(source)},"priority":priority}
            return
        cur["sources"].add(str(source))
        # Never let a lower-priority cache erase richer FBref values. Exact ESPN starts
        # may still raise starts because they are counted from explicit starter flags.
        cur["starts"]=max(float(cur.get("starts") or 0),s)
        cur["minutes"]=max(float(cur.get("minutes") or 0),m)
        cur["priority"]=max(int(cur.get("priority") or 0),priority)
        if priority>=int(cur.get("priority") or 0):cur["team"]=str(team)

    try:
        rows=c.execute("SELECT team_name,player_name,starts,minutes,raw FROM understat_player_seasons WHERE season=2025 AND (COALESCE(starts,0)>0 OR COALESCE(minutes,0)>0)").fetchall()
        for team,name,starts,mins,raw in rows:
            r=raw if isinstance(raw,dict) else {};src=str(r.get("source") or "shared-real-cache")
            add(team,name,starts,mins,src,60);counts["shared_rows"]+=1
            if "espn-historical-lineups" in src:counts["espn_exact_start_rows"]+=1
    except Exception:pass

    try:
        rows=c.execute("SELECT team_name,player_name,starts,minutes,source_method FROM fbref_player_season_stats WHERE season=2025 AND (COALESCE(starts,0)>0 OR COALESCE(minutes,0)>0)").fetchall()
        for team,name,starts,mins,method in rows:
            add(team,name,starts,mins,f"fbref:{method}",100);counts["fbref_rows"]+=1
    except Exception:pass

    for p in prev.values():p["sources"]=sorted(p["sources"])
    return prev,counts


def run_build(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as c:
        prev,prev_counts=previous_activity(c)

        # Explicit current-season starts from completed ESPN summaries.
        current=defaultdict(Counter);matches=defaultdict(set)
        try:
            rows=c.execute("SELECT event_id,summary_raw FROM espn_current_matches WHERE summary_raw IS NOT NULL AND match_date>=DATE '2026-07-01'").fetchall()
            for eid,raw in rows:
                if not isinstance(raw,dict):continue
                for _tid,g in extract_starters(raw).items():
                    team=str(g.get("team_name") or "");ct=v2.canon(team)
                    for p in g.get("players",{}).values():
                        name=p.get("player_name")
                        if name:current[ct][v2.canon(name)]+=1
                    if g.get("players"):matches[ct].add(str(eid))
        except Exception:pass

        targets=c.execute("""SELECT DISTINCT home_team FROM espn_upcoming WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+INTERVAL '8 days'
          UNION SELECT DISTINCT away_team FROM espn_upcoming WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+INTERVAL '8 days'""").fetchall()
        updated=data_ready=starts_only_ready=minutes_ready=0

        for (team,) in targets:
            team=str(team);ct=v2.canon(team)
            snap=c.execute("SELECT raw FROM espn_team_roster_snapshots WHERE team_name=%s ORDER BY snapshot_date DESC LIMIT 1",(team,)).fetchone()
            if not snap or not isinstance(snap[0],dict):continue
            players=roster_base.extract_players(snap[0]);names=[roster_base.pname(p) for p in players if roster_base.pname(p)]
            if not names:continue
            injured,_objs=v2.injury_names(c,team)
            cur_counts={v2.canon(n):float(current[ct].get(v2.canon(n),0)) for n in names}
            prev_starts={v2.canon(n):float((prev.get(v2.canon(n)) or {}).get("starts",0)) for n in names}
            prev_minutes={v2.canon(n):float((prev.get(v2.canon(n)) or {}).get("minutes",0)) for n in names}
            pc,ps,pm=pct(cur_counts),pct(prev_starts),pct(prev_minutes)
            has_hist_minutes=any(v>0 for v in prev_minutes.values())
            has_hist_starts=any(v>0 for v in prev_starts.values())
            ranked=[]
            for n in names:
                k=v2.canon(n);known=cur_counts.get(k,0)>0 or prev_starts.get(k,0)>0 or prev_minutes.get(k,0)>0
                if not known:continue
                current_weight=.62 if len(matches[ct])>=2 else .40 if len(matches[ct])==1 else 0.0
                hist_weight=1-current_weight
                if has_hist_minutes:
                    hist_score=.60*ps.get(k,.5)+.40*pm.get(k,.5)
                else:
                    hist_score=ps.get(k,.5)
                score=current_weight*pc.get(k,.5)+hist_weight*hist_score
                ranked.append({"name":n,"key":k,"score":score,"current_starts":cur_counts.get(k,0),"previous_starts":prev_starts.get(k,0),"previous_minutes":prev_minutes.get(k,0),"injured":k in injured})
            available=sorted([x for x in ranked if not x["injured"]],key=lambda x:(x["current_starts"],x["score"],x["previous_starts"],x["previous_minutes"]),reverse=True)[:11]
            top_all=sorted(ranked,key=lambda x:(x["current_starts"],x["score"],x["previous_starts"],x["previous_minutes"]),reverse=True)[:11]
            informed=len(ranked)>=MIN_KNOWN and len(available)>=MIN_KNOWN
            expected=sum(x["score"] for x in available)/len(available) if informed else None
            top11=sum(x["score"] for x in top_all)/len(top_all) if len(top_all)>=MIN_KNOWN else None
            row=c.execute("SELECT source_meta FROM player_team_context_snapshots WHERE team_name=%s ORDER BY snapshot_hour DESC LIMIT 1",(team,)).fetchone()
            if not row:continue
            meta=row[0] if isinstance(row[0],dict) else {}
            method="current-explicit-starts+previous-exact-starts"
            if has_hist_minutes:method+="+real-minutes"
            method+="+current-injuries"
            historical_sources=sorted({s for n in names for s in (prev.get(v2.canon(n)) or {}).get("sources",[])})
            meta={**meta,"expected_xi_data_informed":bool(informed),"expected_xi_method":method,"current_lineup_matches":len(matches[ct]),"known_ranked_players":len(ranked),"expected_xi_names":[x["name"] for x in available],"confirmed_lineup":False,"neutral_strength_interface":not informed,"historical_activity_sources":historical_sources,"historical_minutes_available":bool(has_hist_minutes),"historical_starts_available":bool(has_hist_starts)}
            c.execute("""UPDATE player_team_context_snapshots SET expected_xi_strength=%s,top11_strength=%s,source_meta=%s
              WHERE team_name=%s AND snapshot_hour=(SELECT MAX(snapshot_hour) FROM player_team_context_snapshots WHERE team_name=%s)""",
              (expected if informed else .50,top11 if top11 is not None else .50,Jsonb(meta),team,team))
            updated+=1;data_ready+=int(informed);starts_only_ready+=int(informed and has_hist_starts and not has_hist_minutes);minutes_ready+=int(informed and has_hist_minutes)

        res={"status":"success","teams_seen":len(targets),"updated":updated,"data_informed_expected_xi":data_ready,"starts_only_expected_xi":starts_only_ready,"minutes_enriched_expected_xi":minutes_ready,"current_usage_teams":sum(1 for x in matches.values() if x),"previous_global_players":len(prev),"previous_source_rows":prev_counts}
        print("EXPECTED_XI_USAGE_RESULT",json.dumps(res,separators=(",",":")),flush=True);return res

if __name__=="__main__":print(json.dumps(run_build(),indent=2))
