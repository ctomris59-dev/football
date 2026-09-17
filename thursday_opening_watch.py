#!/usr/bin/env python3
"""Thursday watcher for the practical Core4 decision workflow.

The primary weekly product is now a risk-adjusted Core4 (plus optional ranked
candidates). The conservative strict-playable-v3 list remains available as an
independent verification layer/rosette and is allowed to be empty.
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
from strict_selection_policy_v3 import POLICY_VERSION as STRICT_POLICY_VERSION
from weekly_core4_decision import POLICY_VERSION as CORE_POLICY_VERSION

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ISTANBUL = ZoneInfo("Europe/Istanbul")
FORCE = os.getenv("OPENING_WATCH_FORCE", "false").lower() in {"1", "true", "yes"}
EARLIEST_THURSDAY_HOUR = int(os.getenv("THURSDAY_EARLIEST_FINALIZE_HOUR", "9"))
FRIDAY_CUTOFF_HOUR = int(os.getenv("FRIDAY_OPENING_WATCH_CUTOFF_HOUR", "12"))
CURRENT_SOURCE = f"{CORE_POLICY_VERSION}+iddaa_official+risk_adjusted"

DDL = """
CREATE TABLE IF NOT EXISTS thursday_final_decisions(
 week_key DATE PRIMARY KEY,
 finalized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 decision_run_id BIGINT NOT NULL,
 payload JSONB NOT NULL,
 source TEXT NOT NULL DEFAULT 'core4-decision-v1'
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
    values = {_pick_key(v): v for v in value_list}
    decorated: List[Dict[str, Any]] = []
    for rank, original in enumerate(picks, start=1):
        item = dict(original)
        value = values.get(_pick_key(original))
        item["verified_rank"] = rank
        item["strict_verified"] = True
        item["verification_tier"] = "strict_playable"
        if value:
            item["model_ev_vs_tr"] = value.get("model_ev_vs_tr", item.get("model_ev_vs_tr"))
            item["model_edge_vs_tr"] = value.get("model_edge_vs_tr", item.get("model_edge_vs_tr"))
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

        # Keep the old decision engine for audit diagnostics/value bookkeeping.
        decision = build_decision(database_url, now=now)

        # Strict verification layer: conservative and allowed to be empty.
        from weekly_trusted_predictions_v2 import build as build_strict
        strict = build_strict(database_url, now=now)
        value_list = decision.get("high_confidence_value") or [] if decision.get("decision_ready") else []
        verified_picks = _decorate_verified_picks(strict.get("picks") or [], value_list)
        strict_70 = [p for p in verified_picks if p.get("strict_high_confidence")]

        # Primary practical decision product: Core4 + optional ranked candidates.
        from weekly_core4_decision import build as build_core4
        core = build_core4(database_url, now=now, strict_picks=verified_picks)
        core4 = core.get("core4") or []
        ranked_picks = core.get("ranked_picks") or []

        status = "ready_to_finalize" if core.get("ready") else "pending_core4"
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
            "decision_engine": CORE_POLICY_VERSION,
            "official_fixture_coverage": core.get("official_fixture_coverage"),
            "core4": core4,
            "ranked_picks": ranked_picks,
            "weekly_reliable": ranked_picks,
            "verified_playable": verified_picks,
            "strict_high_confidence": strict_70,
            "strict_high_confidence_count": len(strict_70),
            "high_confidence_value": value_list,
            "core_diagnostics": {
                "candidate_market_rows": core.get("candidate_market_rows"),
                "candidate_fixture_rows": core.get("candidate_fixture_rows"),
                "market_candidate_counts": core.get("market_candidate_counts") or {},
                "excluded_counts": core.get("excluded_counts") or {},
            },
            "strict_diagnostics": {
                "policy_version": STRICT_POLICY_VERSION,
                "verified_count": len(verified_picks),
                "excluded_counts": strict.get("excluded_counts") or {},
            },
        }

        if core.get("ready"):
            payload = {
                "week_key": week_key,
                "finalized_at": datetime.now(timezone.utc),
                "decision_run_id": decision.get("decision_run_id") or 0,
                "decision_engine": CORE_POLICY_VERSION,
                "official_fixture_coverage": core.get("official_fixture_coverage"),
                "core4": core4,
                "ranked_picks": ranked_picks,
                "weekly_reliable": ranked_picks,
                "verified_playable": verified_picks,
                # Backward compatibility: high_confidence is strict-only now.
                "high_confidence": strict_70,
                "strict_high_confidence": strict_70,
                "high_confidence_value": value_list,
                "policy": {
                    "primary_list": core.get("policy") or {},
                    "selection_semantics": "core4_relative_weekly_rank_plus_separate_strict_verification",
                    "core4_required": True,
                    "core4_size": 4,
                    "core4_is_not_70pct_claim": True,
                    "strict_verification_policy": strict.get("policy") or {},
                    "strict_verification_can_be_empty": True,
                    "turkey_price_required_for_core": True,
                    "strong_market_contradiction_rejected": True,
                    "missing_context_penalized_not_automatically_rejected": True,
                    "current_season_result_tuning": False,
                },
                "diagnostics": {
                    "core": {
                        "candidate_market_rows": core.get("candidate_market_rows"),
                        "candidate_fixture_rows": core.get("candidate_fixture_rows"),
                        "market_candidate_counts": core.get("market_candidate_counts") or {},
                        "excluded_counts": core.get("excluded_counts") or {},
                    },
                    "strict": {
                        "policy_version": STRICT_POLICY_VERSION,
                        "verified_count": len(verified_picks),
                        "excluded_counts": strict.get("excluded_counts") or {},
                    },
                },
                "sources": {
                    "model": "frozen_v1 risk-adjusted weekly ranking",
                    "strict_verification": STRICT_POLICY_VERSION,
                    "international_primary": "no-vig sanity/contradiction reference where available",
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
