#!/usr/bin/env python3
"""Thursday opening watcher for the final two-list betting workflow.

Sequence:
1) check for official Turkish İddaa weekend prices from Thursday morning;
2) once target Turkey prices exist, refresh/map international paired no-vig reference;
3) build two lists;
4) freeze the first sufficiently complete decision for the week.

After freezing, later odds/T-1/T-3 information never rewrites the user's bets.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from thursday_decision_engine_v2 import build_decision
from thursday_decision_engine import json_default, weekend_bounds
from turkey_iddaa_odds_collector import run_import

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ISTANBUL = ZoneInfo("Europe/Istanbul")
FORCE = os.getenv("OPENING_WATCH_FORCE", "false").lower() in {"1", "true", "yes"}
EARLIEST_THURSDAY_HOUR = int(os.getenv("THURSDAY_EARLIEST_FINALIZE_HOUR", "9"))
FRIDAY_CUTOFF_HOUR = int(os.getenv("FRIDAY_OPENING_WATCH_CUTOFF_HOUR", "12"))

DDL = """
CREATE TABLE IF NOT EXISTS thursday_final_decisions(
 week_key DATE PRIMARY KEY,
 finalized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 decision_run_id BIGINT NOT NULL,
 payload JSONB NOT NULL,
 source TEXT NOT NULL DEFAULT 'model+international_no_vig+iddaa_official'
);
CREATE TABLE IF NOT EXISTS thursday_watch_checks(
 week_key DATE NOT NULL,
 check_hour TIMESTAMPTZ NOT NULL,
 checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 status TEXT NOT NULL,
 payload JSONB NOT NULL DEFAULT '{}'::jsonb,
 PRIMARY KEY(week_key,check_hour)
);
"""


def _allowed_now(now: Optional[datetime] = None) -> tuple[bool, datetime, str]:
    local = (now or datetime.now(timezone.utc)).astimezone(ISTANBUL)
    if FORCE:
        return True, local, "forced"
    if local.weekday() == 3 and local.hour >= EARLIEST_THURSDAY_HOUR:
        return True, local, "thursday_bulletin_window"
    if local.weekday() == 4 and local.hour <= FRIDAY_CUTOFF_HOUR:
        return True, local, "friday_fallback"
    return False, local, "outside_watch_window"


def latest_final(database_url: str = DATABASE_URL, week_key: Optional[date] = None) -> Optional[Dict[str, Any]]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    week_key = week_key or weekend_bounds()[0]
    with psycopg.connect(database_url) as conn:
        conn.execute(DDL)
        row = conn.execute(
            "SELECT finalized_at,decision_run_id,payload,source FROM thursday_final_decisions WHERE week_key=%s",
            (week_key,),
        ).fetchone()
    if not row:
        return None
    return {
        "week_key": week_key, "finalized_at": row[0], "decision_run_id": int(row[1]),
        "payload": row[2], "source": row[3],
    }


def _record_check(database_url: str, week_key: date, check_hour: datetime, result: Dict[str, Any]) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(DDL)
        conn.execute(
            """INSERT INTO thursday_watch_checks(week_key,check_hour,status,payload,checked_at)
               VALUES(%s,%s,%s,%s,NOW())
               ON CONFLICT(week_key,check_hour) DO UPDATE SET
                 status=EXCLUDED.status,payload=EXCLUDED.payload,checked_at=NOW()""",
            (
                week_key, check_hour, result["status"],
                Jsonb(result, dumps=lambda x: json.dumps(x, default=json_default, ensure_ascii=False)),
            ),
        )


def main(database_url: str = DATABASE_URL, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    allowed, local, window = _allowed_now(now)
    week_key, start, end = weekend_bounds(now)
    if not allowed:
        result = {"status": "skipped_window", "week_key": week_key, "local_time": local, "window": window}
        print("THURSDAY_OPENING_SKIP", json.dumps(result, ensure_ascii=False, default=json_default, separators=(",", ":")), flush=True)
        return result

    check_hour = local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(DDL)
        existing = conn.execute(
            "SELECT finalized_at,decision_run_id,payload,source FROM thursday_final_decisions WHERE week_key=%s",
            (week_key,),
        ).fetchone()
        if existing:
            result = {
                "status": "already_finalized", "week_key": week_key, "finalized_at": existing[0],
                "decision_run_id": int(existing[1]), "payload": existing[2], "source": existing[3],
            }
            print("THURSDAY_FINAL_DECISION", json.dumps(result, ensure_ascii=False, default=json_default, separators=(",", ":")), flush=True)
            return result

    try:
        turkey = run_import(database_url)
        turkey_ready_to_compare = bool(
            int(turkey.get("matched_fixtures") or 0) > 0
            and int(turkey.get("stored_prices") or 0) > 0
        )

        international: Dict[str, Any]
        if turkey_ready_to_compare:
            try:
                from international_market_reference import refresh_and_map
                international = refresh_and_map(database_url, start=start, end=end)
            except Exception as exc:
                international = {"status": "failed", "error": str(exc)[:1200]}
        else:
            international = {"status": "waiting_for_turkey_prices"}

        decision = build_decision(database_url, now=now)
        status = "pending_bulletin"
        if turkey_ready_to_compare and international.get("status") == "failed":
            status = "pending_international_reference"
        elif turkey_ready_to_compare and not decision.get("decision_ready"):
            status = "pending_market_coverage"

        result: Dict[str, Any] = {
            "status": status,
            "week_key": week_key,
            "window": window,
            "turkey": turkey,
            "international": international,
            "decision_run_id": decision.get("decision_run_id"),
            "decision_engine": decision.get("decision_engine"),
            "official_fixture_coverage": decision.get("official_fixture_coverage"),
            "candidate_rows": decision.get("candidate_rows"),
            "playable_candidate_rows": decision.get("playable_candidate_rows"),
            "tr_candidate_coverage": decision.get("tr_candidate_coverage"),
            "international_candidate_coverage": decision.get("international_candidate_coverage"),
            "raw_high_candidates": decision.get("raw_high_candidates"),
            "priced_high_candidates": decision.get("priced_high_candidates"),
            "verified_high_candidates": decision.get("verified_high_candidates"),
            "candidate_preview": decision.get("candidate_preview") or [],
            "raw_high_preview": decision.get("raw_high_preview") or [],
            "high_confidence": decision.get("high_confidence") or [],
            "high_confidence_value": decision.get("high_confidence_value") or [],
        }

        if decision.get("decision_ready"):
            payload = {
                "week_key": decision["week_key"],
                "finalized_at": datetime.now(timezone.utc),
                "decision_run_id": decision["decision_run_id"],
                "decision_engine": decision.get("decision_engine"),
                "official_fixture_coverage": decision["official_fixture_coverage"],
                "high_confidence": decision["high_confidence"],
                "high_confidence_value": decision["high_confidence_value"],
                "policy": decision["diagnostics"]["policy"],
                "sources": {
                    "model": "validated_v1",
                    "international": "paired_same-book_no-vig_consensus",
                    "executable_price": "iddaa_official_turkey",
                },
            }
            with psycopg.connect(database_url, autocommit=True) as conn:
                conn.execute(DDL)
                conn.execute(
                    """INSERT INTO thursday_final_decisions(week_key,decision_run_id,payload,source)
                       VALUES(%s,%s,%s,'model+international_no_vig+iddaa_official')
                       ON CONFLICT(week_key) DO NOTHING""",
                    (
                        week_key, int(decision["decision_run_id"]),
                        Jsonb(payload, dumps=lambda x: json.dumps(x, default=json_default, ensure_ascii=False)),
                    ),
                )
                stored = conn.execute(
                    "SELECT finalized_at,decision_run_id,payload,source FROM thursday_final_decisions WHERE week_key=%s",
                    (week_key,),
                ).fetchone()
            result = {
                "status": "finalized", "week_key": week_key, "finalized_at": stored[0],
                "decision_run_id": int(stored[1]), "payload": stored[2], "source": stored[3],
            }

        _record_check(database_url, week_key, check_hour, result)
        marker = "THURSDAY_FINAL_DECISION" if result["status"] == "finalized" else "THURSDAY_OPENING_PENDING"
        print(marker, json.dumps(result, ensure_ascii=False, default=json_default, separators=(",", ":")), flush=True)
        return result
    except Exception as exc:
        result = {"status": "failed", "week_key": week_key, "error": str(exc)[:1200]}
        _record_check(database_url, week_key, check_hour, result)
        raise


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2, default=json_default))
