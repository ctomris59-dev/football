#!/usr/bin/env python3
"""JSON-safe production predictor wrapper.

The advanced v3 ranking layer can carry PostgreSQL timestamptz values inside
market-consensus metadata. Psycopg's default Jsonb encoder rejects datetime
objects. Keep the validated v3 model/policy unchanged and make only its final
metadata serialization JSON-safe.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, Optional

from psycopg.types.json import Jsonb as PsyJsonb

import production_predictor_v3 as v3

MODEL_VERSION = v3.MODEL_VERSION
POLICY_VERSION = v3.POLICY_VERSION


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _safe_jsonb(value: Any):
    return PsyJsonb(value, dumps=lambda obj: json.dumps(obj, default=_json_default, separators=(",", ":")))


def run_predictions(database_url: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    original = v3.Jsonb
    try:
        v3.Jsonb = _safe_jsonb
        result = v3.run_predictions(database_url, **kwargs)
        result["serialization"] = "json-safe-v4"
        return result
    finally:
        v3.Jsonb = original


if __name__ == "__main__":
    print(json.dumps(run_predictions(), ensure_ascii=False, indent=2, default=_json_default))
