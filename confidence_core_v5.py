#!/usr/bin/env python3
"""Probability-first weekly football publication engine.

The six evidence layers remain available, but they are no longer six independent
rejection gates.  The weekly product is now a reliability ranking:
- probability drives the decision;
- xG / opponent strength / XI / market / Dixon-Coles improve or penalise the score;
- missing secondary evidence lowers rank instead of automatically deleting a match;
- EV/value never blocks a Core4 selection.

Hard rejection is reserved for genuinely unsafe rows: no executable Turkish price,
very low model direction agreement, or a strong model-vs-market contradiction when
a fresh international reference exists.

The live 2026/27 outcomes are not used to tune weights or thresholds.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import confidence_core_v4 as v4
from thursday_decision_engine import DATABASE_URL, LIST_LIMIT, weekend_bounds

POLICY_VERSION = "confidence-core-v6-probability-first-2026-09-17"
CORE4_SIZE = 4

# Only genuinely contradictory evidence can remove a candidate. Everything else
# affects the ranking score softly.
HARD_MAX_MARKET_GAP = 0.16
HARD_MIN_DIRECTION_AGREEMENT = 0.50
MIN_REASONABLE_BINARY = 0.52
MIN_REASONABLE_1X2 = 0.34
VALUE_MIN_EV = 0.02


def _rows_signature(rows) -> tuple:
    if not rows:
        return (0,)
    first, last = rows[0], rows[-1]
    hg = ag = 0.0
    for r in rows:
        try:
            hg += float(r.get("home_goals") or 0.0)
            ag += float(r.get("away_goals") or 0.0)
        except Exception:
            pass
    return (
        len(rows), str(first.get("match_date")), str(last.get("match_date")),
        str(first.get("home_team")), str(last.get("away_team")),
        round(hg, 3), round(ag, 3),
    )


def _build_v4_week_frozen(database_url: str, *, now=None, limit: int, strict_picks=None):
    """Freeze all causal feature/model fits at the weekly decision cutoff."""
    _week_key, cutoff, _end = weekend_bounds(now)
    original_history = v4._history_rows
    original_enrich = v4._enrich_xg
    original_fit_opp = v4.fit_opponent_strengths
    original_fit_dc = v4.fit_dc_rho

    history_cache: Dict[str, Any] = {}
    xg_cache: Dict[str, Any] = {}
    opp_cache: Dict[tuple, Any] = {}
    dc_cache: Dict[tuple, Any] = {}

    def frozen_history(conn, league, _before):
        key = str(league)
        if key not in history_cache:
            history_cache[key] = original_history(conn, league, cutoff)
        return history_cache[key]

    def frozen_xg(conn, league, _before, history):
        key = str(league)
        if key not in xg_cache:
            xg_cache[key] = original_enrich(conn, league, cutoff, history)
        return xg_cache[key]

    def cached_opp(rows):
        sig = _rows_signature(rows)
        if sig not in opp_cache:
            opp_cache[sig] = original_fit_opp(rows)
        return opp_cache[sig]

    def cached_dc(rows):
        sig = _rows_signature(rows)
        if sig not in dc_cache:
            dc_cache[sig] = original_fit_dc(rows)
        return dc_cache[sig]

    v4._history_rows = frozen_history
    v4._enrich_xg = frozen_xg
    v4.fit_opponent_strengths = cached_opp
    v4.fit_dc_rho = cached_dc
    try:
        return v4.build(database_url, now=now, limit=max(limit, 20), strict_picks=strict_picks)
    finally:
        v4._history_rows = original_history
        v4._enrich_xg = original_enrich
        v4.fit_opponent_strengths = original_fit_opp
        v4.fit_dc_rho = original_fit_dc


def _candidate_probability(row: Dict[str, Any]) -> float:
    # Prefer market-anchored consensus when available; otherwise the multi-model
    # median remains usable.  This deliberately does not require positive EV.
    return float(
        row.get("consensus_probability")
        or row.get("calibrated_probability")
        or row.get("model_probability_estimate")
        or row.get("confidence")
        or 0.0
    )


def _hard_reject(row: Dict[str, Any]) -> bool:
    if row.get("tr_price") is None:
        return True
    p = _candidate_probability(row)
    if row.get("market") == "match_result":
        if p < MIN_REASONABLE_1X2:
            return True
    elif p < MIN_REASONABLE_BINARY:
        return True
    agreement = float(row.get("direction_agreement") or 0.0)
    if agreement < HARD_MIN_DIRECTION_AGREEMENT:
        return True
    gap = row.get("model_market_gap")
    if gap is not None and abs(float(gap)) > HARD_MAX_MARKET_GAP:
        return True
    return False


def _reliability_score(row: Dict[str, Any]) -> float:
    """Probability-first score with soft evidence penalties, never EV-driven."""
    p = _candidate_probability(row)
    lower = float(row.get("confidence_lower_bound") or p)
    evidence = float(row.get("evidence_quality") or 0.0)
    agreement = float(row.get("direction_agreement") or 0.0)

    # Probability dominates. Lower bound and evidence stabilise close calls.
    score = 0.66 * p + 0.14 * lower + 0.12 * evidence + 0.08 * agreement

    # Missing evidence is a modest penalty, not an exclusion.
    if not row.get("xg_used"):
        score *= 0.965
    if not row.get("opponent_model_available"):
        score *= 0.975
    if not row.get("dixon_coles_available"):
        score *= 0.985
    if not row.get("injury_feed_complete"):
        score *= 0.980
    books = row.get("international_bookmakers")
    if books is None:
        score *= 0.960
    elif int(books or 0) < 2:
        score *= 0.980

    # Mild penalty for market disagreement that is below the hard-reject boundary.
    gap = row.get("model_market_gap")
    if gap is not None:
        score *= max(0.94, 1.0 - 0.18 * abs(float(gap)))
    return score


def _dedupe_best(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    best: Dict[str, Dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        if _hard_reject(row):
            continue
        row["reliability_score"] = _reliability_score(row)
        eid = str(row.get("event_id"))
        cur = best.get(eid)
        if cur is None or float(row["reliability_score"]) > float(cur.get("reliability_score") or -1.0):
            best[eid] = row
    return list(best.values())


def _rerank(rows: List[Dict[str, Any]], tier: str, limit: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, raw in enumerate(rows[:limit], start=1):
        row = dict(raw)
        row["rank"] = i
        row["list_tier"] = tier
        row["policy_version"] = POLICY_VERSION
        row["publication_gate_passed"] = True
        row["selection_semantics"] = "probability_first_reliability; value_not_required"
        out.append(row)
    return out


def build(database_url: str = DATABASE_URL, *, now=None, limit: int = LIST_LIMIT, strict_picks=None) -> Dict[str, Any]:
    base = _build_v4_week_frozen(database_url, now=now, limit=max(limit, 20), strict_picks=strict_picks)

    # V4's confidence rows contain evidence-complete candidates. Fallback rows are
    # deliberately re-admitted because missing one secondary evidence layer should
    # no longer erase an otherwise strong favourite/profile.
    pool = list(base.get("confidence_ranked") or base.get("ranked_picks") or [])
    pool.extend(list(base.get("fallback_candidates") or []))
    candidates = _dedupe_best(pool)
    candidates.sort(
        key=lambda r: (
            float(r.get("reliability_score") or 0.0),
            _candidate_probability(r),
            float(r.get("evidence_quality") or 0.0),
        ),
        reverse=True,
    )

    confidence_ranked = _rerank(candidates, "reliability_ranked", limit)
    confidence_core4 = _rerank(candidates, "core4", CORE4_SIZE)

    # Value stays visible only as an optional badge/list; it never controls Core4.
    values = [dict(r) for r in candidates if float(r.get("model_ev_vs_tr") or -9.0) >= VALUE_MIN_EV]
    values.sort(key=lambda r: float(r.get("model_ev_vs_tr") or 0.0), reverse=True)
    value_picks = _rerank(values, "value_optional", limit)

    result = dict(base)
    result.update({
        "policy_version": POLICY_VERSION,
        "confidence_core4": confidence_core4,
        "core4": confidence_core4,
        "confidence_ranked": confidence_ranked,
        "ranked_picks": confidence_ranked,
        "weekly_reliable": confidence_ranked,
        "value_picks": value_picks,
        "playable_picks": confidence_core4,
        "trust_fixture_candidates": len(confidence_ranked),
        "value_fixture_candidates": len(value_picks),
        "ready": bool(float(base.get("official_fixture_coverage") or 0.0) >= 0.90 and len(confidence_core4) >= CORE4_SIZE),
        "publication_complete": True,
        "core4_slots_filled": len(confidence_core4),
        "core4_probability_first": True,
    })
    policy = dict(base.get("policy") or {})
    policy.update({
        "policy_version": POLICY_VERSION,
        "selection_semantics": "probability_first_reliability_ranking",
        "core4_target": CORE4_SIZE,
        "value_required_for_core4": False,
        "positive_ev_required_for_core4": False,
        "xg_required_for_core4": False,
        "multi_book_required_for_core4": False,
        "injury_feed_required_for_core4": False,
        "dixon_coles_required_for_core4": False,
        "secondary_evidence_is_soft_penalty": True,
        "hard_max_model_market_gap": HARD_MAX_MARKET_GAP,
        "hard_min_direction_agreement": HARD_MIN_DIRECTION_AGREEMENT,
        "probability_is_primary_signal": True,
        "weekly_feature_cutoff": "Friday 00:00 Europe/Istanbul; one causal snapshot per league",
        "strength_and_dc_fit_memoized": True,
        "current_season_result_tuning": False,
    })
    result["policy"] = policy
    print("WEEKLY_CORE4_V6_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
    return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2, default=str))
