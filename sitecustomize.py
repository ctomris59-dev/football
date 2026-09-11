"""Optional process-start hooks for one-shot 1X2 historical audits.

Normal production is inert: both flags default false. Hooks are only used to run
holdout-safe research on the existing free Render service without creating a new
resource.
"""
from __future__ import annotations

import logging
import os
import threading

_V1_ENABLED = os.getenv("RUN_ONE_X_TWO_AUDIT_ONCE", "false").lower() in {"1", "true", "yes"}
_V2_ENABLED = os.getenv("RUN_ONE_X_TWO_V2_AUDIT_ONCE", "false").lower() in {"1", "true", "yes"}
_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_log = logging.getLogger("one-x-two-site-hook")
_v1_lock = threading.Lock()
_v2_lock = threading.Lock()


def _run_v1() -> None:
    if not _v1_lock.acquire(blocking=False):
        return
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
    finally:
        _v1_lock.release()


def _run_v2() -> None:
    if not _v2_lock.acquire(blocking=False):
        return
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
    finally:
        _v2_lock.release()


if _DATABASE_URL and _V1_ENABLED:
    threading.Thread(target=_run_v1, name="one-x-two-audit-once-site", daemon=True).start()
if _DATABASE_URL and _V2_ENABLED:
    threading.Thread(target=_run_v2, name="one-x-two-v2-audit-once-site", daemon=True).start()
