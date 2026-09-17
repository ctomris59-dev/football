#!/usr/bin/env python3
"""Serialization-safe facade for three-way no-vig market references.

The V1 builder keeps datetime objects in the raw bookmaker diagnostic payload. That
is valid Python but psycopg's default JSON dumper cannot serialize those objects.
This facade sanitizes only the JSON diagnostic representation; database timestamps
and all probability calculations remain unchanged.
"""
from __future__ import annotations

import json
from typing import Any

import one_x_two_market_reference as _base
from psycopg.types.json import Jsonb as _PsycopgJsonb


def _safe_jsonb(value: Any):
    return _PsycopgJsonb(json.loads(json.dumps(value, default=str)))


# build_refs resolves Jsonb from its defining module at runtime, so replacing that
# one symbol fixes the diagnostics serialization without changing its model logic.
_base.Jsonb = _safe_jsonb

build_refs = _base.build_refs
latest_ref = _base.latest_ref
selected_probability = _base.selected_probability
no_vig_three = _base.no_vig_three
outcome_label = _base.outcome_label

__all__ = [
    "build_refs",
    "latest_ref",
    "selected_probability",
    "no_vig_three",
    "outcome_label",
]
