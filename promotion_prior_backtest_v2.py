#!/usr/bin/env python3
"""Promotion prior backtest v2 using missing-safe aggregation/canonicalization."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

import promotion_prior_builder as builder
from promotion_prior_builder_v2 import aggregate, canon

# Patch before importing the backtest module because it imports helpers by name.
builder.aggregate = aggregate
builder.canon = canon
import promotion_prior_backtest as base  # noqa: E402

VERSION = "promotion-prior-brier-v2-missing-safe"
base.aggregate = aggregate
base.canon = canon
base.VERSION = VERSION


def run_backtest(database_url: Optional[str] = None) -> Dict[str, Any]:
    result = base.run_backtest(database_url)
    result["version"] = VERSION
    result["missing_metric_policy"] = "neutral_1.0_not_zero"
    print("PROMOTION_PRIOR_BACKTEST_V2_RESULT", json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return result


if __name__ == "__main__":
    print(json.dumps(run_backtest(), ensure_ascii=False, indent=2))
