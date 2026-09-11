#!/usr/bin/env python3
"""Store all executable sides of the Turkish target markets.

The original collector keeps the positive side needed by the strict binary value
workflow. This companion pass expands the executable-price layer so the weekly
reliability list may choose Over/Under, BTTS Yes/No, Corners Over/Under and the
three full-time match-result outcomes 1/0/2 without changing model probabilities.
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
    team_score,
)
from turkey_value_workflow import store_price, valid_market_price, valid_price

ALL_TARGET_MARKETS = set(TARGET_MARKETS) | {"match_result"}

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


def _result_selection(outcome_name: Any, home: str, away: str) -> Optional[str]:
    name = _ascii(outcome_name)
    if name in {"1", "ev sahibi", "ev sahibi kazanir", "home", "home win"}:
        return "1"
    if name in {"0", "x", "beraberlik", "draw", "tie"}:
        return "0"
    if name in {"2", "deplasman", "deplasman kazanir", "away", "away win"}:
        return "2"
    hs, aws = team_score(outcome_name, home), team_score(outcome_name, away)
    if hs >= 0.72 and hs - aws >= 0.10:
        return "1"
    if aws >= 0.72 and aws - hs >= 0.10:
        return "2"
    return None


def _result_prices(outcomes: Iterable[Dict[str, Any]], home: str, away: str) -> Dict[str, float]:
    found: Dict[str, float] = {}
    for outcome in outcomes or []:
        selection = _result_selection(outcome.get("n"), home, away)
        if selection and valid_market_price(outcome.get("odd"), "match_result"):
            found[selection] = float(outcome["odd"])
    return found


def run_import(database_url: str = DATABASE_URL) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(database_url, autocommit=True) as conn:
        fixtures, horizon_start, horizon_end = _weekend_fixtures(conn)
        if not fixtures:
            raise RuntimeError("No current Friday-Monday ESPN fixtures available")

        session = requests.Session()
        session.headers.update({"Accept": "application/json", "User-Agent": "football-weekly-reliability/1.1"})
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
                if key not in ALL_TARGET_MARKETS:
                    continue
                if key == "match_result":
                    prices = _result_prices(market.get("o") or [], fixture["home"], fixture["away"])
                    # Require all three outcomes from the same official market before storing.
                    if set(prices) != {"1", "0", "2"}:
                        continue
                    for selection, price in prices.items():
                        counts[f"match_result:{selection}"] += 1
                        if store_price(conn, fixture["event_id"], "match_result", selection, SOURCE, price):
                            stored += 1
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
            "match_result_enabled": True,
            "horizon_start": horizon_start,
            "horizon_end": horizon_end,
        }
        print("TURKEY_TWO_SIDED_ODDS_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
        return result


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2, default=str))
