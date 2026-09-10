#!/usr/bin/env python3
"""Idempotent one-shot executor for the hardened research methodology.

This is operational plumbing only. It never changes model probabilities or activates
challengers. The runner executes the historical V1 structural audit, the Top-3/5/10
general sensitivity audit, the Over 2.5-only sensitivity audit, the separate CLV
evaluation, installs the frozen policy snapshot, and records the methodology change
log once for a fixed execution key.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
EXECUTION_KEY = os.getenv("RESEARCH_ONCE_KEY", "methodology-v2-2026-09-10").strip() or "methodology-v2-2026-09-10"
RUN_ENABLED = os.getenv("RUN_RESEARCH_ONCE", "false").strip().lower() in {"1", "true", "yes"}
LOCK_KEY = int(os.getenv("RESEARCH_ONCE_ADVISORY_LOCK_KEY", "856420262"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("research-once")

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_methodology_execution_runs(
 execution_key TEXT PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL,
 results JSONB,
 message TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _already_successful(conn) -> bool:
    row = conn.execute(
        "SELECT status FROM research_methodology_execution_runs WHERE execution_key=%s",
        (EXECUTION_KEY,),
    ).fetchone()
    return bool(row and row[0] == "success")


def _mark_running(conn) -> None:
    conn.execute(
        """INSERT INTO research_methodology_execution_runs(execution_key,status,started_at,finished_at,results,message)
           VALUES(%s,'running',NOW(),NULL,NULL,NULL)
           ON CONFLICT(execution_key) DO UPDATE SET status='running',started_at=NOW(),finished_at=NULL,results=NULL,message=NULL""",
        (EXECUTION_KEY,),
    )


def _mark_finished(conn, status: str, results: Dict[str, Any], message: str) -> None:
    from psycopg.types.json import Jsonb
    conn.execute(
        """UPDATE research_methodology_execution_runs
           SET status=%s,finished_at=NOW(),results=%s,message=%s
           WHERE execution_key=%s""",
        (status, Jsonb(results), message[:2000], EXECUTION_KEY),
    )


def run(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    if not RUN_ENABLED:
        return {"status": "disabled", "execution_key": EXECUTION_KEY}

    lock = psycopg.connect(db, autocommit=True)
    got = False
    try:
        lock.execute(SCHEMA)
        got = bool(lock.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0])
        if not got:
            return {"status": "skipped_lock_busy", "execution_key": EXECUTION_KEY}
        if _already_successful(lock):
            result = {"status": "already_successful", "execution_key": EXECUTION_KEY}
            log.info("METHODOLOGY_ONCE_RESULT %s", json.dumps(result, separators=(",", ":")))
            return result

        _mark_running(lock)
        summary: Dict[str, Any] = {
            "execution_key": EXECUTION_KEY,
            "started_at": utcnow().isoformat(),
            "steps": {},
        }
        try:
            from edge_structure_audit import run_audit
            edge = run_audit(db)
            summary["steps"]["edge_structure_audit"] = {
                "status": "success",
                "version": edge.get("version"),
                "weekly_topn": edge.get("weekly_topn"),
                "ou25_market_benchmark": edge.get("ou25_market_benchmark"),
                "validation_protocol": edge.get("validation_protocol"),
            }

            from topn_sensitivity_audit import run as run_topn_sensitivity
            topn = run_topn_sensitivity(db)
            summary["steps"]["topn_sensitivity"] = {
                "status": "success",
                "version": topn.get("version"),
                "protocol": topn.get("protocol"),
                "folds": topn.get("folds"),
                "sensitivity": topn.get("sensitivity"),
                "interpretation_rule": topn.get("interpretation_rule"),
            }

            from over25_sensitivity_audit import run as run_over25_sensitivity
            over25 = run_over25_sensitivity(db)
            summary["steps"]["over25_sensitivity"] = {
                "status": "success",
                "version": over25.get("version"),
                "protocol": over25.get("protocol"),
                "folds": over25.get("folds"),
                "sensitivity": over25.get("sensitivity"),
                "interpretation_rule": over25.get("interpretation_rule"),
            }

            from clv_backtest import run_backtest as run_clv
            clv = run_clv(db)
            summary["steps"]["clv_backtest"] = {
                "status": "success",
                "version": clv.get("version"),
                "picks_seen": clv.get("picks_seen"),
                "picks_with_valid_clv": clv.get("picks_with_valid_clv"),
                "historical_validation": clv.get("historical_validation"),
                "live_holdout_monitoring": clv.get("live_holdout_monitoring"),
            }

            from research_change_control import install_freeze, record_change
            freeze = install_freeze(db)
            summary["steps"]["policy_freeze"] = {"status": "success", **freeze}
            bug = record_change(
                "BUG_FIX",
                "Require two-fold holdout-safe evidence before non-v1 V5 activation; legacy single-split evidence fails closed.",
                behavior_change=True,
                source_sha=os.getenv("RENDER_GIT_COMMIT", ""),
                evidence={
                    "gate_version": "two-fold-week-block-v1",
                    "historical_test_seasons": ["2425", "2526"],
                    "live_holdout_season": "2627",
                    "holdout_excluded": True,
                },
                database_url=db,
            )
            infra = record_change(
                "INFRA_ONLY",
                "Install block-bootstrap/shrinkage/CLV/Top-N/Over2.5 sensitivity methodology and execute the one-shot historical validation run.",
                behavior_change=False,
                source_sha=os.getenv("RENDER_GIT_COMMIT", ""),
                evidence={
                    "production_probability_engine_unchanged": True,
                    "new_challenger_activated": False,
                    "closing_odds_evaluation_only": True,
                    "topn_sensitivity_research_only": True,
                    "over25_sensitivity_research_only": True,
                },
                database_url=db,
            )
            summary["steps"]["change_log"] = {"status": "success", "bug_fix": bug, "infra_only": infra}
            summary["finished_at"] = utcnow().isoformat()
            summary["status"] = "success"
            _mark_finished(lock, "success", summary, "ok")
            log.info("METHODOLOGY_ONCE_RESULT %s", json.dumps(summary, ensure_ascii=False, default=str, separators=(",", ":")))
            return summary
        except Exception as exc:
            summary["finished_at"] = utcnow().isoformat()
            summary["status"] = "failed"
            summary["error"] = str(exc)
            _mark_finished(lock, "failed", summary, str(exc))
            log.exception("METHODOLOGY_ONCE_FAILED")
            raise
    finally:
        if got:
            try:
                lock.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
            except Exception:
                pass
        lock.close()


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
