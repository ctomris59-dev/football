#!/usr/bin/env python3
"""Expected-XI/player-impact and squad-continuity context from Understat team pages.

The importer is deliberately fail-soft and bounded. It stores current/previous-season
player aggregates and derives conservative pre-match team context. These features are
shadow/ranking context only until calibrated on archived forecasts.
"""
from __future__ import annotations

import json, math, os, re, time, unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import psycopg
from psycopg.types.json import Jsonb
import requests

DATABASE_URL = os.getenv("DATABASE_URL","").strip()
BASE = "https://understat.com"
CURRENT_SEASON = int(os.getenv("PLAYER_CONTEXT_CURRENT_SEASON","2026"))
PREVIOUS_SEASON = CURRENT_SEASON - 1
MAX_HTTP = max(10, int(os.getenv("PLAYER_CONTEXT_MAX_HTTP_CALLS","120")))
DELAY = float(os.getenv("PLAYER_CONTEXT_REQUEST_DELAY_SECONDS","0.65"))
LOOKAHEAD_DAYS = int(os.getenv("PLAYER_CONTEXT_LOOKAHEAD_DAYS","8"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS understat_player_seasons(
  season INTEGER NOT NULL,
  team_name TEXT NOT NULL,
  team_slug TEXT NOT NULL,
  player_id TEXT NOT NULL,
  player_name TEXT,
  games DOUBLE PRECISION,
  starts DOUBLE PRECISION,
  minutes DOUBLE PRECISION,
  goals DOUBLE PRECISION,
  xg DOUBLE PRECISION,
  assists DOUBLE PRECISION,
  xa DOUBLE PRECISION,
  xgchain DOUBLE PRECISION,
  xgbuildup DOUBLE PRECISION,
  raw JSONB NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(season,team_slug,player_id)
);
CREATE INDEX IF NOT EXISTS idx_understat_player_team ON understat_player_seasons(season,team_slug,minutes DESC);

CREATE TABLE IF NOT EXISTS understat_player_team_state(
  season INTEGER NOT NULL,
  team_name TEXT NOT NULL,
  team_slug TEXT NOT NULL,
  player_rows INTEGER NOT NULL DEFAULT 0,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  status TEXT NOT NULL,
  message TEXT,
  PRIMARY KEY(season,team_slug)
);

CREATE TABLE IF NOT EXISTS player_team_context_snapshots(
  team_name TEXT NOT NULL,
  snapshot_hour TIMESTAMPTZ NOT NULL,
  current_season INTEGER NOT NULL,
  previous_season INTEGER NOT NULL,
  expected_xi_strength DOUBLE PRECISION,
  top11_strength DOUBLE PRECISION,
  injury_impact DOUBLE PRECISION,
  goalkeeper_injured BOOLEAN,
  retained_minutes_share DOUBLE PRECISION,
  starter_continuity DOUBLE PRECISION,
  player_coverage DOUBLE PRECISION,
  key_absences JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY(team_name,snapshot_hour)
);

CREATE TABLE IF NOT EXISTS player_context_runs(
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  teams INTEGER NOT NULL DEFAULT 0,
  teams_with_current INTEGER NOT NULL DEFAULT 0,
  teams_with_previous INTEGER NOT NULL DEFAULT 0,
  http_calls INTEGER NOT NULL DEFAULT 0,
  message TEXT
);
"""

ALIASES = {
 "man utd":"manchester united","man united":"manchester united","man city":"manchester city",
 "nottm forest":"nottingham forest","wolves":"wolverhampton wanderers","spurs":"tottenham hotspur",
 "tottenham":"tottenham hotspur","milan":"ac milan","inter":"inter","psg":"paris saint germain",
 "paris sg":"paris saint germain","mgladbach":"borussia monchengladbach",
}
SLUG_ALIASES = {
 "manchester united":"Manchester_United","manchester city":"Manchester_City",
 "nottingham forest":"Nottingham_Forest","wolverhampton wanderers":"Wolverhampton_Wanderers",
 "tottenham hotspur":"Tottenham","newcastle united":"Newcastle_United","west ham united":"West_Ham",
 "brighton hove albion":"Brighton","crystal palace":"Crystal_Palace","aston villa":"Aston_Villa",
 "real madrid":"Real_Madrid","real sociedad":"Real_Sociedad","atletico madrid":"Atletico_Madrid",
 "athletic club":"Athletic_Club","real betis":"Real_Betis","celta vigo":"Celta_Vigo",
 "inter":"Inter","ac milan":"AC_Milan","hellas verona":"Verona",
 "borussia dortmund":"Borussia_Dortmund","bayern munich":"Bayern_Munich",
 "borussia monchengladbach":"Borussia_M.Gladbach","eintracht frankfurt":"Eintracht_Frankfurt",
 "paris saint germain":"Paris_Saint_Germain","olympique lyonnais":"Lyon","olympique marseille":"Marseille",
}

def canon(v: Any) -> str:
    s=unicodedata.normalize("NFKD",str(v or "")).encode("ascii","ignore").decode().lower().replace("'","")
    s=re.sub(r"\b(fc|cf|ssc|ac|club|football club|afc)\b"," ",s)
    s=re.sub(r"[^a-z0-9]+"," ",s).strip(); s=re.sub(r"\s+"," ",s)
    return ALIASES.get(s,s)

def slug_for(team: str) -> str:
    c=canon(team)
    if c in SLUG_ALIASES: return SLUG_ALIASES[c]
    return "_".join(w[:1].upper()+w[1:] for w in c.split())

def f(v: Any) -> Optional[float]:
    try: return None if v in (None,"","-") else float(v)
    except Exception: return None

def extract_players_data(html: str) -> List[Dict[str,Any]]:
    patterns=[
        r"playersData\s*=\s*JSON\.parse\('(?P<data>(?:\\.|[^'])*)'\)",
        r"JSON\.parse\('(?P<data>(?:\\.|[^'])*)'\)\s*;\s*var\s+playersData",
        r"\('playersData',\s*JSON\.parse\('(?P<data>(?:\\.|[^'])*)'\)\)",
    ]
    for pat in patterns:
        m=re.search(pat,html,re.S)
        if not m: continue
        raw=m.group("data")
        try:
            decoded=bytes(raw,"utf-8").decode("unicode_escape")
            data=json.loads(decoded)
            if isinstance(data,list): return [x for x in data if isinstance(x,dict)]
            if isinstance(data,dict): return [x for x in data.values() if isinstance(x,dict)]
        except Exception:
            continue
    return []

def normalize_player(row: Dict[str,Any]) -> Optional[Dict[str,Any]]:
    pid=row.get("id") or row.get("player_id")
    if pid is None: return None
    return {
      "player_id":str(pid),"player_name":row.get("player_name") or row.get("name"),
      "games":f(row.get("games")),"starts":f(row.get("starts")),"minutes":f(row.get("time") or row.get("minutes")),
      "goals":f(row.get("goals")),"xg":f(row.get("xG")),"assists":f(row.get("assists")),"xa":f(row.get("xA")),
      "xgchain":f(row.get("xGChain")),"xgbuildup":f(row.get("xGBuildup")),"raw":row,
    }

def pct(values: Dict[str,float]) -> Dict[str,float]:
    items=sorted(values.items(),key=lambda x:x[1]); n=len(items)
    if not n:return {}
    return {k:(i+0.5)/n for i,(k,_v) in enumerate(items)}

def player_scores(rows: List[Dict[str,Any]]) -> Dict[str,float]:
    usable=[r for r in rows if r.get("player_name")]
    if not usable:return {}
    metrics={}
    for key in ("minutes","starts","xg","xa","xgchain","xgbuildup"):
        vals={r["player_id"]:float(r[key] or 0.0) for r in usable}
        metrics[key]=pct(vals)
    out={}
    for r in usable:
        pid=r["player_id"]
        out[pid]=(
          .24*metrics["minutes"].get(pid,.5)+.20*metrics["starts"].get(pid,.5)+
          .16*metrics["xg"].get(pid,.5)+.10*metrics["xa"].get(pid,.5)+
          .18*metrics["xgchain"].get(pid,.5)+.12*metrics["xgbuildup"].get(pid,.5)
        )
    return out

def injury_names(conn, team: str) -> Tuple[set[str], List[Dict[str,Any]]]:
    ct=canon(team)
    try:
        rows=conn.execute("""SELECT home_team,away_team,home_injured_players,away_injured_players
          FROM fotmob_fixture_availability_snapshots
          WHERE snapshot_hour=(SELECT MAX(snapshot_hour) FROM fotmob_fixture_availability_snapshots)
        """).fetchall()
    except Exception:
        return set(),[]
    out=[]
    for h,a,hi,ai in rows:
        if canon(h)==ct and isinstance(hi,list): out.extend(x for x in hi if isinstance(x,dict))
        if canon(a)==ct and isinstance(ai,list): out.extend(x for x in ai if isinstance(x,dict))
    names={canon(x.get("name")) for x in out if x.get("name")}
    return names,out

class Importer:
    def __init__(self, db: Optional[str]=None):
        self.db=(db or DATABASE_URL).strip()
        if not self.db: raise RuntimeError("Missing DATABASE_URL")
        self.conn=psycopg.connect(self.db,autocommit=True); self.conn.execute(SCHEMA)
        self.s=requests.Session(); self.s.headers.update({"User-Agent":"Mozilla/5.0","Accept":"text/html"})
        self.calls=0; self.last=0.0

    def close(self): self.conn.close()

    def get_team(self, slug: str, season: int) -> List[Dict[str,Any]]:
        if self.calls>=MAX_HTTP: return []
        wait=DELAY-(time.monotonic()-self.last)
        if wait>0:time.sleep(wait)
        url=f"{BASE}/team/{quote(slug)}/{season}"
        r=self.s.get(url,timeout=30); self.last=time.monotonic(); self.calls+=1
        if r.status_code!=200:return []
        return [p for x in extract_players_data(r.text) if (p:=normalize_player(x))]

    def fresh_rows(self, slug: str, season: int, max_hours: float) -> List[Dict[str,Any]]:
        st=self.conn.execute("SELECT fetched_at,status FROM understat_player_team_state WHERE season=%s AND team_slug=%s",(season,slug)).fetchone()
        if st and st[1]=="success":
            age=(datetime.now(timezone.utc)-st[0]).total_seconds()/3600
            if age<=max_hours:
                return self.read_rows(slug,season)
        rows=self.get_team(slug,season)
        if rows:
            for p in rows:
                self.conn.execute("""INSERT INTO understat_player_seasons(season,team_name,team_slug,player_id,player_name,games,starts,minutes,goals,xg,assists,xa,xgchain,xgbuildup,raw)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT(season,team_slug,player_id) DO UPDATE SET player_name=EXCLUDED.player_name,games=EXCLUDED.games,starts=EXCLUDED.starts,
                  minutes=EXCLUDED.minutes,goals=EXCLUDED.goals,xg=EXCLUDED.xg,assists=EXCLUDED.assists,xa=EXCLUDED.xa,xgchain=EXCLUDED.xgchain,
                  xgbuildup=EXCLUDED.xgbuildup,raw=EXCLUDED.raw,fetched_at=NOW()""",
                  (season,slug.replace("_"," "),slug,p["player_id"],p["player_name"],p["games"],p["starts"],p["minutes"],p["goals"],p["xg"],p["assists"],p["xa"],p["xgchain"],p["xgbuildup"],Jsonb(p["raw"])))
            self.conn.execute("""INSERT INTO understat_player_team_state(season,team_name,team_slug,player_rows,status,message)
              VALUES(%s,%s,%s,%s,'success','ok') ON CONFLICT(season,team_slug) DO UPDATE SET player_rows=EXCLUDED.player_rows,status='success',message='ok',fetched_at=NOW()""",
              (season,slug.replace("_"," "),slug,len(rows)))
        else:
            self.conn.execute("""INSERT INTO understat_player_team_state(season,team_name,team_slug,player_rows,status,message)
              VALUES(%s,%s,%s,0,'unavailable','no playersData') ON CONFLICT(season,team_slug) DO UPDATE SET status='unavailable',message='no playersData',fetched_at=NOW()""",
              (season,slug.replace("_"," "),slug))
        return rows

    def read_rows(self, slug: str, season: int) -> List[Dict[str,Any]]:
        rows=self.conn.execute("""SELECT player_id,player_name,games,starts,minutes,goals,xg,assists,xa,xgchain,xgbuildup,raw
          FROM understat_player_seasons WHERE season=%s AND team_slug=%s""",(season,slug)).fetchall()
        return [{"player_id":str(r[0]),"player_name":r[1],"games":r[2],"starts":r[3],"minutes":r[4],"goals":r[5],"xg":r[6],"assists":r[7],"xa":r[8],"xgchain":r[9],"xgbuildup":r[10],"raw":r[11]} for r in rows]

    def context(self, team: str, cur: List[Dict[str,Any]], prev: List[Dict[str,Any]]) -> Dict[str,Any]:
        names, injury_objs=injury_names(self.conn,team)
        cur_scores=player_scores(cur); prev_scores=player_scores(prev)
        cur_by={canon(r["player_name"]):r for r in cur if r.get("player_name")}
        prev_by={canon(r["player_name"]):r for r in prev if r.get("player_name")}
        all_names=set(cur_by)|set(prev_by)
        merged=[]
        cur_games=max([float(r.get("games") or 0) for r in cur] or [0])
        wcur=max(.18,min(.70,.18+cur_games/20*.52))
        for name in all_names:
            cr=cur_by.get(name); pr=prev_by.get(name)
            cs=cur_scores.get((cr or {}).get("player_id","")) if cr else None
            ps=prev_scores.get((pr or {}).get("player_id","")) if pr else None
            score=(wcur*(cs if cs is not None else .5)+(1-wcur)*(ps if ps is not None else .5))
            mins=float((cr or pr or {}).get("minutes") or 0)
            starts=float((cr or pr or {}).get("starts") or 0)
            merged.append({"name":name,"label":(cr or pr or {}).get("player_name"),"score":score,"minutes":mins,"starts":starts,"injured":name in names})
        top=sorted(merged,key=lambda x:(x["starts"],x["minutes"],x["score"]),reverse=True)[:11]
        denom=sum(x["score"] for x in top) or 1.0
        impact=min(.55,sum(x["score"] for x in merged if x["injured"])/denom)
        avail=sorted([x for x in merged if not x["injured"]],key=lambda x:(x["starts"],x["minutes"],x["score"]),reverse=True)[:11]
        expected=sum(x["score"] for x in avail)/len(avail) if avail else None
        top11=sum(x["score"] for x in top)/len(top) if top else None
        prev_total=sum(float(r.get("minutes") or 0) for r in prev)
        retained=sum(float(r.get("minutes") or 0) for n,r in prev_by.items() if n in cur_by)
        retained_share=retained/prev_total if prev_total>0 else None
        prev_starters={n for n,r in sorted(prev_by.items(),key=lambda kv:(float(kv[1].get("starts") or 0),float(kv[1].get("minutes") or 0)),reverse=True)[:11]}
        cur_names=set(cur_by)
        starter_cont=len(prev_starters & cur_names)/len(prev_starters) if prev_starters else None
        gk=any(("goal" in str(x.get("position") or "").lower() or str(x.get("position") or "").lower()=="gk") for x in injury_objs)
        key_abs=sorted([x for x in merged if x["injured"]],key=lambda x:x["score"],reverse=True)[:6]
        coverage=min(1.0,(len(cur)+min(len(prev),18))/(36.0))
        return {"expected":expected,"top11":top11,"impact":impact,"gk":gk,
                "retained":retained_share,"starter_continuity":starter_cont,"coverage":coverage,
                "key_absences":[{"name":x["label"],"importance":round(x["score"],4)} for x in key_abs],
                "meta":{"current_players":len(cur),"previous_players":len(prev),"current_weight":round(wcur,3)}}

    def run(self) -> Dict[str,Any]:
        rid=self.conn.execute("INSERT INTO player_context_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        teams=sorted({str(x) for row in self.conn.execute("""SELECT home_team,away_team FROM espn_upcoming
          WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+(%s||' days')::interval""",(LOOKAHEAD_DAYS,)).fetchall() for x in row})
        hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)
        have_cur=have_prev=0
        try:
            for team in teams:
                slug=slug_for(team)
                cur=self.fresh_rows(slug,CURRENT_SEASON,72)
                prev=self.fresh_rows(slug,PREVIOUS_SEASON,24*365*10)
                have_cur+=int(bool(cur)); have_prev+=int(bool(prev))
                ctx=self.context(team,cur,prev)
                self.conn.execute("""INSERT INTO player_team_context_snapshots(team_name,snapshot_hour,current_season,previous_season,
                  expected_xi_strength,top11_strength,injury_impact,goalkeeper_injured,retained_minutes_share,starter_continuity,
                  player_coverage,key_absences,source_meta)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT(team_name,snapshot_hour) DO UPDATE SET expected_xi_strength=EXCLUDED.expected_xi_strength,top11_strength=EXCLUDED.top11_strength,
                  injury_impact=EXCLUDED.injury_impact,goalkeeper_injured=EXCLUDED.goalkeeper_injured,retained_minutes_share=EXCLUDED.retained_minutes_share,
                  starter_continuity=EXCLUDED.starter_continuity,player_coverage=EXCLUDED.player_coverage,key_absences=EXCLUDED.key_absences,source_meta=EXCLUDED.source_meta""",
                  (team,hour,CURRENT_SEASON,PREVIOUS_SEASON,ctx["expected"],ctx["top11"],ctx["impact"],ctx["gk"],ctx["retained"],ctx["starter_continuity"],ctx["coverage"],Jsonb(ctx["key_absences"]),Jsonb(ctx["meta"])))
            self.conn.execute("""UPDATE player_context_runs SET finished_at=NOW(),status='success',teams=%s,teams_with_current=%s,teams_with_previous=%s,http_calls=%s,message='shadow-only' WHERE id=%s""",
              (len(teams),have_cur,have_prev,self.calls,rid))
            res={"status":"success","teams":len(teams),"current":have_cur,"previous":have_prev,"http_calls":self.calls}
            print("PLAYER_CONTEXT_RESULT",json.dumps(res,separators=(",",":"))); return res
        except Exception as exc:
            self.conn.execute("UPDATE player_context_runs SET finished_at=NOW(),status='failed',http_calls=%s,message=%s WHERE id=%s",(self.calls,str(exc)[:700],rid))
            raise

def run_import(database_url: Optional[str]=None)->Dict[str,Any]:
    x=Importer(database_url)
    try:return x.run()
    finally:x.close()

if __name__=="__main__": print(json.dumps(run_import(),ensure_ascii=False,indent=2))
