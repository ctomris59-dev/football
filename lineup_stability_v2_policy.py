#!/usr/bin/env python3
"""Predeclared Lineup Stability V2 ranking policy.

This policy keeps the frozen V1 probability untouched. It only changes weekly
ranking if (and only if) the dedicated two-fold OOS audit writes a safe activation
row to policy_activation_registry.

V2 differs from V1 in two ways:
- historical continuity can be estimated from two prior observed lineups instead
  of requiring three, improving causal coverage;
- up to six prior lineups are combined with recency weighting so the most recent
  selection pattern matters more than older lineups.

The live factor consumes the same semantic quantity: starter continuity for each
team. No 2026/27 outcomes are used to fit or tune these thresholds.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

POLICY_KEY = "weekly-lineup-stability-v2"
POLICY_VERSION = "lineup-stability-ranking-v2"
ACTIVE_MODE = "lineup_stability_v2"


def _f(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def continuity_v2_from_lineups(lineups: Sequence[Iterable[str]]) -> Optional[float]:
    """Recency-weighted consecutive XI overlap from the last six known lineups.

    At least two *prior* lineups are required. Only lineups supplied by the caller
    are read, so the historical audit can enforce target-date causality.
    """
    sets = [frozenset(str(x) for x in row if str(x)) for row in lineups][-6:]
    sets = [s for s in sets if len(s) >= 7]
    if len(sets) < 2:
        return None
    overlaps = []
    weights = []
    for idx, (a, b) in enumerate(zip(sets[:-1], sets[1:]), start=1):
        denom = max(1, min(11, len(a), len(b)))
        overlaps.append(len(a & b) / denom)
        # Later pairs receive more weight; 1,2,3,... is transparent and bounded.
        weights.append(float(idx))
    if not overlaps:
        return None
    score = sum(w * x for w, x in zip(weights, overlaps)) / sum(weights)
    return round(_clip(score), 6)


def lineup_stability_v2_factor(home_continuity: Any, away_continuity: Any) -> Tuple[float, bool]:
    """Small bounded ranking factor from two teams' lineup continuity.

    Thresholds are frozen before the V2 audit. The weakest team's continuity is
    intentionally dominant because one heavily rotated side can invalidate a
    supposedly stable match context. The mean is used only as a small stabiliser.
    """
    h, a = _f(home_continuity), _f(away_continuity)
    if h is None or a is None:
        return 1.0, False
    h, a = _clip(h), _clip(a)
    conservative = 0.75 * min(h, a) + 0.25 * ((h + a) / 2.0)
    if conservative < 0.55:
        return 0.97, True
    if conservative < 0.70:
        return 0.985, True
    if conservative >= 0.85 and min(h, a) >= 0.80:
        return 1.01, True
    return 1.0, True


def factor_from_live_environment(environment: Optional[Dict[str, Any]]) -> Tuple[float, bool]:
    """Read current pre-match starter continuity from the unified environment."""
    env = environment or {}
    lineup = env.get("lineup") or {}
    home = lineup.get("home") or {}
    away = lineup.get("away") or {}
    hs = home.get("starter_continuity")
    ass = away.get("starter_continuity")
    if hs is None:
        hs = lineup.get("home_stability")
    if ass is None:
        ass = lineup.get("away_stability")
    return lineup_stability_v2_factor(hs, ass)
