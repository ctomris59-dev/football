#!/usr/bin/env python3
"""Production confidence/value gate for the six-layer football engine.

V5 keeps all six evidence layers and adds two production guarantees:
1) Core4 is a maximum, never a quota;
2) all expensive strength/DC fits are frozen at the weekly decision cutoff and
   memoized, so the same league model is not re-fit for every weekend match day.

The live 2026/27 outcomes are not used to tune weights or thresholds.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import confidence_core_v4 as v4
from thursday_decision_engine import DATABASE_URL, LIST_LIMIT, weekend_bounds

POLICY_VERSION = "confidence-core-v5-honest-publication-2026-09-17"
CORE4_SIZE = 4
MIN_1X2_CONSENSUS = 0.55
MIN_1X2_LOWER = 0.50
MIN_BINARY_CONSENSUS = 0.60
MIN_BINARY_LOWER = 0.56
MIN_EVIDENCE = 0.80
MIN_DIRECTION_AGREEMENT = 0.75
PLAYABLE_MIN_EV = 0.00
VALUE_MIN_EV = 0.02


def _passes_confidence(row: Dict[str, Any]) -> bool:
    consensus = float(row.get("consensus_probability") or row.get("calibrated_probability") or 0.0)
    lower = float(row.get("confidence_lower_bound") or 0.0)
    evidence = float(row.get("evidence_quality") or 0.0)
    agreement = float(row.get("direction_agreement") or 0.0)
    if evidence < MIN_EVIDENCE or agreement < MIN_DIRECTION_AGREEMENT:
        return False
    if row.get("market") == "match_result":
        return consensus >= MIN_1X2_CONSENSUS and lower >= MIN_1X2_LOWER
    return consensus >= MIN_BINARY_CONSENSUS and lower >= MIN_BINARY_LOWER


def _rerank(rows: List[Dict[str, Any]], tier: str, limit: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, raw in enumerate(rows[:limit], start=1):
        row = dict(raw)
        row["rank"] = i
        row["list_tier"] = tier
        row["policy_version"] = POLICY_VERSION
        row["publication_gate_passed"] = True
        out.append(row)
    return out


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
    """Run V4 with one past-only feature snapshot per league for the weekly slate.

    V4 intentionally used match-day cutoffs. For a Thursday-frozen product this caused
    identical historical strength/DC models to be fitted repeatedly. Here all history
    is frozen at the Friday 00:00 Istanbul horizon boundary and model fits are memoized.
    No future result can enter a later Saturday/Sunday fixture during the same run.
    """
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
        return v4.build(database_url, now=now, limit=limit, strict_picks=strict_picks)
    finally:
        v4._history_rows = original_history
        v4._enrich_xg = original_enrich
        v4.fit_opponent_strengths = original_fit_opp
        v4.fit_dc_rho = original_fit_dc


def build(database_url: str = DATABASE_URL, *, now=None, limit: int = LIST_LIMIT, strict_picks=None) -> Dict[str, Any]:
    base = _build_v4_week_frozen(database_url, now=now, limit=max(limit, 20), strict_picks=strict_picks)
    ranked = list(base.get("confidence_ranked") or base.get("ranked_picks") or [])
    qualified = [r for r in ranked if _passes_confidence(r)]
    qualified.sort(
        key=lambda r: (
            float(r.get("confidence_lower_bound") or 0.0),
            float(r.get("consensus_probability") or 0.0),
            float(r.get("evidence_quality") or 0.0),
        ),
        reverse=True,
    )
    confidence_ranked = _rerank(qualified, "confidence_ranked", limit)
    confidence_core4 = _rerank(qualified, "confidence_core4", CORE4_SIZE)

    values = [dict(r) for r in (base.get("value_picks") or []) if float(r.get("model_ev_vs_tr") or 0.0) >= VALUE_MIN_EV]
    values.sort(key=lambda r: (float(r.get("model_ev_vs_tr") or 0.0), float(r.get("evidence_quality") or 0.0)), reverse=True)
    value_picks = _rerank(values, "value", limit)

    playable = [r for r in confidence_ranked if float(r.get("model_ev_vs_tr") or -9.0) >= PLAYABLE_MIN_EV]
    playable = _rerank(playable, "playable_confidence", CORE4_SIZE)

    result = dict(base)
    result.update({
        "policy_version": POLICY_VERSION,
        "confidence_core4": confidence_core4,
        "core4": confidence_core4,
        "confidence_ranked": confidence_ranked,
        "ranked_picks": confidence_ranked,
        "value_picks": value_picks,
        "playable_picks": playable,
        "trust_fixture_candidates": len(confidence_ranked),
        "value_fixture_candidates": len(value_picks),
        "ready": bool(float(base.get("official_fixture_coverage") or 0.0) >= 0.90),
        "publication_complete": True,
        "core4_slots_filled": len(confidence_core4),
        "core4_not_forced": True,
    })
    policy = dict(base.get("policy") or {})
    policy.update({
        "policy_version": POLICY_VERSION,
        "core4_is_maximum_not_quota": True,
        "publication_min_1x2_consensus": MIN_1X2_CONSENSUS,
        "publication_min_1x2_lower_bound": MIN_1X2_LOWER,
        "publication_min_binary_consensus": MIN_BINARY_CONSENSUS,
        "publication_min_binary_lower_bound": MIN_BINARY_LOWER,
        "publication_min_evidence": MIN_EVIDENCE,
        "playable_requires_nonnegative_ev": True,
        "confidence_and_value_remain_separate": True,
        "weekly_feature_cutoff": "Friday 00:00 Europe/Istanbul; one causal snapshot per league",
        "strength_and_dc_fit_memoized": True,
        "current_season_result_tuning": False,
    })
    result["policy"] = policy
    print("WEEKLY_CORE4_V5_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
    return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2, default=str))
