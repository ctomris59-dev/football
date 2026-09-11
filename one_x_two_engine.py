#!/usr/bin/env python3
"""Explainable 1X2 probability engine derived from frozen V1 goal intensities.

This module does not change the frozen V1 goal model. It converts the already-
validated home/away Poisson goal intensities into mutually exclusive 1/X/2 match-
result probabilities, then applies a predeclared cost-aware coupon policy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple

POLICY_KEY = "one-x-two-v1"
POLICY_VERSION = "one-x-two-poisson-v1"
ACTIVE_MODE = "one_x_two_v1"
OUTCOMES = ("1", "0", "2")

# Frozen before historical audit. These thresholds are intentionally conservative:
# strong + separated favourites stay single; ambiguous matches get a double; only
# genuinely three-way fixtures become 102.
SINGLE_MIN_PROB = 0.56
SINGLE_MIN_MARGIN = 0.10
TRIPLE_MAX_TOP = 0.40
TRIPLE_MIN_BOTTOM = 0.20


@dataclass(frozen=True)
class OneXTwoPrediction:
    p1: float
    px: float
    p2: float
    lambda_home: float
    lambda_away: float

    def as_dict(self) -> Dict[str, float]:
        return {
            "p1": self.p1,
            "px": self.px,
            "p2": self.p2,
            "lambda_home": self.lambda_home,
            "lambda_away": self.lambda_away,
        }

    def probabilities(self) -> Dict[str, float]:
        return {"1": self.p1, "0": self.px, "2": self.p2}


def _clip_probability(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _poisson_probs(lam: float, max_goals: int) -> List[float]:
    lam = max(1e-9, float(lam))
    probs = [math.exp(-lam)]
    for k in range(1, max_goals + 1):
        probs.append(probs[-1] * lam / k)
    return probs


def probabilities_from_lambdas(lambda_home: float, lambda_away: float, *, max_goals: int = 12) -> OneXTwoPrediction:
    """Convert independent home/away Poisson goal intensities to 1/X/2.

    A finite score grid is renormalized, so p1+px+p2 is exactly one up to floating
    point error even when lambdas are unusually large.
    """
    lh = max(0.01, float(lambda_home))
    la = max(0.01, float(lambda_away))
    hp = _poisson_probs(lh, max_goals)
    ap = _poisson_probs(la, max_goals)
    p1 = px = p2 = total = 0.0
    for hg, ph in enumerate(hp):
        for ag, pa in enumerate(ap):
            joint = ph * pa
            total += joint
            if hg > ag:
                p1 += joint
            elif hg == ag:
                px += joint
            else:
                p2 += joint
    if total <= 0:
        raise ValueError("Invalid Poisson probability mass")
    p1, px, p2 = p1 / total, px / total, p2 / total
    norm = p1 + px + p2
    return OneXTwoPrediction(
        p1=_clip_probability(p1 / norm),
        px=_clip_probability(px / norm),
        p2=_clip_probability(p2 / norm),
        lambda_home=lh,
        lambda_away=la,
    )


def from_v1_prediction(prediction: Any) -> OneXTwoPrediction:
    return probabilities_from_lambdas(
        float(prediction.lambda_home_goals),
        float(prediction.lambda_away_goals),
    )


def ranked_outcomes(prediction: OneXTwoPrediction) -> List[Tuple[str, float]]:
    probs = prediction.probabilities()
    order = {"1": 0, "0": 1, "2": 2}
    return sorted(probs.items(), key=lambda item: (-item[1], order[item[0]]))


def coupon_selection(prediction: OneXTwoPrediction) -> Dict[str, Any]:
    """Return a cost-aware single/double/triple recommendation.

    The returned string follows Turkish pool convention: 1, 0, 2 in display order,
    e.g. 10, 02, 12, 102. Thresholds are frozen constants and must not be tuned on
    the 2026/27 live holdout.
    """
    ranked = ranked_outcomes(prediction)
    top_label, top_prob = ranked[0]
    second_label, second_prob = ranked[1]
    _, bottom_prob = ranked[2]
    margin = top_prob - second_prob

    if top_prob >= SINGLE_MIN_PROB and margin >= SINGLE_MIN_MARGIN:
        chosen = {top_label}
        tier = "single"
    elif top_prob <= TRIPLE_MAX_TOP and bottom_prob >= TRIPLE_MIN_BOTTOM:
        chosen = set(OUTCOMES)
        tier = "triple"
    else:
        chosen = {top_label, second_label}
        tier = "double"

    display = "".join(label for label in OUTCOMES if label in chosen)
    coverage_probability = sum(prediction.probabilities()[label] for label in chosen)
    return {
        "selection": display,
        "tier": tier,
        "selection_count": len(chosen),
        "top_outcome": top_label,
        "top_probability": top_prob,
        "second_probability": second_prob,
        "bottom_probability": bottom_prob,
        "top_margin": margin,
        "coverage_probability": coverage_probability,
        "probabilities": prediction.probabilities(),
    }


def actual_outcome(home_goals: Any, away_goals: Any) -> str:
    hg, ag = int(home_goals), int(away_goals)
    if hg > ag:
        return "1"
    if hg < ag:
        return "2"
    return "0"


def selection_contains(selection: str, outcome: str) -> bool:
    return str(outcome) in set(str(selection))
