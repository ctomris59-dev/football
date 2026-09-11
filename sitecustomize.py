"""Optional process-start hook for a one-shot 1X2 historical audit.

Python imports sitecustomize automatically when available on sys.path. Normal
production is completely inert because RUN_ONE_X_TWO_AUDIT_ONCE defaults false.
When explicitly enabled on the existing web service, the audit runs in a daemon
thread and writes its gate result to the existing policy registry.
"""
from __future__ import annotations

import logging
import os
import threading

_ENABLED = os.getenv("RUN_ONE_X_TWO_AUDIT_ONCE", "false").lower() in {"1", "true", "yes"}
_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_log = logging.getLogger("one-x-two-site-hook")
_lock = threading.Lock()


def _run() -> None:
    if not _lock.acquire(blocking=False):
        return
    try:
        from one_x_two_audit import run_audit
        result = run_audit(_DATABASE_URL)
        _log.warning(
            "ONE_X_TWO_AUDIT_ONCE_COMPLETED gate_passed=%s activation=%s overall=%s by_fold=%s reasons=%s",
            result.get("gate_passed"),
            result.get("recommended_activation"),
            result.get("overall"),
            result.get("by_fold"),
            result.get("gate_fail_reasons"),
        )
    except Exception:
        _log.exception("ONE_X_TWO_AUDIT_ONCE_FAILED")
    finally:
        _lock.release()


if _ENABLED and _DATABASE_URL:
    threading.Thread(target=_run, name="one-x-two-audit-once-site", daemon=True).start()
