#!/usr/bin/env python3
"""Production pre-match predictor for the Big Five.

Scores upcoming fixtures for:
- Over/Under 2.5 goals
- BTTS Yes/No
- Over/Under 8.5 total corners

The model probability is never overwritten by bookmaker prices. Market prices are
stored separately and used only as a conservative agreement/ranking signal. One
selection per fixture can enter the Top-10. If fewer than ten candidates clear the
quality gates, the list is intentionally shorter rather than forced.
"""
from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.types.json import Jsonb

from model_engine import Prediction, predict_match

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PREDICTION_LOOKAHEAD_DAYS = int(os.getenv("PREDICTION_LOOKAHEAD_DAYS", "7"))
PREDICTION_MIN_CONFIDENCE = float(os.getenv("PREDICTION_MIN_CONFIDENCE", "0.60"))
PREDICTION_MIN_PRICE = float(os.getenv("PREDICTION_MIN_PRICE", "1.20"))
MODEL_VERSION = "production-poisson-form-xg-v1"
POLICY_VERSION = "readiness-market-agreement-v1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS production_prediction_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    model_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    horizon_start TIMESTAMPTZ NOT NULL,
    horizon_end TIMESTAMPTZ NOT NULL,
    fixtures_scored INTEGER NOT NULL DEFAULT 0,
    market_rows INTEGER NOT NULL DEFAULT 0,
    candidate_matches INTEGER NOT NULL DEFAULT 0,
    top10_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    message TEXT
);

CREATE TABLE IF NOT EXISTS production_predictions (
    run_id BIGINT NOT NULL REFERENCES production_prediction_runs(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    snapshot_hour TIMESTAMPTZ NOT NULL,
    match_date TIMESTAMPTZ NOT NULL,
    league_name TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    market TEXT NOT NULL,
    selection TEXT NOT NULL,
    selection_yes BOOLEAN NOT NULL,
    model_probability DOUBLE PRECISION NOT NULL,
    market_no_vig_probability DOUBLE PRECISION,
    market_price DOUBLE PRECISION,
    ranking_score DOUBLE PRECISION NOT NULL,
    model_data_quality DOUBLE PRECISION NOT NULL,
    readiness_score DOUBLE PRECISION,
    provisional_ready BOOLEAN NOT NULL DEFAULT FALSE,
    final_context_ready BOOLEAN NOT NULL DEFAULT FALSE,
    xg_used BOOLEAN NOT NULL DEFAULT FALSE,
    odds_snapshot_age_hours DOUBLE PRECISION,
    home_days_rest DOUBLE PRECISION,
    away_days_rest DOUBLE PRECISION,
    home_injuries INTEGER,
    away_injuries INTEGER,
    availability_source TEXT,
    blockers JSONB NOT NULL,
    model_details JSONB NOT NULL,
    top10_rank INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(run_id,event_id,market)
);
CREATE INDEX IF NOT EXISTS idx_production_predictions_latest
    ON production_predictions(match_date,top10_rank,created_at DESC);
"""

ALIASES = {
    "man utd":"manchester united","man united":"manchester united","man city":"manchester city",
    "nottm forest":"nottingham forest","wolves":"wolverhampton wanderers","newcastle":"newcastle united",
    "spurs":"tottenham hotspur","tottenham":"tottenham hotspur","ath bilbao":"athletic club",
    "athletic bilbao":"athletic club","sociedad":"real sociedad","betis":"real betis","celta":"celta vigo",
    "espanol":"espanyol","milan":"ac milan","inter":"inter milan","verona":"hellas verona",
    "dortmund":"borussia dortmund","mgladbach":"borussia monchengladbach","borussia m gladbach":"borussia monchengladbach",
    "leverkusen":"bayer leverkusen","frankfurt":"eintracht frankfurt","psg":"paris saint germain",
    "paris sg":"paris saint germain","st etienne":"saint etienne",
}


def canon(v: Any) -> str:
    s = unicodedata.normalize("NFKD", str(v or "")).encode("ascii", "ignore").decode().lower().replace("'", "")
    s = re.sub(r"\b(fc|cf|ssc|ac|calcio|club|football club)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return ALIASES.get(s, s)


def sim(a: Any, b: Any) -> float:
    ca, cb = canon(a), canon(b)
    if not ca or not cb:
        return 0.0
    return 1.0 if ca == cb else SequenceMatcher(None, ca, cb).ratio()


def as_date(v: Any) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except Exception:
        return None


def history_rows(conn, league_name: str, before: datetime) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    cur = conn.execute(
        """
        SELECT match_date,home_team,away_team,home_goals,away_goals,
               home_shots_on_target,away_shots_on_target,home_corners,away_corners
        FROM football_data_matches
        WHERE league_name=%s AND match_date < %s
          AND home_goals IS NOT NULL AND away_goals IS NOT NULL
        ORDER BY match_date
        """,
        (league_name, before.date()),
    )
    for dt,h,a,hg,ag,hsot,asot,hc,ac in cur.fetchall():
        rows.append({
            "match_date":dt,"home_team":canon(h),"away_team":canon(a),
            "home_goals":hg,"away_goals":ag,
            "home_shots_on_target":hsot,"away_shots_on_target":asot,
            "home_corners":hc,"away_corners":ac,
            "home_xg":None,"away_xg":None,
        })

    cur = conn.execute(
        """
        SELECT match_date,home_team,away_team,home_goals,away_goals,
               home_shots_on_target,away_shots_on_target,home_corners,away_corners
        FROM espn_current_matches
        WHERE league_name=%s AND match_date < %s
          AND home_goals IS NOT NULL AND away_goals IS NOT NULL
        ORDER BY match_date
        """,
        (league_name, before),
    )
    for dt,h,a,hg,ag,hsot,asot,hc,ac in cur.fetchall():
        rows.append({
            "match_date":dt,"home_team":canon(h),"away_team":canon(a),
            "home_goals":hg,"away_goals":ag,
            "home_shots_on_target":hsot,"away_shots_on_target":asot,
            "home_corners":hc,"away_corners":ac,
            "home_xg":None,"away_xg":None,
        })

    under = defaultdict(list)
    for dt,h,a,hxg,axg in conn.execute(
        """SELECT match_date,home_team,away_team,home_xg,away_xg
           FROM understat_matches
           WHERE league_name=%s AND match_date < %s AND is_result=TRUE
             AND home_xg IS NOT NULL AND away_xg IS NOT NULL""",
        (league_name, before),
    ).fetchall():
        d = as_date(dt)
        if d:
            under[d].append((h,a,float(hxg),float(axg)))

    for m in rows:
        d = as_date(m["match_date"])
        if not d:
            continue
        best = None
        best_score = 0.0
        for uh,ua,hxg,axg in under.get(d, []):
            sh, sa = sim(m["home_team"], uh), sim(m["away_team"], ua)
            score = sh + sa
            if min(sh,sa) >= 0.58 and score > best_score:
                best, best_score = (hxg,axg), score
        if best is not None and best_score >= 1.40:
            m["home_xg"], m["away_xg"] = best

    rows.sort(key=lambda x: (as_date(x["match_date"]) or date.min, x["home_team"], x["away_team"]))
    return rows


def latest_context(conn, event_id: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT snapshot_hour,oddspapi_fixture_id,odds_snapshot_age_hours,
               home_days_rest,away_days_rest,fotmob_home_injuries,fotmob_away_injuries,
               availability_source,current_injury_report_present,
               match_specific_availability_present,availability_confirmed_current
        FROM prematch_feature_snapshots
        WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    if not row:
        return {}
    keys = ["snapshot_hour","oddspapi_fixture_id","odds_age","home_rest","away_rest","home_inj","away_inj",
            "availability_source","current_injury","match_specific","confirmed"]
    return dict(zip(keys,row))


def latest_readiness(conn, event_id: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT snapshot_hour,readiness_score,goals_provisional_ready,btts_provisional_ready,
               corners_provisional_ready,final_context_ready,blockers
        FROM prediction_readiness_snapshots
        WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    if not row:
        return {}
    keys = ["snapshot_hour","score","goals_ready","btts_ready","corners_ready","final_ready","blockers"]
    return dict(zip(keys,row))


def market_kind(name: Any, handicap: Any) -> Optional[str]:
    n = str(name or "").lower()
    try:
        line = float(handicap) if handicap is not None else None
    except Exception:
        line = None
    if "both teams to score" in n:
        return "btts"
    if "corner" in n and line is not None and abs(line - 8.5) < 0.01:
        return "corners_over_8_5"
    if line is not None and abs(line - 2.5) < 0.01 and ("over under" in n or "total" in n or "goal" in n):
        return "over_2_5"
    return None


def outcome_side(market: str, outcome: Any) -> Optional[bool]:
    s = str(outcome or "").lower().strip()
    if market in ("over_2_5", "corners_over_8_5"):
        if "over" in s:
            return True
        if "under" in s:
            return False
    elif market == "btts":
        if re.search(r"\byes\b", s) or s in {"1","true"}:
            return True
        if re.search(r"\bno\b", s) or s in {"0","false"}:
            return False
    return None


def market_prices(conn, fixture_id: Optional[str]) -> Dict[str, Dict[bool, float]]:
    if not fixture_id:
        return {}
    latest = conn.execute(
        "SELECT MAX(snapshot_hour) FROM oddspapi_market_prices WHERE fixture_id=%s",
        (fixture_id,),
    ).fetchone()[0]
    if not latest:
        return {}
    out: Dict[str, Dict[bool, float]] = defaultdict(dict)
    rows = conn.execute(
        """
        SELECT market_name,handicap,outcome_name,price
        FROM oddspapi_market_prices
        WHERE fixture_id=%s AND snapshot_hour=%s
          AND price IS NOT NULL AND price > 1.001 AND COALESCE(active,TRUE)=TRUE
        """,
        (fixture_id, latest),
    ).fetchall()
    for name,line,outcome,price in rows:
        market = market_kind(name,line)
        if not market:
            continue
        side = outcome_side(market,outcome)
        if side is None:
            continue
        p = float(price)
        if side not in out[market] or p > out[market][side]:
            out[market][side] = p
    return dict(out)


def no_vig_selected(prices: Dict[bool,float], selected: bool) -> Tuple[Optional[float],Optional[float]]:
    selected_price = prices.get(selected)
    other_price = prices.get(not selected)
    if selected_price is None:
        return None, None
    if other_price is None:
        return selected_price, None
    a, b = 1.0 / selected_price, 1.0 / other_price
    total = a + b
    return selected_price, (a / total if total > 0 else None)


def market_probability(pred: Prediction, market: str) -> float:
    if market == "over_2_5":
        return pred.p_over_2_5
    if market == "btts":
        return pred.p_btts
    return pred.p_corners_over_8_5


def market_label(market: str, yes: bool) -> str:
    labels = {
        "over_2_5": ("2.5 ÜST","2.5 ALT"),
        "btts": ("BTTS VAR","BTTS YOK"),
        "corners_over_8_5": ("8.5 KORNER ÜST","8.5 KORNER ALT"),
    }
    return labels[market][0 if yes else 1]


def run_predictions(database_url: Optional[str] = None, *, start: Optional[datetime] = None, end: Optional[datetime] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    now = datetime.now(timezone.utc)
    start = start or now
    end = end or (start + timedelta(days=PREDICTION_LOOKAHEAD_DAYS))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
        run_id = conn.execute(
            """INSERT INTO production_prediction_runs(model_version,policy_version,horizon_start,horizon_end,status)
               VALUES(%s,%s,%s,%s,'running') RETURNING id""",
            (MODEL_VERSION,POLICY_VERSION,start,end),
        ).fetchone()[0]
        fixtures_scored = market_rows = 0
        candidates: List[Dict[str,Any]] = []
        try:
            upcoming = conn.execute(
                """SELECT event_id,match_date,league_name,home_team,away_team
                   FROM espn_upcoming
                   WHERE is_current=TRUE AND match_date >= %s AND match_date <= %s
                   ORDER BY match_date""",
                (start,end),
            ).fetchall()

            histories: Dict[str,List[Dict[str,Any]]] = {}
            for event_id,match_date,league,home,away in upcoming:
                if league not in histories:
                    histories[league] = history_rows(conn,league,match_date)
                history = [m for m in histories[league] if as_date(m["match_date"]) is None or as_date(m["match_date"]) <= match_date.date()]
                if not history:
                    continue
                pred = predict_match(history,canon(home),canon(away))
                fixtures_scored += 1
                context = latest_context(conn,event_id)
                readiness = latest_readiness(conn,event_id)
                prices = market_prices(conn,context.get("oddspapi_fixture_id"))
                ready_by_market = {
                    "over_2_5": bool(readiness.get("goals_ready")),
                    "btts": bool(readiness.get("btts_ready")),
                    "corners_over_8_5": bool(readiness.get("corners_ready")),
                }
                blockers = list(readiness.get("blockers") or [])
                snapshot_hour = readiness.get("snapshot_hour") or context.get("snapshot_hour") or now.replace(minute=0,second=0,microsecond=0)

                fixture_candidates: List[Dict[str,Any]] = []
                for market in ("over_2_5","btts","corners_over_8_5"):
                    p_yes = market_probability(pred,market)
                    yes = p_yes >= 0.5
                    confidence = p_yes if yes else 1.0-p_yes
                    price, market_prob = no_vig_selected(prices.get(market,{}) , yes)
                    agreement = 0.5 if market_prob is None else max(0.0, 1.0-abs(confidence-market_prob))
                    readiness_score = float(readiness.get("score") or 0.0)
                    ranking = confidence * (0.70+0.30*pred.data_quality) * (0.75+0.25*readiness_score) * (0.85+0.15*agreement)
                    provisional_ready = ready_by_market[market]
                    eligible = bool(
                        provisional_ready
                        and confidence >= PREDICTION_MIN_CONFIDENCE
                        and price is not None and price >= PREDICTION_MIN_PRICE
                    )
                    details = pred.as_dict()
                    conn.execute(
                        """
                        INSERT INTO production_predictions(
                          run_id,event_id,snapshot_hour,match_date,league_name,home_team,away_team,
                          market,selection,selection_yes,model_probability,market_no_vig_probability,
                          market_price,ranking_score,model_data_quality,readiness_score,provisional_ready,
                          final_context_ready,xg_used,odds_snapshot_age_hours,home_days_rest,away_days_rest,
                          home_injuries,away_injuries,availability_source,blockers,model_details)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                          run_id,event_id,snapshot_hour,match_date,league,home,away,market,market_label(market,yes),yes,
                          confidence,market_prob,price,round(ranking,6),pred.data_quality,readiness_score,provisional_ready,
                          bool(readiness.get("final_ready")),pred.xg_used,context.get("odds_age"),context.get("home_rest"),
                          context.get("away_rest"),context.get("home_inj"),context.get("away_inj"),context.get("availability_source"),
                          Jsonb(blockers),Jsonb(details),
                        ),
                    )
                    market_rows += 1
                    if eligible:
                        fixture_candidates.append({
                            "event_id":event_id,"market":market,"ranking":ranking,"confidence":confidence,
                            "price":price,"match_date":match_date,"league":league,"home":home,"away":away,
                            "selection":market_label(market,yes),"final":bool(readiness.get("final_ready")),
                        })
                if fixture_candidates:
                    candidates.append(max(fixture_candidates,key=lambda x:x["ranking"]))

            ranked = sorted(candidates,key=lambda x:(x["ranking"],x["confidence"]),reverse=True)[:10]
            for rank,item in enumerate(ranked,1):
                conn.execute(
                    "UPDATE production_predictions SET top10_rank=%s WHERE run_id=%s AND event_id=%s AND market=%s",
                    (rank,run_id,item["event_id"],item["market"]),
                )

            conn.execute(
                """UPDATE production_prediction_runs SET finished_at=NOW(),fixtures_scored=%s,market_rows=%s,
                       candidate_matches=%s,top10_count=%s,status='success',message=%s WHERE id=%s""",
                (fixtures_scored,market_rows,len(candidates),len(ranked),
                 "Top-10 is not force-filled; only quality-gated candidates are ranked.",run_id),
            )
            result = {
                "status":"success","run_id":run_id,"model_version":MODEL_VERSION,"policy_version":POLICY_VERSION,
                "horizon_start":start.isoformat(),"horizon_end":end.isoformat(),"fixtures_scored":fixtures_scored,
                "market_rows":market_rows,"candidate_matches":len(candidates),"top10_count":len(ranked),
                "top10":[{k:(v.isoformat() if isinstance(v,datetime) else v) for k,v in x.items()} for x in ranked],
            }
            print("PRODUCTION_PREDICTIONS_RESULT",json.dumps(result,ensure_ascii=False,separators=(",",":")))
            return result
        except Exception as exc:
            conn.execute(
                "UPDATE production_prediction_runs SET finished_at=NOW(),fixtures_scored=%s,market_rows=%s,status='failed',message=%s WHERE id=%s",
                (fixtures_scored,market_rows,str(exc)[:1000],run_id),
            )
            raise


if __name__ == "__main__":
    print(json.dumps(run_predictions(),ensure_ascii=False,indent=2,default=str))
