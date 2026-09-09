#!/usr/bin/env python3
"""Minimal production refresh for the Thursday -> two lists -> bet -> done workflow.

Weekly live decision data only:
- ESPN current results/upcoming fixtures (model history + target fixtures)
- current FotMob availability when reachable (optional)
- current ESPN rosters + real historical starts -> expected-XI/player context
- official Turkish İddaa prices
- streamlined Thursday decision engine

Foreign odds, Asian lines, T-1/T-3 lineups, odds movement, shadow feature audits and
weekly backtests are deliberately excluded from this live path. Their historical
data/code remain available offline for validation, but cannot add noise to the bet.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict
from zoneinfo import ZoneInfo

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
REFRESH_LOCK_KEY = int(os.getenv("LIVE_REFRESH_ADVISORY_LOCK_KEY", "856420261"))
REFRESH_MIN_INTERVAL_MINUTES = float(os.getenv("LIVE_REFRESH_MIN_INTERVAL_MINUTES", "45"))
REFRESH_FORCE = os.getenv("LIVE_REFRESH_FORCE", "false").lower() in {"1", "true", "yes"}
REFRESH_TRIGGER_NAME = os.getenv("LIVE_REFRESH_TRIGGER_NAME", "thursday-decision-prep").strip() or "thursday-decision-prep"
ISTANBUL = ZoneInfo("Europe/Istanbul")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("thursday-refresh")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_step(name: str, fn: Callable[[], Any], summary: Dict[str, Any], *, optional: bool = False) -> None:
    try:
        result = fn()
        summary[name] = {"status": "ok", "result": result}
        log.info("THURSDAY_REFRESH_STEP step=%s status=ok result=%s", name, result)
    except Exception as exc:
        summary[name] = {"status": "failed", "error": str(exc)[:1000]}
        if optional:
            log.warning("THURSDAY_REFRESH_STEP step=%s status=failed_optional error=%s", name, str(exc)[:500])
        else:
            raise


def _run() -> Dict[str, Any]:
    summary: Dict[str, Any] = {"started_at": utcnow().isoformat(), "workflow": "thursday-two-lists", "steps": {}}
    steps = summary["steps"]

    from espn_current_importer import run_import as espn_current
    run_step("espn_current", lambda: espn_current(DATABASE_URL), steps)

    # Current injuries help the expected-XI proxy when the source is reachable, but
    # an upstream outage must not crash the validated form model.
    try:
        from fotmob_availability_importer import run_import as fotmob_availability
        run_step("fotmob_availability", lambda: fotmob_availability(DATABASE_URL), steps, optional=True)
    except Exception as exc:
        steps["fotmob_availability"] = {"status": "unavailable_optional", "error": str(exc)[:500]}

    from player_context_orchestrator import run as player_context
    run_step("player_context", lambda: player_context(DATABASE_URL), steps)

    # First Turkey-price check is useful if the bulletin is already open. A zero-match
    # result is normal before release and will be handled by the lightweight watcher.
    try:
        from turkey_iddaa_odds_collector import run_import as turkey_odds
        run_step("turkey_iddaa_odds", lambda: turkey_odds(DATABASE_URL), steps, optional=True)
    except Exception as exc:
        steps["turkey_iddaa_odds"] = {"status": "unavailable_optional", "error": str(exc)[:500]}

    from thursday_decision_engine import build_decision
    run_step("thursday_decision", lambda: build_decision(DATABASE_URL), steps)

    summary["finished_at"] = utcnow().isoformat()
    summary["status"] = "success"
    log.info("THURSDAY_REFRESH_RESULT %s", json.dumps(summary, ensure_ascii=False, default=str, separators=(",", ":")))
    return summary


def main() -> Dict[str, Any]:
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")

    local = utcnow().astimezone(ISTANBUL)
    if local.weekday() != 3 and not REFRESH_FORCE:  # Thursday=3
        result = {"status": "skipped_not_thursday", "local_time": local.isoformat(), "workflow": "thursday-two-lists"}
        log.info("THURSDAY_REFRESH_SKIP %s", json.dumps(result, separators=(",", ":")))
        return result

    from live_refresh_guard import begin as guard_begin, finish as guard_finish
    lock = psycopg.connect(DATABASE_URL, autocommit=True)
    got = False
    guard_id = None
    try:
        got = bool(lock.execute("SELECT pg_try_advisory_lock(%s)", (REFRESH_LOCK_KEY,)).fetchone()[0])
        if not got:
            return {"status": "skipped_duplicate_refresh", "reason": "postgres_advisory_lock_busy", "at": utcnow().isoformat()}
        guard_id, skipped = guard_begin(lock, REFRESH_MIN_INTERVAL_MINUTES, REFRESH_FORCE, REFRESH_TRIGGER_NAME)
        if skipped:
            return skipped
        try:
            result = _run()
            guard_finish(lock, guard_id, "success", "thursday decision prep completed")
            return result
        except Exception as exc:
            guard_finish(lock, guard_id, "failed", str(exc))
            raise
    finally:
        if got:
            try:
                lock.execute("SELECT pg_advisory_unlock(%s)", (REFRESH_LOCK_KEY,))
            except Exception:
                pass
        lock.close()


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2, default=str))
