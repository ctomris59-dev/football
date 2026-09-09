#!/usr/bin/env python3
"""Advanced feature pipeline v2 with deterministic fallbacks and validation gates."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SECOND_TIER_REFRESH_HOURS = float(os.getenv("SECOND_TIER_REFRESH_HOURS", str(24 * 30)))
FOTMOB_STRENGTH_REFRESH_HOURS = float(os.getenv("FOTMOB_STRENGTH_REFRESH_HOURS", "24"))
SCORE_STATE_REFRESH_HOURS = float(os.getenv("SCORE_STATE_REFRESH_HOURS", str(24 * 30)))
RUN_EXTERNAL_CLUBELO = os.getenv("ADVANCED_EXTERNAL_CLUBELO", "false").lower() in {"1", "true", "yes"}
# Current FotMob deep-stat endpoint resolves seasons but returns zero usable rows on Render.
# Keep it opt-in and use the deterministic DB style fallback in standard production refreshes.
RUN_FOTMOB_DEEP = os.getenv("ADVANCED_FOTMOB_DEEP_STATS", "false").lower() in {"1", "true", "yes"}

EXPECTED_SECOND_TIER = {
    (season, division)
    for season in ("2324", "2425", "2526")
    for division in ("E1", "SP2", "I2", "D2", "F2")
}


def recent(table: str, hours: float) -> bool:
    allowed = {"second_tier_import_runs", "fotmob_strength_runs", "score_state_runs", "score_state_backtest_runs"}
    if table not in allowed or not DATABASE_URL or hours <= 0:
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


def second_tier_complete() -> bool:
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            rows = conn.execute(
                """SELECT season_code,division,COUNT(*) FROM second_tier_matches
                   WHERE season_code IN ('2324','2425','2526') AND division IN ('E1','SP2','I2','D2','F2')
                   GROUP BY season_code,division"""
            ).fetchall()
        present = {(str(s), str(d)) for s, d, n in rows if int(n or 0) >= 20}
        return EXPECTED_SECOND_TIER.issubset(present)
    except Exception:
        return False


def promotion_validation_ready() -> bool:
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute(
                """SELECT matches,status FROM promotion_prior_backtest_runs
                   WHERE version='promotion-prior-brier-v2-missing-safe'
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        return bool(row and row[1] == "success" and int(row[0] or 0) >= 40)
    except Exception:
        return False


def step(name: str, fn: Callable[[], Any], out: Dict[str, Any], optional: bool = True) -> None:
    try:
        result = fn()
        out[name] = {"status": "ok", "result": result}
    except Exception as exc:
        out[name] = {"status": "failed", "error": str(exc)[:800]}
        if not optional:
            raise


def skip(name: str, out: Dict[str, Any], reason: str) -> None:
    out[name] = {"status": "skipped", "reason": reason}


def run(database_url: str | None = None) -> Dict[str, Any]:
    global DATABASE_URL
    if database_url:
        DATABASE_URL = database_url.strip()
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
    started = datetime.now(timezone.utc)
    steps: Dict[str, Any] = {}

    if second_tier_complete() and recent("second_tier_import_runs", SECOND_TIER_REFRESH_HOURS):
        skip("second_tier", steps, "15/15 sources complete and fresh")
    else:
        from second_tier_importer_v3 import run_import
        step("second_tier", lambda: run_import(DATABASE_URL), steps, optional=False)

    from promotion_prior_builder_v2 import build as build_promotion
    step("promotion_priors", lambda: build_promotion(DATABASE_URL), steps)

    # Internal Elo is deterministic and is the production fallback.
    from internal_elo_builder import build as build_elo
    step("internal_elo", lambda: build_elo(DATABASE_URL), steps, optional=False)

    # The external service is diagnostic only; a 5xx can never block production.
    if RUN_EXTERNAL_CLUBELO:
        from clubelo_importer import run_import as run_clubelo
        step("external_clubelo", lambda: run_clubelo(DATABASE_URL), steps)
    else:
        skip("external_clubelo", steps, "disabled; internal Elo is production source")

    if RUN_FOTMOB_DEEP:
        if recent("fotmob_strength_runs", FOTMOB_STRENGTH_REFRESH_HOURS):
            skip("fotmob_strength_style", steps, f"fresh<{FOTMOB_STRENGTH_REFRESH_HOURS}h")
        else:
            from fotmob_strength_style_importer_v2 import run_import as run_fotmob_strength
            step("fotmob_strength_style", lambda: run_fotmob_strength(DATABASE_URL), steps)
    else:
        skip("fotmob_strength_style", steps, "deep stats disabled; DB style fallback active")

    from internal_style_builder import build as build_style
    step("internal_style", lambda: build_style(DATABASE_URL), steps, optional=False)

    if recent("score_state_runs", SCORE_STATE_REFRESH_HOURS):
        skip("score_state_build", steps, f"fresh<{SCORE_STATE_REFRESH_HOURS}h")
    else:
        from score_state_builder import build
        step("score_state_build", lambda: build(DATABASE_URL), steps, optional=False)

    if recent("score_state_backtest_runs", SCORE_STATE_REFRESH_HOURS):
        skip("score_state_backtest", steps, f"validated/fresh<{SCORE_STATE_REFRESH_HOURS}h")
    else:
        from score_state_backtest import run_backtest
        step("score_state_backtest", lambda: run_backtest(DATABASE_URL), steps, optional=False)

    if promotion_validation_ready():
        skip("promotion_prior_backtest", steps, "v2 validation already complete")
    else:
        from promotion_prior_backtest_v2 import run_backtest
        step("promotion_prior_backtest", lambda: run_backtest(DATABASE_URL), steps, optional=False)

    from market_consensus_builder import build as build_consensus
    step("market_consensus", lambda: build_consensus(DATABASE_URL), steps)

    from fixture_enrichment_builder import build as build_enrichment
    step("fixture_enrichment", lambda: build_enrichment(DATABASE_URL), steps, optional=False)

    result = {
        "status": "success",
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "second_tier_complete": second_tier_complete(),
        "steps": steps,
    }
    print("ADVANCED_FEATURES_V2_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")))
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
