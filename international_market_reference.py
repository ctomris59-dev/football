#!/usr/bin/env python3
"""Fresh international no-vig market reference for Thursday betting.

International prices are reference-only: paired same-bookmaker no-vig fair probability,
model sanity check, and comparison with the executable Turkish price.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, Optional, Tuple

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
MAX_AGE_HOURS = float(os.getenv("INTERNATIONAL_REFERENCE_MAX_AGE_HOURS", "6"))
REFRESH_MAX_AGE_HOURS = float(os.getenv("INTERNATIONAL_REFRESH_MAX_AGE_HOURS", "2"))
MATCH_TOLERANCE_HOURS = float(os.getenv("INTERNATIONAL_FIXTURE_TOLERANCE_HOURS", "12"))
MATCH_SCORE_MIN = float(os.getenv("INTERNATIONAL_MATCH_SCORE_MIN", "1.55"))
MATCH_SIDE_MIN = float(os.getenv("INTERNATIONAL_MATCH_SIDE_MIN", "0.72"))
MATCH_MARGIN_MIN = float(os.getenv("INTERNATIONAL_MATCH_MARGIN_MIN", "0.08"))
MAX_DISPERSION = float(os.getenv("INTERNATIONAL_MAX_DISPERSION", "0.06"))
MAX_SHARP_DELTA = float(os.getenv("INTERNATIONAL_MAX_SHARP_DELTA", "0.08"))
TARGET_MARKETS = ("over_2_5", "btts", "corners_over_8_5")

ALIASES = {
    "man utd": "manchester united", "man united": "manchester united", "man city": "manchester city",
    "spurs": "tottenham hotspur", "tottenham": "tottenham hotspur",
    "wolves": "wolverhampton wanderers", "wolverhampton": "wolverhampton wanderers",
    "nottm forest": "nottingham forest", "hoffenheim": "tsg hoffenheim", "stuttgart": "vfb stuttgart",
    "koln": "fc koln", "cologne": "fc koln", "frankfurt": "eintracht frankfurt",
    "gladbach": "borussia monchengladbach", "monchengladbach": "borussia monchengladbach",
    "leverkusen": "bayer leverkusen", "leipzig": "rb leipzig", "inter": "inter milan",
    "internazionale": "inter milan", "psg": "paris saint germain",
}

DDL = """
CREATE TABLE IF NOT EXISTS international_market_refs(
 event_id TEXT NOT NULL,
 market TEXT NOT NULL,
 snapshot_hour TIMESTAMPTZ NOT NULL,
 international_fixture_id TEXT NOT NULL,
 bookmaker_count INTEGER NOT NULL,
 consensus_p_yes DOUBLE PRECISION NOT NULL,
 mean_p_yes DOUBLE PRECISION,
 dispersion DOUBLE PRECISION,
 sharp_bookmaker TEXT,
 sharp_p_yes DOUBLE PRECISION,
 reference_p_yes DOUBLE PRECISION NOT NULL,
 best_price_yes DOUBLE PRECISION,
 median_price_yes DOUBLE PRECISION,
 source_built_at TIMESTAMPTZ NOT NULL,
 quality TEXT NOT NULL,
 mapped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(event_id,market,snapshot_hour)
);
CREATE INDEX IF NOT EXISTS idx_international_market_refs_latest
 ON international_market_refs(event_id,market,mapped_at DESC);
CREATE TABLE IF NOT EXISTS international_market_ref_runs(
 id BIGSERIAL PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL DEFAULT 'running',
 source_refresh_status TEXT,
 target_fixtures INTEGER NOT NULL DEFAULT 0,
 matched_fixtures INTEGER NOT NULL DEFAULT 0,
 reference_rows INTEGER NOT NULL DEFAULT 0,
 message TEXT
);
"""


def json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _norm_team(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = text.translate(str.maketrans({"ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c"}))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    text = re.sub(r"\s+", " ", text)
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
    aa, bb = _norm_team(a), _norm_team(b)
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


def reference_quality(bookmaker_count: int, consensus_p_yes: Optional[float], dispersion: Optional[float], sharp_bookmaker: Optional[str], sharp_p_yes: Optional[float]) -> Tuple[bool, str]:
    if consensus_p_yes is None:
        return False, "missing_consensus"
    p = float(consensus_p_yes)
    if not 0.08 <= p <= 0.92:
        return False, "implausible_probability"
    if dispersion is not None and float(dispersion) > MAX_DISPERSION:
        return False, "dispersion_high"
    if sharp_p_yes is not None and abs(float(sharp_p_yes) - p) > MAX_SHARP_DELTA:
        return False, "sharp_consensus_conflict"
    if int(bookmaker_count or 0) >= 2:
        return True, "multi_book_consensus"
    if int(bookmaker_count or 0) == 1 and sharp_bookmaker and sharp_p_yes is not None:
        return True, "sharp_single_book"
    return False, "insufficient_books"


def _source_is_fresh(conn) -> bool:
    try:
        row = conn.execute("SELECT finished_at,market_rows FROM market_consensus_runs WHERE status='success' ORDER BY id DESC LIMIT 1").fetchone()
        if not row or int(row[1] or 0) <= 0:
            return False
        return bool(conn.execute("SELECT %s >= NOW()-(%s||' hours')::interval", (row[0], REFRESH_MAX_AGE_HOURS)).fetchone()[0])
    except Exception:
        return False


def refresh_source(database_url: str = DATABASE_URL, *, force: bool = False) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(database_url) as conn:
        if not force and _source_is_fresh(conn):
            return {"status": "fresh_skip", "max_age_hours": REFRESH_MAX_AGE_HOURS}
    from oddspapi_allbooks_importer_v4 import run_import
    odds = run_import(database_url)
    if str(odds.get("status")) not in {"success", "catalog_bootstrap"}:
        return {"status": "source_unavailable", "odds": odds}
    from market_consensus_builder import build
    consensus = build(database_url)
    return {"status": "refreshed", "odds": odds, "consensus": consensus}


def _match_fixture(target: Dict[str, Any], candidates: list[Dict[str, Any]]) -> tuple[Optional[Dict[str, Any]], bool]:
    ranked = []
    target_dt = target["match_date"]
    if target_dt.tzinfo is None:
        target_dt = target_dt.replace(tzinfo=timezone.utc)
    for row in candidates:
        dt = row["start_time"]
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        hours = abs((target_dt.astimezone(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()) / 3600.0
        if hours > MATCH_TOLERANCE_HOURS:
            continue
        hs, aws = team_score(row.get("home_team"), target.get("home_team")), team_score(row.get("away_team"), target.get("away_team"))
        if hs < MATCH_SIDE_MIN or aws < MATCH_SIDE_MIN or hs + aws < MATCH_SCORE_MIN:
            continue
        ranked.append((hs + aws + max(0.0, 0.08 * (1.0 - hours / max(1.0, MATCH_TOLERANCE_HOURS))), row))
    if not ranked:
        return None, False
    ranked.sort(key=lambda x: x[0], reverse=True)
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < MATCH_MARGIN_MIN:
        return None, True
    return ranked[0][1], False


def build_refs(database_url: str = DATABASE_URL, *, start: datetime, end: datetime) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(DDL)
        run_id = int(conn.execute("INSERT INTO international_market_ref_runs(status) VALUES('running') RETURNING id").fetchone()[0])
        try:
            targets = [{"event_id": str(r[0]), "match_date": r[1], "home_team": str(r[2]), "away_team": str(r[3])} for r in conn.execute(
                """SELECT event_id,match_date,home_team,away_team FROM espn_upcoming
                   WHERE is_current=TRUE AND match_date>=%s AND match_date<%s""", (start, end)).fetchall()]
            candidates = [{"fixture_id": str(r[0]), "snapshot_hour": r[1], "start_time": r[2], "home_team": r[3], "away_team": r[4]} for r in conn.execute(
                """SELECT DISTINCT ON (fixture_id) fixture_id,snapshot_hour,start_time,home_team,away_team
                   FROM oddspapi_fixture_snapshots WHERE fetched_at>=NOW()-(%s||' hours')::interval
                   ORDER BY fixture_id,snapshot_hour DESC""", (MAX_AGE_HOURS,)).fetchall()]
            consensus_rows = conn.execute(
                """SELECT DISTINCT ON (fixture_id,market)
                          fixture_id,market,snapshot_hour,bookmaker_count,consensus_p_yes,mean_p_yes,
                          dispersion,sharp_bookmaker,sharp_p_yes,best_price_yes,median_price_yes,built_at
                   FROM market_consensus_snapshots WHERE built_at>=NOW()-(%s||' hours')::interval
                   ORDER BY fixture_id,market,snapshot_hour DESC""", (MAX_AGE_HOURS,)).fetchall()
            consensus = {(str(r[0]), str(r[1])): r for r in consensus_rows}
            matched = refs = ambiguous = 0
            for target in targets:
                intl, is_ambiguous = _match_fixture(target, candidates)
                ambiguous += int(is_ambiguous)
                if not intl:
                    continue
                matched += 1
                for market in TARGET_MARKETS:
                    row = consensus.get((intl["fixture_id"], market))
                    if not row:
                        continue
                    (_fid, _market, snapshot_hour, bookmaker_count, consensus_p, mean_p, dispersion, sharp_bookmaker, sharp_p, best_yes, median_yes, built_at) = row
                    ok, quality = reference_quality(int(bookmaker_count or 0), float(consensus_p) if consensus_p is not None else None, float(dispersion) if dispersion is not None else None, str(sharp_bookmaker) if sharp_bookmaker else None, float(sharp_p) if sharp_p is not None else None)
                    if not ok:
                        continue
                    conn.execute(
                        """INSERT INTO international_market_refs(
                             event_id,market,snapshot_hour,international_fixture_id,bookmaker_count,consensus_p_yes,mean_p_yes,
                             dispersion,sharp_bookmaker,sharp_p_yes,reference_p_yes,best_price_yes,median_price_yes,source_built_at,quality,mapped_at)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                           ON CONFLICT(event_id,market,snapshot_hour) DO UPDATE SET
                             international_fixture_id=EXCLUDED.international_fixture_id,bookmaker_count=EXCLUDED.bookmaker_count,
                             consensus_p_yes=EXCLUDED.consensus_p_yes,mean_p_yes=EXCLUDED.mean_p_yes,dispersion=EXCLUDED.dispersion,
                             sharp_bookmaker=EXCLUDED.sharp_bookmaker,sharp_p_yes=EXCLUDED.sharp_p_yes,
                             reference_p_yes=EXCLUDED.reference_p_yes,best_price_yes=EXCLUDED.best_price_yes,
                             median_price_yes=EXCLUDED.median_price_yes,source_built_at=EXCLUDED.source_built_at,
                             quality=EXCLUDED.quality,mapped_at=NOW()""",
                        (target["event_id"], market, snapshot_hour, intl["fixture_id"], int(bookmaker_count), float(consensus_p), float(mean_p) if mean_p is not None else None, float(dispersion) if dispersion is not None else None, str(sharp_bookmaker) if sharp_bookmaker else None, float(sharp_p) if sharp_p is not None else None, float(consensus_p), float(best_yes) if best_yes is not None else None, float(median_yes) if median_yes is not None else None, built_at, quality),
                    )
                    refs += 1
            result = {"status": "success", "target_fixtures": len(targets), "matched_fixtures": matched, "ambiguous_fixtures": ambiguous, "reference_rows": refs, "max_age_hours": MAX_AGE_HOURS}
            conn.execute("""UPDATE international_market_ref_runs SET finished_at=NOW(),status='success',target_fixtures=%s,matched_fixtures=%s,reference_rows=%s,message=%s WHERE id=%s""", (len(targets), matched, refs, json.dumps(result, default=json_default), run_id))
            print("INTERNATIONAL_MARKET_REF_RESULT", json.dumps(result, default=json_default, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            conn.execute("UPDATE international_market_ref_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s", (str(exc)[:1200], run_id))
            raise


def refresh_and_map(database_url: str = DATABASE_URL, *, start: datetime, end: datetime, force: bool = False) -> Dict[str, Any]:
    source = refresh_source(database_url, force=force)
    mapped = build_refs(database_url, start=start, end=end)
    return {"status": "success", "source": source, "mapped": mapped}


def latest_ref(conn, event_id: str, market: str) -> Optional[Dict[str, Any]]:
    try:
        row = conn.execute(
            """SELECT international_fixture_id,bookmaker_count,consensus_p_yes,mean_p_yes,dispersion,
                      sharp_bookmaker,sharp_p_yes,reference_p_yes,best_price_yes,median_price_yes,
                      source_built_at,quality,mapped_at
               FROM international_market_refs WHERE event_id=%s AND market=%s
                 AND source_built_at>=NOW()-(%s||' hours')::interval
               ORDER BY mapped_at DESC LIMIT 1""", (str(event_id), str(market), MAX_AGE_HOURS)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return {"international_fixture_id": str(row[0]), "bookmaker_count": int(row[1]), "consensus_p_yes": float(row[2]), "mean_p_yes": float(row[3]) if row[3] is not None else None, "dispersion": float(row[4]) if row[4] is not None else None, "sharp_bookmaker": str(row[5]) if row[5] else None, "sharp_p_yes": float(row[6]) if row[6] is not None else None, "reference_p_yes": float(row[7]), "best_price_yes": float(row[8]) if row[8] is not None else None, "median_price_yes": float(row[9]) if row[9] is not None else None, "source_built_at": row[10], "quality": str(row[11]), "mapped_at": row[12]}


if __name__ == "__main__":
    # Kept deliberately import-safe; production calls refresh_and_map from the opening watcher.
    raise SystemExit("Use thursday_opening_watch.py for production execution")
