#!/usr/bin/env python3
"""Fast readiness v4 for the production V1 selection engine.

This preserves the v4 readiness rules but removes the old N+1 database pattern.
Historical match/corner/xG rows are loaded once, evaluated in memory, and readiness
snapshots are batch-written. xG remains diagnostic rather than a production blocker.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

from data_readiness_audit import SCHEMA, sim

DATABASE_URL=os.getenv("DATABASE_URL","").strip()
ODDS_MAX_AGE_HOURS=float(os.getenv("READINESS_ODDS_MAX_AGE_HOURS","12"))

ALTER_SQL="""
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS current_injury_report_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS match_specific_availability_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS availability_confirmed_current BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS availability_source TEXT;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS fotmob_home_injuries INTEGER;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS fotmob_away_injuries INTEGER;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS bbs_lineup_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS sofascore_confirmed BOOLEAN;
ALTER TABLE prediction_readiness_snapshots ADD COLUMN IF NOT EXISTS odds_snapshot_age_hours DOUBLE PRECISION;
"""


def _date(v:Any):
    return v.date() if isinstance(v,datetime) else v


def run_audit(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(SCHEMA);conn.execute(ALTER_SQL)
        rid=conn.execute("INSERT INTO data_readiness_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        try:
            cur=conn.execute("""SELECT DISTINCT ON(event_id)
              event_id,snapshot_hour,match_date,league_name,home_team,away_team,
              home_days_rest,away_days_rest,lineup_entries,roster_entries,
              has_ou25,has_btts,has_corner85,odds_snapshot_age_hours,
              current_injury_report_present,match_specific_availability_present,
              availability_confirmed_current,availability_source,
              fotmob_home_injuries,fotmob_away_injuries,bbs_lineup_present,sofascore_confirmed
              FROM prematch_feature_snapshots ORDER BY event_id,snapshot_hour DESC""")
            fixtures=cur.fetchall()
            leagues=sorted({str(r[3]) for r in fixtures})
            print("DATA_READINESS_V4_PROGRESS",json.dumps({"phase":"fixtures","n":len(fixtures),"leagues":len(leagues)},separators=(",",":")),flush=True)

            fd=defaultdict(list);espn=defaultdict(list);xg=defaultdict(list)
            for league,dt,h,a,tc in conn.execute("""SELECT league_name,match_date,home_team,away_team,total_corners
                                                     FROM football_data_matches ORDER BY league_name,match_date""").fetchall():
                fd[str(league)].append((_date(dt),str(h),str(a),tc))
            for league,dt,h,a,tc in conn.execute("""SELECT league_name,match_date,home_team,away_team,total_corners
                                                     FROM espn_current_matches ORDER BY league_name,match_date""").fetchall():
                espn[str(league)].append((_date(dt),str(h),str(a),tc))
            try:
                xg_rows=conn.execute("""SELECT league_name,match_date,home_team,away_team FROM understat_matches
                                      WHERE is_result=TRUE AND home_xg IS NOT NULL AND away_xg IS NOT NULL
                                      ORDER BY league_name,match_date""").fetchall()
            except Exception:xg_rows=[]
            for league,dt,h,a in xg_rows:xg[str(league)].append((_date(dt),str(h),str(a)))
            print("DATA_READINESS_V4_PROGRESS",json.dumps({"phase":"history","fd":sum(map(len,fd.values())),"espn":sum(map(len,espn.values())),"xg":sum(map(len,xg.values()))},separators=(",",":")),flush=True)

            def hist(team:str,league:str,before)->Tuple[int,int]:
                bd=_date(before);n=c=0
                for dt,h,a,tc in fd.get(league,[]):
                    if dt>=bd:break
                    if max(sim(team,h),sim(team,a))>=.78:n+=1;c+=int(tc is not None)
                for dt,h,a,tc in espn.get(league,[]):
                    if dt>=bd:break
                    if max(sim(team,h),sim(team,a))>=.78:n+=1;c+=int(tc is not None)
                return n,c
            def xgc(team:str,league:str,before)->int:
                bd=_date(before);n=0
                for dt,h,a in xg.get(league,[]):
                    if dt>=bd:break
                    if max(sim(team,h),sim(team,a))>=.78:n+=1
                return n

            params=[];goals_ready=btts_ready=corners_ready=final_ready=injury_ready=match_specific=confirmed=xg_ready=0
            for row in fixtures:
                (eid,hour,dt,league,home,away,hr,ar,lineup,roster,has_ou,has_btts,has_corner,odds_age,
                 current_injury,match_specific_present,confirmed_current,source,home_inj,away_inj,bbs_present,sofa_confirmed)=row
                league=str(league);home=str(home);away=str(away)
                hn,hc=hist(home,league,dt);an,ac=hist(away,league,dt);hx=xgc(home,league,dt);ax=xgc(away,league,dt)
                schedule=hr is not None and ar is not None
                lineup_signal=bool((lineup or 0)>0 or (roster or 0)>0 or match_specific_present)
                current_injury=bool(current_injury);match_specific_present=bool(match_specific_present);confirmed_current=bool(confirmed_current)
                availability_present=bool(current_injury or match_specific_present)
                odds_fresh=odds_age is not None and float(odds_age)<=ODDS_MAX_AGE_HOURS
                ou_fresh=bool(has_ou) and odds_fresh;btts_fresh=bool(has_btts) and odds_fresh;corner_fresh=bool(has_corner) and odds_fresh
                match_history_ok=hn>=10 and an>=10;corner_history_ok=hc>=10 and ac>=10;xg_ok=hx>=3 and ax>=3
                goals=bool(match_history_ok and ou_fresh);btts=bool(match_history_ok and btts_fresh);corners=bool(corner_history_ok and corner_fresh)
                final_context=bool(schedule and current_injury and confirmed_current)
                blockers=[]
                if not match_history_ok:blockers.append("insufficient_match_history")
                if not corner_history_ok:blockers.append("insufficient_corner_history")
                if not schedule:blockers.append("schedule_context_missing")
                if not lineup_signal:blockers.append("lineup_not_yet_available")
                if not availability_present:blockers.append("fresh_availability_missing")
                if not current_injury:blockers.append("current_injury_report_missing")
                if not confirmed_current:blockers.append("match_lineup_not_confirmed_current")
                if not has_ou:blockers.append("ou25_odds_missing")
                if not has_btts:blockers.append("btts_odds_missing")
                if not has_corner:blockers.append("corner85_odds_missing")
                if (has_ou or has_btts or has_corner) and not odds_fresh:blockers.append("odds_snapshot_stale")
                components=[match_history_ok,xg_ok,corner_history_ok,schedule,lineup_signal,current_injury,ou_fresh,btts_fresh,corner_fresh]
                score=round(sum(int(bool(v)) for v in components)/len(components),4)
                params.append((eid,hour,dt,league,home,away,hn,an,hc,ac,hx,ax,schedule,lineup_signal,availability_present,
                               False if current_injury else None,bool(has_ou),bool(has_btts),bool(has_corner),goals,btts,corners,final_context,score,Jsonb(blockers),
                               current_injury,match_specific_present,confirmed_current,source,home_inj,away_inj,bool(bbs_present),sofa_confirmed,odds_age))
                goals_ready+=int(goals);btts_ready+=int(btts);corners_ready+=int(corners);final_ready+=int(final_context)
                injury_ready+=int(current_injury);match_specific+=int(match_specific_present);confirmed+=int(confirmed_current);xg_ready+=int(xg_ok)

            sql="""INSERT INTO prediction_readiness_snapshots(
              event_id,snapshot_hour,match_date,league_name,home_team,away_team,home_history_matches,away_history_matches,
              home_corner_matches,away_corner_matches,xg_home_matches,xg_away_matches,schedule_complete,lineup_available,
              availability_present,availability_stale,odds_ou25,odds_btts,odds_corner85,goals_provisional_ready,
              btts_provisional_ready,corners_provisional_ready,final_context_ready,readiness_score,blockers,
              current_injury_report_present,match_specific_availability_present,availability_confirmed_current,availability_source,
              fotmob_home_injuries,fotmob_away_injuries,bbs_lineup_present,sofascore_confirmed,odds_snapshot_age_hours)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(event_id,snapshot_hour) DO UPDATE SET
              home_history_matches=EXCLUDED.home_history_matches,away_history_matches=EXCLUDED.away_history_matches,
              home_corner_matches=EXCLUDED.home_corner_matches,away_corner_matches=EXCLUDED.away_corner_matches,
              xg_home_matches=EXCLUDED.xg_home_matches,xg_away_matches=EXCLUDED.xg_away_matches,
              schedule_complete=EXCLUDED.schedule_complete,lineup_available=EXCLUDED.lineup_available,
              availability_present=EXCLUDED.availability_present,availability_stale=EXCLUDED.availability_stale,
              odds_ou25=EXCLUDED.odds_ou25,odds_btts=EXCLUDED.odds_btts,odds_corner85=EXCLUDED.odds_corner85,
              goals_provisional_ready=EXCLUDED.goals_provisional_ready,btts_provisional_ready=EXCLUDED.btts_provisional_ready,
              corners_provisional_ready=EXCLUDED.corners_provisional_ready,final_context_ready=EXCLUDED.final_context_ready,
              readiness_score=EXCLUDED.readiness_score,blockers=EXCLUDED.blockers,
              current_injury_report_present=EXCLUDED.current_injury_report_present,
              match_specific_availability_present=EXCLUDED.match_specific_availability_present,
              availability_confirmed_current=EXCLUDED.availability_confirmed_current,availability_source=EXCLUDED.availability_source,
              fotmob_home_injuries=EXCLUDED.fotmob_home_injuries,fotmob_away_injuries=EXCLUDED.fotmob_away_injuries,
              bbs_lineup_present=EXCLUDED.bbs_lineup_present,sofascore_confirmed=EXCLUDED.sofascore_confirmed,
              odds_snapshot_age_hours=EXCLUDED.odds_snapshot_age_hours,built_at=NOW()"""
            if params:
                with conn.cursor() as cur:cur.executemany(sql,params)
            conn.execute("""UPDATE data_readiness_runs SET finished_at=NOW(),status='success',fixtures=%s,goals_ready=%s,
                          btts_ready=%s,corners_ready=%s,final_context_ready=%s,message='v4 vectorized v1-primary' WHERE id=%s""",
                         (len(fixtures),goals_ready,btts_ready,corners_ready,final_ready,rid))
            result={"status":"success","fixtures":len(fixtures),"version":"readiness-v4-v1-primary-vectorized",
                    "v4_updated":len(fixtures),"current_injury_reports":injury_ready,"match_specific_availability":match_specific,
                    "confirmed_current_lineups":confirmed,"xg_diagnostic_ready":xg_ready,"goals_ready":goals_ready,
                    "btts_ready":btts_ready,"corners_ready":corners_ready,"final_context_ready":final_ready,
                    "odds_max_age_hours":ODDS_MAX_AGE_HOURS}
            print("DATA_READINESS_V4_RESULT",json.dumps(result,separators=(",",":")),flush=True);return result
        except Exception as exc:
            conn.execute("UPDATE data_readiness_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:1000],rid));raise

if __name__=="__main__":print(json.dumps(run_audit(),indent=2))
