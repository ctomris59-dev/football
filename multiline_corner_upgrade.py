#!/usr/bin/env python3
"""Upgrade a frozen weekly reliability list with executable multi-line corners.

This is a market-line coverage extension, not a newly fitted model: the frozen V1
already produces lambda_total_corners. We evaluate that same Poisson intensity at
7.5/8.5/9.5/10.5 and only admit a side when official Turkish İddaa actually prices
that exact side. The strict value list remains separate and unchanged unless a valid
international reference already exists for that exact market key.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

import psycopg
import requests
from psycopg.types.json import Jsonb

from corner_multiline import corner_market_specs, corner_selections, normalize_corner_line
from international_market_reference import latest_ref
from model_engine_v1 import predict_match
from production_predictor import canon
from thursday_decision_engine import (
    DATABASE_URL,
    HIGH_CONFIDENCE_MIN,
    INTERNATIONAL_MAX_MODEL_DIVERGENCE,
    LIST_LIMIT,
    _early_gate,
    _history_rows,
    _last_rest_days,
    _player_context,
    _price_payload,
    weekend_bounds,
)
from turkey_iddaa_odds_collector import (
    SOURCE,
    _ascii,
    _get_json,
    _line_value,
    _weekend_fixtures,
    match_fixture,
    render_market_name,
)
from turkey_value_workflow import store_price, valid_price

FORCE = os.getenv("MULTILINE_CORNER_FORCE", "false").strip().lower() in {"1", "true", "yes"}


def _corner_market_line(rendered_name: str, market: Dict[str, Any]) -> Optional[float]:
    name = _ascii(rendered_name)
    if "ilk yari" in name or "first half" in name:
        return None
    if "toplam korner sayisi" not in name and "total corners" not in name:
        return None
    return normalize_corner_line(_line_value(market, rendered_name))


def _side(outcome_name: Any) -> Optional[str]:
    name = _ascii(outcome_name)
    if name in {"ust", "over"} or name.startswith("ust ") or name.startswith("over "):
        return "yes"
    if name in {"alt", "under"} or name.startswith("alt ") or name.startswith("under "):
        return "no"
    return None


def collect_multiline_prices(database_url: str) -> Dict[str, Any]:
    """Store both sides for supported official Turkish corner lines."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        fixtures, horizon_start, horizon_end = _weekend_fixtures(conn)
        if not fixtures:
            return {"status": "no_fixtures", "stored_rows": 0, "selection_counts": {}}
        session = requests.Session()
        session.headers.update({"Accept": "application/json", "User-Agent": "football-multiline-corners/1.0"})
        events_payload = _get_json(session, "events?st=1&type=0&version=0")
        config_payload = _get_json(session, "get_market_config")
        events = ((events_payload.get("data") or {}).get("events") or [])
        market_config = ((config_payload.get("data") or {}).get("m") or {})
        matched = set()
        stored = 0
        counts: Dict[str, int] = defaultdict(int)
        for event in events:
            fixture, _ambiguous = match_fixture(event, fixtures)
            if not fixture:
                continue
            matched.add(fixture["event_id"])
            for market in event.get("m") or []:
                rendered = render_market_name(market, market_config)
                line = _corner_market_line(rendered, market)
                if line is None:
                    continue
                spec = next(x for x in corner_market_specs(9.0) if abs(float(x["line"]) - line) < 1e-9)
                market_key = str(spec["market"])
                yes_selection, no_selection = corner_selections(line)
                for outcome in market.get("o") or []:
                    side = _side(outcome.get("n"))
                    if not side or not valid_price(outcome.get("odd")):
                        continue
                    selection = yes_selection if side == "yes" else no_selection
                    counts[f"{market_key}:{side}"] += 1
                    if store_price(conn, fixture["event_id"], market_key, selection, SOURCE, float(outcome["odd"])):
                        stored += 1
        return {
            "status": "success",
            "official_events": len(events),
            "production_fixtures": len(fixtures),
            "matched_fixtures": len(matched),
            "fixture_coverage": round(len(matched) / len(fixtures), 4) if fixtures else 0.0,
            "stored_rows": stored,
            "selection_counts": dict(counts),
            "horizon_start": horizon_start,
            "horizon_end": horizon_end,
        }


def _selected_ref_probability(ref: Optional[Dict[str, Any]], selected_yes: bool) -> Optional[float]:
    if not ref or ref.get("reference_p_yes") is None:
        return None
    p = float(ref["reference_p_yes"])
    return p if selected_yes else 1.0 - p


def _corner_public(row: Dict[str, Any]) -> Dict[str, Any]:
    ref = row.get("international") or {}
    return {
        "event_id": row["event_id"],
        "match_date": row["match_date"],
        "league": row["league"],
        "home": row["home"],
        "away": row["away"],
        "market": row["market"],
        "selection": row["selection"],
        "confidence": row["confidence"],
        "model_probability_estimate": row["confidence"],
        "confidence_semantics": "selected_side_raw_v1_probability_from_same_frozen_corner_lambda",
        "confidence_tier": "Yüksek Güven" if row["confidence"] >= HIGH_CONFIDENCE_MIN else "Haftanın En Güvenilirleri",
        "strict_high_confidence": row["confidence"] >= HIGH_CONFIDENCE_MIN,
        "ranking_score": row["ranking_score"],
        "tr_price": row["price"].get("tr_price"),
        "tr_opening_price": row["price"].get("tr_opening_price"),
        "tr_source": row["price"].get("tr_source"),
        "international_fair_probability": row.get("international_selected_probability"),
        "international_bookmakers": ref.get("bookmaker_count"),
        "international_quality": ref.get("quality"),
        "market_check": row["market_check"],
        "early_context": row["early_context"],
        "corner_line": row["corner_line"],
    }


def build_multiline_corner_candidates(database_url: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    week_key, start, end = weekend_bounds(now)
    with psycopg.connect(database_url) as conn:
        fixtures = conn.execute(
            """SELECT event_id,match_date,league_name,home_team,away_team
                 FROM espn_upcoming
                WHERE is_current=TRUE AND match_date>=%s AND match_date<%s
                ORDER BY match_date""",
            (start, end),
        ).fetchall()
        histories: Dict[str, list[Dict[str, Any]]] = {}
        candidates: list[Dict[str, Any]] = []
        excluded: Dict[str, int] = defaultdict(int)
        offered: Dict[str, int] = defaultdict(int)
        for eid, match_date, league, home, away in fixtures:
            league_s = str(league)
            if league_s not in histories:
                histories[league_s] = _history_rows(conn, league_s, match_date)
            history = histories[league_s]
            if not history:
                excluded["no_history"] += 1
                continue
            pred = predict_match(history, canon(home), canon(away), recent_matches=18)
            home_ctx = _player_context(conn, str(home))
            away_ctx = _player_context(conn, str(away))
            eligible, blockers, gate_diag = _early_gate(
                pred,
                home_ctx,
                away_ctx,
                _last_rest_days(history, str(home), match_date),
                _last_rest_days(history, str(away), match_date),
            )
            if not eligible:
                for blocker in blockers:
                    excluded[blocker] += 1
                continue
            for spec in corner_market_specs(pred.lambda_total_corners):
                market = str(spec["market"])
                p_yes = float(spec["p_yes"])
                selected_yes = p_yes >= 0.5
                confidence = p_yes if selected_yes else 1.0 - p_yes
                selection = spec["yes_selection"] if selected_yes else spec["no_selection"]
                price = _price_payload(conn, str(eid), market, selection)
                if not price:
                    excluded["selected_corner_line_not_priced"] += 1
                    continue
                offered[market] += 1
                ref = latest_ref(conn, str(eid), market)
                ref_selected = _selected_ref_probability(ref, selected_yes)
                if ref_selected is not None and abs(confidence - ref_selected) > INTERNATIONAL_MAX_MODEL_DIVERGENCE:
                    excluded["strong_international_contradiction"] += 1
                    continue
                data_quality = float(gate_diag.get("model_data_quality") or 0.0)
                candidates.append({
                    "event_id": str(eid),
                    "match_date": match_date,
                    "league": league_s,
                    "home": str(home),
                    "away": str(away),
                    "market": market,
                    "corner_line": float(spec["line"]),
                    "selection": selection,
                    "confidence": confidence,
                    "ranking_score": confidence * (0.75 + 0.25 * data_quality),
                    "price": price,
                    "international": ref,
                    "international_selected_probability": ref_selected,
                    "market_check": "aligned" if ref_selected is not None else "reference_unavailable",
                    "early_context": gate_diag,
                })
        best: Dict[str, Dict[str, Any]] = {}
        for row in candidates:
            cur = best.get(row["event_id"])
            key = (float(row["ranking_score"]), float(row["confidence"]), row["market_check"] == "aligned")
            if cur is None or key > (float(cur["ranking_score"]), float(cur["confidence"]), cur["market_check"] == "aligned"):
                best[row["event_id"]] = row
        return {
            "week_key": week_key,
            "fixture_count": len(fixtures),
            "candidate_rows": len(candidates),
            "best_per_fixture": [_corner_public(x) for x in best.values()],
            "offered_market_counts": dict(offered),
            "excluded_counts": dict(excluded),
        }


def upgrade_final(database_url: str = DATABASE_URL, *, now: Optional[datetime] = None, limit: int = LIST_LIMIT) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    week_key, _start, _end = weekend_bounds(now)
    prices = collect_multiline_prices(database_url)
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT decision_run_id,payload,source FROM thursday_final_decisions WHERE week_key=%s",
            (week_key,),
        ).fetchone()
    if not row:
        return {"status": "waiting_for_final", "week_key": week_key, "multiline_prices": prices}
    existing_payload = dict(row[1] or {})
    primary_policy = dict((existing_payload.get("policy") or {}).get("primary_list") or {})
    if primary_policy.get("multiline_corners") is True and not FORCE:
        return {"status": "already_upgraded", "week_key": week_key, "multiline_prices": prices}

    # Rebuild the base list at a wide limit so dynamic corners compete fairly with
    # every fixture's strongest existing goal/BTTS/fixed-corner candidate.
    from weekly_trusted_predictions import build as build_base
    base = build_base(database_url, now=now, limit=100)
    corner = build_multiline_corner_candidates(database_url, now=now)

    best: Dict[str, Dict[str, Any]] = {}
    for item in list(base.get("picks") or []) + list(corner.get("best_per_fixture") or []):
        eid = str(item.get("event_id"))
        cur = best.get(eid)
        key = (
            float(item.get("ranking_score") or item.get("confidence") or 0.0),
            float(item.get("confidence") or 0.0),
            item.get("market_check") == "aligned",
        )
        if cur is None:
            best[eid] = item
        else:
            cur_key = (
                float(cur.get("ranking_score") or cur.get("confidence") or 0.0),
                float(cur.get("confidence") or 0.0),
                cur.get("market_check") == "aligned",
            )
            if key > cur_key:
                best[eid] = item
    ordered = sorted(
        best.values(),
        key=lambda x: (
            float(x.get("ranking_score") or x.get("confidence") or 0.0),
            float(x.get("confidence") or 0.0),
            x.get("market_check") == "aligned",
        ),
        reverse=True,
    )[:limit]
    strict = [x for x in ordered if bool(x.get("strict_high_confidence"))]

    payload = existing_payload
    payload["finalized_at"] = datetime.now(timezone.utc)
    payload["high_confidence"] = ordered
    payload["weekly_reliable"] = ordered
    payload["strict_high_confidence"] = strict
    policy = dict(payload.get("policy") or {})
    primary = dict(policy.get("primary_list") or {})
    primary.update({
        "multiline_corners": True,
        "corner_lines": [7.5, 8.5, 9.5, 10.5],
        "corner_probability_source": "same_frozen_v1_lambda_total_corners_poisson_threshold",
        "turkey_exact_line_price_required": True,
        "value_separate": True,
    })
    policy["primary_list"] = primary
    payload["policy"] = policy
    payload["multiline_corner_diagnostics"] = {
        "prices": prices,
        "candidate_rows": corner.get("candidate_rows"),
        "best_corner_fixtures": len(corner.get("best_per_fixture") or []),
        "offered_market_counts": corner.get("offered_market_counts") or {},
        "excluded_counts": corner.get("excluded_counts") or {},
        "corners_in_final_top10": sum(str(x.get("market") or "").startswith("corners_over_") for x in ordered),
    }
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            """UPDATE thursday_final_decisions
                  SET finalized_at=NOW(),payload=%s,source='v1_reliability+multiline_corners+optional_value+iddaa_official'
                WHERE week_key=%s""",
            (Jsonb(payload, dumps=lambda x: json.dumps(x, ensure_ascii=False, default=str)), week_key),
        )
    result = {
        "status": "upgraded",
        "week_key": week_key,
        "multiline_prices": prices,
        "corner_diagnostics": payload["multiline_corner_diagnostics"],
        "final_picks": ordered,
        "strict_high_confidence": strict,
        "high_confidence_value": payload.get("high_confidence_value") or [],
    }
    print("MULTILINE_CORNER_UPGRADE_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
    return result


if __name__ == "__main__":
    print(json.dumps(upgrade_final(), ensure_ascii=False, indent=2, default=str))
