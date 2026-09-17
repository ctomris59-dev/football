#!/usr/bin/env python3
"""Fail-closed production selection policy for the weekly betting shortlist.

A row is user-playable only when all independently required layers are present:
- model probability clears the minimum threshold;
- a current official Turkey executable price exists;
- the model has positive edge and EV at that executable price;
- a fresh, quality-approved international no-vig reference exists;
- model and international probability are not in strong contradiction.

The policy deliberately permits an empty weekly list. It never force-fills Top-10.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

POLICY_VERSION = "strict-playable-v3-2026-09-17"
MIN_MODEL_PROBABILITY = float(os.getenv("STRICT_PLAYABLE_MIN_MODEL_PROBABILITY", "0.65"))
MIN_MODEL_EDGE_VS_TR = float(os.getenv("STRICT_PLAYABLE_MIN_EDGE_VS_TR", "0.015"))
MIN_MODEL_EV_VS_TR = float(os.getenv("STRICT_PLAYABLE_MIN_EV_VS_TR", "0.02"))
ALLOWED_REFERENCE_QUALITIES = {"multi_book_consensus", "sharp_single_book"}


def qualify_candidate(
    *,
    confidence: float,
    tr_price: Optional[float],
    international_probability: Optional[float],
    international_quality: Optional[str],
    international_bookmakers: Optional[int],
    max_model_divergence: float,
) -> Dict[str, Any]:
    """Return an auditable fail-closed qualification result for one selection."""
    p = float(confidence)
    if p < MIN_MODEL_PROBABILITY:
        return {"qualified": False, "reason": "model_probability_below_min"}
    if tr_price is None or float(tr_price) <= 1.0:
        return {"qualified": False, "reason": "turkey_executable_price_missing"}
    if international_probability is None:
        return {"qualified": False, "reason": "international_reference_missing"}
    quality = str(international_quality or "")
    if quality not in ALLOWED_REFERENCE_QUALITIES:
        return {"qualified": False, "reason": "international_reference_quality_insufficient"}
    books = int(international_bookmakers or 0)
    if quality == "multi_book_consensus" and books < 2:
        return {"qualified": False, "reason": "international_book_count_inconsistent"}
    if quality == "sharp_single_book" and books < 1:
        return {"qualified": False, "reason": "international_book_count_inconsistent"}

    intl = float(international_probability)
    gap = p - intl
    if abs(gap) > float(max_model_divergence):
        return {
            "qualified": False,
            "reason": "model_market_divergence_high",
            "model_market_gap": gap,
        }

    price = float(tr_price)
    implied = 1.0 / price
    edge = p - implied
    ev = p * price - 1.0
    if edge < MIN_MODEL_EDGE_VS_TR:
        return {
            "qualified": False,
            "reason": "model_edge_vs_tr_below_min",
            "tr_implied_probability": implied,
            "model_edge_vs_tr": edge,
            "model_ev_vs_tr": ev,
            "model_market_gap": gap,
        }
    if ev < MIN_MODEL_EV_VS_TR:
        return {
            "qualified": False,
            "reason": "model_ev_vs_tr_below_min",
            "tr_implied_probability": implied,
            "model_edge_vs_tr": edge,
            "model_ev_vs_tr": ev,
            "model_market_gap": gap,
        }
    return {
        "qualified": True,
        "reason": "strict_playable",
        "tr_implied_probability": implied,
        "model_edge_vs_tr": edge,
        "model_ev_vs_tr": ev,
        "model_market_gap": gap,
    }


def policy_payload() -> Dict[str, Any]:
    return {
        "policy_version": POLICY_VERSION,
        "selection_semantics": "strict_playable_variable_0_to_10",
        "force_fill_top10": False,
        "international_reference_required": True,
        "turkey_executable_price_required": True,
        "min_model_probability": MIN_MODEL_PROBABILITY,
        "min_model_edge_vs_tr": MIN_MODEL_EDGE_VS_TR,
        "min_model_ev_vs_tr": MIN_MODEL_EV_VS_TR,
        "accepted_reference_qualities": sorted(ALLOWED_REFERENCE_QUALITIES),
    }
