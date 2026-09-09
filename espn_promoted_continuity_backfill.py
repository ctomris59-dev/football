#!/usr/bin/env python3
"""Targeted ESPN exact-XI backfill for promoted Big-Five teams.

Continuity must follow the club across promotion. A newly promoted 2026/27 club has
its relevant 2025/26 XI history in the second tier, not in the Big Five archive.
Likewise, the leakage-safe 2025/26 backtest needs 2024/25 second-tier XI history for
clubs promoted into that test season.

This module discovers all second-tier scoreboards but fetches expensive match summaries
ONLY for target promoted clubs. Explicit ESPN starter flags are archived into the same
historical tables/shared real player cache as the Big-Five importer. No minutes are
fabricated.
"""
from __future__ import annotations
import json, os, time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
import psycopg
from psycopg.types.json import Jsonb
import espn_historical_lineups_importer as base
import understat_player_continuity_v2 as v2

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
MAX_SUMMARIES=int(os.getenv("ESPN_PROMOTED_MAX_SUMMARIES_PER_RUN","1000"))
TARGET_COVERAGE=float(os.getenv("ESPN_PROMOTED_TARGET_COVERAGE","0.95"))
SECOND_TIERS={
 "Premier League":("eng.2","English Championship"),
 "La Liga":("esp.2","Spanish Segunda División"),
 "Serie A":("ita.2","Italian Serie B"),
 "Bundesliga":("ger.2","German 2. Bundesliga"),
 "Ligue 1":("fra.2","French Ligue 2"),
}
SEASONS={2024:("2024-08-01","2025-06-15"),2025:("2025-08-01","2026-06-15")}


def current_promoted(conn)->Dict[str,Set[str]]:
    out={k:set() for k in SECOND_TIERS}
    # Preferred: validated promotion-prior table for target 2026/27.
    try:
        for team,parent in conn.execute("SELECT team_name,parent_league_name FROM promotion_priors WHERE target_season='2627'").fetchall():
            if str(parent) in out:out[str(parent)].add(v2.canon(team))
    except Exception:pass
    # Deterministic fallback: active 2026/27 teams absent from 2025/26 Big-Five history.
    for league in out:
        try:
            previous={v2.canon(x[0]) for x in conn.execute("""SELECT home_team FROM football_data_matches WHERE season_code='2526' AND league_name=%s
              UNION SELECT away_team FROM football_data_matches WHERE season_code='2526' AND league_name=%s""",(league,league)).fetchall()}
            current={v2.canon(x[0]) for x in conn.execute("""SELECT home_team FROM espn_upcoming WHERE is_current=TRUE AND league_name=%s
              UNION SELECT away_team FROM espn_upcoming WHERE is_current=TRUE AND league_name=%s""",(league,league)).fetchall()}
            out[league].update(x for x in current if x and x not in previous)
        except Exception:pass
    return out


def backtest_promoted(conn)->Dict[str,Set[str]]:
    out={k:set() for k in SECOND_TIERS}
    for league in out:
        try:
            prev={v2.canon(x[0]) for x in conn.execute("""SELECT home_team FROM football_data_matches WHERE season_code='2425' AND league_name=%s
              UNION SELECT away_team FROM football_data_matches WHERE season_code='2425' AND league_name=%s""",(league,league)).fetchall()}
            test={v2.canon(x[0]) for x in conn.execute("""SELECT home_team FROM football_data_matches WHERE season_code='2526' AND league_name=%s
              UNION SELECT away_team FROM football_data_matches WHERE season_code='2526' AND league_name=%s""",(league,league)).fetchall()}
            out[league].update(x for x in test if x and x not in prev)
        except Exception:pass
    return out


def month_windows(start_s:str,end_s:str):
    start=date.fromisoformat(start_s);end=date.fromisoformat(end_s);cur=start
    while cur<=end:
        nxt=(cur.replace(day=28)+timedelta(days=4)).replace(day=1)
        yield cur,min(end,nxt-timedelta(days=1));cur=nxt


def target_state(conn,season:int,league_slug:str,targets:Set[str])->Dict[str,int|float]:
    if not targets:return {"events":0,"success":0,"pending":0,"coverage":1.0}
    rows=conn.execute("SELECT home_team,away_team,summary_status FROM espn_historical_events WHERE season=%s AND league_slug=%s",(season,league_slug)).fetchall()
    vals=[r for r in rows if v2.canon(r[0]) in targets or v2.canon(r[1]) in targets]
    ok=sum(1 for r in vals if r[2]=='success');n=len(vals)
    return {"events":n,"success":ok,"pending":n-ok,"coverage":round(ok/n,4) if n else 0.0}


def run_import(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    i=base.Importer(db);out={};total_summary_calls=0
    old_season=base.SEASON
    try:
        target_sets={2024:backtest_promoted(i.conn),2025:current_promoted(i.conn)}
        for season,(start_s,end_s) in SEASONS.items():
            base.SEASON=season;season_out={}
            for parent,(slug,lname) in SECOND_TIERS.items():
                targets=target_sets[season].get(parent,set())
                before=target_state(i.conn,season,slug,targets)
                if not targets:
                    season_out[parent]={"status":"no_targets","targets":[]};continue
                if before["events"]>=max(10,len(targets)*20) and float(before["coverage"])>=TARGET_COVERAGE:
                    season_out[parent]={"status":"complete_skip","targets":sorted(targets),"state":before};continue
                discovered=0
                for start,end in month_windows(start_s,end_s):
                    payload=i.get_json(f"{base.SITE_BASE}/{slug}/scoreboard",{"dates":f"{start:%Y%m%d}-{end:%Y%m%d}","limit":200})
                    events=payload.get("events") if isinstance(payload.get("events"),list) else []
                    for event in events:
                        if not isinstance(event,dict):continue
                        pair=base.event_pair(event);eid=str(event.get("id") or "");match_dt=base.dt(event.get("date"))
                        status_obj=((event.get("status") or {}).get("type") or {}) if isinstance(event.get("status"),dict) else {}
                        if not pair or not eid or not match_dt or not bool(status_obj.get("completed")):continue
                        home,away=pair;hn=base.team_name(home);an=base.team_name(away)
                        if v2.canon(hn) not in targets and v2.canon(an) not in targets:continue
                        hid=str(home.get("id") or (home.get("team") or {}).get("id") or "");aid=str(away.get("id") or (away.get("team") or {}).get("id") or "")
                        i.conn.execute("""INSERT INTO espn_historical_events(event_id,season,league_slug,league_name,match_date,home_team_id,home_team,away_team_id,away_team,completed,scoreboard_raw)
                          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s) ON CONFLICT(event_id) DO UPDATE SET season=EXCLUDED.season,league_slug=EXCLUDED.league_slug,
                          league_name=EXCLUDED.league_name,match_date=EXCLUDED.match_date,home_team_id=EXCLUDED.home_team_id,home_team=EXCLUDED.home_team,
                          away_team_id=EXCLUDED.away_team_id,away_team=EXCLUDED.away_team,completed=TRUE,scoreboard_raw=EXCLUDED.scoreboard_raw,updated_at=NOW()""",
                          (eid,season,slug,lname,match_dt,hid or None,hn,aid or None,an,Jsonb(event)));discovered+=1
                pending=i.conn.execute("""SELECT event_id,home_team_id,away_team_id FROM espn_historical_events WHERE season=%s AND league_slug=%s AND summary_status<>'success' ORDER BY match_date""",(season,slug)).fetchall()
                pending=[r for r in pending if any(v2.canon(x) in targets for x in i.conn.execute("SELECT home_team,away_team FROM espn_historical_events WHERE event_id=%s",(r[0],)).fetchone())][:MAX_SUMMARIES]
                both=starter_rows=0;errors={}
                for eid,hid,aid in pending:
                    try:
                        summary=i.get_json(f"{base.SITE_BASE}/{slug}/summary",{"event":str(eid)});total_summary_calls+=1
                        groups=base.extract_starters(summary);hc=len((groups.get(str(hid)) or {}).get("players",{})) if hid else 0;ac=len((groups.get(str(aid)) or {}).get("players",{})) if aid else 0
                        valid=hc>=10 and ac>=10;i.conn.execute("DELETE FROM espn_historical_lineup_players WHERE event_id=%s",(str(eid),))
                        for tid,g in groups.items():
                            for p in g["players"].values():
                                i.conn.execute("""INSERT INTO espn_historical_lineup_players(event_id,team_id,team_name,player_id,player_name,starter,source_path,raw)
                                  VALUES(%s,%s,%s,%s,%s,TRUE,%s,%s) ON CONFLICT(event_id,team_id,player_id) DO UPDATE SET player_name=EXCLUDED.player_name,starter=TRUE,source_path=EXCLUDED.source_path,raw=EXCLUDED.raw,fetched_at=NOW()""",
                                  (str(eid),tid,g["team_name"],p["player_id"],p["player_name"],p["source_path"],Jsonb(p["raw"])));starter_rows+=1
                        i.conn.execute("UPDATE espn_historical_events SET summary_status=%s,home_starters=%s,away_starters=%s,summary_raw=%s,updated_at=NOW() WHERE event_id=%s",('success' if valid else 'no_complete_lineup',hc,ac,Jsonb(summary),str(eid)));both+=int(valid)
                    except Exception as exc:
                        errors[str(eid)]=str(exc)[:180];i.conn.execute("UPDATE espn_historical_events SET summary_status='failed',updated_at=NOW() WHERE event_id=%s",(str(eid),))
                # Aggregate all exact starts for this season into the shared real cache.
                i.aggregate_cache();after=target_state(i.conn,season,slug,targets)
                season_out[parent]={"status":"success" if after["events"] else "empty","targets":sorted(targets),"discovered":discovered,"both_this_run":both,"starter_rows_this_run":starter_rows,"before":before,"after":after,"errors":errors}
            out[str(season)]=season_out
        result={"status":"success","summary_calls":total_summary_calls,"seasons":out}
        print("ESPN_PROMOTED_CONTINUITY_RESULT",json.dumps(result,separators=(",",":")),flush=True);return result
    finally:
        base.SEASON=old_season;i.close()

if __name__=="__main__":print(json.dumps(run_import(),indent=2))
