#!/usr/bin/env python3
"""Thursday-only decision engine for early Turkish betting.

User workflow:
    Thursday -> two actionable lists -> place bets -> done.

Production principles:
- model confidence comes only from the validated V1 football model;
- only O2.5 goals, BTTS Yes and O8.5 corners are considered;
- foreign bookmaker prices never influence confidence or value;
- an actionable pick must have a valid Turkish İddaa price;
- known early uncertainty can exclude a pick;
- T-1/T-3 and confirmed-XI gates are not part of the betting decision.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from model_engine_v1 import predict_match
from production_predictor import canon
from turkey_value_workflow import DDL as TURKEY_PRICE_DDL, latest_tr_price

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ISTANBUL = ZoneInfo("Europe/Istanbul")

HIGH_CONFIDENCE_MIN = float(os.getenv("HIGH_CONFIDENCE_MIN", "0.70"))
VALUE_MIN_CONFIDENCE = float(os.getenv("VALUE_MIN_CONFIDENCE", "0.65"))
VALUE_MIN_EDGE = float(os.getenv("VALUE_MIN_EDGE", "0.015"))
VALUE_MIN_EV = float(os.getenv("VALUE_MIN_EV", "0.02"))
MIN_MODEL_DATA_QUALITY = float(os.getenv("THURSDAY_MIN_MODEL_DATA_QUALITY", "0.80"))
MIN_PLAYER_COVERAGE = float(os.getenv("THURSDAY_MIN_PLAYER_COVERAGE", "0.70"))
MIN_STARTER_CONTINUITY = float(os.getenv("THURSDAY_MIN_STARTER_CONTINUITY", "0.50"))
MAX_KNOWN_INJURY_IMPACT = float(os.getenv("THURSDAY_MAX_KNOWN_INJURY_IMPACT", "0.20"))
MIN_REST_DAYS = float(os.getenv("THURSDAY_MIN_REST_DAYS", "2.5"))
BULLETIN_FIXTURE_COVERAGE_MIN = float(os.getenv("THURSDAY_BULLETIN_COVERAGE_MIN", "0.85"))
HIGH_PRICE_COVERAGE_MIN = float(os.getenv("THURSDAY_HIGH_PRICE_COVERAGE_MIN", "0.80"))
LIST_LIMIT = int(os.getenv("THURSDAY_LIST_LIMIT", "10"))

MARKETS = (
    ("over_2_5", "2.5 ÜST", "p_over_2_5"),
    ("btts", "KG VAR", "p_btts"),
    ("corners_over_8_5", "8.5 KORNER ÜST", "p_corners_over_8_5"),
)

DDL = """
CREATE TABLE IF NOT EXISTS thursday_decision_runs(
 id BIGSERIAL PRIMARY KEY,
 week_key DATE NOT NULL,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL DEFAULT 'running',
 horizon_start TIMESTAMPTZ NOT NULL,
 horizon_end TIMESTAMPTZ NOT NULL,
 fixture_count INTEGER NOT NULL DEFAULT 0,
 model_market_rows INTEGER NOT NULL DEFAULT 0,
 eligible_market_rows INTEGER NOT NULL DEFAULT 0,
 official_events INTEGER NOT NULL DEFAULT 0,
 matched_fixtures INTEGER NOT NULL DEFAULT 0,
 official_fixture_coverage DOUBLE PRECISION NOT NULL DEFAULT 0,
 raw_high_candidates INTEGER NOT NULL DEFAULT 0,
 priced_high_candidates INTEGER NOT NULL DEFAULT 0,
 high_confidence_count INTEGER NOT NULL DEFAULT 0,
 value_count INTEGER NOT NULL DEFAULT 0,
 decision_ready BOOLEAN NOT NULL DEFAULT FALSE,
 high_confidence JSONB NOT NULL DEFAULT '[]'::jsonb,
 high_confidence_value JSONB NOT NULL DEFAULT '[]'::jsonb,
 diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
 message TEXT
);
CREATE INDEX IF NOT EXISTS idx_thursday_decision_week
 ON thursday_decision_runs(week_key,started_at DESC);
"""


def json_default(v: Any):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def weekend_bounds(now: Optional[datetime] = None) -> Tuple[date, datetime, datetime]:
    """Friday 00:00 through Monday 00:00 Istanbul time (Fri-Sun matches)."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ISTANBUL)
    days_to_friday = (4 - local.weekday()) % 7
    friday = local.date() + timedelta(days=days_to_friday)
    start_local = datetime.combine(friday, time.min, tzinfo=ISTANBUL)
    end_local = start_local + timedelta(days=3)
    return friday, start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _row_date(v: Any) -> date:
    return v.date() if isinstance(v, datetime) else v


def _history_rows(conn, league: str, before: datetime) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    queries = [
        """SELECT match_date,home_team,away_team,home_goals,away_goals,
                  home_shots_on_target,away_shots_on_target,home_corners,away_corners
             FROM football_data_matches
            WHERE league_name=%s AND match_date<%s
              AND home_goals IS NOT NULL AND away_goals IS NOT NULL
            ORDER BY match_date""",
        """SELECT match_date,home_team,away_team,home_goals,away_goals,
                  home_shots_on_target,away_shots_on_target,home_corners,away_corners
             FROM espn_current_matches
            WHERE league_name=%s AND match_date<%s
              AND home_goals IS NOT NULL AND away_goals IS NOT NULL
            ORDER BY match_date""",
    ]
    for sql in queries:
        try:
            for dt, h, a, hg, ag, hs, ass, hc, ac in conn.execute(sql, (league, before)).fetchall():
                rows.append({
                    "match_date": dt, "home_team": canon(h), "away_team": canon(a),
                    "home_goals": hg, "away_goals": ag,
                    "home_shots_on_target": hs, "away_shots_on_target": ass,
                    "home_corners": hc, "away_corners": ac,
                })
        except Exception:
            continue
    dedup: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        dedup[(_row_date(row["match_date"]), row["home_team"], row["away_team"])] = row
    return sorted(dedup.values(), key=lambda r: (_row_date(r["match_date"]), r["home_team"], r["away_team"]))


def _last_rest_days(history: Iterable[Dict[str, Any]], team: str, match_date: datetime) -> Optional[float]:
    t = canon(team)
    last: Optional[date] = None
    for row in history:
        if t not in (row.get("home_team"), row.get("away_team")):
            continue
        d = _row_date(row.get("match_date"))
        if isinstance(d, date) and d < match_date.date() and (last is None or d > last):
            last = d
    return float((match_date.date() - last).days) if last else None


def _player_context(conn, team: str) -> Dict[str, Any]:
    try:
        row = conn.execute(
            """SELECT expected_xi_strength,injury_impact,goalkeeper_injured,
                      starter_continuity,player_coverage,key_absences,source_meta
                 FROM player_team_context_snapshots
                WHERE team_name=%s ORDER BY snapshot_hour DESC LIMIT 1""",
            (team,),
        ).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    return {
        "expected_xi_strength": row[0], "injury_impact": row[1], "goalkeeper_injured": row[2],
        "starter_continuity": row[3], "player_coverage": row[4], "key_absences": row[5], "source_meta": row[6],
    }


def _early_gate(pred, home_ctx: Dict[str, Any], away_ctx: Dict[str, Any], home_rest: Optional[float], away_rest: Optional[float]) -> Tuple[bool, List[str], Dict[str, Any]]:
    blockers: List[str] = []
    quality = float(getattr(pred, "data_quality", 0.0) or 0.0)
    if quality < MIN_MODEL_DATA_QUALITY:
        blockers.append("thin_match_sample")

    covs = [float(x.get("player_coverage") or 0.0) for x in (home_ctx, away_ctx)]
    player_cov = sum(covs) / 2.0
    if player_cov < MIN_PLAYER_COVERAGE:
        blockers.append("player_context_coverage_low")

    continuities = [float(x["starter_continuity"]) for x in (home_ctx, away_ctx) if x.get("starter_continuity") is not None]
    min_cont = min(continuities) if continuities else None
    if min_cont is not None and min_cont < MIN_STARTER_CONTINUITY:
        blockers.append("starter_continuity_low")

    impacts = [float(x.get("injury_impact") or 0.0) for x in (home_ctx, away_ctx)]
    max_impact = max(impacts) if impacts else 0.0
    if max_impact >= MAX_KNOWN_INJURY_IMPACT:
        blockers.append("known_injury_impact_high")
    if bool(home_ctx.get("goalkeeper_injured")) or bool(away_ctx.get("goalkeeper_injured")):
        blockers.append("goalkeeper_injury")

    rests = [r for r in (home_rest, away_rest) if r is not None]
    min_rest = min(rests) if rests else None
    if min_rest is not None and min_rest < MIN_REST_DAYS:
        blockers.append("extreme_short_rest")

    diagnostics = {
        "model_data_quality": quality, "player_coverage": player_cov,
        "min_starter_continuity": min_cont, "max_known_injury_impact": max_impact,
        "home_rest_days": home_rest, "away_rest_days": away_rest,
    }
    return not blockers, blockers, diagnostics


def _latest_import_coverage(conn, fixture_count: int) -> Dict[str, Any]:
    try:
        row = conn.execute(
            """SELECT official_events,matched_fixtures,production_fixtures,target_market_rows,stored_prices,finished_at,status
                 FROM turkey_odds_import_runs ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    except Exception:
        row = None
    if not row:
        return {"official_events": 0, "matched_fixtures": 0, "fixture_coverage": 0.0, "target_market_rows": 0, "stored_prices": 0}
    denominator = int(row[2] or fixture_count or 0)
    matched = int(row[1] or 0)
    return {
        "official_events": int(row[0] or 0), "matched_fixtures": matched,
        "fixture_coverage": (matched / denominator) if denominator else 0.0,
        "target_market_rows": int(row[3] or 0), "stored_prices": int(row[4] or 0),
        "import_finished_at": row[5], "import_status": row[6],
    }


def _price_payload(conn, event_id: str, market: str, selection: str) -> Optional[Dict[str, Any]]:
    row = latest_tr_price(conn, event_id, market, selection)
    if not row:
        return None
    source, price, fetched_at, opening_price, first_seen_at = row
    return {
        "tr_source": str(source), "tr_price": float(price), "tr_price_at": fetched_at,
        "tr_opening_price": float(opening_price) if opening_price is not None else None,
        "tr_opening_at": first_seen_at,
    }


def _one_per_fixture(rows: Iterable[Dict[str, Any]], key_fn):
    best: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cur = best.get(row["event_id"])
        if cur is None or key_fn(row) > key_fn(cur):
            best[row["event_id"]] = row
    return list(best.values())


def build_decision(database_url: str = DATABASE_URL, *, now: Optional[datetime] = None, limit: int = LIST_LIMIT) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    week_key, start, end = weekend_bounds(now)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(TURKEY_PRICE_DDL)
        conn.execute(DDL)
        run_id = int(conn.execute(
            "INSERT INTO thursday_decision_runs(week_key,horizon_start,horizon_end) VALUES(%s,%s,%s) RETURNING id",
            (week_key, start, end),
        ).fetchone()[0])
        try:
            fixtures = conn.execute(
                """SELECT event_id,match_date,league_name,home_team,away_team
                     FROM espn_upcoming WHERE is_current=TRUE AND match_date>=%s AND match_date<%s ORDER BY match_date""",
                (start, end),
            ).fetchall()
            histories: Dict[str, List[Dict[str, Any]]] = {}
            market_rows: List[Dict[str, Any]] = []
            excluded_counts: Dict[str, int] = defaultdict(int)

            for eid, match_date, league, home, away in fixtures:
                if league not in histories:
                    histories[str(league)] = _history_rows(conn, str(league), match_date)
                history = histories[str(league)]
                if not history:
                    excluded_counts["no_history"] += 1
                    continue
                pred = predict_match(history, canon(home), canon(away), recent_matches=18)
                home_ctx, away_ctx = _player_context(conn, str(home)), _player_context(conn, str(away))
                home_rest = _last_rest_days(history, str(home), match_date)
                away_rest = _last_rest_days(history, str(away), match_date)
                eligible, blockers, gate_diag = _early_gate(pred, home_ctx, away_ctx, home_rest, away_rest)
                for blocker in blockers:
                    excluded_counts[blocker] += 1
                probs = {
                    "p_over_2_5": float(pred.p_over_2_5),
                    "p_btts": float(pred.p_btts),
                    "p_corners_over_8_5": float(pred.p_corners_over_8_5),
                }
                for market, selection, attr in MARKETS:
                    market_rows.append({
                        "event_id": str(eid), "match_date": match_date, "league": str(league),
                        "home": str(home), "away": str(away), "market": market, "selection": selection,
                        "confidence": probs[attr], "eligible": bool(eligible), "blockers": blockers,
                        "early_context": gate_diag, "price": _price_payload(conn, str(eid), market, selection),
                    })

            raw_high = [r for r in market_rows if r["eligible"] and r["confidence"] >= HIGH_CONFIDENCE_MIN]
            priced_high = [r for r in raw_high if r["price"]]
            raw_high_fixtures = _one_per_fixture(raw_high, lambda r: (r["confidence"],))
            priced_high_fixtures = _one_per_fixture(priced_high, lambda r: (r["confidence"],))
            high_rows = sorted(priced_high_fixtures, key=lambda r: r["confidence"], reverse=True)

            high_list: List[Dict[str, Any]] = []
            for r in high_rows[:limit]:
                pinfo = r["price"] or {}
                high_list.append({
                    "event_id": r["event_id"], "match_date": r["match_date"], "league": r["league"],
                    "home": r["home"], "away": r["away"], "market": r["market"], "selection": r["selection"],
                    "confidence": r["confidence"], "tr_price": pinfo.get("tr_price"),
                    "tr_opening_price": pinfo.get("tr_opening_price"), "tr_source": pinfo.get("tr_source"),
                    "early_context": r["early_context"],
                })

            value_candidates: List[Dict[str, Any]] = []
            for r in market_rows:
                if not r["eligible"] or r["confidence"] < VALUE_MIN_CONFIDENCE or not r["price"]:
                    continue
                price = float(r["price"]["tr_price"])
                market_p = 1.0 / price
                edge = r["confidence"] - market_p
                ev = r["confidence"] * price - 1.0
                if edge >= VALUE_MIN_EDGE and ev >= VALUE_MIN_EV:
                    item = dict(r)
                    item.update({"market_implied_probability": market_p, "edge": edge, "ev": ev})
                    value_candidates.append(item)
            value_rows = _one_per_fixture(value_candidates, lambda r: (r["confidence"], r["ev"], r["edge"]))
            value_rows.sort(key=lambda r: (r["confidence"], r["ev"], r["edge"]), reverse=True)

            value_list: List[Dict[str, Any]] = []
            for r in value_rows[:limit]:
                pinfo = r["price"] or {}
                value_list.append({
                    "event_id": r["event_id"], "match_date": r["match_date"], "league": r["league"],
                    "home": r["home"], "away": r["away"], "market": r["market"], "selection": r["selection"],
                    "confidence": r["confidence"], "tr_price": pinfo.get("tr_price"),
                    "tr_opening_price": pinfo.get("tr_opening_price"), "tr_source": pinfo.get("tr_source"),
                    "market_implied_probability": r["market_implied_probability"], "edge": r["edge"], "ev": r["ev"],
                    "early_context": r["early_context"],
                })

            coverage = _latest_import_coverage(conn, len(fixtures))
            official_coverage = float(coverage.get("fixture_coverage") or 0.0)
            required_priced_high = math.ceil(len(raw_high_fixtures) * HIGH_PRICE_COVERAGE_MIN) if raw_high_fixtures else 0
            high_price_coverage = (len(priced_high_fixtures) / len(raw_high_fixtures)) if raw_high_fixtures else 0.0
            decision_ready = bool(
                raw_high_fixtures
                and official_coverage >= BULLETIN_FIXTURE_COVERAGE_MIN
                and len(priced_high_fixtures) >= required_priced_high
                and len(high_list) > 0
            )

            diagnostics = {
                "policy": {
                    "markets": [m[0] for m in MARKETS], "high_confidence_min": HIGH_CONFIDENCE_MIN,
                    "value_min_confidence": VALUE_MIN_CONFIDENCE, "value_min_edge": VALUE_MIN_EDGE,
                    "value_min_ev": VALUE_MIN_EV, "min_model_data_quality": MIN_MODEL_DATA_QUALITY,
                    "min_player_coverage": MIN_PLAYER_COVERAGE, "min_starter_continuity": MIN_STARTER_CONTINUITY,
                    "max_known_injury_impact": MAX_KNOWN_INJURY_IMPACT, "min_rest_days": MIN_REST_DAYS,
                    "bulletin_fixture_coverage_min": BULLETIN_FIXTURE_COVERAGE_MIN,
                    "high_price_coverage_min": HIGH_PRICE_COVERAGE_MIN,
                    "foreign_odds_used": False, "confirmed_lineup_required": False,
                },
                "excluded_counts": dict(excluded_counts), "turkey_import": coverage,
                "raw_high_market_candidates": len(raw_high), "priced_high_market_candidates": len(priced_high),
                "raw_high_fixture_candidates": len(raw_high_fixtures),
                "priced_high_fixture_candidates": len(priced_high_fixtures),
                "high_price_coverage": high_price_coverage,
            }

            result = {
                "status": "success", "decision_run_id": run_id, "week_key": week_key,
                "horizon_start": start, "horizon_end": end, "fixture_count": len(fixtures),
                "model_market_rows": len(market_rows), "eligible_market_rows": sum(1 for r in market_rows if r["eligible"]),
                "official_events": int(coverage.get("official_events") or 0),
                "matched_fixtures": int(coverage.get("matched_fixtures") or 0),
                "official_fixture_coverage": official_coverage,
                "raw_high_candidates": len(raw_high_fixtures), "priced_high_candidates": len(priced_high_fixtures),
                "decision_ready": decision_ready, "high_confidence": high_list,
                "high_confidence_value": value_list, "diagnostics": diagnostics,
                "generated_at": datetime.now(timezone.utc),
            }
            conn.execute(
                """UPDATE thursday_decision_runs SET finished_at=NOW(),status='success',fixture_count=%s,
                          model_market_rows=%s,eligible_market_rows=%s,official_events=%s,matched_fixtures=%s,
                          official_fixture_coverage=%s,raw_high_candidates=%s,priced_high_candidates=%s,
                          high_confidence_count=%s,value_count=%s,decision_ready=%s,
                          high_confidence=%s,high_confidence_value=%s,diagnostics=%s,message=%s WHERE id=%s""",
                (
                    len(fixtures), len(market_rows), sum(1 for r in market_rows if r["eligible"]),
                    int(coverage.get("official_events") or 0), int(coverage.get("matched_fixtures") or 0),
                    official_coverage, len(raw_high_fixtures), len(priced_high_fixtures), len(high_list), len(value_list),
                    decision_ready,
                    Jsonb(high_list, dumps=lambda x: json.dumps(x, default=json_default, ensure_ascii=False)),
                    Jsonb(value_list, dumps=lambda x: json.dumps(x, default=json_default, ensure_ascii=False)),
                    Jsonb(diagnostics, dumps=lambda x: json.dumps(x, default=json_default, ensure_ascii=False)),
                    "Thursday early-play decision; Turkey price only; no T-1/T-3 gate", run_id,
                ),
            )
            print("THURSDAY_DECISION_RESULT", json.dumps(result, ensure_ascii=False, default=json_default, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            conn.execute("UPDATE thursday_decision_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s", (str(exc)[:1500], run_id))
            raise


if __name__ == "__main__":
    print(json.dumps(build_decision(), ensure_ascii=False, indent=2, default=json_default))
