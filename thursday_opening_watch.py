#!/usr/bin/env python3
"""Thursday watcher for the strict, variable-length playable shortlist.

A weekly final produced by an older policy is automatically superseded once this
strict policy is deployed. The user-facing list may contain 0..10 selections and
is never force-filled. Every published row must already have passed the strict
playable policy in weekly_trusted_predictions_v2.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from thursday_decision_engine_v3 import build_decision
from thursday_decision_engine import json_default, weekend_bounds
from turkey_iddaa_odds_collector import run_import
from strict_selection_policy_v3 import POLICY_VERSION

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ISTANBUL = ZoneInfo("Europe/Istanbul")
FORCE = os.getenv("OPENING_WATCH_FORCE", "false").lower() in {"1", "true", "yes"}
EARLIEST_THURSDAY_HOUR = int(os.getenv("THURSDAY_EARLIEST_FINALIZE_HOUR", "9"))
FRIDAY_CUTOFF_HOUR = int(os.getenv("FRIDAY_OPENING_WATCH_CUTOFF_HOUR", "12"))
CURRENT_SOURCE = f"{POLICY_VERSION}+iddaa_official+international_no_vig"

DDL = """
CREATE TABLE IF NOT EXISTS thursday_final_decisions(
 week_key DATE PRIMARY KEY,
 finalized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 decision_run_id BIGINT NOT NULL,
 payload JSONB NOT NULL,
 source TEXT NOT NULL DEFAULT 'strict-playable-v3'
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


def _pick_key(item: Dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("event_id") or ""),
        str(item.get("market") or ""),
        str(item.get("selection") or ""),
    )


def _decorate_verified_picks(
    picks: List[Dict[str, Any]], value_list: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Attach rank/value metadata without changing strict membership."""
    values = {_pick_key(v): v for v in value_list}
    decorated: List[Dict[str, Any]] = []
    for rank, original in enumerate(picks, start=1):
        item = dict(original)
        value = values.get(_pick_key(original))
        item["rank"] = rank
        item["list_tier"] = "verified_playable"
        item["is_value"] = bool(value) or item.get("qualification") == "strict_playable"
        if value:
            item["model_ev_vs_tr"] = value.get("model_ev_vs_tr", item.get("model_ev_vs_tr"))
            item["model_edge_vs_tr"] = value.get("model_edge_vs_tr", item.get("model_edge_vs_tr"))
        item["value_semantics"] = "strict_playable_requires_positive_model_ev_at_turkey_price"
        decorated.append(item)
    return decorated


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
    superseded_source: Optional[str] = None
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(DDL)
        existing = conn.execute(
            "SELECT finalized_at,decision_run_id,payload,source FROM thursday_final_decisions WHERE week_key=%s",
            (week_key,),
        ).fetchone()
        if existing:
            existing_source = str(existing[3] or "")
            if existing_source == CURRENT_SOURCE:
                result = {
                    "status": "already_finalized",
                    "week_key": week_key,
                    "finalized_at": existing[0],
                    "decision_run_id": int(existing[1]),
                    "payload": existing[2],
                    "source": existing_source,
                }
                print("THURSDAY_FINAL_DECISION", json.dumps(result, ensure_ascii=False, default=json_default, separators=(",", ":")), flush=True)
                return result
            # A legacy weekly final must never block a stricter deployed policy.
            superseded_source = existing_source
            conn.execute("DELETE FROM thursday_final_decisions WHERE week_key=%s", (week_key,))
            print(
                "THURSDAY_LEGACY_FINAL_SUPERSEDED",
                json.dumps({"week_key": week_key, "old_source": existing_source, "new_source": CURRENT_SOURCE}, default=json_default, ensure_ascii=False),
                flush=True,
            )

    try:
        turkey = run_import(database_url)
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
        international_1x2: Dict[str, Any]
        if turkey_ready:
            try:
                from international_market_reference import refresh_and_map
                international = refresh_and_map(database_url, start=start, end=end)
            except Exception as exc:
                international = {"status": "failed", "error": str(exc)[:1200]}

            try:
                from one_x_two_market_reference import build_refs as build_1x2_refs
                international_1x2 = build_1x2_refs(database_url, start=start, end=end)
            except Exception as exc:
                international_1x2 = {"status": "failed_optional", "error": str(exc)[:1200]}
        else:
            international = {"status": "waiting_for_turkey_prices"}
            international_1x2 = {"status": "waiting_for_turkey_prices"}

        decision = build_decision(database_url, now=now)

        from weekly_trusted_predictions_v2 import build as build_weekly_trusted
        trusted = build_weekly_trusted(database_url, now=now)

        value_list = decision.get("high_confidence_value") or [] if decision.get("decision_ready") else []
        verified_picks = _decorate_verified_picks(trusted.get("picks") or [], value_list)
        strict_70 = [p for p in verified_picks if p.get("strict_high_confidence")]

        status = "ready_to_finalize" if trusted.get("ready") else "pending_bulletin"
        if turkey_ready and not trusted.get("ready"):
            status = "pending_reliable_universe"

        result: Dict[str, Any] = {
            "status": status,
            "week_key": week_key,
            "window": window,
            "superseded_source": superseded_source,
            "turkey": turkey,
            "turkey_two_sided": turkey_two_sided,
            "international": international,
            "international_1x2": international_1x2,
            "decision_run_id": decision.get("decision_run_id"),
            "decision_engine": POLICY_VERSION,
            "official_fixture_coverage": trusted.get("official_fixture_coverage"),
            "verified_playable": verified_picks,
            "weekly_reliable": verified_picks,
            "strict_high_confidence": strict_70,
            "strict_high_confidence_count": len(strict_70),
            "high_confidence_value": value_list,
            "value_decision_ready": bool(decision.get("decision_ready")),
            "excluded_counts": trusted.get("excluded_counts") or {},
        }

        if trusted.get("ready"):
            payload = {
                "week_key": week_key,
                "finalized_at": datetime.now(timezone.utc),
                "decision_run_id": decision.get("decision_run_id") or 0,
                "decision_engine": POLICY_VERSION,
                "official_fixture_coverage": trusted.get("official_fixture_coverage"),
                "verified_playable": verified_picks,
                "weekly_reliable": verified_picks,
                # Compatibility only; no longer semantically means >=70%.
                "high_confidence": verified_picks,
                "strict_high_confidence": strict_70,
                "high_confidence_value": value_list,
                "policy": {
                    "primary_list": trusted.get("policy") or {},
                    "selection_semantics": "strict_playable_variable_0_to_10",
                    "force_fill_top10": False,
                    "ranking_priority": "only_after_strict_playable_qualification",
                    "turkey_price_required": True,
                    "international_reference_required": True,
                    "positive_model_ev_required": True,
                    "legacy_core4_disabled": True,
                    "no_bet_week_allowed": True,
                },
                "sources": {
                    "model": "validated_v1_plus_guarded_1x2_when_strictly_qualified",
                    "international_primary": "mandatory no-vig probability sanity reference",
                    "executable_price": "iddaa_official_turkey",
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
                        int(decision.get("decision_run_id") or 0),
                        Jsonb(payload, dumps=lambda x: json.dumps(x, default=json_default, ensure_ascii=False)),
                        CURRENT_SOURCE,
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
                "superseded_source": superseded_source,
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
