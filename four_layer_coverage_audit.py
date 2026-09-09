#!/usr/bin/env python3
"""Truthful live coverage audit for player, continuity, style, pressure and market layers."""
from __future__ import annotations
import json, os
from typing import Any, Dict, Optional
import psycopg
from psycopg.types.json import Jsonb
from asian_event_context import resolve_event_market
DATABASE_URL=os.getenv("DATABASE_URL","").strip()
SCHEMA="""CREATE TABLE IF NOT EXISTS four_layer_coverage_runs(id BIGSERIAL PRIMARY KEY,started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),finished_at TIMESTAMPTZ,status TEXT NOT NULL,fixtures INTEGER NOT NULL DEFAULT 0,metrics JSONB,message TEXT);"""
def pct(n,d):return round(n/d,4) if d else 0.0
def run_audit(database_url:Optional[str]=None)->Dict[str,Any]:
 db=(database_url or DATABASE_URL).strip()
 if not db:raise RuntimeError("Missing DATABASE_URL")
 with psycopg.connect(db,autocommit=True) as c:
  c.execute(SCHEMA);rid=c.execute("INSERT INTO four_layer_coverage_runs(status) VALUES('running') RETURNING id").fetchone()[0]
  try:
   rows=c.execute("SELECT event_id,home_team,away_team FROM espn_upcoming WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours' AND match_date<=NOW()+INTERVAL '8 days'").fetchall();total=len(rows)
   roster_any=roster_both=expected_any=expected_both=continuity=both_cont=pressure=asian_goal=asian_corner=asian_any=advanced=style_both=0
   promo_eligible=promo_mapped=0;books=[];total_cov=[];player_cov=[];pressure_cov=[];src_goal={"oddspapi":0,"football-data":0,"espn":0};fd_any=espn_any=0
   for eid,h,a in rows:
    def pctx(team):
     r=c.execute("SELECT expected_xi_strength,retained_minutes_share,starter_continuity,player_coverage,source_meta FROM player_team_context_snapshots WHERE team_name=%s ORDER BY snapshot_hour DESC LIMIT 1",(team,)).fetchone();return r
    hp,ap=pctx(h),pctx(a)
    def roster_ok(x):return bool(x and float(x[3] or 0)>0)
    def expected_ok(x):
     if not roster_ok(x) or x[0] is None:return False
     m=x[4] if isinstance(x[4],dict) else {}
     # Explicitly exclude the 0.50 compatibility value written by roster-only bridges.
     return bool(m.get("calibrated_player_strength") or m.get("expected_xi_data_informed") or not m.get("neutral_strength_interface",False))
    hr,ar=roster_ok(hp),roster_ok(ap);he,ae=expected_ok(hp),expected_ok(ap);roster_any+=int(hr or ar);roster_both+=int(hr and ar);expected_any+=int(he or ae);expected_both+=int(he and ae)
    hcont=bool(hp and (hp[1] is not None or hp[2] is not None));acont=bool(ap and (ap[1] is not None or ap[2] is not None));continuity+=int(hcont or acont);both_cont+=int(hcont and acont)
    if hp:player_cov.append(float(hp[3] or 0));
    if ap:player_cov.append(float(ap[3] or 0))
    pr=c.execute("SELECT coverage FROM fixture_pressure_snapshots WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1",(eid,)).fetchone()
    if pr and float(pr[0] or 0)>0:pressure+=1;pressure_cov.append(float(pr[0] or 0))
    A=resolve_event_market(c,str(eid));asian_any+=int(bool(A.get("has_any")));asian_goal+=int(A.get("goal_p") is not None);asian_corner+=int(A.get("corner_p") is not None)
    if A.get("has_any"):books.append(int(A.get("books") or 0))
    gs=A.get("goal_source");src_goal[gs]=src_goal.get(gs,0)+1 if gs else src_goal.get(gs,0);fd_any+=int("football-data" in (A.get("sources") or []));espn_any+=int("espn" in (A.get("sources") or []))
    e=c.execute("SELECT home_style,away_style,home_promoted,away_promoted,home_promotion_prior,away_promotion_prior FROM fixture_enrichment_snapshots WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1",(eid,)).fetchone()
    if e:
     style_both+=int(e[0] is not None and e[1] is not None);eligible=bool(e[2] or e[3]);promo_eligible+=int(eligible);promo_mapped+=int(eligible and ((e[2] and e[4] is not None) or (e[3] and e[5] is not None)))
    ac=c.execute("SELECT total_coverage FROM advanced_fixture_context_v4 WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1",(eid,)).fetchone()
    if ac:advanced+=1;total_cov.append(float(ac[0] or 0))
   metrics={"fixtures":total,
    "roster_context_any":{"n":roster_any,"rate":pct(roster_any,total)},"roster_context_both":{"n":roster_both,"rate":pct(roster_both,total)},
    "expected_xi_any":{"n":expected_any,"rate":pct(expected_any,total)},"expected_xi_both":{"n":expected_both,"rate":pct(expected_both,total),"definition":"data-informed or calibrated; neutral 0.50 roster placeholders excluded"},
    "continuity_any":{"n":continuity,"rate":pct(continuity,total)},"continuity_both":{"n":both_cont,"rate":pct(both_cont,total)},"style_both":{"n":style_both,"rate":pct(style_both,total)},
    "promotion_prior":{"eligible_fixtures":promo_eligible,"mapped_eligible":promo_mapped,"eligible_rate":pct(promo_mapped,promo_eligible)},
    "pressure":{"n":pressure,"rate":pct(pressure,total)},"asian_any":{"n":asian_any,"rate":pct(asian_any,total)},"asian_goal_2_5":{"n":asian_goal,"rate":pct(asian_goal,total)},"asian_corner_8_5":{"n":asian_corner,"rate":pct(asian_corner,total)},
    "asian_goal_sources":src_goal,"football_data_rows_available":{"n":fd_any,"rate":pct(fd_any,total)},"espn_odds_rows_available":{"n":espn_any,"rate":pct(espn_any,total)},"advanced_context":{"n":advanced,"rate":pct(advanced,total)},
    "avg_player_coverage":round(sum(player_cov)/len(player_cov),4) if player_cov else 0,"avg_pressure_coverage":round(sum(pressure_cov)/len(pressure_cov),4) if pressure_cov else 0,"avg_total_coverage":round(sum(total_cov)/len(total_cov),4) if total_cov else 0,"median_asian_bookmakers":sorted(books)[len(books)//2] if books else 0}
   c.execute("UPDATE four_layer_coverage_runs SET finished_at=NOW(),status='success',fixtures=%s,metrics=%s,message='truthful coverage audit; neutral placeholders excluded' WHERE id=%s",(total,Jsonb(metrics),rid));res={"status":"success",**metrics};print("FOUR_LAYER_COVERAGE_RESULT",json.dumps(res,separators=(",",":")),flush=True);return res
  except Exception as exc:c.execute("UPDATE four_layer_coverage_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:700],rid));raise
if __name__=="__main__":print(json.dumps(run_audit(),indent=2))
