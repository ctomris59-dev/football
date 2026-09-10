#!/usr/bin/env python3
"""Thursday watcher: mandatory weekly reliability list + optional value list.

The first list is always the best available V1-ranked, Turkey-playable forecast set
once the Friday-Monday bulletin is sufficiently complete. It is not artificially
labelled 70%+: each item carries its real V1 selected-side estimate and only >=70%
items receive the strict High Confidence badge.

The second list remains strict model + international no-vig + Turkey value. It may
legitimately be empty. Later T-1/T-3 information does not rewrite a frozen week.
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
 source TEXT NOT NULL DEFAULT 'v1_reliability+optional_value+iddaa_official'
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
        "week_key": week_key,
        "finalized_at": row[0],
        "decision_run_id": int(row[1]),
        "payload": row[2],
        "source": row[3],
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
                week_key,
                check_hour,
                result["status"],
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
                "status": "already_finalized",
                "week_key": week_key,
                "finalized_at": existing[0],
                "decision_run_id": int(existing[1]),
                "payload": existing[2],
                "source": existing[3],
            }
            print("THURSDAY_FINAL_DECISION", json.dumps(result, ensure_ascii=False, default=json_default, separators=(",", ":")), flush=True)
            return result

    try:
        turkey = run_import(database_url)
        # Expand the same official bulletin to both sides of each target market. This
        # does not alter value logic; it only lets the weekly V1 list choose its
        # stronger predicted side when that side is actually executable in Turkey.
        try:
            from turkey_two_sided_odds import run_import as run_two_sided
            turkey_two_sided = run_two_sided(database_url)
        except Exception as exc:
            turkey_two_sided = {"status": "failed_optional", "error": str(exc)[:1200]}

        turkey_ready = bool(
            int(turkey.get("matched_fixtures") or 0) > 0
            and int(turkey.get("stored_prices") or 0) > 0
        )

        international: Dict[str, Any]
        if turkey_ready:
            try:
                from international_market_reference import refresh_and_map
                international = refresh_and_map(database_url, start=start, end=end)
            except Exception as exc:
                international = {"status": "failed", "error": str(exc)[:1200]}
        else:
            international = {"status": "waiting_for_turkey_prices"}

        # Strict value engine remains untouched and is now explicitly secondary.
        decision = build_decision(database_url, now=now)

        from weekly_trusted_predictions import build as build_weekly_trusted
        trusted = build_weekly_trusted(database_url, now=now)

        value_list = (
            decision.get("high_confidence_value") or []
            if decision.get("decision_ready")
            else []
        )
        status = "ready_to_finalize" if trusted.get("ready") else "pending_bulletin"
        if turkey_ready and not trusted.get("ready"):
            status = "pending_reliable_universe"

        result: Dict[str, Any] = {
            "status": status,
            "week_key": week_key,
            "window": window,
            "turkey": turkey,
            "turkey_two_sided": turkey_two_sided,
            "international": international,
            "decision_run_id": decision.get("decision_run_id"),
            "decision_engine": decision.get("decision_engine"),
            "official_fixture_coverage": trusted.get("official_fixture_coverage"),
            "weekly_reliable": trusted.get("picks") or [],
            "strict_high_confidence": trusted.get("strict_high_confidence") or [],
            "strict_high_confidence_count": trusted.get("strict_high_confidence_count"),
            "high_confidence_value": value_list,
            "value_decision_ready": bool(decision.get("decision_ready")),
            "value_candidate_preview": decision.get("candidate_preview") or [],
        }

        if trusted.get("ready"):
            payload = {
                "week_key": week_key,
                "finalized_at": datetime.now(timezone.utc),
                "decision_run_id": decision.get("decision_run_id") or 0,
                "decision_engine": "weekly_v1_reliability_plus_optional_value_v1",
                "official_fixture_coverage": trusted.get("official_fixture_coverage"),
                # Backward-compatible key: the pinned page/API already reads this.
                # Semantics are now explicit in each item's confidence_tier.
                "high_confidence": trusted.get("picks") or [],
                "weekly_reliable": trusted.get("picks") or [],
                "strict_high_confidence": trusted.get("strict_high_confidence") or [],
                "high_confidence_value": value_list,
                "policy": {
                    "primary_list": trusted.get("policy") or {},
                    "value_list": (decision.get("diagnostics") or {}).get("policy") or {},
                    "value_optional": True,
                },
                "sources": {
                    "model": "validated_v1",
                    "international_primary": "safety_check_when_available; strong contradiction rejected",
                    "international_value": "mandatory paired_same-book_no-vig_consensus",
                    "executable_price": "iddaa_official_turkey",
                },
            }
            with psycopg.connect(database_url, autocommit=True) as conn:
                conn.execute(DDL)
                conn.execute(
                    """INSERT INTO thursday_final_decisions(week_key,decision_run_id,payload,source)
                       VALUES(%s,%s,%s,'v1_reliability+optional_value+iddaa_official')
                       ON CONFLICT(week_key) DO NOTHING""",
                    (
                        week_key,
                        int(decision.get("decision_run_id") or 0),
                        Jsonb(payload, dumps=lambda x: json.dumps(x, default=json_default, ensure_ascii=False)),
                    ),
                )
                stored = conn.execute(
                    "SELECT finalized_at,decision_run_id,payload,source FROM thursday_final_decisions WHERE week_key=%s",
                    (week_key,),
                ).fetchone()
            result = {
                "status": "finalized",
                "week_key": week_key,
                "finalized_at": stored[0],
                "decision_run_id": int(stored[1]),
                "payload": stored[2],
                "source": stored[3],
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
