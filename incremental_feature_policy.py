#!/usr/bin/env python3
"""Shared incremental feature ranking policy.

The factors here are deliberately small, bounded and transparent. They never
modify frozen V1 probabilities; they only re-rank the already-selected V1 market
for a fixture. The same functions are used by historical OOS audit and live
production, preventing audit/production formula drift.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

POLICY_KEY = "weekly-feature-challenger-v1"
POLICY_VERSION = "incremental-feature-ranking-v1"
FEATURE_ORDER = ("xg_regression", "elo", "pace", "venue", "lineup_stability")
ACTIVATABLE_FEATURES = ("xg_regression", "elo", "pace", "venue", "lineup_stability")


def _f(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def selection_is_yes(market: str, selection: str) -> bool:
    s = str(selection or "").upper()
    if market == "btts":
        return "YOK" not in s and "NO" not in s
    return "ALT" not in s and "UNDER" not in s


def xg_regression_factor(
    home_profile: Dict[str, Any],
    away_profile: Dict[str, Any],
    market: str,
    selection: str,
) -> Tuple[float, bool]:
    """Small regression-to-xG factor for goal/BTTS markets only."""
    if market not in ("over_2_5", "btts"):
        return 1.0, False
    if int(home_profile.get("matches") or 0) < 4 or int(away_profile.get("matches") or 0) < 4:
        return 1.0, False
    vals = [
        _f(home_profile.get("attack_xg_minus_goals")),
        _f(away_profile.get("attack_xg_minus_goals")),
        _f(home_profile.get("defence_goals_minus_xg")),
        _f(away_profile.get("defence_goals_minus_xg")),
    ]
    if any(v is None for v in vals):
        return 1.0, False
    ha, aa, hd, ad = [float(v) for v in vals]
    signal = _clip(((ha + aa) - (hd + ad)) / 2.0, -1.0, 1.0)
    direction = 1.0 if selection_is_yes(market, selection) else -1.0
    return round(_clip(1.0 + 0.03 * direction * signal, 0.97, 1.03), 6), True


def elo_factor(elo_gap: Any, market: str, selection: str) -> Tuple[float, bool]:
    """Use competitive balance only for BTTS, where it has a clear direction."""
    gap = _f(elo_gap)
    if gap is None or market != "btts":
        return 1.0, False
    closeness = 1.0 - min(abs(gap), 400.0) / 400.0
    centered = (closeness - 0.5) * 2.0
    direction = 1.0 if selection_is_yes(market, selection) else -1.0
    return round(_clip(1.0 + 0.03 * direction * centered, 0.97, 1.03), 6), True


def pace_factor(pace_score: Any, market: str, selection: str) -> Tuple[float, bool]:
    score = _f(pace_score)
    if score is None or market not in ("over_2_5", "btts", "corners_over_8_5"):
        return 1.0, False
    centered = _clip((score - 50.0) / 50.0, -1.0, 1.0)
    direction = 1.0 if selection_is_yes(market, selection) else -1.0
    return round(_clip(1.0 + 0.04 * direction * centered, 0.96, 1.04), 6), True


def venue_factor(home_index: Any, away_index: Any, market: str, selection: str) -> Tuple[float, bool]:
    """Venue-strength balance is used only for BTTS; other directions are ambiguous."""
    h, a = _f(home_index), _f(away_index)
    if h is None or a is None or market != "btts":
        return 1.0, False
    gap = min(abs(h - a), 0.60) / 0.60
    closeness = 1.0 - gap
    centered = (closeness - 0.5) * 2.0
    direction = 1.0 if selection_is_yes(market, selection) else -1.0
    return round(_clip(1.0 + 0.025 * direction * centered, 0.975, 1.025), 6), True


def lineup_stability_factor(home_stability: Any, away_stability: Any) -> Tuple[float, bool]:
    h, a = _f(home_stability), _f(away_stability)
    if h is None or a is None:
        return 1.0, False
    m = min(h, a)
    if m < 0.55:
        return 0.97, True
    if m < 0.70:
        return 0.985, True
    if m >= 0.85:
        return 1.01, True
    return 1.0, True


def factor_from_live_environment(
    mode: str,
    environment: Optional[Dict[str, Any]],
    market: str,
    selection: str,
) -> Tuple[float, bool]:
    env = environment or {}
    if mode == "xg_regression":
        xg = env.get("xg_regression") or {}
        return xg_regression_factor(xg.get("home") or {}, xg.get("away") or {}, market, selection)
    if mode == "elo":
        elo = env.get("elo") or {}
        return elo_factor(elo.get("gap"), market, selection)
    if mode == "pace":
        pace = env.get("pace") or {}
        return pace_factor(pace.get("score"), market, selection)
    if mode == "venue":
        venue = env.get("venue") or {}
        return venue_factor(venue.get("home_index"), venue.get("away_index"), market, selection)
    if mode == "lineup_stability":
        lu = env.get("lineup") or {}
        home = lu.get("home") or {}
        away = lu.get("away") or {}
        hs = home.get("starter_continuity")
        ass = away.get("starter_continuity")
        if hs is None:
            hs = lu.get("home_stability")
        if ass is None:
            ass = lu.get("away_stability")
        return lineup_stability_factor(hs, ass)
    return 1.0, False
