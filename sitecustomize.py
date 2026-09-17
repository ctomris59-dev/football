"""Optional process-start hooks for football audits and strict weekly migration.

Normal research hooks remain opt-in. The strict-week migration is safe to run on
web-service startup: it only rebuilds when the current week's stored final was
created by an older selection policy. Once a strict-v3 final exists it is inert.
"""
from __future__ import annotations

import logging
import os
import threading

_V1_ENABLED = os.getenv("RUN_ONE_X_TWO_AUDIT_ONCE", "false").lower() in {"1", "true", "yes"}
_V2_ENABLED = os.getenv("RUN_ONE_X_TWO_V2_AUDIT_ONCE", "false").lower() in {"1", "true", "yes"}
_MARKET_ENABLED = os.getenv("RUN_ONE_X_TWO_MARKET_AUDIT_ONCE", "false").lower() in {"1", "true", "yes"}
_SHADOW_1X2_ENABLED = os.getenv("RUN_SHADOW_1X2_PREVIEW_ONCE", "false").lower() in {"1", "true", "yes"}
_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_log = logging.getLogger("football-site-hook")


def _runtime_dependencies_ready() -> bool:
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def _run_v1() -> None:
    try:
        from one_x_two_audit import run_audit
        result = run_audit(_DATABASE_URL)
        _log.warning(
            "ONE_X_TWO_AUDIT_ONCE_COMPLETED gate_passed=%s activation=%s overall=%s by_fold=%s reasons=%s",
            result.get("gate_passed"), result.get("recommended_activation"),
            result.get("overall"), result.get("by_fold"), result.get("gate_fail_reasons"),
        )
    except Exception:
        _log.exception("ONE_X_TWO_AUDIT_ONCE_FAILED")


def _run_v2() -> None:
    try:
        from one_x_two_coupon_v2_audit import run_v2_audit
        result = run_v2_audit(_DATABASE_URL)
        _log.warning(
            "ONE_X_TWO_V2_AUDIT_ONCE_COMPLETED gate_passed=%s activation=%s policy=%s development=%s holdout=%s reasons=%s",
            result.get("gate_passed"), result.get("recommended_activation"),
            result.get("selected_coupon_policy"), result.get("development"),
            result.get("final_holdout"), result.get("gate_fail_reasons"),
        )
    except Exception:
        _log.exception("ONE_X_TWO_V2_AUDIT_ONCE_FAILED")


def _run_market() -> None:
    try:
        from one_x_two_market_audit import run_audit
        result = run_audit(_DATABASE_URL)
        _log.warning(
            "ONE_X_TWO_MARKET_AUDIT_ONCE_COMPLETED gate_passed=%s activation=%s by_fold=%s reasons=%s",
            result.get("gate_passed"), result.get("recommended_activation"),
            result.get("by_fold"), result.get("gate_fail_reasons"),
        )
    except Exception:
        _log.exception("ONE_X_TWO_MARKET_AUDIT_ONCE_FAILED")


def _run_shadow_1x2() -> None:
    try:
        from shadow_1x2_preview_once import run
        result = run(_DATABASE_URL)
        comparison = result.get("comparison") or {}
        _log.warning(
            "SHADOW_1X2_PREVIEW_ONCE_COMPLETED week=%s one_x_two=%s changed=%s entered=%s exited=%s frozen_untouched=%s",
            result.get("week_key"), result.get("one_x_two_in_shadow_top10"),
            len(comparison.get("changed_same_fixture") or []),
            len(comparison.get("entered") or []), len(comparison.get("exited") or []),
            result.get("frozen_final_untouched"),
        )
    except Exception:
        _log.exception("SHADOW_1X2_PREVIEW_ONCE_FAILED")


def _run_strict_week_migration() -> None:
    """Replace this week's legacy frozen decision with strict-playable-v3 once."""
    try:
        from thursday_opening_watch import CURRENT_SOURCE, latest_final, main
        final = latest_final(_DATABASE_URL)
        old_source = str((final or {}).get("source") or "")
        if final and old_source == CURRENT_SOURCE:
            _log.warning("STRICT_WEEK_MIGRATION_NOT_NEEDED source=%s", CURRENT_SOURCE)
            return
        _log.warning("STRICT_WEEK_MIGRATION_STARTED old_source=%s", old_source or "none")
        result = main(_DATABASE_URL)
        _log.warning(
            "STRICT_WEEK_MIGRATION_COMPLETED status=%s source=%s picks=%s",
            result.get("status"),
            result.get("source") or CURRENT_SOURCE,
            len(((result.get("payload") or {}).get("verified_playable") or result.get("verified_playable") or [])),
        )
    except Exception:
        _log.exception("STRICT_WEEK_MIGRATION_FAILED")


def _start(name: str, fn) -> None:
    _log.warning("%s_STARTED", name)
    threading.Thread(target=fn, name=name.lower().replace("_", "-"), daemon=True).start()


if _DATABASE_URL and _runtime_dependencies_ready():
    # Production safety migration: legacy Thursday finals must not survive a strict
    # policy deploy. This becomes a no-op after the current-source final is stored.
    _start("STRICT_WEEK_MIGRATION", _run_strict_week_migration)

    if _V1_ENABLED:
        _start("ONE_X_TWO_AUDIT_ONCE", _run_v1)
    if _V2_ENABLED:
        _start("ONE_X_TWO_V2_AUDIT_ONCE", _run_v2)
    if _MARKET_ENABLED:
        _start("ONE_X_TWO_MARKET_AUDIT_ONCE", _run_market)
    if _SHADOW_1X2_ENABLED:
        _start("SHADOW_1X2_PREVIEW_ONCE", _run_shadow_1x2)
