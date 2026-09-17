#!/usr/bin/env python3
"""Production confidence/value post-gate for the six-layer V4 engine.

V5 keeps all six V4 evidence layers, but fixes the remaining product-level problem:
'Core4' is a maximum of four genuinely strong confidence decisions, never a quota.
It also exposes a separate playable intersection so a high-confidence event with a
bad Turkish price is not silently presented as a good bet.

No threshold here is tuned on 2026/27 outcomes. These are conservative publication
rules around the already-frozen six-layer model/evidence stack.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from confidence_core_v4 import build as build_v4
from thursday_decision_engine import DATABASE_URL, LIST_LIMIT

POLICY_VERSION = "confidence-core-v5-honest-publication-2026-09-17"
CORE4_SIZE = 4

# Publication floors. 1X2 has intrinsically lower winning probabilities than a
# binary market, so thresholds are market-specific.
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


def build(database_url: str = DATABASE_URL, *, now=None, limit: int = LIST_LIMIT, strict_picks=None) -> Dict[str, Any]:
    base = build_v4(database_url, now=now, limit=max(limit, 20), strict_picks=strict_picks)
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

    # Actionable list = confidence publication gate AND non-negative executable EV.
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
        "current_season_result_tuning": False,
    })
    result["policy"] = policy
    print("WEEKLY_CORE4_V5_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
    return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2, default=str))
