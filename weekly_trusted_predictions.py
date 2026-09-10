#!/usr/bin/env python3
"""Build the mandatory weekly reliability-ranked list from the frozen V1 model.

This is not a new model and does not alter V1 probabilities. It guarantees an
operational weekly *ranking* when sufficient Friday-Monday Turkish bulletin coverage
exists. A pick is called strict high confidence only when its selected-side V1
probability is >= the existing HIGH_CONFIDENCE_MIN. Lower-ranked entries are exposed
as "Haftanın En Güvenilirleri", never relabelled as 70%+.

International market data is a safety check when available: a strong contradiction
rejects the pick; missing international reference does not erase a model forecast.
Value remains a separate, stricter list.

Schedule context is operational only: the latest official club fixture is read across
domestic + UEFA competitions. Raw V1 probabilities stay frozen; short-rest context
only adjusts ranking and a still-unplayed official match before the target fixture
keeps that candidate pending until the next refresh.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg

from international_market_reference import latest_ref
from model_engine_v1 import predict_match
from production_predictor import canon
from schedule_context import SCHEDULE_CONTEXT_VERSION, team_schedule_context
from thursday_decision_engine import (
    DATABASE_URL,
    HIGH_CONFIDENCE_MIN,
    INTERNATIONAL_MAX_MODEL_DIVERGENCE,
    LIST_LIMIT,
    TURKEY_PRICE_DDL,
    _early_gate,
    _history_rows,
    _last_rest_days,
    _latest_import_coverage,
    _one_per_fixture,
    _player_context,
    _price_payload,
    weekend_bounds,
)

MIN_WEEKLY_PICKS = int(os.getenv("WEEKLY_RELIABLE_MIN_PICKS", "5"))

MARKETS = (
    ("over_2_5", "p_over_2_5", "2.5 ÜST", "2.5 ALT"),
    ("btts", "p_btts", "KG VAR", "KG YOK"),
    ("corners_over_8_5", "p_corners_over_8_5", "8.5 KORNER ÜST", "8.5 KORNER ALT"),
)


def _selected_market_probability(ref: Optional[Dict[str, Any]], selected_yes: bool) -> Optional[float]:
    if not ref or ref.get("reference_p_yes") is None:
        return None
    p_yes = float(ref["reference_p_yes"])
    return p_yes if selected_yes else 1.0 - p_yes


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
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
        "confidence_semantics": "selected_side_raw_v1_probability_estimate_not_perfectly_calibrated",
        "confidence_tier": "Yüksek Güven" if row["confidence"] >= HIGH_CONFIDENCE_MIN else "Haftanın En Güvenilirleri",
        "strict_high_confidence": row["confidence"] >= HIGH_CONFIDENCE_MIN,
        "ranking_score": row["ranking_score"],
        "schedule_rank_factor": row.get("schedule_rank_factor", 1.0),
        "tr_price": row["price"].get("tr_price"),
        "tr_opening_price": row["price"].get("tr_opening_price"),
        "tr_source": row["price"].get("tr_source"),
        "international_fair_probability": row.get("international_selected_probability"),
        "international_bookmakers": ref.get("bookmaker_count"),
        "international_quality": ref.get("quality"),
        "market_check": row["market_check"],
        "early_context": row["early_context"],
    }


def build(database_url: str = DATABASE_URL, *, now: Optional[datetime] = None, limit: int = LIST_LIMIT) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    as_of = now or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    week_key, start, end = weekend_bounds(as_of)
    with psycopg.connect(database_url) as conn:
        conn.execute(TURKEY_PRICE_DDL)
        fixtures = conn.execute(
            """SELECT event_id,match_date,league_name,home_team,away_team
                 FROM espn_upcoming
                WHERE is_current=TRUE AND match_date>=%s AND match_date<%s
                ORDER BY match_date""",
            (start, end),
        ).fetchall()

        histories: Dict[str, List[Dict[str, Any]]] = {}
        rows: List[Dict[str, Any]] = []
        excluded: Dict[str, int] = defaultdict(int)

        for eid, match_date, league, home, away in fixtures:
            league_s = str(league)
            if league_s not in histories:
                histories[league_s] = _history_rows(conn, league_s, match_date)
            history = histories[league_s]
            if not history:
                excluded["no_history"] += 1
                continue
            pred = predict_match(history, canon(home), canon(away), recent_matches=18)
            home_player_ctx, away_player_ctx = _player_context(conn, str(home)), _player_context(conn, str(away))

            legacy_home_rest = _last_rest_days(history, str(home), match_date)
            legacy_away_rest = _last_rest_days(history, str(away), match_date)
            home_sched = team_schedule_context(
                conn,
                str(home),
                match_date,
                as_of=as_of,
                fallback_rest_days=legacy_home_rest,
            )
            away_sched = team_schedule_context(
                conn,
                str(away),
                match_date,
                as_of=as_of,
                fallback_rest_days=legacy_away_rest,
            )

            if home_sched.get("pending_pre_fixture_match") or away_sched.get("pending_pre_fixture_match"):
                excluded["pending_intervening_official_match"] += 1
                continue

            home_rest = home_sched.get("rest_days")
            away_rest = away_sched.get("rest_days")
            eligible, blockers, gate_diag = _early_gate(
                pred,
                home_player_ctx,
                away_player_ctx,
                home_rest,
                away_rest,
            )
            if not eligible:
                for blocker in blockers:
                    excluded[blocker] += 1
                continue

            schedule_factor = min(
                float(home_sched.get("rank_factor") or 0.0),
                float(away_sched.get("rank_factor") or 0.0),
            )
            gate_diag.update({
                "schedule_context_version": SCHEDULE_CONTEXT_VERSION,
                "schedule_scope": "all_competitions_with_domestic_fallback",
                "schedule_rank_factor": schedule_factor,
                "home_schedule": home_sched,
                "away_schedule": away_sched,
            })

            for market, attr, yes_selection, no_selection in MARKETS:
                p_yes = float(getattr(pred, attr))
                selected_yes = p_yes >= 0.5
                confidence = p_yes if selected_yes else 1.0 - p_yes
                selection = yes_selection if selected_yes else no_selection
                price = _price_payload(conn, str(eid), market, selection)
                if not price:
                    excluded["selected_side_not_priced"] += 1
                    continue

                ref = latest_ref(conn, str(eid), market)
                ref_selected = _selected_market_probability(ref, selected_yes)
                if ref_selected is not None and abs(confidence - ref_selected) > INTERNATIONAL_MAX_MODEL_DIVERGENCE:
                    excluded["strong_international_contradiction"] += 1
                    continue
                market_check = "aligned" if ref_selected is not None else "reference_unavailable"
                data_quality = float(gate_diag.get("model_data_quality") or 0.0)
                ranking_score = confidence * (0.75 + 0.25 * data_quality) * schedule_factor
                rows.append({
                    "event_id": str(eid),
                    "match_date": match_date,
                    "league": league_s,
                    "home": str(home),
                    "away": str(away),
                    "market": market,
                    "selection": selection,
                    "confidence": confidence,
                    "ranking_score": ranking_score,
                    "schedule_rank_factor": schedule_factor,
                    "price": price,
                    "international": ref,
                    "international_selected_probability": ref_selected,
                    "market_check": market_check,
                    "early_context": gate_diag,
                })

        one_per_fixture = _one_per_fixture(
            rows,
            lambda r: (float(r["ranking_score"]), float(r["confidence"]), r["market_check"] == "aligned"),
        )
        one_per_fixture.sort(
            key=lambda r: (float(r["ranking_score"]), float(r["confidence"]), r["market_check"] == "aligned"),
            reverse=True,
        )
        picks = [_public(r) for r in one_per_fixture[:limit]]
        strict_high = [p for p in picks if p["strict_high_confidence"]]
        coverage = _latest_import_coverage(conn, len(fixtures))
        official_coverage = float(coverage.get("fixture_coverage") or 0.0)
        ready = official_coverage >= 0.90 and len(picks) >= min(MIN_WEEKLY_PICKS, max(1, len(fixtures)))
        result = {
            "status": "success",
            "week_key": week_key,
            "horizon_start": start,
            "horizon_end": end,
            "fixture_count": len(fixtures),
            "official_fixture_coverage": official_coverage,
            "ready": ready,
            "picks": picks,
            "strict_high_confidence": strict_high,
            "strict_high_confidence_count": len(strict_high),
            "ranked_pick_count": len(picks),
            "excluded_counts": dict(excluded),
            "policy": {
                "list_semantics": "weekly_reliability_ranking_not_guaranteed_70pct",
                "strict_high_confidence_min": HIGH_CONFIDENCE_MIN,
                "ranking": "existing_v1_probability_x_data_quality_x_operational_schedule_factor",
                "schedule_context_version": SCHEDULE_CONTEXT_VERSION,
                "schedule_scope": "all_competitions_with_domestic_fallback",
                "pending_intervening_official_match": "exclude_until_next_refresh",
                "turkey_price_required": True,
                "international_reference": "reject strong contradiction when available; missing reference allowed for reliability list",
                "value_separate": True,
            },
        }
        print("WEEKLY_TRUSTED_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
        return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2, default=str))
