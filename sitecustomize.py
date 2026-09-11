"""Optional process-start hooks for one-shot 1X2 historical audits.

Normal production is inert: all flags default false. Render build interpreters may
import sitecustomize before dependencies exist; those invocations fail closed. At
runtime, explicitly enabled audits start in background threads so web-service port
binding is never delayed while holdout-safe research runs.
"""
from __future__ import annotations

import logging
import os
import threading

_V1_ENABLED = os.getenv("RUN_ONE_X_TWO_AUDIT_ONCE", "false").lower() in {"1", "true", "yes"}
_V2_ENABLED = os.getenv("RUN_ONE_X_TWO_V2_AUDIT_ONCE", "false").lower() in {"1", "true", "yes"}
_MARKET_ENABLED = os.getenv("RUN_ONE_X_TWO_MARKET_AUDIT_ONCE", "false").lower() in {"1", "true", "yes"}
_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_log = logging.getLogger("one-x-two-site-hook")


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


def _start(name: str, fn) -> None:
    _log.warning("%s_STARTED", name)
    threading.Thread(target=fn, name=name.lower().replace("_", "-"), daemon=True).start()


if _DATABASE_URL and (_V1_ENABLED or _V2_ENABLED or _MARKET_ENABLED) and _runtime_dependencies_ready():
    if _V1_ENABLED:
        _start("ONE_X_TWO_AUDIT_ONCE", _run_v1)
    if _V2_ENABLED:
        _start("ONE_X_TWO_V2_AUDIT_ONCE", _run_v2)
    if _MARKET_ENABLED:
        _start("ONE_X_TWO_MARKET_AUDIT_ONCE", _run_market)
