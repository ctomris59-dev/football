#!/usr/bin/env python3
"""Missing-safe promotion-prior builder compatibility layer.

Results-only fallback datasets legitimately lack shots/SOT/corners. The original
aggregator divided missing fields by match count, silently turning missing data
into zero. This version keeps per-metric observation counts and maps unavailable
metrics to a neutral relative prior (1.0), so only observed evidence can move a
promoted-team prior.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from typing import Any, Dict, Optional

import promotion_prior_builder as base

METRICS = base.METRICS

ALIASES = {
    "hamburger sv": "hamburg",
    "hamburg sv": "hamburg",
    "real oviedo": "oviedo",
    "fc koln": "koln",
    "1 koln": "koln",
    "1 fc koln": "koln",
    "saint etienne": "st etienne",
    "as saint etienne": "st etienne",
    "stade de reims": "reims",
    "ea guingamp": "guingamp",
    "us cremona": "cremonese",
    "us cremonese": "cremonese",
    "us sassuolo": "sassuolo",
    "sassuolo calcio": "sassuolo",
    "venezia fc": "venezia",
    "pisa sc": "pisa",
    "paris fc": "paris",
    "fc lorient": "lorient",
    "fc metz": "metz",
}


def canon(v: Any) -> str:
    s = unicodedata.normalize("NFKD", str(v or "")).encode("ascii", "ignore").decode().lower().replace("'", "")
    s = re.sub(r"\b(afc|fc|cf|ssc|ac|calcio|club|football club|sc|sv|ss|as|us)\b", " ", s)
    s = re.sub(r"\b(18|19|20)\d{2}\b", " ", s)
    s = re.sub(r"^1\s+", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return ALIASES.get(s, s)


def aggregate(rows):
    sums: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    obs: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    matches: Dict[str, int] = defaultdict(int)
    divisions: Dict[str, str] = {}
    for div, h, a, hg, ag, hs, ass, hst, ast, hc, ac in rows:
        ch, ca = canon(h), canon(a)
        if not ch or not ca or hg is None or ag is None:
            continue
        divisions.setdefault(ch, str(div)); divisions.setdefault(ca, str(div))
        matches[ch] += 1; matches[ca] += 1
        pairs = [
            ("goals_for", ch, hg), ("goals_against", ch, ag),
            ("goals_for", ca, ag), ("goals_against", ca, hg),
            ("shots_for", ch, hs), ("shots_against", ch, ass),
            ("shots_for", ca, ass), ("shots_against", ca, hs),
            ("sot_for", ch, hst), ("sot_against", ch, ast),
            ("sot_for", ca, ast), ("sot_against", ca, hst),
            ("corners_for", ch, hc), ("corners_against", ch, ac),
            ("corners_for", ca, ac), ("corners_against", ca, hc),
        ]
        for metric, team, value in pairs:
            if value is None:
                continue
            sums[team][metric] += float(value)
            obs[team][metric] += 1
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for team, n in matches.items():
        d: Dict[str, Optional[float]] = {"matches": float(n)}
        for metric in METRICS:
            count = obs[team].get(metric, 0)
            d[metric] = sums[team].get(metric, 0.0) / count if count else None
        out[team] = d
    return out, divisions


# All functions inside base.build resolve these globals from base at runtime.
base.canon = canon
base.aggregate = aggregate


def build(database_url: Optional[str] = None):
    result = base.build(database_url)
    result["missing_metric_policy"] = "neutral_1.0_not_zero"
    print("PROMOTION_PRIORS_V2_RESULT", json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
