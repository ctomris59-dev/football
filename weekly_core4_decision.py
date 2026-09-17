#!/usr/bin/env python3
"""Practical weekly Core4 decision selector with EV-aware ranking.

Purpose
-------
The strict-playable policy is intentionally conservative and may return zero rows.
That remains useful as a verification badge, but the primary weekly product is a
Core4 selected from the best Turkey-playable choices across the Friday-Monday slate.

Important semantics
-------------------
- Core4 means "best four risk-adjusted choices in this week's universe".
- It does NOT mean >=70% calibrated win probability or a guarantee.
- Strict-v3 remains a separate verification layer and can be empty.
- Turkey executable price is mandatory.
- A strong international-market contradiction is a hard reject.
- Missing international/player/injury context is an uncertainty penalty.
- EV is now a first-class ranking input: positive EV is preferred for Core4 and
  materially negative EV receives a strong asymmetric penalty.
- If four positive-EV fixtures exist, no negative-EV row may enter Core4.
- Current-season results are not used to tune thresholds here; this is an
  operational ranking contract around the frozen V1 probability engine.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import psycopg

from international_market_reference import latest_ref
from model_engine_v1 import predict_match
from one_x_two_engine import from_v1_prediction, ranked_outcomes
from one_x_two_market_reference import latest_ref as latest_1x2_ref, selected_probability as selected_1x2_probability
from production_predictor import canon
from schedule_context import SCHEDULE_CONTEXT_VERSION, team_schedule_context
from thursday_decision_engine import (
    DATABASE_URL,
    INTERNATIONAL_MAX_MODEL_DIVERGENCE,
    LIST_LIMIT,
    TURKEY_PRICE_DDL,
    _early_gate,
    _history_rows,
    _last_rest_days,
    _latest_import_coverage,
    _player_context,
    _price_payload,
    weekend_bounds,
)

POLICY_VERSION = "core4-decision-v2-ev-aware-2026-09-17"
CORE4_SIZE = 4
MIN_BINARY_CONFIDENCE = 0.54
MIN_1X2_CONFIDENCE = 0.40
CORE_MIN_EV = 0.0
CORE_NEAR_FAIR_MIN_EV = -0.02
HARD_MARKET_DIVERGENCE = max(0.16, float(INTERNATIONAL_MAX_MODEL_DIVERGENCE))

SOFT_BLOCKER_FACTORS: Dict[str, float] = {
    "thin_match_sample": 0.94,
    "player_context_coverage_low": 0.95,
    "starter_continuity_low": 0.96,
    "known_injury_impact_high": 0.94,
    "goalkeeper_injury": 0.94,
    "extreme_short_rest": 0.94,
}


def _latest_injury_covered_teams(conn) -> Set[str]:
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


def _binary_ref_selected(ref: Optional[Dict[str, Any]], selected_yes: bool) -> Optional[float]:
    if not ref or ref.get("reference_p_yes") is None:
        return None
    p_yes = float(ref["reference_p_yes"])
    return p_yes if selected_yes else 1.0 - p_yes


def _safe_schedule_factor(ctx: Dict[str, Any]) -> float:
    value = ctx.get("rank_factor")
    if value is None:
        return 0.96
    try:
        return min(1.02, max(0.88, float(value)))
    except (TypeError, ValueError):
        return 0.96


def _context_penalty(blockers: Iterable[str], injury_feed_complete: bool) -> float:
    factor = 1.0
    for blocker in blockers:
        factor *= SOFT_BLOCKER_FACTORS.get(str(blocker), 0.97)
    if not injury_feed_complete:
        factor *= 0.95
    return max(0.78, factor)


def _market_factor(confidence: float, ref_probability: Optional[float], quality: Optional[str]) -> Tuple[Optional[float], str, Optional[float]]:
    if ref_probability is None:
        return 0.95, "reference_missing_penalty", None
    gap = float(confidence) - float(ref_probability)
    if abs(gap) > HARD_MARKET_DIVERGENCE:
        return None, "strong_market_contradiction", gap
    factor = max(0.91, 1.0 - abs(gap) * 0.55)
    if str(quality or "") == "multi_book_consensus":
        factor = min(1.01, factor + 0.01)
    return factor, "aligned_or_tolerable", gap


def _price_factor(confidence: float, tr_price: float) -> Tuple[float, float, float]:
    """Return an asymmetric EV factor, edge and model EV at Turkey price.

    V1 only moved ranking by roughly +/-3%, which let deeply negative-EV choices
    remain near the top. V2 rewards positive EV modestly but penalizes negative EV
    much more aggressively. Probability still matters, but a bad executable price
    can no longer be almost ignored.
    """
    price = float(tr_price)
    implied = 1.0 / price
    edge = float(confidence) - implied
    ev = float(confidence) * price - 1.0
    if ev >= 0.0:
        factor = 1.0 + min(0.10, ev * 0.45)
    else:
        factor = max(0.72, 1.0 + ev * 1.20)
    return factor, edge, ev


def _ev_bucket(ev: float) -> int:
    if ev >= CORE_MIN_EV:
        return 2
    if ev >= CORE_NEAR_FAIR_MIN_EV:
        return 1
    return 0


def _row_rank_key(row: Dict[str, Any]) -> Tuple[int, float, float, float]:
    ev = float(row.get("model_ev_vs_tr") if row.get("model_ev_vs_tr") is not None else -9.0)
    return (
        _ev_bucket(ev),
        float(row["decision_score"]),
        float(row["confidence"]),
        ev,
    )


def _public(row: Dict[str, Any], rank: int, strict_keys: Set[Tuple[str, str, str]]) -> Dict[str, Any]:
    key = (str(row["event_id"]), str(row["market"]), str(row["selection"]))
    ev = float(row.get("model_ev_vs_tr") if row.get("model_ev_vs_tr") is not None else -9.0)
    return {
        "rank": rank,
        "list_tier": "core4" if rank <= CORE4_SIZE else "ranked_candidate",
        "event_id": row["event_id"],
        "match_date": row["match_date"],
        "league": row["league"],
        "home": row["home"],
        "away": row["away"],
        "market": row["market"],
        "selection": row["selection"],
        "confidence": row["confidence"],
        "model_probability_estimate": row["confidence"],
        "decision_score": row["decision_score"],
        "confidence_semantics": "raw frozen-v1 estimate; Core4 is a relative weekly rank, not a calibrated guarantee",
        "tr_price": row["tr_price"],
        "tr_opening_price": row.get("tr_opening_price"),
        "tr_source": row.get("tr_source"),
        "international_fair_probability": row.get("international_probability"),
        "international_quality": row.get("international_quality"),
        "international_bookmakers": row.get("international_bookmakers"),
        "market_check": row.get("market_check"),
        "model_market_gap": row.get("model_market_gap"),
        "model_edge_vs_tr": row.get("model_edge_vs_tr"),
        "model_ev_vs_tr": ev,
        "ev_status": "positive" if ev >= CORE_MIN_EV else ("near_fair" if ev >= CORE_NEAR_FAIR_MIN_EV else "negative"),
        "data_quality": row.get("data_quality"),
        "schedule_rank_factor": row.get("schedule_factor"),
        "context_penalty": row.get("context_penalty"),
        "injury_feed_complete": row.get("injury_feed_complete"),
        "soft_blockers": row.get("soft_blockers") or [],
        "strict_verified": key in strict_keys,
        "policy_version": POLICY_VERSION,
    }


def build(
    database_url: str = DATABASE_URL,
    *,
    now: Optional[datetime] = None,
    limit: int = LIST_LIMIT,
    strict_picks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    as_of = now or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    week_key, start, end = weekend_bounds(as_of)
    strict_keys: Set[Tuple[str, str, str]] = {
        (str(p.get("event_id") or ""), str(p.get("market") or ""), str(p.get("selection") or ""))
        for p in (strict_picks or [])
    }

    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(TURKEY_PRICE_DDL)
        injury_covered = _latest_injury_covered_teams(conn)
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
        market_counts: Dict[str, int] = defaultdict(int)

        for eid, match_date, league, home, away in fixtures:
            league_s = str(league)
            if league_s not in histories:
                histories[league_s] = _history_rows(conn, league_s, match_date)
            history = histories[league_s]
            if not history:
                excluded["no_history"] += 1
                continue

            try:
                pred = predict_match(history, canon(home), canon(away), recent_matches=18)
            except Exception:
                excluded["prediction_failed"] += 1
                continue

            home_ctx = _player_context(conn, str(home))
            away_ctx = _player_context(conn, str(away))
            legacy_home_rest = _last_rest_days(history, str(home), match_date)
            legacy_away_rest = _last_rest_days(history, str(away), match_date)
            home_sched = team_schedule_context(conn, str(home), match_date, as_of=as_of, fallback_rest_days=legacy_home_rest)
            away_sched = team_schedule_context(conn, str(away), match_date, as_of=as_of, fallback_rest_days=legacy_away_rest)
            if home_sched.get("pending_pre_fixture_match") or away_sched.get("pending_pre_fixture_match"):
                excluded["pending_intervening_official_match"] += 1
                continue

            _eligible, blockers, gate_diag = _early_gate(
                pred,
                home_ctx,
                away_ctx,
                home_sched.get("rest_days"),
                away_sched.get("rest_days"),
            )
            injury_complete = canon(home) in injury_covered and canon(away) in injury_covered
            context_factor = _context_penalty(blockers, injury_complete)
            schedule_factor = min(_safe_schedule_factor(home_sched), _safe_schedule_factor(away_sched))
            data_quality = min(1.0, max(0.35, float(gate_diag.get("model_data_quality") or getattr(pred, "data_quality", 0.0) or 0.0)))
            quality_factor = 0.80 + 0.20 * data_quality

            binary_specs = (
                ("over_2_5", float(pred.p_over_2_5), "2.5 ÜST", "2.5 ALT"),
                ("btts", float(pred.p_btts), "KG VAR", "KG YOK"),
            )
            for market, p_yes, yes_selection, no_selection in binary_specs:
                selected_yes = p_yes >= 0.5
                confidence = p_yes if selected_yes else 1.0 - p_yes
                if confidence < MIN_BINARY_CONFIDENCE:
                    excluded["binary_probability_too_close_to_coinflip"] += 1
                    continue
                selection = yes_selection if selected_yes else no_selection
                price = _price_payload(conn, str(eid), market, selection)
                if not price:
                    excluded["turkey_price_missing"] += 1
                    continue
                ref = latest_ref(conn, str(eid), market)
                ref_p = _binary_ref_selected(ref, selected_yes)
                market_factor, market_check, gap = _market_factor(confidence, ref_p, (ref or {}).get("quality"))
                if market_factor is None:
                    excluded["strong_market_contradiction"] += 1
                    continue
                price_factor, edge, ev = _price_factor(confidence, float(price["tr_price"]))
                score = confidence * quality_factor * schedule_factor * context_factor * market_factor * price_factor
                rows.append({
                    "event_id": str(eid), "match_date": match_date, "league": league_s,
                    "home": str(home), "away": str(away), "market": market, "selection": selection,
                    "confidence": confidence, "decision_score": score,
                    "tr_price": float(price["tr_price"]), "tr_opening_price": price.get("tr_opening_price"),
                    "tr_source": price.get("tr_source"), "international_probability": ref_p,
                    "international_quality": (ref or {}).get("quality"),
                    "international_bookmakers": (ref or {}).get("bookmaker_count"),
                    "market_check": market_check, "model_market_gap": gap,
                    "model_edge_vs_tr": edge, "model_ev_vs_tr": ev,
                    "data_quality": data_quality, "schedule_factor": schedule_factor,
                    "context_penalty": context_factor, "injury_feed_complete": injury_complete,
                    "soft_blockers": list(blockers),
                })
                market_counts[market] += 1

            try:
                one = from_v1_prediction(pred)
                selection, confidence = ranked_outcomes(one)[0]
                confidence = float(confidence)
            except Exception:
                selection, confidence = None, 0.0
            if selection and confidence >= MIN_1X2_CONFIDENCE:
                price = _price_payload(conn, str(eid), "match_result", str(selection))
                if price:
                    ref = latest_1x2_ref(conn, str(eid))
                    ref_p = selected_1x2_probability(ref, str(selection))
                    market_factor, market_check, gap = _market_factor(confidence, ref_p, (ref or {}).get("quality"))
                    if market_factor is not None:
                        price_factor, edge, ev = _price_factor(confidence, float(price["tr_price"]))
                        score = confidence * quality_factor * schedule_factor * context_factor * market_factor * price_factor
                        rows.append({
                            "event_id": str(eid), "match_date": match_date, "league": league_s,
                            "home": str(home), "away": str(away), "market": "match_result", "selection": str(selection),
                            "confidence": confidence, "decision_score": score,
                            "tr_price": float(price["tr_price"]), "tr_opening_price": price.get("tr_opening_price"),
                            "tr_source": price.get("tr_source"), "international_probability": ref_p,
                            "international_quality": (ref or {}).get("quality"),
                            "international_bookmakers": (ref or {}).get("bookmaker_count"),
                            "market_check": market_check, "model_market_gap": gap,
                            "model_edge_vs_tr": edge, "model_ev_vs_tr": ev,
                            "data_quality": data_quality, "schedule_factor": schedule_factor,
                            "context_penalty": context_factor, "injury_feed_complete": injury_complete,
                            "soft_blockers": list(blockers),
                        })
                        market_counts["match_result"] += 1
                    else:
                        excluded["strong_market_contradiction"] += 1
                else:
                    excluded["turkey_1x2_price_missing"] += 1

        # One bet maximum per fixture. Positive EV now wins the first comparison
        # bucket, then near-fair, then materially negative EV. Inside each bucket we
        # retain the full risk-adjusted score/probability ordering.
        best_by_fixture: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            current = best_by_fixture.get(row["event_id"])
            if current is None or _row_rank_key(row) > _row_rank_key(current):
                best_by_fixture[row["event_id"]] = row

        ranked_all = list(best_by_fixture.values())
        ranked_all.sort(key=_row_rank_key, reverse=True)
        positive = [r for r in ranked_all if float(r.get("model_ev_vs_tr") or 0.0) >= CORE_MIN_EV]
        near_fair = [
            r for r in ranked_all
            if CORE_NEAR_FAIR_MIN_EV <= float(r.get("model_ev_vs_tr") or -9.0) < CORE_MIN_EV
        ]
        negative = [r for r in ranked_all if float(r.get("model_ev_vs_tr") or -9.0) < CORE_NEAR_FAIR_MIN_EV]

        # Core4 contract with EV protection:
        # 1) use positive-EV fixtures first;
        # 2) only if needed, add near-fair rows (down to -2% EV);
        # 3) only as a last-resort product fallback, use the least-bad negative rows.
        core_pool = positive[:CORE4_SIZE]
        if len(core_pool) < CORE4_SIZE:
            core_pool.extend(near_fair[: CORE4_SIZE - len(core_pool)])
        if len(core_pool) < CORE4_SIZE:
            core_pool.extend(negative[: CORE4_SIZE - len(core_pool)])

        core_ids = {str(r["event_id"]) for r in core_pool}
        remaining = [r for r in ranked_all if str(r["event_id"]) not in core_ids]
        ordered = core_pool + remaining

        if len(core_pool) >= CORE4_SIZE:
            fourth_score = float(core_pool[-1]["decision_score"])
            extra_floor = max(0.45, fourth_score * 0.88)
            extras = [r for r in remaining if float(r["decision_score"]) >= extra_floor]
            display_raw = (core_pool + extras)[:limit]
        else:
            display_raw = ordered[:limit]

        public = [_public(row, idx, strict_keys) for idx, row in enumerate(display_raw, start=1)]
        core4 = public[:CORE4_SIZE]
        coverage = _latest_import_coverage(conn, len(fixtures))
        official_coverage = float(coverage.get("fixture_coverage") or 0.0)
        ready = bool(official_coverage >= 0.90 and len(core4) >= CORE4_SIZE)
        negative_core_fallbacks = sum(float(p.get("model_ev_vs_tr") or -9.0) < CORE_NEAR_FAIR_MIN_EV for p in core4)

        result = {
            "status": "success",
            "policy_version": POLICY_VERSION,
            "week_key": week_key,
            "horizon_start": start,
            "horizon_end": end,
            "fixture_count": len(fixtures),
            "official_fixture_coverage": official_coverage,
            "candidate_market_rows": len(rows),
            "candidate_fixture_rows": len(ranked_all),
            "positive_ev_fixture_candidates": len(positive),
            "near_fair_fixture_candidates": len(near_fair),
            "negative_ev_fixture_candidates": len(negative),
            "negative_ev_core_fallbacks": negative_core_fallbacks,
            "ready": ready,
            "core4": core4,
            "ranked_picks": public,
            "excluded_counts": dict(excluded),
            "market_candidate_counts": dict(market_counts),
            "policy": {
                "selection_semantics": "ev_aware_relative_weekly_core4_plus_optional_ranked_candidates",
                "core4_required_when_four_playable_modelled_fixtures_exist": True,
                "core4_size": CORE4_SIZE,
                "core4_is_not_70pct_claim": True,
                "strict_verification_is_separate": True,
                "turkey_executable_price_required": True,
                "strong_market_contradiction_rejected": True,
                "core_positive_ev_preferred": True,
                "core_min_ev": CORE_MIN_EV,
                "core_near_fair_min_ev": CORE_NEAR_FAIR_MIN_EV,
                "negative_ev_core_only_last_resort": True,
                "negative_ev_penalty": "asymmetric_price_factor_min_0.72",
                "missing_international_reference": "soft_uncertainty_penalty",
                "missing_injury_feed": "soft_uncertainty_penalty",
                "early_gate_blockers": "soft_risk_penalties_except_no_history_or_pending_fixture",
                "markets": ["over_2_5", "btts", "match_result"],
                "corners": "strict/research only until exact-line price coverage improves",
                "schedule_context_version": SCHEDULE_CONTEXT_VERSION,
                "current_season_result_tuning": False,
            },
        }
        print("WEEKLY_CORE4_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
        return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2, default=str))