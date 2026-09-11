#!/usr/bin/env python3
"""Collect official Turkish İddaa prices for this weekend's Big-Five fixtures.

This collector is intentionally independent of the legacy production readiness gate.
It matches official İddaa events directly to ESPN upcoming fixtures for Friday-Monday
and stores only the three markets used by the Thursday decision engine:
O2.5 goals, BTTS Yes, O8.5 corners.

The shared classifier also recognizes the full-time match-result market so the
companion all-sides collector can store 1/0/2 without broadening this legacy pass.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, Optional

import psycopg
import requests

from thursday_decision_engine import weekend_bounds
from turkey_value_workflow import store_price, valid_price

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BASE_URL = os.getenv("IDDAA_SPORTSBOOK_BASE_URL", "https://sportsbookv2.iddaa.com/sportsbook").rstrip("/")
SOURCE = "iddaa_official"
TIMEOUT = float(os.getenv("IDDAA_TIMEOUT_SECONDS", "30"))
FIXTURE_TOLERANCE_HOURS = float(os.getenv("IDDAA_FIXTURE_TOLERANCE_HOURS", "12"))
MATCH_SCORE_MIN = float(os.getenv("IDDAA_MATCH_SCORE_MIN", "1.55"))
MATCH_SIDE_MIN = float(os.getenv("IDDAA_MATCH_SIDE_MIN", "0.72"))
MATCH_MARGIN_MIN = float(os.getenv("IDDAA_MATCH_MARGIN_MIN", "0.08"))
TARGET_MARKETS = {"over_2_5", "btts", "corners_over_8_5"}

DDL = """
CREATE TABLE IF NOT EXISTS turkey_odds_import_runs(
 id BIGSERIAL PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL DEFAULT 'running',
 official_events INTEGER NOT NULL DEFAULT 0,
 production_fixtures INTEGER NOT NULL DEFAULT 0,
 matched_fixtures INTEGER NOT NULL DEFAULT 0,
 target_market_rows INTEGER NOT NULL DEFAULT 0,
 stored_prices INTEGER NOT NULL DEFAULT 0,
 ambiguous_fixtures INTEGER NOT NULL DEFAULT 0,
 message TEXT
);
"""

ALIASES = {
    "man utd": "manchester united", "man united": "manchester united", "man city": "manchester city",
    "spurs": "tottenham hotspur", "tottenham": "tottenham hotspur",
    "wolves": "wolverhampton wanderers", "wolverhampton": "wolverhampton wanderers",
    "brighton": "brighton hove albion", "west ham": "west ham united", "newcastle": "newcastle united",
    "nottm forest": "nottingham forest", "hoffenheim": "tsg hoffenheim", "stuttgart": "vfb stuttgart",
    "koln": "fc koln", "cologne": "fc koln", "frankfurt": "eintracht frankfurt",
    "gladbach": "borussia monchengladbach", "monchengladbach": "borussia monchengladbach",
    "leverkusen": "bayer leverkusen", "leipzig": "rb leipzig", "inter": "inter milan",
    "internazionale": "inter milan", "ac milan": "milan", "psg": "paris saint germain",
}

CANONICAL_SELECTION = {
    "over_2_5": "2.5 ÜST",
    "btts": "KG VAR",
    "corners_over_8_5": "8.5 KORNER ÜST",
}


def _ascii(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = text.translate(str.maketrans({"ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c"}))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return re.sub(r"\s+", " ", text)


def normalize_team(value: Any) -> str:
    text = _ascii(value)
    for prefix in ("1 fc ", "fc ", "afc ", "ac ", "cf ", "ss "):
        if text.startswith(prefix) and len(text) > len(prefix) + 3:
            text = text[len(prefix):]
            break
    for suffix in (" fc", " afc", " cf"):
        if text.endswith(suffix) and len(text) > len(suffix) + 3:
            text = text[: -len(suffix)]
            break
    return ALIASES.get(text, text)


def team_score(a: Any, b: Any) -> float:
    aa, bb = normalize_team(a), normalize_team(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    if min(len(aa), len(bb)) >= 4 and (aa in bb or bb in aa):
        return 0.93
    ratio = SequenceMatcher(None, aa, bb).ratio()
    sa, sb = set(aa.split()), set(bb.split())
    token = len(sa & sb) / max(1, len(sa | sb))
    return max(ratio, token)


def _line_value(market: Dict[str, Any], rendered_name: str) -> Optional[float]:
    raw = market.get("sov")
    if raw is not None:
        try:
            return float(str(raw).replace(",", "."))
        except ValueError:
            pass
    m = re.search(r"(?<!\d)(\d+[\.,]\d+)(?!\d)", rendered_name)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def classify_market(rendered_name: str, market: Dict[str, Any]) -> Optional[str]:
    n = _ascii(rendered_name)
    line = _line_value(market, rendered_name)
    if "ilk yari" in n or "first half" in n:
        return None
    if n in {"mac sonucu", "match result", "1x2", "tam zamanli mac sonucu", "full time result", "fulltime result"}:
        return "match_result"
    if "toplam korner sayisi" in n or "total corners" in n:
        return "corners_over_8_5" if line is not None and abs(line - 8.5) < 1e-9 else None
    if n in {"karsilikli gol", "both teams to score"}:
        return "btts"
    forbidden = ("mac sonucu", "match result", "karsilikli gol", "both teams", "ev sahibi", "deplasman", "home team", "away team", "korner")
    if any(token in n for token in forbidden):
        return None
    if line is not None and abs(line - 2.5) < 1e-9 and any(x in n for x in ("alt ust", "alti ustu", "under over", "toplam gol", "total goals")):
        return "over_2_5"
    return None


def wanted_outcome(market_key: str, outcomes: Iterable[Dict[str, Any]]) -> Optional[float]:
    for outcome in outcomes or []:
        name = _ascii(outcome.get("n"))
        wanted = name in {"var", "evet", "yes"} if market_key == "btts" else (name in {"ust", "over"} or name.startswith("ust ") or name.startswith("over "))
        if wanted and valid_price(outcome.get("odd")):
            return float(outcome["odd"])
    return None


def render_market_name(market: Dict[str, Any], market_config: Dict[str, Any]) -> str:
    cfg = market_config.get(f"{market.get('t')}_{market.get('st')}") or {}
    name = str(cfg.get("n") or "")
    if market.get("sov") is not None and "{0}" in name:
        name = name.replace("{0}", str(market.get("sov")))
    return name


def _event_time(event: Dict[str, Any]) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(float(event["d"]), tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _weekend_fixtures(conn) -> tuple[list[Dict[str, Any]], datetime, datetime]:
    _, start, end = weekend_bounds()
    rows = conn.execute(
        """SELECT event_id,match_date,home_team,away_team
             FROM espn_upcoming
            WHERE is_current=TRUE AND match_date>=%s AND match_date<%s
            ORDER BY match_date""",
        (start, end),
    ).fetchall()
    fixtures = [{"event_id": str(r[0]), "match_date": r[1], "home": str(r[2]), "away": str(r[3])} for r in rows]
    return fixtures, start, end


def match_fixture(event: Dict[str, Any], fixtures: list[Dict[str, Any]]) -> tuple[Optional[Dict[str, Any]], bool]:
    dt = _event_time(event)
    if dt is None:
        return None, False
    ranked = []
    for fixture in fixtures:
        fdt = fixture["match_date"]
        if fdt.tzinfo is None:
            fdt = fdt.replace(tzinfo=timezone.utc)
        hours = abs((fdt.astimezone(timezone.utc) - dt).total_seconds()) / 3600.0
        if hours > FIXTURE_TOLERANCE_HOURS:
            continue
        hs = team_score(event.get("hn"), fixture["home"])
        aws = team_score(event.get("an"), fixture["away"])
        if hs < MATCH_SIDE_MIN or aws < MATCH_SIDE_MIN:
            continue
        score = hs + aws + max(0.0, 0.08 * (1.0 - hours / max(1.0, FIXTURE_TOLERANCE_HOURS)))
        if hs + aws >= MATCH_SCORE_MIN:
            ranked.append((score, fixture))
    if not ranked:
        return None, False
    ranked.sort(key=lambda x: x[0], reverse=True)
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < MATCH_MARGIN_MIN:
        return None, True
    return ranked[0][1], False


def _get_json(session: requests.Session, path: str) -> Dict[str, Any]:
    response = session.get(f"{BASE_URL}/{path.lstrip('/')}", timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or "data" not in payload:
        raise RuntimeError(f"Unexpected İddaa response for {path}")
    return payload


def run_import(database_url: str = DATABASE_URL) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    started = datetime.now(timezone.utc)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(DDL)
        import_run_id = int(conn.execute("INSERT INTO turkey_odds_import_runs(started_at) VALUES(%s) RETURNING id", (started,)).fetchone()[0])
        try:
            fixtures, horizon_start, horizon_end = _weekend_fixtures(conn)
            if not fixtures:
                raise RuntimeError("No current weekend ESPN fixtures available for safe İddaa matching")

            session = requests.Session()
            session.headers.update({"Accept": "application/json", "User-Agent": "football-thursday-value/2.0"})
            events_payload = _get_json(session, "events?st=1&type=0&version=0")
            config_payload = _get_json(session, "get_market_config")
            events = ((events_payload.get("data") or {}).get("events") or [])
            market_config = ((config_payload.get("data") or {}).get("m") or {})

            matched_event_ids = set()
            ambiguous = target_rows = stored = 0
            for event in events:
                fixture, is_ambiguous = match_fixture(event, fixtures)
                if is_ambiguous:
                    ambiguous += 1
                if not fixture:
                    continue
                matched_event_ids.add(fixture["event_id"])
                for market in event.get("m") or []:
                    rendered = render_market_name(market, market_config)
                    key = classify_market(rendered, market)
                    if key not in TARGET_MARKETS:
                        continue
                    price = wanted_outcome(key, market.get("o") or [])
                    if price is None:
                        continue
                    target_rows += 1
                    if store_price(conn, fixture["event_id"], key, CANONICAL_SELECTION[key], SOURCE, price):
                        stored += 1

            matched = len(matched_event_ids)
            result = {
                "status": "success", "import_run_id": import_run_id, "official_events": len(events),
                "production_fixtures": len(fixtures), "matched_fixtures": matched,
                "fixture_coverage": round(matched / len(fixtures), 4) if fixtures else 0.0,
                "ambiguous_fixtures": ambiguous, "target_market_rows": target_rows, "stored_prices": stored,
                "source": SOURCE, "horizon_start": horizon_start, "horizon_end": horizon_end,
            }
            conn.execute(
                """UPDATE turkey_odds_import_runs SET finished_at=NOW(),status='success',official_events=%s,
                          production_fixtures=%s,matched_fixtures=%s,target_market_rows=%s,stored_prices=%s,
                          ambiguous_fixtures=%s,message=%s WHERE id=%s""",
                (len(events), len(fixtures), matched, target_rows, stored, ambiguous, json.dumps(result, ensure_ascii=False, default=str), import_run_id),
            )
            print("TURKEY_IDDAA_ODDS_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            conn.execute("UPDATE turkey_odds_import_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s", (str(exc)[:1200], import_run_id))
            raise


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2, default=str))
