#!/usr/bin/env python3
"""Build the strict, variable-length weekly playable shortlist.

Frozen V1 remains the probability baseline. A selection reaches the user-facing
list only when Turkey executable price, international no-vig validation, player
context, injury-feed coverage and positive model EV are all present. The list may
contain zero to LIST_LIMIT selections; Top-10 is never force-filled.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import psycopg

from advanced_goal_models import (
    MODE_V1 as ADVANCED_V1_MODE,
    POLICY_KEY as ADVANCED_GOAL_POLICY_KEY,
    apply_mode as apply_advanced_goal_mode,
)
from corner_multiline import SUPPORTED_CORNER_LINES, corner_market_specs
from international_market_reference import latest_ref
from lineup_stability_v2_policy import (
    ACTIVE_MODE as LINEUP_V2_ACTIVE_MODE,
    POLICY_KEY as LINEUP_V2_POLICY_KEY,
    factor_from_live_environment as lineup_v2_factor_from_environment,
)
from match_environment_builder import latest_environment
from model_engine_v1 import predict_match
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
from strict_selection_policy_v3 import POLICY_VERSION, policy_payload, qualify_candidate
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

# Retained for backward compatibility with callers/tests. v3 does not require a
# minimum number of picks; sufficient bulletin coverage can finalize an empty list.
MIN_WEEKLY_PICKS = 0

GOAL_MARKETS = (
    ("over_2_5", "p_over_2_5", "2.5 ÜST", "2.5 ALT"),
    ("btts", "p_btts", "KG VAR", "KG YOK"),
)
MARKETS = GOAL_MARKETS + (
    ("corners_over_8_5", "p_corners_over_8_5", "8.5 KORNER ÜST", "8.5 KORNER ALT"),
)


def _fixture_market_specs(pred: Any, advanced: Dict[str, Any]) -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    for market, attr, yes_selection, no_selection in GOAL_MARKETS:
        specs.append({
            "market": market,
            "p_yes": float(advanced.get(attr, getattr(pred, attr))),
            "yes_selection": yes_selection,
            "no_selection": no_selection,
            "corner_line": None,
            "confidence_semantics": None,
        })
    for spec in corner_market_specs(pred.lambda_total_corners):
        specs.append({
            "market": str(spec["market"]),
            "p_yes": float(spec["p_yes"]),
            "yes_selection": str(spec["yes_selection"]),
            "no_selection": str(spec["no_selection"]),
            "corner_line": float(spec["line"]),
            "confidence_semantics": "selected_side_raw_v1_probability_from_same_frozen_corner_lambda",
        })
    return specs


def _selected_market_probability(ref: Optional[Dict[str, Any]], selected_yes: bool) -> Optional[float]:
    if not ref or ref.get("reference_p_yes") is None:
        return None
    p_yes = float(ref["reference_p_yes"])
    return p_yes if selected_yes else 1.0 - p_yes


def _latest_corner_pressure_signal(conn, event_id: str) -> Optional[float]:
    try:
        row = conn.execute(
            """SELECT corner_pressure_signal FROM fixture_pressure_snapshots
                WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
    except Exception:
        return None
    try:
        return float(row[0]) if row and row[0] is not None else None
    except (TypeError, ValueError):
        return None


def latest_injury_covered_teams(conn) -> Set[str]:
    """Teams explicitly present in the freshest FotMob availability snapshot.

    An empty injury list is valid only when the team itself is present in the feed.
    This prevents missing injury data from silently becoming zero injury impact.
    """
    try:
        hour_row = conn.execute(
            "SELECT MAX(snapshot_hour) FROM fotmob_fixture_availability_snapshots"
        ).fetchone()
        hour = hour_row[0] if hour_row else None
        if hour is None:
            return set()
        rows = conn.execute(
            """SELECT home_team,away_team FROM fotmob_fixture_availability_snapshots
                WHERE snapshot_hour=%s""",
            (hour,),
        ).fetchall()
    except Exception:
        return set()
    covered: Set[str] = set()
    for home, away in rows:
        if home:
            covered.add(canon(home))
        if away:
            covered.add(canon(away))
    return covered


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    ref = row.get("international") or {}
    goal_mode = row.get("goal_model_mode", ADVANCED_V1_MODE)
    semantics = row.get("confidence_semantics") or (
        "selected_side_raw_v1_probability_estimate_not_perfectly_calibrated"
        if goal_mode == ADVANCED_V1_MODE
        else "selected_side_guarded_advanced_goal_probability_estimate_not_perfectly_calibrated"
    )
    qualification = row.get("qualification") or {}
    out = {
        "event_id": row["event_id"],
        "match_date": row["match_date"],
        "league": row["league"],
        "home": row["home"],
        "away": row["away"],
        "market": row["market"],
        "selection": row["selection"],
        "confidence": row["confidence"],
        "model_probability_estimate": row["confidence"],
        "confidence_semantics": semantics,
        # Do not market raw point estimates as "high confidence" until live
        # calibration/shadow evidence validates that terminology.
        "confidence_tier": "Doğrulanmış Aday",
        "strict_high_confidence": row["confidence"] >= HIGH_CONFIDENCE_MIN,
        "ranking_score": row["ranking_score"],
        "schedule_rank_factor": row.get("schedule_rank_factor", 1.0),
        "goal_model_mode": goal_mode,
        "advanced_goal_available": row.get("advanced_goal_available", True),
        "lineup_v2_active": row.get("lineup_v2_active", False),
        "lineup_v2_available": row.get("lineup_v2_available", False),
        "lineup_v2_factor": row.get("lineup_v2_factor", 1.0),
        "ranking_stack_mode": row.get("ranking_stack_mode", STACK_MODE_V1),
        "ranking_stack_factor": row.get("ranking_stack_factor", 1.0),
        "missing_player_available": row.get("missing_player_available", False),
        "missing_player_factor": row.get("missing_player_factor", 1.0),
        "corner_specific_available": row.get("corner_specific_available", False),
        "corner_specific_factor": row.get("corner_specific_factor", 1.0),
        "tr_price": row["price"].get("tr_price"),
        "tr_opening_price": row["price"].get("tr_opening_price"),
        "tr_source": row["price"].get("tr_source"),
        "international_fair_probability": row.get("international_selected_probability"),
        "international_bookmakers": ref.get("bookmaker_count"),
        "international_quality": ref.get("quality"),
        "market_check": "aligned",
        "qualification": "strict_playable",
        "model_edge_vs_tr": qualification.get("model_edge_vs_tr"),
        "model_ev_vs_tr": qualification.get("model_ev_vs_tr"),
        "model_market_gap": qualification.get("model_market_gap"),
        "tr_implied_probability": qualification.get("tr_implied_probability"),
        "policy_version": POLICY_VERSION,
        "early_context": row["early_context"],
    }
    if row.get("corner_line") is not None:
        out["corner_line"] = float(row["corner_line"])
    return out


def build(database_url: str = DATABASE_URL, *, now: Optional[datetime] = None, limit: int = LIST_LIMIT) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    as_of = now or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    week_key, start, end = weekend_bounds(as_of)

    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(TURKEY_PRICE_DDL)
        ranking_stack_mode = registry_activation_mode(conn, RANKING_STACK_POLICY_KEY)
        ranking_stack_active = ranking_stack_mode != STACK_MODE_V1
        lineup_v2_mode = registry_activation_mode(conn, LINEUP_V2_POLICY_KEY)
        legacy_lineup_v2_active = (not ranking_stack_active) and lineup_v2_mode == LINEUP_V2_ACTIVE_MODE
        stack_lineup_active = ranking_stack_active and stack_feature_enabled(ranking_stack_mode, STACK_FEATURE_LINEUP)
        advanced_goal_mode = registry_activation_mode(conn, ADVANCED_GOAL_POLICY_KEY)
        injury_covered = latest_injury_covered_teams(conn)

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
        offered_corner_markets: Dict[str, int] = defaultdict(int)
        corner_candidate_rows = 0

        for eid, match_date, league, home, away in fixtures:
            league_s = str(league)
            if league_s not in histories:
                histories[league_s] = _history_rows(conn, league_s, match_date)
            history = histories[league_s]
            if not history:
                excluded["no_history"] += 1
                continue

            pred = predict_match(history, canon(home), canon(away), recent_matches=18)
            advanced = apply_advanced_goal_mode(pred, history, canon(home), canon(away), advanced_goal_mode)
            advanced_available = bool(advanced.get("available", True))
            effective_goal_mode = advanced_goal_mode
            if advanced_goal_mode != ADVANCED_V1_MODE and not advanced_available:
                advanced = apply_advanced_goal_mode(pred, history, canon(home), canon(away), ADVANCED_V1_MODE)
                effective_goal_mode = ADVANCED_V1_MODE

            home_player_ctx = _player_context(conn, str(home))
            away_player_ctx = _player_context(conn, str(away))
            legacy_home_rest = _last_rest_days(history, str(home), match_date)
            legacy_away_rest = _last_rest_days(history, str(away), match_date)
            home_sched = team_schedule_context(conn, str(home), match_date, as_of=as_of, fallback_rest_days=legacy_home_rest)
            away_sched = team_schedule_context(conn, str(away), match_date, as_of=as_of, fallback_rest_days=legacy_away_rest)

            if home_sched.get("pending_pre_fixture_match") or away_sched.get("pending_pre_fixture_match"):
                excluded["pending_intervening_official_match"] += 1
                continue

            # Missing availability feed coverage is not equivalent to zero injuries.
            if canon(home) not in injury_covered or canon(away) not in injury_covered:
                excluded["injury_context_missing"] += 1
                continue

            home_rest = home_sched.get("rest_days")
            away_rest = away_sched.get("rest_days")
            eligible, blockers, gate_diag = _early_gate(pred, home_player_ctx, away_player_ctx, home_rest, away_rest)
            if not eligible:
                for blocker in blockers:
                    excluded[blocker] += 1
                continue

            schedule_factor = min(float(home_sched.get("rank_factor") or 0.0), float(away_sched.get("rank_factor") or 0.0))
            environment = latest_environment(conn, str(eid)) or {}
            corner_pressure = _latest_corner_pressure_signal(conn, str(eid))
            if corner_pressure is not None:
                environment = dict(environment)
                pressure = dict(environment.get("pressure") or {}) if isinstance(environment.get("pressure"), dict) else {}
                pressure["corner_pressure_signal"] = corner_pressure
                environment["pressure"] = pressure

            legacy_lineup_factor, legacy_lineup_available = (1.0, False)
            if legacy_lineup_v2_active:
                legacy_lineup_factor, legacy_lineup_available = lineup_v2_factor_from_environment(environment)

            gate_diag.update({
                "schedule_context_version": SCHEDULE_CONTEXT_VERSION,
                "schedule_scope": "all_competitions_with_domestic_fallback",
                "schedule_rank_factor": schedule_factor,
                "home_schedule": home_sched,
                "away_schedule": away_sched,
                "injury_context_complete": True,
                "match_environment": environment,
                "match_environment_semantics": "shadow_unless_guarded_policy_activated",
                "advanced_goal_policy_key": ADVANCED_GOAL_POLICY_KEY,
                "advanced_goal_registry_mode": advanced_goal_mode,
                "advanced_goal_effective_mode": effective_goal_mode,
                "advanced_goal_available": advanced_available,
                "advanced_goal_meta": advanced.get("meta") or {},
                "lineup_v2_policy_key": LINEUP_V2_POLICY_KEY,
                "lineup_v2_mode": lineup_v2_mode,
                "lineup_v2_active": bool(legacy_lineup_v2_active or stack_lineup_active),
                "ranking_stack_policy_key": RANKING_STACK_POLICY_KEY,
                "ranking_stack_mode": ranking_stack_mode,
                "ranking_stack_active": ranking_stack_active,
            })

            for spec in _fixture_market_specs(pred, advanced):
                market = str(spec["market"])
                p_yes = float(spec["p_yes"])
                selected_yes = p_yes >= 0.5
                confidence = p_yes if selected_yes else 1.0 - p_yes
                selection = spec["yes_selection"] if selected_yes else spec["no_selection"]
                is_corner = spec.get("corner_line") is not None

                price = _price_payload(conn, str(eid), market, selection)
                if not price:
                    excluded["selected_corner_line_not_priced" if is_corner else "selected_side_not_priced"] += 1
                    continue
                if is_corner:
                    offered_corner_markets[market] += 1

                ref = latest_ref(conn, str(eid), market)
                ref_selected = _selected_market_probability(ref, selected_yes)
                qualification = qualify_candidate(
                    confidence=confidence,
                    tr_price=price.get("tr_price"),
                    international_probability=ref_selected,
                    international_quality=(ref or {}).get("quality"),
                    international_bookmakers=(ref or {}).get("bookmaker_count"),
                    max_model_divergence=INTERNATIONAL_MAX_MODEL_DIVERGENCE,
                )
                if not qualification.get("qualified"):
                    excluded[str(qualification.get("reason") or "strict_policy_rejected")] += 1
                    continue

                data_quality = float(gate_diag.get("model_data_quality") or 0.0)
                if ranking_stack_active:
                    stack = ranking_stack_live_factors(environment, market, selected_yes, ranking_stack_mode)
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
                        "corner_specific_factor": 1.0,
                        "corner_specific_available": False,
                    }
                    ranking_factor = float(legacy_lineup_factor)
                    lineup_factor = float(legacy_lineup_factor)
                    lineup_available = bool(legacy_lineup_available)

                ranking_score = confidence * (0.75 + 0.25 * data_quality) * schedule_factor * ranking_factor
                rows.append({
                    "event_id": str(eid), "match_date": match_date, "league": league_s,
                    "home": str(home), "away": str(away), "market": market, "selection": selection,
                    "confidence": confidence, "ranking_score": ranking_score,
                    "schedule_rank_factor": schedule_factor,
                    "goal_model_mode": effective_goal_mode, "advanced_goal_available": advanced_available,
                    "lineup_v2_active": bool(legacy_lineup_v2_active or stack_lineup_active),
                    "lineup_v2_available": lineup_available, "lineup_v2_factor": lineup_factor,
                    "ranking_stack_mode": stack.get("mode", STACK_MODE_V1),
                    "ranking_stack_factor": ranking_factor,
                    "missing_player_available": bool(stack.get("missing_player_available", False)),
                    "missing_player_factor": float(stack.get("missing_player_factor", 1.0)),
                    "corner_specific_available": bool(stack.get("corner_specific_available", False)),
                    "corner_specific_factor": float(stack.get("corner_specific_factor", 1.0)),
                    "price": price, "international": ref,
                    "international_selected_probability": ref_selected,
                    "market_check": "aligned", "qualification": qualification,
                    "early_context": gate_diag,
                    "corner_line": spec.get("corner_line"),
                    "confidence_semantics": spec.get("confidence_semantics"),
                })
                if is_corner:
                    corner_candidate_rows += 1

        one_per_fixture = _one_per_fixture(
            rows,
            lambda r: (float(r["ranking_score"]), float(r["confidence"]), float((r.get("qualification") or {}).get("model_ev_vs_tr") or 0.0)),
        )
        one_per_fixture.sort(
            key=lambda r: (float(r["ranking_score"]), float(r["confidence"]), float((r.get("qualification") or {}).get("model_ev_vs_tr") or 0.0)),
            reverse=True,
        )
        picks = [_public(r) for r in one_per_fixture[:limit]]
        strict_high = [p for p in picks if p["strict_high_confidence"]]
        coverage = _latest_import_coverage(conn, len(fixtures))
        official_coverage = float(coverage.get("fixture_coverage") or 0.0)
        # v3 explicitly permits a finalized zero-pick week. Coverage, not quota,
        # determines whether the universe was sufficiently observed.
        ready = official_coverage >= 0.90

        result = {
            "status": "success",
            "policy_version": POLICY_VERSION,
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
            "multiline_corner_diagnostics": {
                "evaluated_lines": list(SUPPORTED_CORNER_LINES),
                "offered_market_counts": dict(offered_corner_markets),
                "candidate_rows": corner_candidate_rows,
                "corners_in_ranked_picks": sum(str(p.get("market") or "").startswith("corners_over_") for p in picks),
                "turkey_exact_line_price_required": True,
            },
            "policy": {
                **policy_payload(),
                "strict_high_confidence_min": HIGH_CONFIDENCE_MIN,
                "ranking": "strict_playable_probability_x_data_quality_x_schedule_x_holdout_safe_ranking_stack",
                "advanced_goal_policy_key": ADVANCED_GOAL_POLICY_KEY,
                "advanced_goal_mode": advanced_goal_mode,
                "advanced_goal_fail_closed": True,
                "corner_lines": list(SUPPORTED_CORNER_LINES),
                "corner_probability_source": "same_frozen_v1_lambda_total_corners_poisson_threshold",
                "multiline_corners_in_fresh_preview": True,
                "turkey_exact_corner_line_price_required": True,
                "schedule_context_version": SCHEDULE_CONTEXT_VERSION,
                "schedule_scope": "all_competitions_completed_only_with_domestic_fallback",
                "injury_feed_coverage_required": True,
                "lineup_v2_policy_key": LINEUP_V2_POLICY_KEY,
                "lineup_v2_mode": lineup_v2_mode,
                "lineup_v2_active": bool(legacy_lineup_v2_active or stack_lineup_active),
                "ranking_stack_policy_key": RANKING_STACK_POLICY_KEY,
                "ranking_stack_mode": ranking_stack_mode,
                "ranking_stack_active": ranking_stack_active,
                "ranking_stack_fail_closed": True,
                "pending_intervening_official_match": "exclude_until_next_refresh",
                "international_reference": "mandatory quality-approved no-vig reference",
                "value_separate": False,
            },
        }
        print("WEEKLY_TRUSTED_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
        return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2, default=str))
