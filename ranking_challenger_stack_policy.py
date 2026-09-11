#!/usr/bin/env python3
"""Guarded ranking factors for the sequential weekly challenger stack.

The frozen V1 probabilities are never modified here. These factors may affect
weekly ranking only when research_change_control accepts a holdout-safe registry
row for POLICY_KEY. Feature order is fixed before validation:

1. Lineup Stability V2
2. Missing Player Impact
3. Corner-specific pressure
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple

from lineup_stability_v2_policy import factor_from_live_environment as lineup_factor_from_environment

POLICY_KEY = "weekly-ranking-challenger-stack-v1"
POLICY_VERSION = "ranking-challenger-stack-v1"
FEATURE_LINEUP = "lineup_stability_v2"
FEATURE_MISSING = "missing_player_impact"
FEATURE_CORNER = "corner_specific"
FEATURE_ORDER = (FEATURE_LINEUP, FEATURE_MISSING, FEATURE_CORNER)
MODE_V1 = "v1_only"


def _f(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def mode_from_features(features: Iterable[str]) -> str:
    chosen = [feature for feature in FEATURE_ORDER if feature in set(features)]
    return MODE_V1 if not chosen else "stack:" + ",".join(chosen)


def features_from_mode(mode: Any) -> Tuple[str, ...]:
    text = str(mode or MODE_V1)
    if not text.startswith("stack:"):
        return ()
    raw = {part.strip() for part in text[6:].split(",") if part.strip()}
    return tuple(feature for feature in FEATURE_ORDER if feature in raw)


def feature_enabled(mode: Any, feature: str) -> bool:
    return feature in features_from_mode(mode)


def missing_player_score(context: Optional[Dict[str, Any]]) -> Optional[float]:
    """Return a bounded team-level missing-player impact estimate.

    Primary evidence is the existing player-context injury_impact (already a
    normalized share, capped by its builder). When data-informed expected-XI and
    top-11 strengths are available, their relative drop is a second independent
    proxy. The larger signal is used conservatively; no value is fabricated.
    """
    ctx = context or {}
    evidence = []
    impact = _f(ctx.get("injury_impact"))
    if impact is not None:
        evidence.append(_clip(impact, 0.0, 0.55))

    expected = _f(ctx.get("expected_xi_strength"))
    top11 = _f(ctx.get("top11_strength"))
    meta = ctx.get("source_meta") if isinstance(ctx.get("source_meta"), dict) else {}
    informed = bool(meta.get("expected_xi_data_informed"))
    if informed and expected is not None and top11 is not None and top11 > 1e-9:
        evidence.append(_clip((top11 - expected) / top11, 0.0, 0.55))

    if not evidence:
        return None
    return max(evidence)


def missing_player_factor(home_context: Optional[Dict[str, Any]], away_context: Optional[Dict[str, Any]]) -> Tuple[float, bool]:
    """Small predeclared ranking penalty/reward from missing-player impact."""
    home = missing_player_score(home_context)
    away = missing_player_score(away_context)
    if home is None or away is None:
        return 1.0, False
    worst = max(home, away)
    combined = 0.75 * worst + 0.25 * ((home + away) / 2.0)
    if combined >= 0.30:
        return 0.95, True
    if combined >= 0.20:
        return 0.97, True
    if combined >= 0.10:
        return 0.985, True
    if combined <= 0.03:
        return 1.005, True
    return 1.0, True


def corner_specific_factor(market: Any, selected_yes: bool, signal: Any) -> Tuple[float, bool]:
    """Bounded ranking factor for corner selections only.

    signal=1.0 is neutral. Values above one mean an above-baseline total-corner
    environment and values below one mean below-baseline. The same signal therefore
    boosts OVER and penalizes UNDER (and vice versa). It never changes probability.
    """
    if not str(market or "").startswith("corners_over_"):
        return 1.0, False
    value = _f(signal)
    if value is None:
        return 1.0, False
    value = _clip(value, 0.55, 1.45)
    direction = 1.0 if bool(selected_yes) else -1.0
    factor = 1.0 + direction * 0.08 * (value - 1.0)
    return round(_clip(factor, 0.964, 1.036), 6), True


def live_factors(environment: Optional[Dict[str, Any]], market: Any, selected_yes: bool, mode: Any) -> Dict[str, Any]:
    """Return fail-closed live factors for an already validated stack mode."""
    env = environment or {}
    enabled = set(features_from_mode(mode))

    lineup_factor, lineup_available = (1.0, False)
    if FEATURE_LINEUP in enabled:
        lineup_factor, lineup_available = lineup_factor_from_environment(env)

    missing_factor, missing_available = (1.0, False)
    if FEATURE_MISSING in enabled:
        lineup = env.get("lineup") if isinstance(env.get("lineup"), dict) else {}
        home = lineup.get("home") if isinstance(lineup.get("home"), dict) else {}
        away = lineup.get("away") if isinstance(lineup.get("away"), dict) else {}
        missing_factor, missing_available = missing_player_factor(home, away)

    corner_factor, corner_available = (1.0, False)
    if FEATURE_CORNER in enabled:
        pressure = env.get("pressure") if isinstance(env.get("pressure"), dict) else {}
        corner_factor, corner_available = corner_specific_factor(
            market, selected_yes, pressure.get("corner_pressure_signal")
        )

    return {
        "mode": mode_from_features(enabled),
        "lineup_factor": float(lineup_factor),
        "lineup_available": bool(lineup_available),
        "missing_player_factor": float(missing_factor),
        "missing_player_available": bool(missing_available),
        "corner_specific_factor": float(corner_factor),
        "corner_specific_available": bool(corner_available),
        "combined_factor": float(lineup_factor) * float(missing_factor) * float(corner_factor),
    }
