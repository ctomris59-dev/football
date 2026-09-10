#!/usr/bin/env python3
"""Minimal production refresh for Thursday -> two lists -> bet -> done.

Active weekly preparation:
- ESPN current results/upcoming fixtures;
- current injury availability (freshness-gated, optional);
- current rosters + real historical starts -> expected-XI/player context;
- official Turkish İddaa opening-price watch;
- international paired same-book no-vig validation ONLY after Turkey target prices appear;
- one frozen Thursday decision.

International prices are never executable prices and never replace model confidence.
They are fetched at freeze time only as a fair-market sanity/value reference. T-1/T-3,
confirmed-lineup reselection, odds-movement rebets, shadow audits and weekly backtests
remain outside the live user decision path.
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
FOTMOB_MAX_AGE_HOURS = float(os.getenv("THURSDAY_FOTMOB_MAX_AGE_HOURS", "12"))
RUN_TOPN_SENSITIVITY_ONCE = os.getenv("RUN_TOPN_SENSITIVITY_ONCE", "false").strip().lower() in {"1", "true", "yes"}
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


def _fotmob_is_fresh() -> bool:
    if FOTMOB_MAX_AGE_HOURS <= 0:
        return False
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            return bool(conn.execute(
                """SELECT 1 FROM fotmob_availability_runs
                   WHERE status='success' AND finished_at>=NOW()-(%s||' hours')::interval
                   ORDER BY finished_at DESC LIMIT 1""",
                (FOTMOB_MAX_AGE_HOURS,),
            ).fetchone())
    except Exception:
        return False


def _compact_topn(result: Dict[str, Any]) -> Dict[str, Any]:
    compact: Dict[str, Any] = {
        "version": result.get("version"),
        "folds": result.get("folds"),
        "candidate_rows": result.get("candidate_rows"),
        "topn": {},
    }
    sensitivity = result.get("sensitivity") or {}
    for n in ("3", "5", "10"):
        item = sensitivity.get(n) or {}
        overall = item.get("overall") or {}
        compact["topn"][n] = {
            "overall": {
                "n": overall.get("n"),
                "hits": overall.get("hits"),
                "hit_rate": overall.get("hit_rate"),
                "avg_confidence": overall.get("avg_confidence"),
                "model_brier": overall.get("model_brier"),
                "bootstrap95": overall.get("bootstrap95"),
            },
            "by_fold": {
                str(fold): {
                    "n": (metrics or {}).get("n"),
                    "hits": (metrics or {}).get("hits"),
                    "hit_rate": (metrics or {}).get("hit_rate"),
                    "avg_confidence": (metrics or {}).get("avg_confidence"),
                    "model_brier": (metrics or {}).get("model_brier"),
                    "bootstrap95": (metrics or {}).get("bootstrap95"),
                }
                for fold, metrics in (item.get("by_fold") or {}).items()
            },
        }
    return compact


def _run() -> Dict[str, Any]:
    summary: Dict[str, Any] = {"started_at": utcnow().isoformat(), "workflow": "thursday-two-lists-final", "steps": {}}
    steps = summary["steps"]

    # Temporary, research-only operational mode. It deliberately exits before any
    # live fixture/player/price refresh so the historical Top-N audit can be run in
    # isolation. Disabled by default and removed after the one-shot verification.
    if RUN_TOPN_SENSITIVITY_ONCE:
        from topn_sensitivity_audit import run as run_topn_sensitivity
        result = run_topn_sensitivity(DATABASE_URL)
        compact = _compact_topn(result)
        steps["topn_sensitivity_once"] = {"status": "ok", "result": compact}
        summary["finished_at"] = utcnow().isoformat()
        summary["status"] = "success"
        summary["workflow"] = "research-topn-only-once"
        log.info("TOPN_SENSITIVITY_COMPACT %s", json.dumps(compact, ensure_ascii=False, default=str, separators=(",", ":")))
        log.info("THURSDAY_REFRESH_RESULT %s", json.dumps(summary, ensure_ascii=False, default=str, separators=(",", ":")))
        return summary

    from espn_current_importer import run_import as espn_current
    run_step("espn_current", lambda: espn_current(DATABASE_URL), steps)

    if _fotmob_is_fresh():
        steps["fotmob_availability"] = {"status": "skipped", "reason": f"fresh<{FOTMOB_MAX_AGE_HOURS}h"}
        log.info("THURSDAY_REFRESH_STEP step=fotmob_availability status=skipped reason=fresh")
    else:
        try:
            from fotmob_availability_importer import run_import as fotmob_availability
            run_step("fotmob_availability", lambda: fotmob_availability(DATABASE_URL), steps, optional=True)
        except Exception as exc:
            steps["fotmob_availability"] = {"status": "unavailable_optional", "error": str(exc)[:500]}

    from player_context_orchestrator import run as player_context
    run_step("player_context", lambda: player_context(DATABASE_URL), steps)

    # This single step owns all price logic. It checks official Turkey prices first.
    # Only once playable target prices exist does it refresh the international paired
    # no-vig reference and then freeze the first sufficiently complete two-list output.
    from thursday_opening_watch import main as opening_watch
    run_step("opening_watch", lambda: opening_watch(DATABASE_URL), steps)

    # Operational one-shot only: disabled by default and idempotent in Postgres.
    # Closing odds remain evaluation-only and no challenger is activated here.
    if os.getenv("RUN_RESEARCH_ONCE", "false").strip().lower() in {"1", "true", "yes"}:
        from research_once_runner import run as research_once
        run_step("research_methodology_once", lambda: research_once(DATABASE_URL), steps)

    summary["finished_at"] = utcnow().isoformat()
    summary["status"] = "success"
    log.info("THURSDAY_REFRESH_RESULT %s", json.dumps(summary, ensure_ascii=False, default=str, separators=(",", ":")))
    return summary


def main() -> Dict[str, Any]:
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
    local = utcnow().astimezone(ISTANBUL)
    if local.weekday() != 3 and not REFRESH_FORCE:
        result = {"status": "skipped_not_thursday", "local_time": local.isoformat(), "workflow": "thursday-two-lists-final"}
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
            guard_finish(lock, guard_id, "success", "Thursday final decision preparation completed")
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