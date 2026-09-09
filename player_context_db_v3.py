#!/usr/bin/env python3
"""Fast cache-first DB-v3 Expected-XI and squad-continuity context builder.

DB-v3 reuses cached Understat player rows first, fetches only missing league/season
pages in parallel with bounded timeouts, preloads injury data once, and batch-writes
team context snapshots. A validation run is normally cache-first, but an empty cache
is allowed one bounded bootstrap fetch so the fail-closed safety gate can actually be
proven on a fresh database. This module never activates V5 itself.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
import requests
from psycopg.types.json import Jsonb

import understat_player_continuity_v2 as v2

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
CURRENT_SEASON=int(os.getenv("PLAYER_CONTEXT_CURRENT_SEASON","2026"))
PREVIOUS_SEASON=CURRENT_SEASON-1
LOOKAHEAD_DAYS=int(os.getenv("PLAYER_CONTEXT_LOOKAHEAD_DAYS","8"))
HTTP_TIMEOUT=float(os.getenv("PLAYER_CONTEXT_HTTP_TIMEOUT_SECONDS","8"))
MAX_WORKERS=max(1,min(10,int(os.getenv("PLAYER_CONTEXT_FETCH_WORKERS","5"))))
VALIDATION_MODE=bool(os.getenv("VALIDATION_TRIGGER_TOKEN","").strip())
NETWORK_CONFIGURED=os.getenv("PLAYER_CONTEXT_ALLOW_NETWORK","true").lower() in {"1","true","yes"}
LEAGUES=v2.LEAGUES


def progress(phase:str,**extra:Any)->None:
    print("PLAYER_CONTEXT_DB_V3_PROGRESS",json.dumps({"phase":phase,**extra},ensure_ascii=False,separators=(",",":")),flush=True)


def load_cached(conn,season:int)->Dict[str,Tuple[str,List[Dict[str,Any]]]]:
    grouped:Dict[str,List[Dict[str,Any]]]=defaultdict(list);labels:Dict[str,str]={}
    try:
        rows=conn.execute("""SELECT team_name,player_id,player_name,games,starts,minutes,goals,xg,assists,xa,xgchain,xgbuildup,raw
                           FROM understat_player_seasons WHERE season=%s""",(season,)).fetchall()
    except Exception:
        return {}
    for r in rows:
        label=str(r[0]);key=v2.canon(label);labels[key]=label
        grouped[key].append({"player_id":str(r[1]),"player_name":r[2],"games":r[3],"starts":r[4],"minutes":r[5],
                             "goals":r[6],"xg":r[7],"assists":r[8],"xa":r[9],"xgchain":r[10],"xgbuildup":r[11],
                             "raw":r[12] if isinstance(r[12],dict) else {},"team_title":label})
    return {k:(labels[k],vals) for k,vals in grouped.items() if vals}


def fetch_memory(code:str,season:int)->Dict[str,Tuple[str,List[Dict[str,Any]]]]:
    s=requests.Session();s.headers.update({"User-Agent":"Mozilla/5.0 Chrome/152 Safari/537.36","Accept":"text/html,application/xhtml+xml"})
    r=s.get(f"{v2.BASE}/league/{code}/{season}",timeout=HTTP_TIMEOUT)
    if r.status_code!=200:raise RuntimeError(f"Understat HTTP {r.status_code}: {code}/{season}")
    raw_rows=v2.extract_players_data(r.text)
    if not raw_rows:raise RuntimeError(f"Understat playersData empty: {code}/{season}")
    buckets:Dict[str,List[Dict[str,Any]]]=defaultdict(list);labels:Dict[str,str]={}
    for raw in raw_rows:
        label=v2.team_title(raw);p=v2.normalized_player(raw)
        if not label or not p:continue
        key=v2.canon(label);labels[key]=label;buckets[key].append(p)
    if not buckets:raise RuntimeError(f"Understat no team rows: {code}/{season}")
    return {k:(labels[k],vals) for k,vals in buckets.items() if vals}


def persist_cache(conn,season:int,available:Dict[str,Tuple[str,List[Dict[str,Any]]]])->int:
    rows=[]
    for _key,(label,players) in available.items():
        slug=v2.team_slug(label)
        for p in players:
            rows.append((season,label,slug,p["player_id"],p.get("player_name"),p.get("games"),p.get("starts"),p.get("minutes"),p.get("goals"),p.get("xg"),p.get("assists"),p.get("xa"),p.get("xgchain"),p.get("xgbuildup"),Jsonb(p.get("raw") or {})))
    if not rows:return 0
    sql="""INSERT INTO understat_player_seasons(season,team_name,team_slug,player_id,player_name,games,starts,minutes,goals,xg,assists,xa,xgchain,xgbuildup,raw)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(season,team_slug,player_id) DO UPDATE SET team_name=EXCLUDED.team_name,player_name=EXCLUDED.player_name,
             games=EXCLUDED.games,starts=EXCLUDED.starts,minutes=EXCLUDED.minutes,goals=EXCLUDED.goals,xg=EXCLUDED.xg,
             assists=EXCLUDED.assists,xa=EXCLUDED.xa,xgchain=EXCLUDED.xgchain,xgbuildup=EXCLUDED.xgbuildup,raw=EXCLUDED.raw,fetched_at=NOW()"""
    with conn.cursor() as cur:cur.executemany(sql,rows)
    return len(rows)


def all_mapped(names:set[str],available:Dict[str,Tuple[str,List[Dict[str,Any]]]])->bool:
    return bool(names) and all(v2.match_team(name,available) is not None for name in names)


def injury_map(conn)->Dict[str,List[Dict[str,Any]]]:
    out:Dict[str,List[Dict[str,Any]]]=defaultdict(list)
    try:
        rows=conn.execute("""SELECT home_team,away_team,home_injured_players,away_injured_players
                           FROM fotmob_fixture_availability_snapshots
                           WHERE snapshot_hour=(SELECT MAX(snapshot_hour) FROM fotmob_fixture_availability_snapshots)""").fetchall()
    except Exception:return {}
    for h,a,hi,ai in rows:
        if isinstance(hi,list):out[v2.canon(h)].extend(x for x in hi if isinstance(x,dict))
        if isinstance(ai,list):out[v2.canon(a)].extend(x for x in ai if isinstance(x,dict))
    return out


def build_context(team:str,cur:List[Dict[str,Any]],prev:List[Dict[str,Any]],injuries:Dict[str,List[Dict[str,Any]]])->Dict[str,Any]:
    injury_objs=injuries.get(v2.canon(team),[]);injured_names={v2.canon(x.get("name")) for x in injury_objs if x.get("name")}
    cur_scores=v2.player_scores(cur);prev_scores=v2.player_scores(prev)
    cur_by={v2.canon(r.get("player_name")):r for r in cur if r.get("player_name")};prev_by={v2.canon(r.get("player_name")):r for r in prev if r.get("player_name")}
    cur_games=max([float(r.get("games") or 0) for r in cur] or [0.0]);w=max(.20,min(.72,.20+cur_games/20.0*.52));merged=[]
    for name in set(cur_by)|set(prev_by):
        cr,pr=cur_by.get(name),prev_by.get(name);cs=cur_scores.get((cr or {}).get("player_id","")) if cr else None;ps=prev_scores.get((pr or {}).get("player_id","")) if pr else None
        if cr is not None and pr is None:score=w*(cs if cs is not None else .5)+(1-w)*.45
        elif pr is not None and cr is None:score=w*.35+(1-w)*(ps if ps is not None else .5)
        else:score=w*(cs if cs is not None else .5)+(1-w)*(ps if ps is not None else .5)
        row=cr or pr or {};merged.append({"name":name,"label":row.get("player_name"),"score":float(score),"minutes":float(row.get("minutes") or 0),"starts":float(row.get("starts") or 0),"injured":name in injured_names})
    top=sorted(merged,key=lambda x:(x["starts"],x["minutes"],x["score"]),reverse=True)[:11];denom=sum(x["score"] for x in top) or 1.0
    impact=min(.55,sum(x["score"] for x in merged if x["injured"])/denom);avail=sorted((x for x in merged if not x["injured"]),key=lambda x:(x["starts"],x["minutes"],x["score"]),reverse=True)[:11]
    expected=sum(x["score"] for x in avail)/len(avail) if avail else None;top11=sum(x["score"] for x in top)/len(top) if top else None
    prev_total=sum(float(r.get("minutes") or 0) for r in prev);retained=sum(float(r.get("minutes") or 0) for n,r in prev_by.items() if n in cur_by);retained_share=retained/prev_total if prev_total>0 else None
    prev_starters={n for n,r in sorted(prev_by.items(),key=lambda kv:(float(kv[1].get("starts") or 0),float(kv[1].get("minutes") or 0)),reverse=True)[:11]};continuity=len(prev_starters&set(cur_by))/len(prev_starters) if prev_starters else None
    gk=any("goal" in str(x.get("position") or "").lower() or str(x.get("position") or "").lower()=="gk" for x in injury_objs)
    key_abs=sorted((x for x in merged if x["injured"]),key=lambda x:x["score"],reverse=True)[:6]
    coverage=min(1.0,.60*min(1.0,len(cur)/18.0)+.40*min(1.0,len(prev)/18.0)) if prev else min(.65,.65*min(1.0,len(cur)/18.0))
    return {"expected":expected,"top11":top11,"impact":impact,"gk":gk,"retained":retained_share,"continuity":continuity,"coverage":coverage,"key":[{"name":x["label"],"importance":round(x["score"],4)} for x in key_abs],"current_players":len(cur),"previous_players":len(prev),"current_weight":round(w,3)}


def run_import(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    progress("connect",validation_mode=VALIDATION_MODE,network_configured=NETWORK_CONFIGURED)
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(v2.SCHEMA);rid=conn.execute("INSERT INTO player_context_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        try:
            upcoming=conn.execute("""SELECT DISTINCT league_name,home_team,away_team FROM espn_upcoming
                                     WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours'
                                     AND match_date<=NOW()+(%s||' days')::interval""",(LOOKAHEAD_DAYS,)).fetchall()
            by_league:Dict[str,set[str]]=defaultdict(set)
            for league,home,away in upcoming:by_league[str(league)].update((str(home),str(away)))
            progress("upcoming_loaded",fixtures_teams=sum(len(v) for v in by_league.values()),leagues=len(by_league))
            current_cache=load_cached(conn,CURRENT_SEASON);previous_cache=load_cached(conn,PREVIOUS_SEASON);code_by_name={name:code for code,name in LEAGUES};errors={};sources={}
            bootstrap=not current_cache and not previous_cache
            allow_network=NETWORK_CONFIGURED and (not VALIDATION_MODE or bootstrap)
            progress("cache_loaded",current_cache_teams=len(current_cache),previous_cache_teams=len(previous_cache),bootstrap=bootstrap,allow_network=allow_network)
            jobs=[]
            for league,names in by_league.items():
                code=code_by_name.get(league)
                if not code:errors[f"{league}:league"]="unsupported";continue
                if not all_mapped(names,current_cache):jobs.append((league,code,CURRENT_SEASON))
                else:sources[f"{league}:{CURRENT_SEASON}"]="postgres-cache"
                if not all_mapped(names,previous_cache):jobs.append((league,code,PREVIOUS_SEASON))
                else:sources[f"{league}:{PREVIOUS_SEASON}"]="postgres-cache"
            progress("fetch_plan",jobs=len(jobs),network_enabled=allow_network)
            fetched_by_season:Dict[int,Dict[str,Tuple[str,List[Dict[str,Any]]]]]={CURRENT_SEASON:{},PREVIOUS_SEASON:{}}
            if jobs and allow_network:
                with ThreadPoolExecutor(max_workers=min(MAX_WORKERS,len(jobs))) as ex:
                    futs={ex.submit(fetch_memory,code,season):(league,season) for league,code,season in jobs}
                    for fut in as_completed(futs):
                        league,season=futs[fut]
                        try:
                            fresh=fut.result();(current_cache if season==CURRENT_SEASON else previous_cache).update(fresh);fetched_by_season[season].update(fresh);sources[f"{league}:{season}"]="network-memory"
                        except Exception as exc:errors[f"{league}:{season}"]=str(exc)[:300];sources[f"{league}:{season}"]="failed-network"
                persisted=persist_cache(conn,CURRENT_SEASON,fetched_by_season[CURRENT_SEASON])+persist_cache(conn,PREVIOUS_SEASON,fetched_by_season[PREVIOUS_SEASON])
                progress("cache_persisted",player_rows=persisted)
            elif jobs:
                for league,_code,season in jobs:sources[f"{league}:{season}"]="cache-miss-network-disabled"
            progress("fetch_complete",errors=len(errors),current_cache_teams=len(current_cache),previous_cache_teams=len(previous_cache))
            injuries=injury_map(conn);hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0);params=[];teams=current=previous=mapped=current_only=0;coverages=[]
            sql="""INSERT INTO player_team_context_snapshots(team_name,snapshot_hour,current_season,previous_season,expected_xi_strength,top11_strength,injury_impact,goalkeeper_injured,retained_minutes_share,starter_continuity,player_coverage,key_absences,source_meta)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(team_name,snapshot_hour) DO UPDATE SET current_season=EXCLUDED.current_season,previous_season=EXCLUDED.previous_season,expected_xi_strength=EXCLUDED.expected_xi_strength,top11_strength=EXCLUDED.top11_strength,injury_impact=EXCLUDED.injury_impact,goalkeeper_injured=EXCLUDED.goalkeeper_injured,retained_minutes_share=EXCLUDED.retained_minutes_share,starter_continuity=EXCLUDED.starter_continuity,player_coverage=EXCLUDED.player_coverage,key_absences=EXCLUDED.key_absences,source_meta=EXCLUDED.source_meta"""
            for league,names in by_league.items():
                for team in sorted(names):
                    teams+=1;cm=v2.match_team(team,current_cache);pm=v2.match_team(team,previous_cache);cur=cm[1] if cm else [];prev=pm[1] if pm else [];current+=int(bool(cur));previous+=int(bool(prev));mapped+=int(bool(cur or prev));current_only+=int(bool(cur and not prev));ctx=build_context(team,cur,prev,injuries);coverages.append(float(ctx["coverage"] or 0))
                    meta={"source":"db-v3-fast-cache-first","league":league,"current_source":sources.get(f"{league}:{CURRENT_SEASON}"),"previous_source":sources.get(f"{league}:{PREVIOUS_SEASON}"),"current_match_score":round(cm[2],4) if cm else None,"previous_match_score":round(pm[2],4) if pm else None,"current_players":ctx["current_players"],"previous_players":ctx["previous_players"],"current_weight":ctx["current_weight"]}
                    params.append((team,hour,CURRENT_SEASON,PREVIOUS_SEASON,ctx["expected"],ctx["top11"],ctx["impact"],ctx["gk"],ctx["retained"],ctx["continuity"],ctx["coverage"],Jsonb(ctx["key"]),Jsonb(meta)))
            progress("contexts_built",teams=teams,current=current,previous=previous,mapped=mapped,rows=len(params))
            if params:
                with conn.cursor() as cur:cur.executemany(sql,params)
            progress("contexts_written",rows=len(params))
            avg=round(sum(coverages)/len(coverages),4) if coverages else 0.0;status="success" if current>0 and mapped>0 else "failed";message={"source":"db-v3-fast-cache-first","mapped":mapped,"current_only":current_only,"avg_coverage":avg,"errors":errors,"sources":sources,"bootstrap":bootstrap}
            conn.execute("""UPDATE player_context_runs SET finished_at=NOW(),status=%s,teams=%s,teams_with_current=%s,teams_with_previous=%s,http_calls=%s,message=%s WHERE id=%s""",(status,teams,current,previous,(len(jobs) if allow_network else 0),json.dumps(message,separators=(",",":")),rid))
            result={"status":status,"teams":teams,"mapped":mapped,"current":current,"previous":previous,"current_only":current_only,"http_calls":len(jobs) if allow_network else 0,"avg_coverage":avg,"errors":errors,"validation_mode":VALIDATION_MODE,"bootstrap":bootstrap}
            print("PLAYER_CONTEXT_DB_V3_RESULT",json.dumps(result,ensure_ascii=False,separators=(",",":")),flush=True)
            if status!="success":raise RuntimeError(f"DB-v3 player context failed closed: mapped={mapped}, current={current}")
            return result
        except Exception as exc:
            try:conn.execute("UPDATE player_context_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:700],rid))
            except Exception:pass
            raise

if __name__=="__main__":print(json.dumps(run_import(),ensure_ascii=False,indent=2))
