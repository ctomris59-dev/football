#!/usr/bin/env python3
"""Store both executable sides of the three Turkish target markets.

The original collector keeps the positive side needed by the strict value workflow.
This companion pass expands only the executable-price layer so the weekly reliability
list may choose the model's stronger side (Over/Under, BTTS Yes/No, Corners Over/Under)
without changing V1 probabilities or value thresholds.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, Iterable, Optional

import psycopg
import requests

from turkey_iddaa_odds_collector import (
    DATABASE_URL,
    SOURCE,
    TARGET_MARKETS,
    _ascii,
    _get_json,
    _weekend_fixtures,
    classify_market,
    match_fixture,
    render_market_name,
)
from turkey_value_workflow import store_price, valid_price

SELECTIONS = {
    "over_2_5": {"yes": "2.5 ÜST", "no": "2.5 ALT"},
    "btts": {"yes": "KG VAR", "no": "KG YOK"},
    "corners_over_8_5": {"yes": "8.5 KORNER ÜST", "no": "8.5 KORNER ALT"},
}


def _side(market_key: str, outcome_name: Any) -> Optional[str]:
    name = _ascii(outcome_name)
    if market_key == "btts":
        if name in {"var", "evet", "yes"}:
            return "yes"
        if name in {"yok", "hayir", "hayır", "no"}:
            return "no"
        return None
    if name in {"ust", "over"} or name.startswith("ust ") or name.startswith("over "):
        return "yes"
    if name in {"alt", "under"} or name.startswith("alt ") or name.startswith("under "):
        return "no"
    return None


def _prices(market_key: str, outcomes: Iterable[Dict[str, Any]]) -> Dict[str, float]:
    found: Dict[str, float] = {}
    for outcome in outcomes or []:
        side = _side(market_key, outcome.get("n"))
        if side and valid_price(outcome.get("odd")):
            found[side] = float(outcome["odd"])
    return found


def run_import(database_url: str = DATABASE_URL) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(database_url, autocommit=True) as conn:
        fixtures, horizon_start, horizon_end = _weekend_fixtures(conn)
        if not fixtures:
            raise RuntimeError("No current Friday-Monday ESPN fixtures available")

        session = requests.Session()
        session.headers.update({"Accept": "application/json", "User-Agent": "football-weekly-reliability/1.0"})
        events_payload = _get_json(session, "events?st=1&type=0&version=0")
        config_payload = _get_json(session, "get_market_config")
        events = ((events_payload.get("data") or {}).get("events") or [])
        market_config = ((config_payload.get("data") or {}).get("m") or {})

        matched = set()
        counts: Dict[str, int] = defaultdict(int)
        stored = 0
        for event in events:
            fixture, _ambiguous = match_fixture(event, fixtures)
            if not fixture:
                continue
            matched.add(fixture["event_id"])
            for market in event.get("m") or []:
                rendered = render_market_name(market, market_config)
                key = classify_market(rendered, market)
                if key not in TARGET_MARKETS:
                    continue
                for side, price in _prices(key, market.get("o") or []).items():
                    selection = SELECTIONS[key][side]
                    counts[f"{key}:{side}"] += 1
                    if store_price(conn, fixture["event_id"], key, selection, SOURCE, price):
                        stored += 1

        result = {
            "status": "success",
            "production_fixtures": len(fixtures),
            "matched_fixtures": len(matched),
            "fixture_coverage": round(len(matched) / len(fixtures), 4) if fixtures else 0.0,
            "stored_rows": stored,
            "selection_counts": dict(counts),
            "horizon_start": horizon_start,
            "horizon_end": horizon_end,
        }
        print("TURKEY_TWO_SIDED_ODDS_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
        return result


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2, default=str))
