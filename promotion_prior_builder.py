#!/usr/bin/env python3
"""Build promotion-aware priors from second-tier history.

Learns how relative second-tier performance carried into the following top-flight
season using the 2024/25 -> 2025/26 promotions, then applies those learned
transfer coefficients to 2025/26 -> 2026/27 promoted teams.
"""
from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PARENTS = ["Premier League", "La Liga", "Serie A", "Bundesliga", "Ligue 1"]
METRICS = ["goals_for","goals_against","shots_for","shots_against","sot_for","sot_against","corners_for","corners_against"]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS promotion_transfer_factors (
    learned_from TEXT NOT NULL,
    parent_league_name TEXT NOT NULL,
    metric TEXT NOT NULL,
    beta DOUBLE PRECISION NOT NULL,
    sample_teams INTEGER NOT NULL,
    raw_pairs JSONB NOT NULL,
    built_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(learned_from,parent_league_name,metric)
);
CREATE TABLE IF NOT EXISTS promotion_priors (
    target_season TEXT NOT NULL,
    parent_league_name TEXT NOT NULL,
    team_name TEXT NOT NULL,
    source_division TEXT,
    source_matches INTEGER NOT NULL,
    source_metrics JSONB NOT NULL,
    source_relative JSONB NOT NULL,
    transferred_relative JSONB NOT NULL,
    transfer_factors JSONB NOT NULL,
    built_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(target_season,parent_league_name,team_name)
);
CREATE TABLE IF NOT EXISTS promotion_prior_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    learned_promoted_teams INTEGER NOT NULL DEFAULT 0,
    current_promoted_teams INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""

ALIASES = {
    "man utd":"manchester united","man united":"manchester united","man city":"manchester city",
    "nottm forest":"nottingham forest","wolves":"wolverhampton wanderers",
    "spurs":"tottenham hotspur","tottenham":"tottenham hotspur",
    "milan":"ac milan","inter":"inter milan","psg":"paris saint germain","paris sg":"paris saint germain",
    "ath bilbao":"athletic club","athletic bilbao":"athletic club",
    "mgladbach":"borussia monchengladbach","borussia m gladbach":"borussia monchengladbach",
}

def canon(v: Any) -> str:
    s = unicodedata.normalize("NFKD", str(v or "")).encode("ascii","ignore").decode().lower().replace("'","")
    s = re.sub(r"\b(fc|cf|ssc|ac|calcio|club|football club)\b"," ",s)
    s = re.sub(r"[^a-z0-9]+"," ",s).strip()
    s = re.sub(r"\s+"," ",s)
    return ALIASES.get(s,s)

def safe_ratio(v: Optional[float], base: Optional[float]) -> float:
    if v is None or base is None or base <= 1e-9:
        return 1.0
    return max(0.35, min(2.5, v / base))

def rows_for_table(conn, table: str, season: str, parent: str):
    if table == "second_tier_matches":
        sql = f"""SELECT division,home_team,away_team,home_goals,away_goals,
                         home_shots,away_shots,home_shots_on_target,away_shots_on_target,
                         home_corners,away_corners
                  FROM {table}
                  WHERE season_code=%s AND parent_league_name=%s"""
    else:
        sql = f"""SELECT division,home_team,away_team,home_goals,away_goals,
                         home_shots,away_shots,home_shots_on_target,away_shots_on_target,
                         home_corners,away_corners
                  FROM {table}
                  WHERE season_code=%s AND league_name=%s"""
    return conn.execute(sql,(season,parent)).fetchall()

def aggregate(rows):
    sums: Dict[str, Dict[str,float]] = defaultdict(lambda: defaultdict(float))
    counts: Dict[str,int] = defaultdict(int)
    divisions: Dict[str,str] = {}
    for div,h,a,hg,ag,hs,ass,hst,ast,hc,ac in rows:
        ch,ca = canon(h),canon(a)
        if not ch or not ca or hg is None or ag is None:
            continue
        divisions.setdefault(ch,str(div)); divisions.setdefault(ca,str(div))
        counts[ch]+=1; counts[ca]+=1
        vals = [
            ("goals_for",hg,ag),("goals_against",ag,hg),
            ("shots_for",hs,ass),("shots_against",ass,hs),
            ("sot_for",hst,ast),("sot_against",ast,hst),
            ("corners_for",hc,ac),("corners_against",ac,hc),
        ]
        for key,hv,av in vals:
            if hv is not None: sums[ch][key]+=float(hv)
            if av is not None: sums[ca][key]+=float(av)
    out: Dict[str,Dict[str,float]] = {}
    for team,n in counts.items():
        d={"matches":float(n)}
        for m in METRICS:
            d[m]=sums[team].get(m,0.0)/max(1,n)
        out[team]=d
    return out, divisions

def baselines(stats: Dict[str,Dict[str,float]]) -> Dict[str,float]:
    out={}
    for m in METRICS:
        vals=[x[m] for x in stats.values() if x.get("matches",0)>=5 and x.get(m) is not None]
        out[m]=sum(vals)/len(vals) if vals else 1.0
    return out

def relative(stats: Dict[str,Dict[str,float]], base: Dict[str,float]) -> Dict[str,Dict[str,float]]:
    return {t:{m:safe_ratio(v.get(m),base.get(m)) for m in METRICS} for t,v in stats.items()}

def learn_beta(pairs: List[Tuple[float,float]]) -> float:
    usable=[(x,y) for x,y in pairs if math.isfinite(x) and math.isfinite(y)]
    if len(usable)<3:
        return 0.55
    den=sum((x-1.0)**2 for x,_ in usable)
    if den < 1e-6:
        return 0.55
    beta=sum((x-1.0)*(y-1.0) for x,y in usable)/den
    return max(0.0,min(1.20,beta))

def current_top_teams(conn, parent: str) -> Dict[str,str]:
    rows=conn.execute(
        """SELECT home_team FROM espn_current_matches WHERE league_name=%s
           UNION SELECT away_team FROM espn_current_matches WHERE league_name=%s
           UNION SELECT home_team FROM espn_upcoming WHERE league_name=%s AND is_current=TRUE
           UNION SELECT away_team FROM espn_upcoming WHERE league_name=%s AND is_current=TRUE""",
        (parent,parent,parent,parent)
    ).fetchall()
    return {canon(r[0]):str(r[0]) for r in rows if r and r[0]}

def build(database_url: Optional[str]=None) -> Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db: raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
        rid=conn.execute("INSERT INTO promotion_prior_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        learned_total=current_total=0
        details={}
        try:
            for parent in PARENTS:
                lower_old, _ = aggregate(rows_for_table(conn,"second_tier_matches","2425",parent))
                top_next, _ = aggregate(rows_for_table(conn,"football_data_matches","2526",parent))
                lower_base, top_base = baselines(lower_old), baselines(top_next)
                lower_rel, top_rel = relative(lower_old,lower_base), relative(top_next,top_base)
                promoted = sorted(set(lower_rel).intersection(top_rel))
                learned_total += len(promoted)
                betas={}
                for metric in METRICS:
                    pairs=[(lower_rel[t][metric],top_rel[t][metric]) for t in promoted]
                    beta=learn_beta(pairs)
                    betas[metric]=beta
                    conn.execute(
                        """INSERT INTO promotion_transfer_factors(learned_from,parent_league_name,metric,beta,sample_teams,raw_pairs,built_at)
                           VALUES('2425_to_2526',%s,%s,%s,%s,%s,NOW())
                           ON CONFLICT(learned_from,parent_league_name,metric) DO UPDATE SET
                             beta=EXCLUDED.beta,sample_teams=EXCLUDED.sample_teams,raw_pairs=EXCLUDED.raw_pairs,built_at=NOW()""",
                        (parent,metric,beta,len(pairs),Jsonb([{"team":t,"lower":lower_rel[t][metric],"top":top_rel[t][metric]} for t in promoted]))
                    )

                lower_cur, divisions = aggregate(rows_for_table(conn,"second_tier_matches","2526",parent))
                lower_cur_rel = relative(lower_cur,baselines(lower_cur))
                current = current_top_teams(conn,parent)
                current_promoted = sorted(set(lower_cur_rel).intersection(current))
                current_total += len(current_promoted)
                for team in current_promoted:
                    transferred={m:max(0.55,min(1.65,1.0+betas[m]*(lower_cur_rel[team][m]-1.0))) for m in METRICS}
                    label=current.get(team,team)
                    conn.execute(
                        """INSERT INTO promotion_priors(target_season,parent_league_name,team_name,source_division,source_matches,
                              source_metrics,source_relative,transferred_relative,transfer_factors,built_at)
                           VALUES('2627',%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                           ON CONFLICT(target_season,parent_league_name,team_name) DO UPDATE SET
                             source_division=EXCLUDED.source_division,source_matches=EXCLUDED.source_matches,
                             source_metrics=EXCLUDED.source_metrics,source_relative=EXCLUDED.source_relative,
                             transferred_relative=EXCLUDED.transferred_relative,transfer_factors=EXCLUDED.transfer_factors,built_at=NOW()""",
                        (parent,label,divisions.get(team),int(lower_cur[team]["matches"]),
                         Jsonb(lower_cur[team]),Jsonb(lower_cur_rel[team]),Jsonb(transferred),Jsonb(betas))
                    )
                details[parent]={"learned_promoted":len(promoted),"current_promoted":len(current_promoted),"betas":betas}
            conn.execute(
                "UPDATE promotion_prior_runs SET finished_at=NOW(),status='success',learned_promoted_teams=%s,current_promoted_teams=%s,message=%s WHERE id=%s",
                (learned_total,current_total,json.dumps(details,separators=(",",":")),rid)
            )
            result={"status":"success","learned_promoted_teams":learned_total,"current_promoted_teams":current_total,"details":details}
            print("PROMOTION_PRIORS_RESULT",json.dumps(result,separators=(",",":")))
            return result
        except Exception as exc:
            conn.execute("UPDATE promotion_prior_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:1000],rid))
            raise

if __name__=="__main__":
    print(json.dumps(build(),ensure_ascii=False,indent=2))
