#!/usr/bin/env python3
"""Production predictor v2: v1 probabilities + current readiness/market pipeline.

The xG-aware engine remains available for diagnostics/backtests, but the primary
selection probability is produced by the historically stronger v1 engine.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

import production_predictor as core
from model_engine import Prediction as CompatiblePrediction
from model_engine_v1 import predict_match as predict_v1

MODEL_VERSION = "production-poisson-form-v1"


def _primary_predict(history, home_team, away_team, *, recent_matches=18):
    p = predict_v1(history, home_team, away_team, recent_matches=recent_matches)
    # Reuse the current production storage/ranking pipeline with a compatible
    # object. xg_used=False explicitly means xG did not drive the primary pick.
    return CompatiblePrediction(
        p.p_over_2_5,
        p.p_btts,
        p.p_corners_over_8_5,
        p.lambda_home_goals,
        p.lambda_away_goals,
        p.lambda_total_corners,
        p.home_sample,
        p.away_sample,
        p.data_quality,
        False,
    )


def run_predictions(database_url: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    previous_predict = core.predict_match
    previous_version = core.MODEL_VERSION
    try:
        core.predict_match = _primary_predict
        core.MODEL_VERSION = MODEL_VERSION
        result = core.run_predictions(database_url, **kwargs)
        result["primary_model_note"] = "Backtest-leading v1 probabilities; xG is not used to drive the primary selection."
        print("PRODUCTION_V2_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")))
        return result
    finally:
        core.predict_match = previous_predict
        core.MODEL_VERSION = previous_version


if __name__ == "__main__":
    print(json.dumps(run_predictions(), ensure_ascii=False, indent=2, default=str))
