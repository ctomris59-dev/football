#!/usr/bin/env python3
"""Advanced goal-model challengers with live/backtest parity.

Two independent challengers are defined here:
1) Dixon-Coles low-score dependence correction applied to frozen V1 goal lambdas.
2) Opponent-adjusted attack/defence strengths, blended conservatively with V1 lambdas.

No production activation happens in this module. `advanced_goal_audit.py` must first
write holdout-safe two-fold evidence to the guarded registry.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from model_engine_v1 import Prediction, poisson_prob_over
from production_predictor import canon

POLICY_KEY = "advanced-goal-challenger-v1"
POLICY_VERSION = "advanced-goal-models-v1"
MODE_V1 = "v1_only"
MODE_DC = "dixon_coles"
MODE_OPP = "opponent_adjusted"
ALLOWED_MODES = {MODE_V1, MODE_DC, MODE_OPP}

# Frozen before the audit. These are not tuned on 2425/2526 outcomes.
DC_RHO_MIN = -0.20
DC_RHO_MAX = 0.20
DC_RHO_STEP = 0.01
OPPONENT_BLEND_WEIGHT = 0.30
OPPONENT_ITERATIONS = 28
OPPONENT_PRIOR_MATCHES = 8.0
OPPONENT_HALF_LIFE_APPEARANCES = 24.0
MIN_TEAM_OBSERVATIONS = 6
MAX_GOALS_GRID = 10


def _clip(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, float(x)))


def _f(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _poisson_pmf(k: int, lam: float) -> float:
    lam = max(1e-9, float(lam))
    return math.exp(-lam) * (lam ** int(k)) / math.factorial(int(k))


def _dc_tau(home_goals: int, away_goals: int, lam_h: float, lam_a: float, rho: float) -> float:
    """Dixon-Coles low-score correction factor."""
    r = _clip(rho, DC_RHO_MIN, DC_RHO_MAX)
    if home_goals == 0 and away_goals == 0:
        return max(1e-9, 1.0 - lam_h * lam_a * r)
    if home_goals == 0 and away_goals == 1:
        return max(1e-9, 1.0 + lam_h * r)
    if home_goals == 1 and away_goals == 0:
        return max(1e-9, 1.0 + lam_a * r)
    if home_goals == 1 and away_goals == 1:
        return max(1e-9, 1.0 - r)
    return 1.0


def dixon_coles_probabilities(lam_h: float, lam_a: float, rho: float, *, max_goals: int = MAX_GOALS_GRID) -> Dict[str, float]:
    cells: List[Tuple[int, int, float]] = []
    total = 0.0
    for hg in range(max(3, int(max_goals)) + 1):
        ph = _poisson_pmf(hg, lam_h)
        for ag in range(max(3, int(max_goals)) + 1):
            p = ph * _poisson_pmf(ag, lam_a) * _dc_tau(hg, ag, lam_h, lam_a, rho)
            cells.append((hg, ag, p))
            total += p
    total = max(total, 1e-12)
    p_over = sum(p for hg, ag, p in cells if hg + ag >= 3) / total
    p_btts = sum(p for hg, ag, p in cells if hg >= 1 and ag >= 1) / total
    p_home = sum(p for hg, ag, p in cells if hg > ag) / total
    p_draw = sum(p for hg, ag, p in cells if hg == ag) / total
    p_away = sum(p for hg, ag, p in cells if hg < ag) / total
    return {
        "p_over_2_5": _clip(p_over, 0.0, 1.0),
        "p_btts": _clip(p_btts, 0.0, 1.0),
        "p_home": _clip(p_home, 0.0, 1.0),
        "p_draw": _clip(p_draw, 0.0, 1.0),
        "p_away": _clip(p_away, 0.0, 1.0),
    }


def _league_goal_baselines(history: Sequence[Mapping[str, Any]]) -> Tuple[float, float]:
    hg = [_f(m.get("home_goals")) for m in history]
    ag = [_f(m.get("away_goals")) for m in history]
    hv = [x for x in hg if x is not None]
    av = [x for x in ag if x is not None]
    return (
        sum(hv) / len(hv) if hv else 1.50,
        sum(av) / len(av) if av else 1.20,
    )


def _appearance_weights(history: Sequence[Mapping[str, Any]]) -> List[float]:
    """Recency weights by each team's own recent appearances, not global row age."""
    decay = math.exp(math.log(0.5) / max(1.0, OPPONENT_HALF_LIFE_APPEARANCES))
    counts: Dict[str, int] = defaultdict(int)
    out = [1.0] * len(history)
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        h, a = canon(m.get("home_team")), canon(m.get("away_team"))
        age = max(counts[h], counts[a])
        out[i] = decay ** age
        counts[h] += 1
        counts[a] += 1
    return out


@dataclass(frozen=True)
class OpponentStrengthModel:
    base_home: float
    base_away: float
    attack: Dict[str, float]
    defence: Dict[str, float]
    observations: Dict[str, int]
    matches: int


def _normalize(values: Dict[str, float]) -> Dict[str, float]:
    vals = [max(1e-9, float(v)) for v in values.values()]
    if not vals:
        return values
    gm = math.exp(sum(math.log(v) for v in vals) / len(vals))
    return {k: _clip(v / gm, 0.50, 2.00) for k, v in values.items()}


def fit_opponent_strengths(history: Sequence[Mapping[str, Any]]) -> OpponentStrengthModel:
    """Iteratively estimate attack and defensive concession multipliers.

    Expected goals are:
      home = league_home * attack(home) * defence(away)
      away = league_away * attack(away) * defence(home)

    defence > 1 means easier to score against. Every update is shrunk toward 1.0.
    """
    clean: List[Mapping[str, Any]] = []
    for m in history:
        if _f(m.get("home_goals")) is None or _f(m.get("away_goals")) is None:
            continue
        if not canon(m.get("home_team")) or not canon(m.get("away_team")):
            continue
        clean.append(m)
    bh, ba = _league_goal_baselines(clean)
    teams = sorted({canon(m.get("home_team")) for m in clean} | {canon(m.get("away_team")) for m in clean})
    attack = {t: 1.0 for t in teams}
    defence = {t: 1.0 for t in teams}
    observations: Dict[str, int] = defaultdict(int)
    weights = _appearance_weights(clean)
    prior_base = max(0.25, (bh + ba) / 2.0)

    for m in clean:
        observations[canon(m.get("home_team"))] += 1
        observations[canon(m.get("away_team"))] += 1

    for _ in range(OPPONENT_ITERATIONS):
        a_num: Dict[str, float] = defaultdict(lambda: OPPONENT_PRIOR_MATCHES * prior_base)
        a_den: Dict[str, float] = defaultdict(lambda: OPPONENT_PRIOR_MATCHES * prior_base)
        for m, w in zip(clean, weights):
            h, a = canon(m.get("home_team")), canon(m.get("away_team"))
            hg, ag = float(m["home_goals"]), float(m["away_goals"])
            a_num[h] += w * hg
            a_den[h] += w * bh * defence.get(a, 1.0)
            a_num[a] += w * ag
            a_den[a] += w * ba * defence.get(h, 1.0)
        attack = {t: _clip(a_num[t] / max(1e-9, a_den[t]), 0.50, 2.00) for t in teams}
        attack = _normalize(attack)

        d_num: Dict[str, float] = defaultdict(lambda: OPPONENT_PRIOR_MATCHES * prior_base)
        d_den: Dict[str, float] = defaultdict(lambda: OPPONENT_PRIOR_MATCHES * prior_base)
        for m, w in zip(clean, weights):
            h, a = canon(m.get("home_team")), canon(m.get("away_team"))
            hg, ag = float(m["home_goals"]), float(m["away_goals"])
            d_num[a] += w * hg
            d_den[a] += w * bh * attack.get(h, 1.0)
            d_num[h] += w * ag
            d_den[h] += w * ba * attack.get(a, 1.0)
        defence = {t: _clip(d_num[t] / max(1e-9, d_den[t]), 0.50, 2.00) for t in teams}
        defence = _normalize(defence)

    return OpponentStrengthModel(
        base_home=bh,
        base_away=ba,
        attack=attack,
        defence=defence,
        observations=dict(observations),
        matches=len(clean),
    )


def opponent_adjusted_lambdas(model: OpponentStrengthModel, home_team: str, away_team: str) -> Tuple[float, float, bool]:
    h, a = canon(home_team), canon(away_team)
    lh = model.base_home * model.attack.get(h, 1.0) * model.defence.get(a, 1.0)
    la = model.base_away * model.attack.get(a, 1.0) * model.defence.get(h, 1.0)
    available = model.observations.get(h, 0) >= MIN_TEAM_OBSERVATIONS and model.observations.get(a, 0) >= MIN_TEAM_OBSERVATIONS
    return _clip(lh, 0.20, 4.50), _clip(la, 0.15, 4.00), bool(available)


def _blend_lambda(v1_lambda: float, challenger_lambda: float) -> float:
    w = OPPONENT_BLEND_WEIGHT
    # Geometric blending preserves positivity and treats ratios symmetrically.
    return math.exp((1.0 - w) * math.log(max(1e-9, v1_lambda)) + w * math.log(max(1e-9, challenger_lambda)))


def opponent_adjusted_probabilities(pred: Prediction, model: OpponentStrengthModel, home_team: str, away_team: str) -> Dict[str, Any]:
    oh, oa, available = opponent_adjusted_lambdas(model, home_team, away_team)
    lh = _clip(_blend_lambda(pred.lambda_home_goals, oh), 0.20, 4.50)
    la = _clip(_blend_lambda(pred.lambda_away_goals, oa), 0.15, 4.00)
    return {
        "p_over_2_5": poisson_prob_over(2, lh + la),
        "p_btts": _clip((1.0 - math.exp(-lh)) * (1.0 - math.exp(-la)), 0.0, 1.0),
        "p_corners_over_8_5": pred.p_corners_over_8_5,
        "lambda_home_goals": lh,
        "lambda_away_goals": la,
        "available": available,
        "raw_opponent_lambda_home": oh,
        "raw_opponent_lambda_away": oa,
    }


def fit_dc_rho(history: Sequence[Mapping[str, Any]]) -> Tuple[float, bool, Dict[str, Any]]:
    """Fit rho on past-only history by low-score maximum likelihood.

    Match-specific lambdas for the fit come from an opponent-adjusted model fitted
    on the same *past-only* sample. This affects rho estimation only; the DC
    challenger applies the fitted rho to frozen V1 lambdas at prediction time.
    """
    model = fit_opponent_strengths(history)
    low_rows: List[Tuple[int, int, float, float]] = []
    for m in history:
        hg = _f(m.get("home_goals")); ag = _f(m.get("away_goals"))
        if hg is None or ag is None:
            continue
        ih, ia = int(hg), int(ag)
        if ih > 1 or ia > 1:
            continue
        lh, la, _ = opponent_adjusted_lambdas(model, str(m.get("home_team")), str(m.get("away_team")))
        low_rows.append((ih, ia, lh, la))
    if len(low_rows) < 40:
        return 0.0, False, {"low_score_rows": len(low_rows), "fit_matches": model.matches}

    best_rho = 0.0
    best_ll = float("-inf")
    n_steps = int(round((DC_RHO_MAX - DC_RHO_MIN) / DC_RHO_STEP))
    for i in range(n_steps + 1):
        rho = DC_RHO_MIN + i * DC_RHO_STEP
        ll = 0.0
        valid = True
        for hg, ag, lh, la in low_rows:
            tau = _dc_tau(hg, ag, lh, la, rho)
            if tau <= 0:
                valid = False
                break
            ll += math.log(tau)
        if valid and ll > best_ll:
            best_ll = ll
            best_rho = rho
    return round(_clip(best_rho, DC_RHO_MIN, DC_RHO_MAX), 4), True, {
        "low_score_rows": len(low_rows),
        "fit_matches": model.matches,
        "log_tau_likelihood": round(best_ll, 6),
    }


def dixon_coles_from_v1(pred: Prediction, rho: float) -> Dict[str, Any]:
    probs = dixon_coles_probabilities(pred.lambda_home_goals, pred.lambda_away_goals, rho)
    return {
        "p_over_2_5": probs["p_over_2_5"],
        "p_btts": probs["p_btts"],
        "p_corners_over_8_5": pred.p_corners_over_8_5,
        "lambda_home_goals": pred.lambda_home_goals,
        "lambda_away_goals": pred.lambda_away_goals,
        "rho": float(rho),
    }


def best_market_from_probabilities(probs: Mapping[str, float], data_quality: float) -> Dict[str, Any]:
    options = [
        ("over_2_5", float(probs["p_over_2_5"]), "2.5 ÜST", "2.5 ALT"),
        ("btts", float(probs["p_btts"]), "BTTS VAR", "BTTS YOK"),
        ("corners_over_8_5", float(probs["p_corners_over_8_5"]), "8.5 KORNER ÜST", "8.5 KORNER ALT"),
    ]
    market, p_yes, yes_label, no_label = max(options, key=lambda x: max(x[1], 1.0 - x[1]))
    yes = p_yes >= 0.5
    confidence = p_yes if yes else 1.0 - p_yes
    return {
        "market": market,
        "selection": yes_label if yes else no_label,
        "selection_yes": yes,
        "probability": confidence,
        "raw_yes_probability": p_yes,
        "data_quality": float(data_quality),
    }


def apply_mode(pred: Prediction, history: Sequence[Mapping[str, Any]], home_team: str, away_team: str, mode: str) -> Dict[str, Any]:
    """Return probabilities for an already guard-approved mode."""
    requested = str(mode or MODE_V1)
    if requested == MODE_DC:
        rho, available, meta = fit_dc_rho(history)
        out = dixon_coles_from_v1(pred, rho)
        out.update({"available": available, "mode": MODE_DC, "meta": meta})
        return out
    if requested == MODE_OPP:
        model = fit_opponent_strengths(history)
        out = opponent_adjusted_probabilities(pred, model, home_team, away_team)
        out.update({"mode": MODE_OPP, "meta": {"fit_matches": model.matches}})
        return out
    return {
        "p_over_2_5": pred.p_over_2_5,
        "p_btts": pred.p_btts,
        "p_corners_over_8_5": pred.p_corners_over_8_5,
        "lambda_home_goals": pred.lambda_home_goals,
        "lambda_away_goals": pred.lambda_away_goals,
        "available": True,
        "mode": MODE_V1,
        "meta": {},
    }
