#!/usr/bin/env python3
"""Thursday finalizer for the evidence-complete six-layer V4 engine."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from confidence_core_v4 import POLICY_VERSION, build as build_core
from strict_selection_policy_v3 import POLICY_VERSION as STRICT_POLICY_VERSION
from thursday_decision_engine import json_default, weekend_bounds
from turkey_iddaa_odds_collector import run_import

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ISTANBUL = ZoneInfo("Europe/Istanbul")
FORCE = os.getenv("OPENING_WATCH_FORCE", "false").lower() in {"1", "true", "yes"}
EARLIEST_THURSDAY_HOUR = int(os.getenv("THURSDAY_EARLIEST_FINALIZE_HOUR", "9"))
FRIDAY_CUTOFF_HOUR = int(os.getenv("FRIDAY_OPENING_WATCH_CUTOFF_HOUR", "12"))
CONSENSUS_BOOK_REQUESTS = max(3, min(8, int(os.getenv("CONFIDENCE_CONSENSUS_BOOK_REQUESTS", "6"))))
CURRENT_SOURCE = f"{POLICY_VERSION}+iddaa_official+multibook+opponent_xg+dixon_coles"

DDL = """
CREATE TABLE IF NOT EXISTS thursday_final_decisions(
 week_key DATE PRIMARY KEY,
 finalized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 decision_run_id BIGINT NOT NULL,
 payload JSONB NOT NULL,
 source TEXT NOT NULL DEFAULT 'confidence-core-v4'
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


def _allowed_now(now: Optional[datetime] = None):
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
    payload = dict(row[2] or {})

    # Keep three products distinct:
    #   core4        -> the four user-facing diversified reliability selections
    #   ranked_picks -> the underlying reliability ranking (normally top 10)
    #   value_picks  -> only selections whose Turkey price passes the value rule
    core4 = payload.get("confidence_core4") or payload.get("core4") or []
    ranked = payload.get("confidence_ranked") or payload.get("ranked_picks") or payload.get("weekly_reliable") or core4
    value = payload.get("value_picks") or payload.get("high_confidence_value") or []

    payload["confidence_core4"] = core4
    payload["core4"] = core4
    payload["confidence_ranked"] = ranked
    payload["ranked_picks"] = ranked
    payload["weekly_reliable"] = ranked
    payload["value_picks"] = value

    # Legacy aliases: high_confidence must mean the actual Core4, not the first
    # four rows of the ranked list. This prevents the UI from bypassing the
    # diversification layer.
    payload["high_confidence"] = core4
    payload["high_confidence_value"] = value
    return {
        "week_key": week_key,
        "finalized_at": row[0],
        "decision_run_id": int(row[1]),
        "payload": payload,
        "source": row[3],
    }


def _record(database_url: str, week_key: date, check_hour: datetime, result: Dict[str, Any]) -> None:
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
        print("THURSDAY_OPENING_SKIP", json.dumps(result, default=json_default, ensure_ascii=False), flush=True)
        return result

    check_hour = local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    superseded = None
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(DDL)
        existing = conn.execute(
            "SELECT finalized_at,decision_run_id,payload,source FROM thursday_final_decisions WHERE week_key=%s",
            (week_key,),
        ).fetchone()
        if existing:
            source = str(existing[3] or "")
            if source == CURRENT_SOURCE:
                found = latest_final(database_url, week_key)
                return (found or {}) | {"status": "already_finalized"}
            superseded = source
            conn.execute("DELETE FROM thursday_final_decisions WHERE week_key=%s", (week_key,))
            print(
                "THURSDAY_LEGACY_FINAL_SUPERSEDED",
                json.dumps({"week_key": week_key, "old_source": source, "new_source": CURRENT_SOURCE}, default=json_default),
                flush=True,
            )

    try:
        turkey = run_import(database_url)
        try:
            from turkey_two_sided_odds import run_import as run_two_sided
            turkey_two = run_two_sided(database_url)
        except Exception as exc:
            turkey_two = {"status": "failed_optional", "error": str(exc)[:800]}

        try:
            from understat_xg_importer import run_import as run_understat
            understat = run_understat(database_url)
        except Exception as exc:
            understat = {"status": "failed_optional", "error": str(exc)[:800]}

        try:
            import oddspapi_allbooks_importer_v3 as odds_multi
            odds_multi.MAX_BOOKS = max(int(getattr(odds_multi, "MAX_BOOKS", 3)), CONSENSUS_BOOK_REQUESTS)
        except Exception:
            pass

        try:
            from international_market_reference import refresh_and_map
            international = refresh_and_map(database_url, start=start, end=end, force=True)
        except Exception as exc:
            international = {"status": "failed", "error": str(exc)[:800]}

        try:
            from one_x_two_market_reference_v2 import build_refs
            international_1x2 = build_refs(database_url, start=start, end=end)
        except Exception as exc:
            international_1x2 = {"status": "failed_optional", "error": str(exc)[:800]}

        try:
            from weekly_trusted_predictions_v2 import build as build_strict
            strict = build_strict(database_url, now=now)
            verified = strict.get("picks") or []
        except Exception as exc:
            strict = {"status": "failed_optional", "error": str(exc)[:800], "picks": []}
            verified = []

        core = build_core(database_url, now=now, strict_picks=verified)
        trust_core = core.get("confidence_core4") or []
        trust_ranked = core.get("confidence_ranked") or []
        values = core.get("value_picks") or []
        fallbacks = core.get("fallback_candidates") or []
        status = "ready_to_finalize" if core.get("ready") else "pending_confidence_core4"
        result: Dict[str, Any] = {
            "status": status,
            "week_key": week_key,
            "window": window,
            "superseded_source": superseded,
            "turkey": turkey,
            "turkey_two_sided": turkey_two,
            "understat": understat,
            "international": international,
            "international_1x2": international_1x2,
            "confidence_core4": trust_core,
            "confidence_ranked": trust_ranked,
            "value_picks": values,
            "fallback_candidates": fallbacks,
            "official_fixture_coverage": core.get("official_fixture_coverage"),
            "diagnostics": {
                "candidate_market_rows": core.get("candidate_market_rows"),
                "trust_fixture_candidates": core.get("trust_fixture_candidates"),
                "value_fixture_candidates": core.get("value_fixture_candidates"),
                "xg_active_fixtures": core.get("xg_active_fixtures"),
                "multi_book_binary_rows": core.get("multi_book_binary_rows"),
                "multi_book_1x2_rows": core.get("multi_book_1x2_rows"),
                "excluded_counts": core.get("excluded_counts") or {},
            },
            "policy": core.get("policy") or {},
        }

        if core.get("ready"):
            run_id = int(datetime.now(timezone.utc).timestamp())
            payload = {
                "week_key": week_key,
                "finalized_at": datetime.now(timezone.utc),
                "decision_run_id": run_id,
                "decision_engine": POLICY_VERSION,
                "official_fixture_coverage": core.get("official_fixture_coverage"),
                "confidence_core4": trust_core,
                "core4": trust_core,
                "confidence_ranked": trust_ranked,
                "ranked_picks": trust_ranked,
                "weekly_reliable": trust_ranked,
                "high_confidence": trust_core,
                "value_picks": values,
                "high_confidence_value": values,
                "fallback_candidates": fallbacks,
                "verified_playable": verified,
                "strict_high_confidence": [],
                "policy": core.get("policy") or {},
                "diagnostics": result["diagnostics"],
                "sources": {
                    "model": f"{POLICY_VERSION}: multi-model opponent-adjusted reliability stack",
                    "international": "fresh multi-book same-market no-vig consensus when available",
                    "executable_price": "iddaa_official_turkey",
                    "strict_verification": STRICT_POLICY_VERSION,
                },
            }
            with psycopg.connect(database_url, autocommit=True) as conn:
                conn.execute(DDL)
                conn.execute(
                    """INSERT INTO thursday_final_decisions(week_key,decision_run_id,payload,source)
                       VALUES(%s,%s,%s,%s)
                       ON CONFLICT(week_key) DO UPDATE SET
                         finalized_at=NOW(),decision_run_id=EXCLUDED.decision_run_id,
                         payload=EXCLUDED.payload,source=EXCLUDED.source""",
                    (
                        week_key,
                        run_id,
                        Jsonb(payload, dumps=lambda x: json.dumps(x, default=json_default, ensure_ascii=False)),
                        CURRENT_SOURCE,
                    ),
                )
            result = latest_final(database_url, week_key) or result
            result["status"] = "finalized"
            result["superseded_source"] = superseded

        _record(database_url, week_key, check_hour, result)
        marker = "THURSDAY_FINAL_DECISION_V4" if result["status"] in {"finalized", "already_finalized"} else "THURSDAY_OPENING_PENDING_V4"
        print(marker, json.dumps(result, default=json_default, ensure_ascii=False, separators=(",", ":")), flush=True)
        return result
    except Exception as exc:
        result = {"status": "failed", "week_key": week_key, "error": str(exc)[:1200]}
        _record(database_url, week_key, check_hour, result)
        raise


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2, default=json_default))