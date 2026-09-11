#!/usr/bin/env python3
"""Minimal production refresh for Thursday -> weekly list + optional value -> done.

Active weekly preparation:
- ESPN current results/upcoming fixtures;
- all-competition club schedules (domestic + UEFA) for true rest/fatigue context;
- current injury availability (freshness-gated, optional);
- current rosters + real historical starts -> expected-XI/player context;
- optional xG/Elo/pressure refresh + unified match-environment shadow snapshot;
- official Turkish İddaa opening-price watch;
- synchronized two-sided Turkey price refresh for goals/BTTS/base corners;
- international paired same-book no-vig validation after Turkey target prices appear;
- frozen weekly decision;
- exact-line 7.5/8.5/9.5/10.5 corner upgrade using the same frozen V1 corner lambda.

International prices are never executable prices and never replace model confidence.
The match-environment layer is shadow-only: xG regression, pace, Elo, venue and
scoreline diagnostics cannot alter frozen V1 probabilities without two-fold OOS
validation. All-competition rest is an operational data-correction layer and can
block/de-rank a candidate when a real intervening match exists.
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


def _run() -> Dict[str, Any]:
    summary: Dict[str, Any] = {"started_at": utcnow().isoformat(), "workflow": "thursday-two-lists-final", "steps": {}}
    steps = summary["steps"]

    from espn_current_importer import run_import as espn_current
    run_step("espn_current", lambda: espn_current(DATABASE_URL), steps)

    # Critical schedule-context fix: collect each Big Five club's domestic + UEFA
    # schedule before ranking so rest is not accidentally league-only.
    from espn_team_schedule_importer import run_import as espn_team_schedule
    run_step("espn_team_schedule_all_comp", lambda: espn_team_schedule(DATABASE_URL), steps)

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

    # These context sources are useful but are deliberately non-blocking. If an
    # external xG endpoint is unavailable, V1 + Turkey prices can still operate.
    try:
        from understat_xg_importer import run_import as understat_xg
        run_step("understat_xg_shadow", lambda: understat_xg(DATABASE_URL), steps, optional=True)
    except Exception as exc:
        steps["understat_xg_shadow"] = {"status": "unavailable_optional", "error": str(exc)[:500]}

    try:
        from internal_elo_builder import build as internal_elo
        run_step("internal_elo_shadow", lambda: internal_elo(DATABASE_URL), steps, optional=True)
    except Exception as exc:
        steps["internal_elo_shadow"] = {"status": "unavailable_optional", "error": str(exc)[:500]}

    try:
        from pressure_features_builder import build as pressure_features
        run_step("pressure_features_shadow", lambda: pressure_features(DATABASE_URL), steps, optional=True)
    except Exception as exc:
        steps["pressure_features_shadow"] = {"status": "unavailable_optional", "error": str(exc)[:500]}

    try:
        from match_environment_builder import build as match_environment
        run_step("match_environment_shadow", lambda: match_environment(DATABASE_URL), steps, optional=True)
    except Exception as exc:
        steps["match_environment_shadow"] = {"status": "unavailable_optional", "error": str(exc)[:500]}

    from thursday_opening_watch import main as opening_watch
    run_step("opening_watch", lambda: opening_watch(DATABASE_URL), steps)

    # Once a Thursday decision is frozen, opening_watch() returns early and no longer
    # refreshes the executable Turkey price table. The multiline-corner pass still
    # writes fresh corner rows, so the 6-hour freshness gate could make older KG/2.5
    # rows disappear while corners remained visible. Refresh every two-sided base
    # market immediately before the corner pass so all market families share the
    # same freshness window. This is append-only price storage; frozen opening odds
    # and the already-finalized decision are not rewritten by this step.
    from turkey_two_sided_odds import run_import as turkey_all_sides
    run_step("turkey_prices_all_sides", lambda: turkey_all_sides(DATABASE_URL), steps)

    from multiline_corner_upgrade import upgrade_final
    run_step("multiline_corners", lambda: upgrade_final(DATABASE_URL), steps)

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
