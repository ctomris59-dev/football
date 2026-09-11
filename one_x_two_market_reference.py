#!/usr/bin/env python3
"""Three-way no-vig international reference for full-time 1X2.

The regular international reference pipeline is intentionally binary (yes/no).
This module handles match-result prices separately so 1/X/2 probabilities are
always de-vigged from all three outcomes from the SAME bookmaker/market.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import unicodedata
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

from international_market_reference import MAX_AGE_HOURS, _match_fixture
from oddspapi_allbooks_importer import market_kind

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SHARP_PRIORITY = [x.strip().lower() for x in os.getenv("SHARP_BOOKMAKERS", "pinnacle,betfair_ex_eu,betfair").split(",") if x.strip()]
PRICE_MIN = float(os.getenv("INTERNATIONAL_1X2_MIN_PRICE", "1.01"))
PRICE_MAX = float(os.getenv("INTERNATIONAL_1X2_MAX_PRICE", "30.0"))
MIN_OVERROUND = float(os.getenv("INTERNATIONAL_1X2_MIN_OVERROUND", "0.95"))
MAX_OVERROUND = float(os.getenv("INTERNATIONAL_1X2_MAX_OVERROUND", "1.40"))
MAX_DISPERSION = float(os.getenv("INTERNATIONAL_1X2_MAX_DISPERSION", "0.08"))
MAX_SHARP_DELTA = float(os.getenv("INTERNATIONAL_1X2_MAX_SHARP_DELTA", "0.10"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS one_x_two_market_refs(
 event_id TEXT NOT NULL,
 snapshot_hour TIMESTAMPTZ NOT NULL,
 international_fixture_id TEXT NOT NULL,
 bookmaker_count INTEGER NOT NULL,
 p1 DOUBLE PRECISION NOT NULL,
 p0 DOUBLE PRECISION NOT NULL,
 p2 DOUBLE PRECISION NOT NULL,
 dispersion DOUBLE PRECISION,
 sharp_bookmaker TEXT,
 sharp_p1 DOUBLE PRECISION,
 sharp_p0 DOUBLE PRECISION,
 sharp_p2 DOUBLE PRECISION,
 best_price_1 DOUBLE PRECISION,
 best_price_0 DOUBLE PRECISION,
 best_price_2 DOUBLE PRECISION,
 median_price_1 DOUBLE PRECISION,
 median_price_0 DOUBLE PRECISION,
 median_price_2 DOUBLE PRECISION,
 source_built_at TIMESTAMPTZ NOT NULL,
 quality TEXT NOT NULL,
 raw_bookmakers JSONB NOT NULL DEFAULT '[]'::jsonb,
 mapped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(event_id,snapshot_hour)
);
CREATE INDEX IF NOT EXISTS idx_one_x_two_market_refs_latest
 ON one_x_two_market_refs(event_id,mapped_at DESC);
CREATE TABLE IF NOT EXISTS one_x_two_market_ref_runs(
 id BIGSERIAL PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL DEFAULT 'running',
 target_fixtures INTEGER NOT NULL DEFAULT 0,
 matched_fixtures INTEGER NOT NULL DEFAULT 0,
 reference_rows INTEGER NOT NULL DEFAULT 0,
 message TEXT
);
"""


def _norm(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = text.translate(str.maketrans({"ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c"}))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return re.sub(r"\s+", " ", text)


def _team_similarity(a: Any, b: Any) -> float:
    aa, bb = _norm(a), _norm(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    if min(len(aa), len(bb)) >= 4 and (aa in bb or bb in aa):
        return 0.93
    sa, sb = set(aa.split()), set(bb.split())
    return len(sa & sb) / max(1, len(sa | sb))


def outcome_label(name: Any, home: Any, away: Any) -> Optional[str]:
    """Normalize provider outcome labels to 1/0/2 without guessing ties."""
    n = _norm(name)
    if n in {"draw", "tie", "x", "0", "beraberlik"} or "draw" in n:
        return "0"
    if n in {"1", "home", "home win", "home team"}:
        return "1"
    if n in {"2", "away", "away win", "away team"}:
        return "2"
    hs, aws = _team_similarity(name, home), _team_similarity(name, away)
    if hs >= 0.72 and hs - aws >= 0.10:
        return "1"
    if aws >= 0.72 and aws - hs >= 0.10:
        return "2"
    return None


def no_vig_three(price_1: float, price_0: float, price_2: float) -> Optional[Dict[str, float]]:
    try:
        prices = {"1": float(price_1), "0": float(price_0), "2": float(price_2)}
    except (TypeError, ValueError):
        return None
    if any(not PRICE_MIN <= p <= PRICE_MAX for p in prices.values()):
        return None
    raw = {k: 1.0 / p for k, p in prices.items()}
    overround = sum(raw.values())
    if not MIN_OVERROUND <= overround <= MAX_OVERROUND:
        return None
    probs = {k: raw[k] / overround for k in ("1", "0", "2")}
    return {"p1": probs["1"], "p0": probs["0"], "p2": probs["2"], "overround": overround}


def _reference_quality(valid: list[Dict[str, Any]]) -> Tuple[bool, str, float, Optional[Dict[str, Any]]]:
    if not valid:
        return False, "no_valid_three_way_books", 0.0, None
    dispersion = max(
        statistics.pstdev([float(x[key]) for x in valid]) if len(valid) > 1 else 0.0
        for key in ("p1", "p0", "p2")
    )
    if dispersion > MAX_DISPERSION:
        return False, "dispersion_high", dispersion, None
    sharp = None
    for wanted in SHARP_PRIORITY:
        sharp = next((x for x in valid if x["bookmaker"] == wanted), None)
        if sharp:
            break
    if len(valid) >= 2:
        return True, "multi_book_three_way_consensus", dispersion, sharp
    if sharp:
        return True, "sharp_single_book_three_way", dispersion, sharp
    return False, "insufficient_books", dispersion, sharp


def _consensus(valid: list[Dict[str, Any]]) -> Dict[str, float]:
    med = {key: statistics.median(float(x[key]) for x in valid) for key in ("p1", "p0", "p2")}
    total = sum(med.values())
    return {key: med[key] / total for key in med}


def build_refs(database_url: str = DATABASE_URL, *, start: datetime, end: datetime) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(SCHEMA)
        run_id = int(conn.execute("INSERT INTO one_x_two_market_ref_runs(status) VALUES('running') RETURNING id").fetchone()[0])
        try:
            targets = [
                {"event_id": str(r[0]), "match_date": r[1], "home_team": str(r[2]), "away_team": str(r[3])}
                for r in conn.execute(
                    """SELECT event_id,match_date,home_team,away_team FROM espn_upcoming
                       WHERE is_current=TRUE AND match_date>=%s AND match_date<%s""",
                    (start, end),
                ).fetchall()
            ]
            candidates = [
                {"fixture_id": str(r[0]), "snapshot_hour": r[1], "start_time": r[2], "home_team": r[3], "away_team": r[4]}
                for r in conn.execute(
                    """SELECT DISTINCT ON (fixture_id) fixture_id,snapshot_hour,start_time,home_team,away_team
                       FROM oddspapi_fixture_snapshots
                       WHERE fetched_at>=NOW()-(%s||' hours')::interval
                       ORDER BY fixture_id,snapshot_hour DESC""",
                    (MAX_AGE_HOURS,),
                ).fetchall()
            ]

            matched = refs = ambiguous = 0
            rejection_counts: Dict[str, int] = defaultdict(int)
            for target in targets:
                intl, is_ambiguous = _match_fixture(target, candidates)
                ambiguous += int(is_ambiguous)
                if not intl:
                    continue
                matched += 1
                fixture_id = str(intl["fixture_id"])
                snapshot_hour = intl["snapshot_hour"]
                rows = conn.execute(
                    """SELECT bookmaker,market_id,market_name,handicap,outcome_name,price,fetched_at
                       FROM oddspapi_market_prices
                       WHERE fixture_id=%s AND snapshot_hour=%s
                         AND price IS NOT NULL AND price>1.001 AND COALESCE(active,TRUE)=TRUE""",
                    (fixture_id, snapshot_hour),
                ).fetchall()
                grouped: Dict[tuple[str, int], Dict[str, Any]] = defaultdict(lambda: {"prices": {}, "fetched_at": None})
                for book, market_id, market_name, handicap, outcome_name, price, fetched_at in rows:
                    if market_kind(market_name, handicap) != "match_result":
                        continue
                    label = outcome_label(outcome_name, intl.get("home_team"), intl.get("away_team"))
                    if label is None:
                        continue
                    key = (str(book).lower(), int(market_id))
                    grouped[key]["prices"][label] = float(price)
                    prev = grouped[key]["fetched_at"]
                    if prev is None or (fetched_at is not None and fetched_at > prev):
                        grouped[key]["fetched_at"] = fetched_at

                per_book: Dict[str, Dict[str, Any]] = {}
                for (book, market_id), payload in grouped.items():
                    prices = payload["prices"]
                    if set(prices) != {"1", "0", "2"}:
                        continue
                    fair = no_vig_three(prices["1"], prices["0"], prices["2"])
                    if fair is None:
                        rejection_counts["invalid_three_way"] += 1
                        continue
                    candidate = {
                        "bookmaker": book,
                        "market_id": market_id,
                        "price_1": prices["1"], "price_0": prices["0"], "price_2": prices["2"],
                        **fair,
                        "source_built_at": payload["fetched_at"] or snapshot_hour,
                    }
                    current = per_book.get(book)
                    if current is None or abs(float(candidate["overround"]) - 1.0) < abs(float(current["overround"]) - 1.0):
                        per_book[book] = candidate

                valid = list(per_book.values())
                ok, quality, dispersion, sharp = _reference_quality(valid)
                if not ok:
                    rejection_counts[quality] += 1
                    continue
                consensus = _consensus(valid)
                if sharp and max(abs(float(sharp[k]) - consensus[k]) for k in ("p1", "p0", "p2")) > MAX_SHARP_DELTA:
                    rejection_counts["sharp_consensus_conflict"] += 1
                    continue
                source_built_at = max(x["source_built_at"] for x in valid)
                prices_1 = [float(x["price_1"]) for x in valid]
                prices_0 = [float(x["price_0"]) for x in valid]
                prices_2 = [float(x["price_2"]) for x in valid]
                conn.execute(
                    """INSERT INTO one_x_two_market_refs(
                         event_id,snapshot_hour,international_fixture_id,bookmaker_count,p1,p0,p2,dispersion,
                         sharp_bookmaker,sharp_p1,sharp_p0,sharp_p2,best_price_1,best_price_0,best_price_2,
                         median_price_1,median_price_0,median_price_2,source_built_at,quality,raw_bookmakers,mapped_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT(event_id,snapshot_hour) DO UPDATE SET
                         international_fixture_id=EXCLUDED.international_fixture_id,
                         bookmaker_count=EXCLUDED.bookmaker_count,p1=EXCLUDED.p1,p0=EXCLUDED.p0,p2=EXCLUDED.p2,
                         dispersion=EXCLUDED.dispersion,sharp_bookmaker=EXCLUDED.sharp_bookmaker,
                         sharp_p1=EXCLUDED.sharp_p1,sharp_p0=EXCLUDED.sharp_p0,sharp_p2=EXCLUDED.sharp_p2,
                         best_price_1=EXCLUDED.best_price_1,best_price_0=EXCLUDED.best_price_0,best_price_2=EXCLUDED.best_price_2,
                         median_price_1=EXCLUDED.median_price_1,median_price_0=EXCLUDED.median_price_0,median_price_2=EXCLUDED.median_price_2,
                         source_built_at=EXCLUDED.source_built_at,quality=EXCLUDED.quality,
                         raw_bookmakers=EXCLUDED.raw_bookmakers,mapped_at=NOW()""",
                    (
                        target["event_id"], snapshot_hour, fixture_id, len(valid),
                        consensus["p1"], consensus["p0"], consensus["p2"], dispersion,
                        sharp["bookmaker"] if sharp else None,
                        sharp["p1"] if sharp else None, sharp["p0"] if sharp else None, sharp["p2"] if sharp else None,
                        max(prices_1), max(prices_0), max(prices_2),
                        statistics.median(prices_1), statistics.median(prices_0), statistics.median(prices_2),
                        source_built_at, quality, Jsonb(valid),
                    ),
                )
                refs += 1

            result = {
                "status": "success", "target_fixtures": len(targets), "matched_fixtures": matched,
                "ambiguous_fixtures": ambiguous, "reference_rows": refs,
                "rejection_counts": dict(rejection_counts), "max_age_hours": MAX_AGE_HOURS,
            }
            conn.execute(
                """UPDATE one_x_two_market_ref_runs SET finished_at=NOW(),status='success',target_fixtures=%s,
                   matched_fixtures=%s,reference_rows=%s,message=%s WHERE id=%s""",
                (len(targets), matched, refs, json.dumps(result, default=str, separators=(",", ":")), run_id),
            )
            print("ONE_X_TWO_MARKET_REF_RESULT", json.dumps(result, default=str, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            conn.execute(
                "UPDATE one_x_two_market_ref_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                (str(exc)[:1600], run_id),
            )
            raise


def latest_ref(conn, event_id: str) -> Optional[Dict[str, Any]]:
    try:
        row = conn.execute(
            """SELECT international_fixture_id,bookmaker_count,p1,p0,p2,dispersion,
                      sharp_bookmaker,sharp_p1,sharp_p0,sharp_p2,
                      best_price_1,best_price_0,best_price_2,
                      median_price_1,median_price_0,median_price_2,
                      source_built_at,quality,mapped_at
               FROM one_x_two_market_refs
               WHERE event_id=%s AND source_built_at>=NOW()-(%s||' hours')::interval
               ORDER BY mapped_at DESC LIMIT 1""",
            (str(event_id), MAX_AGE_HOURS),
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return {
        "international_fixture_id": str(row[0]), "bookmaker_count": int(row[1]),
        "p1": float(row[2]), "p0": float(row[3]), "p2": float(row[4]),
        "dispersion": float(row[5]) if row[5] is not None else None,
        "sharp_bookmaker": row[6],
        "sharp_p1": float(row[7]) if row[7] is not None else None,
        "sharp_p0": float(row[8]) if row[8] is not None else None,
        "sharp_p2": float(row[9]) if row[9] is not None else None,
        "best_price_1": float(row[10]) if row[10] is not None else None,
        "best_price_0": float(row[11]) if row[11] is not None else None,
        "best_price_2": float(row[12]) if row[12] is not None else None,
        "median_price_1": float(row[13]) if row[13] is not None else None,
        "median_price_0": float(row[14]) if row[14] is not None else None,
        "median_price_2": float(row[15]) if row[15] is not None else None,
        "source_built_at": row[16], "quality": str(row[17]), "mapped_at": row[18],
    }


def selected_probability(ref: Optional[Dict[str, Any]], selection: str) -> Optional[float]:
    if not ref or selection not in {"1", "0", "2"}:
        return None
    value = ref.get({"1": "p1", "0": "p0", "2": "p2"}[selection])
    return float(value) if value is not None else None


if __name__ == "__main__":
    raise SystemExit("Use build_refs(database_url, start=..., end=...) from the Thursday workflow")
