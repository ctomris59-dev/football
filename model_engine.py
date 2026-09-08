#!/usr/bin/env python3
"""Explainable probability engine for Big Five football betting markets.

Markets currently modelled:
- Over/Under 2.5 goals
- BTTS Yes/No
- Over/Under 8.5 total corners

The engine is intentionally dependency-light. It uses recency-weighted team form,
home/away splits, league shrinkage and Poisson probability models. It never uses
future matches: callers pass only matches known before the fixture being scored.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_RECENT_MATCHES = 18
DEFAULT_HALF_LIFE_MATCHES = 7.0
DEFAULT_PRIOR_MATCHES = 7.0


@dataclass(frozen=True)
class Prediction:
    p_over_2_5: float
    p_btts: float
    p_corners_over_8_5: float
    lambda_home_goals: float
    lambda_away_goals: float
    lambda_total_corners: float
    home_sample: int
    away_sample: int
    data_quality: float

    def as_dict(self) -> Dict[str, float | int]:
        return {
            "p_over_2_5": self.p_over_2_5,
            "p_under_2_5": 1.0 - self.p_over_2_5,
            "p_btts": self.p_btts,
            "p_btts_no": 1.0 - self.p_btts,
            "p_corners_over_8_5": self.p_corners_over_8_5,
            "p_corners_under_8_5": 1.0 - self.p_corners_over_8_5,
            "lambda_home_goals": self.lambda_home_goals,
            "lambda_away_goals": self.lambda_away_goals,
            "lambda_total_corners": self.lambda_total_corners,
            "home_sample": self.home_sample,
            "away_sample": self.away_sample,
            "data_quality": self.data_quality,
        }


def _f(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clip(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def _weighted_mean(values: Sequence[Optional[float]], half_life: float = DEFAULT_HALF_LIFE_MATCHES) -> Tuple[Optional[float], int]:
    """Most recent value must be first. Returns mean and count of non-null values."""
    total = 0.0
    weights = 0.0
    count = 0
    decay = math.exp(math.log(0.5) / max(0.1, half_life))
    for i, value in enumerate(values):
        if value is None:
            continue
        w = decay ** i
        total += value * w
        weights += w
        count += 1
    if not count or weights <= 0:
        return None, 0
    return total / weights, count


def _shrink(observed: Optional[float], n: int, prior: float, prior_matches: float = DEFAULT_PRIOR_MATCHES) -> float:
    if observed is None or n <= 0:
        return prior
    return (observed * n + prior * prior_matches) / (n + prior_matches)


def _poisson_cdf(k: int, lam: float) -> float:
    lam = max(1e-9, lam)
    term = math.exp(-lam)
    acc = term
    for i in range(1, k + 1):
        term *= lam / i
        acc += term
    return _clip(acc, 0.0, 1.0)


def poisson_prob_over(line_floor: int, lam: float) -> float:
    """Probability X > line_floor for integer Poisson X. E.g. >2 => Over 2.5."""
    return _clip(1.0 - _poisson_cdf(line_floor, lam), 0.0, 1.0)


def _league_baselines(history: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    if not history:
        return {
            "home_goals": 1.50, "away_goals": 1.20,
            "home_sot": 4.8, "away_sot": 4.0,
            "home_corners": 5.3, "away_corners": 4.4,
        }

    def avg(key: str, default: float) -> float:
        vals = [_f(m.get(key)) for m in history]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else default

    return {
        "home_goals": avg("home_goals", 1.50),
        "away_goals": avg("away_goals", 1.20),
        "home_sot": avg("home_shots_on_target", 4.8),
        "away_sot": avg("away_shots_on_target", 4.0),
        "home_corners": avg("home_corners", 5.3),
        "away_corners": avg("away_corners", 4.4),
    }


def _ratio(value: float, baseline: float) -> float:
    if baseline <= 1e-9:
        return 1.0
    return _clip(value / baseline, 0.35, 2.50)


def _team_recent(history: Sequence[Dict[str, Any]], team: str, *, venue: Optional[str], limit: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in reversed(history):
        if venue == "home" and m.get("home_team") != team:
            continue
        if venue == "away" and m.get("away_team") != team:
            continue
        if venue is None and team not in (m.get("home_team"), m.get("away_team")):
            continue
        out.append(m)
        if len(out) >= limit:
            break
    return out


def _weighted_rate(values: Sequence[Optional[float]], prior: float) -> Tuple[float, int]:
    mean, n = _weighted_mean(values)
    return _shrink(mean, n, prior), n


def _venue_rate(matches: Sequence[Dict[str, Any]], key: str, prior: float) -> Tuple[float, int]:
    return _weighted_rate([_f(m.get(key)) for m in matches], prior)


def _overall_team_rate(matches: Sequence[Dict[str, Any]], team: str, metric: str, prior: float) -> Tuple[float, int]:
    vals: List[Optional[float]] = []
    for m in matches:
        if m.get("home_team") == team:
            vals.append(_f(m.get("home_" + metric)))
        elif m.get("away_team") == team:
            vals.append(_f(m.get("away_" + metric)))
    return _weighted_rate(vals, prior)


def predict_match(history: Sequence[Dict[str, Any]], home_team: str, away_team: str, *, recent_matches: int = DEFAULT_RECENT_MATCHES) -> Prediction:
    base = _league_baselines(history)
    h_home = _team_recent(history, home_team, venue="home", limit=recent_matches)
    a_away = _team_recent(history, away_team, venue="away", limit=recent_matches)
    h_all = _team_recent(history, home_team, venue=None, limit=recent_matches)
    a_all = _team_recent(history, away_team, venue=None, limit=recent_matches)

    h_gf, h_n = _venue_rate(h_home, "home_goals", base["home_goals"])
    h_ga, _ = _venue_rate(h_home, "away_goals", base["away_goals"])
    a_gf, a_n = _venue_rate(a_away, "away_goals", base["away_goals"])
    a_ga, _ = _venue_rate(a_away, "home_goals", base["home_goals"])

    h_gf_all, _ = _overall_team_rate(h_all, home_team, "goals", (base["home_goals"] + base["away_goals"]) / 2)
    a_gf_all, _ = _overall_team_rate(a_all, away_team, "goals", (base["home_goals"] + base["away_goals"]) / 2)
    h_gf = 0.78 * h_gf + 0.22 * h_gf_all
    a_gf = 0.78 * a_gf + 0.22 * a_gf_all

    lam_h_goals = base["home_goals"] * math.sqrt(_ratio(h_gf, base["home_goals"]) * _ratio(a_ga, base["home_goals"]))
    lam_a_goals = base["away_goals"] * math.sqrt(_ratio(a_gf, base["away_goals"]) * _ratio(h_ga, base["away_goals"]))

    h_sot_for, _ = _venue_rate(h_home, "home_shots_on_target", base["home_sot"])
    h_sot_against, _ = _venue_rate(h_home, "away_shots_on_target", base["away_sot"])
    a_sot_for, _ = _venue_rate(a_away, "away_shots_on_target", base["away_sot"])
    a_sot_against, _ = _venue_rate(a_away, "home_shots_on_target", base["home_sot"])
    lam_h_shot = base["home_goals"] * math.sqrt(_ratio(h_sot_for, base["home_sot"]) * _ratio(a_sot_against, base["home_sot"]))
    lam_a_shot = base["away_goals"] * math.sqrt(_ratio(a_sot_for, base["away_sot"]) * _ratio(h_sot_against, base["away_sot"]))

    lam_h = _clip(0.72 * lam_h_goals + 0.28 * lam_h_shot, 0.20, 4.50)
    lam_a = _clip(0.72 * lam_a_goals + 0.28 * lam_a_shot, 0.15, 4.00)
    p_over25 = poisson_prob_over(2, lam_h + lam_a)
    p_btts = _clip((1.0 - math.exp(-lam_h)) * (1.0 - math.exp(-lam_a)), 0.0, 1.0)

    h_cf, hc_n = _venue_rate(h_home, "home_corners", base["home_corners"])
    h_ca, _ = _venue_rate(h_home, "away_corners", base["away_corners"])
    a_cf, ac_n = _venue_rate(a_away, "away_corners", base["away_corners"])
    a_ca, _ = _venue_rate(a_away, "home_corners", base["home_corners"])
    lam_h_c = base["home_corners"] * math.sqrt(_ratio(h_cf, base["home_corners"]) * _ratio(a_ca, base["home_corners"]))
    lam_a_c = base["away_corners"] * math.sqrt(_ratio(a_cf, base["away_corners"]) * _ratio(h_ca, base["away_corners"]))
    lam_corners = _clip(lam_h_c + lam_a_c, 3.0, 16.0)
    p_corners_over85 = poisson_prob_over(8, lam_corners)

    home_sample = max(h_n, hc_n, len(h_all))
    away_sample = max(a_n, ac_n, len(a_all))
    quality = _clip(min(home_sample, away_sample) / 15.0, 0.20, 1.0)
    return Prediction(p_over25, p_btts, p_corners_over85, lam_h, lam_a, lam_corners, home_sample, away_sample, quality)


def best_market(prediction: Prediction) -> Dict[str, Any]:
    options = [
        ("over_2_5", prediction.p_over_2_5, "2.5 ÜST", "2.5 ALT"),
        ("btts", prediction.p_btts, "BTTS VAR", "BTTS YOK"),
        ("corners_over_8_5", prediction.p_corners_over_8_5, "8.5 KORNER ÜST", "8.5 KORNER ALT"),
    ]
    market, p_yes, yes_label, no_label = max(options, key=lambda x: max(x[1], 1.0 - x[1]))
    yes = p_yes >= 0.5
    confidence = p_yes if yes else 1.0 - p_yes
    return {"market": market, "selection": yes_label if yes else no_label, "selection_yes": yes, "probability": confidence, "raw_yes_probability": p_yes, "data_quality": prediction.data_quality}
