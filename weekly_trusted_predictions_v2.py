#!/usr/bin/env python3
"""Weekly reliability list with guarded 1X2 as a fourth market family.

The existing BTTS/goal/corner engine remains unchanged. When the dedicated 1X2
probability audit registry is active, the most likely 1/0/2 outcome for each fixture
is evaluated with the same data-quality, schedule, player-context, Turkey-price and
international-contradiction guards, then competes against the existing best market
for that fixture. One selection per fixture is preserved.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg

from lineup_stability_v2_policy import (
    ACTIVE_MODE as LINEUP_V2_ACTIVE_MODE,
    POLICY_KEY as LINEUP_V2_POLICY_KEY,
    factor_from_live_environment as lineup_v2_factor_from_environment,
)
from match_environment_builder import latest_environment
from model_engine_v1 import predict_match
from one_x_two_engine import from_v1_prediction, ranked_outcomes
from one_x_two_market_audit import ACTIVE_MODE as ONE_X_TWO_ACTIVE_MODE, POLICY_KEY as ONE_X_TWO_POLICY_KEY
from one_x_two_market_reference import latest_ref as latest_1x2_ref, selected_probability as selected_1x2_probability
from production_predictor import canon
from ranking_challenger_stack_policy import (
    FEATURE_LINEUP as STACK_FEATURE_LINEUP,
    MODE_V1 as STACK_MODE_V1,
    POLICY_KEY as RANKING_STACK_POLICY_KEY,
    feature_enabled as stack_feature_enabled,
    live_factors as ranking_stack_live_factors,
)
from research_change_control import registry_activation_mode
from schedule_context import SCHEDULE_CONTEXT_VERSION, team_schedule_context
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
from weekly_trusted_predictions import MIN_WEEKLY_PICKS, build as build_base


def _public_1x2(row: Dict[str, Any]) -> Dict[str, Any]:
    ref = row.get("international") or {}
    return {
        "event_id": row["event_id"],
        "match_date": row["match_date"],
        "league": row["league"],
        "home": row["home"],
        "away": row["away"],
        "market": "match_result",
        "selection": row["selection"],
        "confidence": row["confidence"],
        "model_probability_estimate": row["confidence"],
        "confidence_semantics": "selected_1x2_probability_from_frozen_v1_home_away_poisson_lambdas",
        "confidence_tier": "Yüksek Güven" if row["confidence"] >= HIGH_CONFIDENCE_MIN else "Haftanın En Güvenilirleri",
        "strict_high_confidence": row["confidence"] >= HIGH_CONFIDENCE_MIN,
        "ranking_score": row["ranking_score"],
        "schedule_rank_factor": row.get("schedule_rank_factor", 1.0),
        "goal_model_mode": "v1",
        "advanced_goal_available": True,
        "lineup_v2_active": row.get("lineup_v2_active", False),
        "lineup_v2_available": row.get("lineup_v2_available", False),
        "lineup_v2_factor": row.get("lineup_v2_factor", 1.0),
        "ranking_stack_mode": row.get("ranking_stack_mode", STACK_MODE_V1),
        "ranking_stack_factor": row.get("ranking_stack_factor", 1.0),
        "missing_player_available": row.get("missing_player_available", False),
        "missing_player_factor": row.get("missing_player_factor", 1.0),
        "corner_specific_available": False,
        "corner_specific_factor": 1.0,
        "tr_price": row["price"].get("tr_price"),
        "tr_opening_price": row["price"].get("tr_opening_price"),
        "tr_source": row["price"].get("tr_source"),
        "international_fair_probability": row.get("international_selected_probability"),
        "international_bookmakers": ref.get("bookmaker_count"),
        "international_quality": ref.get("quality"),
        "market_check": row["market_check"],
        "early_context": row["early_context"],
        "one_x_two_probabilities": row.get("one_x_two_probabilities"),
        "one_x_two_registry_mode": ONE_X_TWO_ACTIVE_MODE,
    }


def _build_1x2_candidates(database_url: str, *, now: datetime) -> Dict[str, Any]:
    week_key, start, end = weekend_bounds(now)
    with psycopg.connect(database_url, autocommit=True) as conn:
        one_x_two_mode = registry_activation_mode(conn, ONE_X_TWO_POLICY_KEY)
        if one_x_two_mode != ONE_X_TWO_ACTIVE_MODE:
            return {"active": False, "registry_mode": one_x_two_mode, "rows": [], "excluded": {}}

        ranking_stack_mode = registry_activation_mode(conn, RANKING_STACK_POLICY_KEY)
        ranking_stack_active = ranking_stack_mode != STACK_MODE_V1
        lineup_v2_mode = registry_activation_mode(conn, LINEUP_V2_POLICY_KEY)
        legacy_lineup_v2_active = (not ranking_stack_active) and lineup_v2_mode == LINEUP_V2_ACTIVE_MODE
        stack_lineup_active = ranking_stack_active and stack_feature_enabled(ranking_stack_mode, STACK_FEATURE_LINEUP)

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
            one = from_v1_prediction(pred)
            selection, confidence = ranked_outcomes(one)[0]

            home_player_ctx, away_player_ctx = _player_context(conn, str(home)), _player_context(conn, str(away))
            legacy_home_rest = _last_rest_days(history, str(home), match_date)
            legacy_away_rest = _last_rest_days(history, str(away), match_date)
            home_sched = team_schedule_context(conn, str(home), match_date, as_of=now, fallback_rest_days=legacy_home_rest)
            away_sched = team_schedule_context(conn, str(away), match_date, as_of=now, fallback_rest_days=legacy_away_rest)
            if home_sched.get("pending_pre_fixture_match") or away_sched.get("pending_pre_fixture_match"):
                excluded["pending_intervening_official_match"] += 1
                continue

            eligible, blockers, gate_diag = _early_gate(
                pred, home_player_ctx, away_player_ctx, home_sched.get("rest_days"), away_sched.get("rest_days")
            )
            if not eligible:
                for blocker in blockers:
                    excluded[blocker] += 1
                continue

            price = _price_payload(conn, str(eid), "match_result", selection)
            if not price:
                excluded["selected_1x2_not_priced"] += 1
                continue

            ref = latest_1x2_ref(conn, str(eid))
            ref_selected = selected_1x2_probability(ref, selection)
            if ref_selected is not None and abs(float(confidence) - ref_selected) > INTERNATIONAL_MAX_MODEL_DIVERGENCE:
                excluded["strong_1x2_international_contradiction"] += 1
                continue
            market_check = "aligned" if ref_selected is not None else "reference_unavailable"

            schedule_factor = min(float(home_sched.get("rank_factor") or 0.0), float(away_sched.get("rank_factor") or 0.0))
            environment = latest_environment(conn, str(eid)) or {}
            legacy_lineup_factor, legacy_lineup_available = (1.0, False)
            if legacy_lineup_v2_active:
                legacy_lineup_factor, legacy_lineup_available = lineup_v2_factor_from_environment(environment)

            if ranking_stack_active:
                stack = ranking_stack_live_factors(environment, "match_result", True, ranking_stack_mode)
                ranking_factor = float(stack["combined_factor"])
                lineup_factor = float(stack["lineup_factor"])
                lineup_available = bool(stack["lineup_available"])
            else:
                stack = {
                    "mode": STACK_MODE_V1,
                    "combined_factor": float(legacy_lineup_factor),
                    "lineup_factor": float(legacy_lineup_factor),
                    "lineup_available": bool(legacy_lineup_available),
                    "missing_player_factor": 1.0,
                    "missing_player_available": False,
                }
                ranking_factor = float(legacy_lineup_factor)
                lineup_factor = float(legacy_lineup_factor)
                lineup_available = bool(legacy_lineup_available)

            data_quality = float(gate_diag.get("model_data_quality") or pred.data_quality)
            ranking_score = float(confidence) * (0.75 + 0.25 * data_quality) * schedule_factor * ranking_factor
            gate_diag.update({
                "schedule_context_version": SCHEDULE_CONTEXT_VERSION,
                "schedule_scope": "all_competitions_with_domestic_fallback",
                "schedule_rank_factor": schedule_factor,
                "home_schedule": home_sched,
                "away_schedule": away_sched,
                "match_environment": environment,
                "one_x_two_policy_key": ONE_X_TWO_POLICY_KEY,
                "one_x_two_registry_mode": one_x_two_mode,
                "one_x_two_market_active": True,
            })
            rows.append({
                "event_id": str(eid), "match_date": match_date, "league": league_s,
                "home": str(home), "away": str(away), "selection": selection,
                "confidence": float(confidence), "ranking_score": ranking_score,
                "schedule_rank_factor": schedule_factor,
                "lineup_v2_active": bool(legacy_lineup_v2_active or stack_lineup_active),
                "lineup_v2_available": lineup_available, "lineup_v2_factor": lineup_factor,
                "ranking_stack_mode": stack.get("mode", STACK_MODE_V1),
                "ranking_stack_factor": ranking_factor,
                "missing_player_available": bool(stack.get("missing_player_available", False)),
                "missing_player_factor": float(stack.get("missing_player_factor", 1.0)),
                "price": price, "international": ref,
                "international_selected_probability": ref_selected,
                "market_check": market_check, "early_context": gate_diag,
                "one_x_two_probabilities": {"1": round(one.p1, 6), "0": round(one.px, 6), "2": round(one.p2, 6)},
            })

        return {
            "active": True,
            "registry_mode": one_x_two_mode,
            "rows": [_public_1x2(r) for r in rows],
            "excluded": dict(excluded),
        }


def build(database_url: str = DATABASE_URL, *, now: Optional[datetime] = None, limit: int = LIST_LIMIT) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    as_of = now or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    # Ask the existing engine for the complete one-per-fixture baseline, then let
    # one guarded 1X2 candidate per fixture compete with that baseline.
    base = build_base(database_url, now=as_of, limit=1000)
    x12 = _build_1x2_candidates(database_url, now=as_of)
    if not x12["active"]:
        picks = (base.get("picks") or [])[:limit]
        strict = [p for p in picks if p.get("strict_high_confidence")]
        out = dict(base)
        out["picks"] = picks
        out["strict_high_confidence"] = strict
        out["strict_high_confidence_count"] = len(strict)
        out["ranked_pick_count"] = len(picks)
        policy = dict(out.get("policy") or {})
        policy.update({
            "one_x_two_policy_key": ONE_X_TWO_POLICY_KEY,
            "one_x_two_registry_mode": x12["registry_mode"],
            "one_x_two_market_active": False,
            "one_x_two_fail_closed": True,
        })
        out["policy"] = policy
        return out

    best_by_fixture: Dict[str, Dict[str, Any]] = {}
    for item in list(base.get("picks") or []) + list(x12["rows"]):
        key = str(item.get("event_id"))
        cur = best_by_fixture.get(key)
        candidate_key = (
            float(item.get("ranking_score") or 0.0),
            float(item.get("confidence") or 0.0),
            item.get("market_check") == "aligned",
        )
        current_key = (
            float(cur.get("ranking_score") or 0.0),
            float(cur.get("confidence") or 0.0),
            cur.get("market_check") == "aligned",
        ) if cur else None
        if cur is None or candidate_key > current_key:
            best_by_fixture[key] = item

    merged = list(best_by_fixture.values())
    merged.sort(
        key=lambda p: (
            float(p.get("ranking_score") or 0.0),
            float(p.get("confidence") or 0.0),
            p.get("market_check") == "aligned",
        ),
        reverse=True,
    )
    picks = merged[:limit]
    strict = [p for p in picks if p.get("strict_high_confidence")]
    ready = float(base.get("official_fixture_coverage") or 0.0) >= 0.90 and len(picks) >= min(
        MIN_WEEKLY_PICKS, max(1, int(base.get("fixture_count") or 0))
    )

    result = dict(base)
    result.update({
        "ready": ready,
        "picks": picks,
        "strict_high_confidence": strict,
        "strict_high_confidence_count": len(strict),
        "ranked_pick_count": len(picks),
        "one_x_two_diagnostics": {
            "registry_mode": x12["registry_mode"],
            "candidate_rows": len(x12["rows"]),
            "excluded_counts": x12["excluded"],
            "one_x_two_in_ranked_picks": sum(p.get("market") == "match_result" for p in picks),
            "probability_source": "frozen_v1_home_away_poisson_lambdas",
            "turkey_price_required": True,
            "international_reference": "three_way_same_book_no_vig_when_available",
        },
    })
    policy = dict(result.get("policy") or {})
    policy.update({
        "ranking": "existing_guarded_market_pool_plus_holdout_safe_1x2_candidate",
        "one_x_two_policy_key": ONE_X_TWO_POLICY_KEY,
        "one_x_two_registry_mode": x12["registry_mode"],
        "one_x_two_market_active": True,
        "one_x_two_fail_closed": True,
        "one_x_two_probability_source": "frozen_v1_home_away_poisson_lambdas",
        "one_x_two_turkey_price_required": True,
        "one_x_two_international_reference": "three_way_same_book_no_vig",
    })
    result["policy"] = policy
    print("WEEKLY_TRUSTED_V2_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
    return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2, default=str))
