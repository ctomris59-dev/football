#!/usr/bin/env python3
"""Build advanced v3 feature layers without wasting provider quota.

External/static layers are freshness-gated. Pure database transforms are cheap and
may rebuild on every scheduled weekly refresh. Validation flags decide whether
experimental priors are allowed to influence production.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SECOND_TIER_REFRESH_HOURS = float(os.getenv("SECOND_TIER_REFRESH_HOURS", str(24 * 30)))
CLUBELO_REFRESH_HOURS = float(os.getenv("CLUBELO_REFRESH_HOURS", "48"))
FOTMOB_STRENGTH_REFRESH_HOURS = float(os.getenv("FOTMOB_STRENGTH_REFRESH_HOURS", "24"))
SCORE_STATE_REFRESH_HOURS = float(os.getenv("SCORE_STATE_REFRESH_HOURS", str(24 * 30)))
PROMOTION_BACKTEST_REFRESH_HOURS = float(os.getenv("PROMOTION_BACKTEST_REFRESH_HOURS", str(24 * 30)))

ALLOWED_RUN_TABLES = {
    "second_tier_import_runs",
    "clubelo_import_runs",
    "fotmob_strength_runs",
    "score_state_runs",
    "score_state_backtest_runs",
    "promotion_prior_backtest_runs",
}


def _recent(table: str, hours: float) -> bool:
    if table not in ALLOWED_RUN_TABLES or not DATABASE_URL or hours <= 0:
        return False
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute(
                f"SELECT 1 FROM {table} WHERE status='success' AND finished_at>=NOW()-(%s||' hours')::interval LIMIT 1",
                (hours,),
            ).fetchone()
        return bool(row)
    except Exception:
        return False


def _step(name: str, fn: Callable[[], Any], out: Dict[str, Any], optional: bool = True) -> None:
    try:
        out[name] = {"status": "ok", "result": fn()}
    except Exception as exc:
        out[name] = {"status": "failed", "error": str(exc)}
        if not optional:
            raise


def _skip(name: str, out: Dict[str, Any], reason: str) -> None:
    out[name] = {"status": "skipped", "reason": reason}


def run(database_url: str | None = None) -> Dict[str, Any]:
    global DATABASE_URL
    if database_url:
        DATABASE_URL = database_url.strip()
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
    started = datetime.now(timezone.utc)
    steps: Dict[str, Any] = {}

    if _recent("second_tier_import_runs", SECOND_TIER_REFRESH_HOURS):
        _skip("second_tier", steps, f"fresh<{SECOND_TIER_REFRESH_HOURS}h")
    else:
        from second_tier_importer_v2 import run_import
        _step("second_tier", lambda: run_import(DATABASE_URL), steps)

    # Cheap DB transform; always rebuild after second-tier state is known.
    from promotion_prior_builder import build as build_promotion
    _step("promotion_priors", lambda: build_promotion(DATABASE_URL), steps)

    if _recent("clubelo_import_runs", CLUBELO_REFRESH_HOURS):
        _skip("clubelo", steps, f"fresh<{CLUBELO_REFRESH_HOURS}h")
    else:
        from clubelo_importer import run_import
        _step("clubelo", lambda: run_import(DATABASE_URL), steps)

    if _recent("fotmob_strength_runs", FOTMOB_STRENGTH_REFRESH_HOURS):
        _skip("fotmob_strength_style", steps, f"fresh<{FOTMOB_STRENGTH_REFRESH_HOURS}h")
    else:
        from fotmob_strength_style_importer import run_import
        _step("fotmob_strength_style", lambda: run_import(DATABASE_URL), steps)

    if _recent("score_state_runs", SCORE_STATE_REFRESH_HOURS):
        _skip("score_state_build", steps, f"fresh<{SCORE_STATE_REFRESH_HOURS}h")
    else:
        from score_state_builder import build
        _step("score_state_build", lambda: build(DATABASE_URL), steps)

    if _recent("score_state_backtest_runs", SCORE_STATE_REFRESH_HOURS):
        _skip("score_state_backtest", steps, f"fresh<{SCORE_STATE_REFRESH_HOURS}h")
    else:
        from score_state_backtest import run_backtest
        _step("score_state_backtest", lambda: run_backtest(DATABASE_URL), steps)

    if _recent("promotion_prior_backtest_runs", PROMOTION_BACKTEST_REFRESH_HOURS):
        _skip("promotion_prior_backtest", steps, f"fresh<{PROMOTION_BACKTEST_REFRESH_HOURS}h")
    else:
        from promotion_prior_backtest import run_backtest
        _step("promotion_prior_backtest", lambda: run_backtest(DATABASE_URL), steps)

    # These are DB-only transforms and should reflect the newest scheduled odds/availability snapshot.
    from market_consensus_builder import build as build_consensus
    _step("market_consensus", lambda: build_consensus(DATABASE_URL), steps)

    from fixture_enrichment_builder import build as build_enrichment
    _step("fixture_enrichment", lambda: build_enrichment(DATABASE_URL), steps)

    result = {
        "status": "success",
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "steps": steps,
    }
    print("ADVANCED_FEATURES_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")))
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
