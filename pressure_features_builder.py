#!/usr/bin/env python3
"""DB-only event-pressure / xT-style proxy from xG, shots, SOT, corners and possession."""
from __future__ import annotations
import json, os, re, unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict
import psycopg
from psycopg.types.json import Jsonb
DATABASE_URL=os.getenv("DATABASE_URL","").strip();RECENT=int(os.getenv("PRESSURE_RECENT_MATCHES","18"))
SCHEMA="""
CREATE TABLE IF NOT EXISTS team_pressure_snapshots(league_name TEXT NOT NULL,team_name TEXT NOT NULL,snapshot_hour TIMESTAMPTZ NOT NULL,matches INTEGER NOT NULL,threat_share DOUBLE PRECISION,goal_environment DOUBLE PRECISION,corner_environment DOUBLE PRECISION,possession_share DOUBLE PRECISION,shot_share DOUBLE PRECISION,sot_share DOUBLE PRECISION,corner_share DOUBLE PRECISION,xg_share DOUBLE PRECISION,metrics JSONB NOT NULL DEFAULT '{}'::jsonb,PRIMARY KEY(league_name,team_name,snapshot_hour));
CREATE TABLE IF NOT EXISTS fixture_pressure_snapshots(event_id TEXT NOT NULL,snapshot_hour TIMESTAMPTZ NOT NULL,home_threat_share DOUBLE PRECISION,away_threat_share DOUBLE PRECISION,goal_pressure_signal DOUBLE PRECISION,corner_pressure_signal DOUBLE PRECISION,coverage DOUBLE PRECISION NOT NULL DEFAULT 0,raw JSONB NOT NULL DEFAULT '{}'::jsonb,PRIMARY KEY(event_id,snapshot_hour));
CREATE TABLE IF NOT EXISTS pressure_feature_runs(id BIGSERIAL PRIMARY KEY,started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),finished_at TIMESTAMPTZ,status TEXT NOT NULL,teams INTEGER NOT NULL DEFAULT 0,fixtures INTEGER NOT NULL DEFAULT 0,message TEXT);
"""
ALIASES={"man utd":"manchester united","man city":"manchester city","nottm forest":"nottingham forest","spurs":"tottenham hotspur","tottenham":"tottenham hotspur","milan":"ac milan","psg":"paris saint germain"}
def canon(v):
 s=unicodedata.normalize("NFKD",str(v or "")).encode("ascii","ignore").decode().lower();s=re.sub(r"\b(fc|cf|ssc|ac|club|football club|afc)\b"," ",s);s=re.sub(r"[^a-z0-9]+"," ",s).strip();s=re.sub(r"\s+"," ",s);return ALIASES.get(s,s)
def avg(v):return sum(v)/len(v) if v else None
def clamp(v,a=.2,b=1.8):return max(a,min(b,v))
def share(a,b):return a/(a+b) if a is not None and b is not None and a+b>0 else None
def build(database_url=None)->Dict[str,Any]:
 db=(database_url or DATABASE_URL).strip()
 if not db:raise RuntimeError("Missing DATABASE_URL")
 with psycopg.connect(db,autocommit=True) as c:
  c.execute(SCHEMA);rid=c.execute("INSERT INTO pressure_feature_runs(status) VALUES('running') RETURNING id").fetchone()[0]
  try:
   upcoming=c.execute("SELECT event_id,league_name,home_team,away_team FROM espn_upcoming WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+INTERVAL '8 days'").fetchall();teamset={(league,canon(t)):t for _e,league,h,a in upcoming for t in (h,a)};hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0);contexts={}
   for (league,ct),label in teamset.items():
    rows=c.execute("""SELECT match_date,home_team,away_team,home_goals,away_goals,home_shots,away_shots,home_shots_on_target,away_shots_on_target,home_corners,away_corners,NULL::double precision,NULL::double precision FROM football_data_matches WHERE league_name=%s AND season_code IN ('2425','2526') UNION ALL SELECT match_date,home_team,away_team,home_goals,away_goals,home_shots,away_shots,home_shots_on_target,away_shots_on_target,home_corners,away_corners,home_possession,away_possession FROM espn_current_matches WHERE league_name=%s ORDER BY 1 DESC LIMIT 900""",(league,league)).fetchall();vals=defaultdict(list);used=0
    for row in rows:
     _d,h,a,hg,ag,hs,as_,hst,ast,hc,ac,hp,ap=row;home=canon(h)==ct;away=canon(a)==ct
     if not home and not away:continue
     used+=1
     for k,(x,y) in {"goals":(hg,ag),"shots":(hs,as_),"sot":(hst,ast),"corners":(hc,ac),"poss":(hp,ap)}.items():
      o,op=(x,y) if home else (y,x)
      if o is not None and op is not None:vals[k].append((float(o),float(op)))
     if used>=RECENT:break
    xgr=c.execute("SELECT home_team,away_team,home_xg,away_xg FROM understat_matches WHERE league_name=%s AND is_result=TRUE ORDER BY match_date DESC LIMIT 400",(league,)).fetchall();xu=[];ux=0
    for h,a,hx,ax in xgr:
     if canon(h)==ct and hx is not None and ax is not None:xu.append((float(hx),float(ax)));ux+=1
     elif canon(a)==ct and hx is not None and ax is not None:xu.append((float(ax),float(hx)));ux+=1
     if ux>=RECENT:break
    def sh(k):
     z=vals.get(k,[]);return share(avg([a for a,b in z]),avg([b for a,b in z])) if z else None
    poss,shot,sot,corner,xg=sh("poss"),sh("shots"),sh("sot"),sh("corners"),share(avg([a for a,b in xu]),avg([b for a,b in xu])) if xu else None;comps=[(w,v) for w,v in ((.30,xg),(.22,sot),(.18,shot),(.18,corner),(.12,poss)) if v is not None];threat=sum(w*v for w,v in comps)/sum(w for w,v in comps) if comps else None;g=vals.get("goals",[]);co=vals.get("corners",[]);genv=avg([a+b for a,b in g]) if g else None;cenv=avg([a+b for a,b in co]) if co else None;metrics={"matches":used,"xg_matches":ux,"threat_components":{"possession":poss,"shots":shot,"sot":sot,"corners":corner,"xg":xg}};contexts[(league,ct)]={"threat":threat,"genv":genv,"cenv":cenv,"poss":poss,"shot":shot,"sot":sot,"corner":corner,"xg":xg,"metrics":metrics}
    c.execute("""INSERT INTO team_pressure_snapshots(league_name,team_name,snapshot_hour,matches,threat_share,goal_environment,corner_environment,possession_share,shot_share,sot_share,corner_share,xg_share,metrics) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(league_name,team_name,snapshot_hour) DO UPDATE SET matches=EXCLUDED.matches,threat_share=EXCLUDED.threat_share,goal_environment=EXCLUDED.goal_environment,corner_environment=EXCLUDED.corner_environment,possession_share=EXCLUDED.possession_share,shot_share=EXCLUDED.shot_share,sot_share=EXCLUDED.sot_share,corner_share=EXCLUDED.corner_share,xg_share=EXCLUDED.xg_share,metrics=EXCLUDED.metrics""",(league,label,hour,used,threat,genv,cenv,poss,shot,sot,corner,xg,Jsonb(metrics)))
   fixtures=0
   for eid,league,h,a in upcoming:
    H=contexts.get((league,canon(h)),{});A=contexts.get((league,canon(a)),{});hv,av=H.get("threat"),A.get("threat");coverage=sum(x is not None for x in (hv,av,H.get("genv"),A.get("genv"),H.get("cenv"),A.get("cenv")))/6;goal=clamp(((H["genv"]+A["genv"])/2)/2.6,.55,1.45) if H.get("genv") is not None and A.get("genv") is not None else None;corner_sig=clamp(((H["cenv"]+A["cenv"])/2)/9.5,.55,1.45) if H.get("cenv") is not None and A.get("cenv") is not None else None;raw={"home":H,"away":A};c.execute("""INSERT INTO fixture_pressure_snapshots(event_id,snapshot_hour,home_threat_share,away_threat_share,goal_pressure_signal,corner_pressure_signal,coverage,raw) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id,snapshot_hour) DO UPDATE SET home_threat_share=EXCLUDED.home_threat_share,away_threat_share=EXCLUDED.away_threat_share,goal_pressure_signal=EXCLUDED.goal_pressure_signal,corner_pressure_signal=EXCLUDED.corner_pressure_signal,coverage=EXCLUDED.coverage,raw=EXCLUDED.raw""",(eid,hour,hv,av,goal,corner_sig,coverage,Jsonb(raw)));fixtures+=1
   c.execute("UPDATE pressure_feature_runs SET finished_at=NOW(),status='success',teams=%s,fixtures=%s,message='xT-style proxy; not true xT' WHERE id=%s",(len(contexts),fixtures,rid));res={"status":"success","teams":len(contexts),"fixtures":fixtures};print("PRESSURE_FEATURES_RESULT",json.dumps(res,separators=(",",":")));return res
  except Exception as exc:c.execute("UPDATE pressure_feature_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:700],rid));raise
if __name__=="__main__":print(json.dumps(build(),indent=2))
