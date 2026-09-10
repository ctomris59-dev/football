#!/usr/bin/env python3
"""Shared helpers for executable multi-line total-corner markets.

The frozen V1 model already estimates one total-corners Poisson intensity. This
module only evaluates that same lambda at the line that is actually offered; it
does not change the underlying V1 model parameters.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Optional, Tuple

from model_engine_v1 import poisson_prob_over

SUPPORTED_CORNER_LINES: Tuple[float, ...] = (7.5, 8.5, 9.5, 10.5)


def normalize_corner_line(value: Any) -> Optional[float]:
    try:
        line = round(float(value), 1)
    except (TypeError, ValueError):
        return None
    for allowed in SUPPORTED_CORNER_LINES:
        if abs(line - allowed) < 1e-9:
            return allowed
    return None


def corner_market_key(line: Any) -> Optional[str]:
    x = normalize_corner_line(line)
    if x is None:
        return None
    return f"corners_over_{str(x).replace('.', '_')}"


def corner_line_from_market(market: Any) -> Optional[float]:
    text = str(market or "")
    prefix = "corners_over_"
    if not text.startswith(prefix):
        return None
    return normalize_corner_line(text[len(prefix):].replace("_", "."))


def is_corner_market(market: Any) -> bool:
    return corner_line_from_market(market) is not None


def corner_probability(lambda_total_corners: Any, line: Any) -> float:
    x = normalize_corner_line(line)
    if x is None:
        raise ValueError(f"Unsupported corner line: {line}")
    return float(poisson_prob_over(int(math.floor(x)), float(lambda_total_corners)))


def corner_selections(line: Any) -> Tuple[str, str]:
    x = normalize_corner_line(line)
    if x is None:
        raise ValueError(f"Unsupported corner line: {line}")
    shown = f"{x:.1f}"
    return f"{shown} KORNER ÜST", f"{shown} KORNER ALT"


def corner_market_specs(lambda_total_corners: Any) -> Iterable[Dict[str, Any]]:
    for line in SUPPORTED_CORNER_LINES:
        yes, no = corner_selections(line)
        yield {
            "market": corner_market_key(line),
            "line": line,
            "p_yes": corner_probability(lambda_total_corners, line),
            "yes_selection": yes,
            "no_selection": no,
        }


def all_corner_market_keys() -> Tuple[str, ...]:
    return tuple(corner_market_key(x) for x in SUPPORTED_CORNER_LINES)
