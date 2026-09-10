#!/usr/bin/env python3
"""Transparent, challenger-only match-environment features.

This module packages several useful ideas seen in modern football analytics tools
without copying any proprietary formula. It deliberately does *not* change the
frozen V1 production probabilities. The outputs are diagnostics/features for
future leakage-safe validation:

- projected goal environment from frozen V1 lambdas;
- pace proxy from existing goal/corner pressure signals;
- xG regression deltas (chance quality vs realised goals);
- venue strength gap;
- opponent-adjusted Elo gap;
- all-competition rest/fatigue context;
- expected-XI / injury / continuity context;
- exact independent-Poisson scoreline distribution.

Why exact scorelines instead of 10,000 Monte Carlo simulations?
Under the current independent-Poisson core, the exact grid is deterministic and
contains the same information without simulation noise. A Dixon-Coles correction
is intentionally *not* activated here because rho must be estimated and validated
out-of-sample before it can influence production.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

MATCH_ENVIRONMENT_VERSION = "match-env-shadow-v1"


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def projected_goal_environment(lambda_home: float, lambda_away: float) -> Dict[str, float]:
    """Our own interpretable goal-environment metric; not DataGaffer's XoG."""
    lh = max(0.0, float(lambda_home))
    la = max(0.0, float(lambda_away))
    total = lh + la
    balance = 1.0 - (abs(lh - la) / total if total > 1e-12 else 0.0)
    return {
        "lambda_home_goals": round(lh, 6),
        "lambda_away_goals": round(la, 6),
        "projected_total_goals": round(total, 6),
        "goal_balance": round(clamp(balance, 0.0, 1.0), 6),
    }


def pace_proxy_score(goal_pressure_signal: Any, corner_pressure_signal: Any) -> Optional[float]:
    """Map neutral=1.0 pressure signals to a transparent 0-100 pace proxy.

    This is intentionally simple and auditable. Goal pressure gets 60% weight and
    corner pressure 40%; missing components are re-normalised rather than imputed.
    A weighted signal of 1.0 maps to 50, 0.55 maps near 0 and 1.45 maps near 100.
    """
    pairs: List[Tuple[float, float]] = []
    gp = safe_float(goal_pressure_signal)
    cp = safe_float(corner_pressure_signal)
    if gp is not None:
        pairs.append((0.60, gp))
    if cp is not None:
        pairs.append((0.40, cp))
    if not pairs:
        return None
    w = sum(x for x, _ in pairs)
    signal = sum(weight * value for weight, value in pairs) / w
    score = 50.0 + ((signal - 1.0) / 0.45) * 50.0
    return round(clamp(score, 0.0, 100.0), 3)


def pace_band(score: Any) -> Optional[str]:
    s = safe_float(score)
    if s is None:
        return None
    if s >= 72:
        return "high"
    if s >= 58:
        return "above_average"
    if s <= 28:
        return "low"
    if s <= 42:
        return "below_average"
    return "neutral"


def xg_regression_profile(
    goals_for: Any,
    xg_for: Any,
    goals_against: Any,
    xg_against: Any,
    *,
    matches: int = 0,
) -> Dict[str, Any]:
    """Return non-prescriptive xG-vs-goals regression diagnostics.

    Positive attack_delta means the team created more xG than goals scored.
    Positive defence_delta means it conceded more goals than xG allowed.
    These are descriptive features only; no automatic 'due' adjustment is applied.
    """
    gf, xf = safe_float(goals_for), safe_float(xg_for)
    ga, xa = safe_float(goals_against), safe_float(xg_against)
    attack_delta = (xf - gf) if xf is not None and gf is not None else None
    defence_delta = (ga - xa) if ga is not None and xa is not None else None
    return {
        "matches": int(matches or 0),
        "goals_for_per_match": round(gf, 6) if gf is not None else None,
        "xg_for_per_match": round(xf, 6) if xf is not None else None,
        "goals_against_per_match": round(ga, 6) if ga is not None else None,
        "xg_against_per_match": round(xa, 6) if xa is not None else None,
        "attack_xg_minus_goals": round(attack_delta, 6) if attack_delta is not None else None,
        "defence_goals_minus_xg": round(defence_delta, 6) if defence_delta is not None else None,
    }


def venue_strength_index(
    goals_for: Any,
    goals_against: Any,
    sot_for: Any = None,
    sot_against: Any = None,
    *,
    league_goals_for: Any = None,
    league_goals_against: Any = None,
) -> Optional[float]:
    """Conservative venue-role strength index centred at 1.0.

    It primarily uses goals-for/against relative to league role baselines and adds
    only a small SOT differential component when SOT data is available.
    """
    gf, ga = safe_float(goals_for), safe_float(goals_against)
    lgf, lga = safe_float(league_goals_for), safe_float(league_goals_against)
    if gf is None or ga is None or not lgf or not lga:
        return None
    attack = clamp(gf / lgf, 0.45, 1.80)
    defence = clamp(lga / max(ga, 0.15), 0.45, 1.80)
    base = math.sqrt(attack * defence)
    sf, sa = safe_float(sot_for), safe_float(sot_against)
    if sf is not None and sa is not None and sf + sa > 0:
        sot_balance = clamp((sf + 0.5) / (sa + 0.5), 0.55, 1.65)
        base = 0.90 * base + 0.10 * math.sqrt(sot_balance)
    return round(clamp(base, 0.50, 1.60), 6)


def lineup_stability(
    starter_continuity: Any,
    retained_minutes_share: Any,
    injury_impact: Any,
    goalkeeper_injured: Any = False,
) -> Optional[float]:
    """Transparent 0-1 availability/stability summary for display and research."""
    c = safe_float(starter_continuity)
    r = safe_float(retained_minutes_share)
    i = safe_float(injury_impact)
    pieces: List[Tuple[float, float]] = []
    if c is not None:
        pieces.append((0.45, clamp(c, 0.0, 1.0)))
    if r is not None:
        pieces.append((0.35, clamp(r, 0.0, 1.0)))
    if i is not None:
        pieces.append((0.20, 1.0 - clamp(i, 0.0, 0.55) / 0.55))
    if not pieces:
        return None
    total_w = sum(w for w, _ in pieces)
    score = sum(w * v for w, v in pieces) / total_w
    if bool(goalkeeper_injured):
        score *= 0.94
    return round(clamp(score, 0.0, 1.0), 6)


def fatigue_asymmetry(home_rest_days: Any, away_rest_days: Any) -> Optional[float]:
    h, a = safe_float(home_rest_days), safe_float(away_rest_days)
    if h is None or a is None:
        return None
    return round(h - a, 3)


def _poisson_pmf(k: int, lam: float) -> float:
    if k < 0:
        return 0.0
    l = max(1e-12, float(lam))
    return math.exp(-l) * (l ** k) / math.factorial(k)


def exact_scoreline_distribution(
    lambda_home: float,
    lambda_away: float,
    *,
    max_goals: int = 10,
    top_n: int = 6,
) -> Dict[str, Any]:
    """Exact independent-Poisson score grid and core outcome probabilities."""
    lh, la = max(1e-9, float(lambda_home)), max(1e-9, float(lambda_away))
    cells: List[Tuple[int, int, float]] = []
    home_win = draw = away_win = btts = over25 = mass = 0.0
    for hg in range(max_goals + 1):
        ph = _poisson_pmf(hg, lh)
        for ag in range(max_goals + 1):
            p = ph * _poisson_pmf(ag, la)
            mass += p
            cells.append((hg, ag, p))
            if hg > ag:
                home_win += p
            elif hg == ag:
                draw += p
            else:
                away_win += p
            if hg > 0 and ag > 0:
                btts += p
            if hg + ag >= 3:
                over25 += p
    # The 0..max_goals grid omits an extremely small tail. Renormalise all outputs.
    denom = mass if mass > 0 else 1.0
    cells.sort(key=lambda x: x[2], reverse=True)
    return {
        "method": "exact_independent_poisson",
        "grid_max_goals": int(max_goals),
        "captured_mass": round(mass, 10),
        "home_win": round(home_win / denom, 6),
        "draw": round(draw / denom, 6),
        "away_win": round(away_win / denom, 6),
        "btts": round(btts / denom, 6),
        "over_2_5": round(over25 / denom, 6),
        "top_scorelines": [
            {"home": hg, "away": ag, "probability": round(p / denom, 6)}
            for hg, ag, p in cells[: max(1, int(top_n))]
        ],
    }


def coverage_score(features: Sequence[Any]) -> float:
    if not features:
        return 0.0
    present = sum(v is not None for v in features)
    return round(present / len(features), 6)
