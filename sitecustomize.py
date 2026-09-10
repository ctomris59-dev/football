"""Temporary compact reporter for the 2026-09-10 research run.

Operational-only: reads persisted research results after startup and prints a compact
summary. It does not run an audit, mutate predictions, or alter the frozen Thursday
list. Remove after capture.
"""
from __future__ import annotations

import json
import os
import threading
import time


def _compact_topn(step):
    if not isinstance(step, dict):
        return None
    sens = step.get("sensitivity") or {}
    out = {}
    for n in ("3", "5", "10"):
        item = sens.get(n) or {}
        overall = item.get("overall") or {}
        folds = item.get("by_fold") or {}
        out[n] = {
            "overall": {
                "n": overall.get("n"),
                "hits": overall.get("hits"),
                "hit_rate": overall.get("hit_rate"),
                "model_brier": overall.get("model_brier"),
                "bootstrap95": overall.get("bootstrap95"),
            },
            "by_fold": {
                str(k): {
                    "n": (v or {}).get("n"),
                    "hits": (v or {}).get("hits"),
                    "hit_rate": (v or {}).get("hit_rate"),
                    "model_brier": (v or {}).get("model_brier"),
                    "bootstrap95": (v or {}).get("bootstrap95"),
                }
                for k, v in folds.items()
            },
        }
    return out


def _worker():
    time.sleep(4)
    db = os.getenv("DATABASE_URL", "").strip()
    if not db:
        print("TOPN_COMPACT_STATUS " + json.dumps({"status": "missing_database_url"}), flush=True)
        return
    try:
        import psycopg
        with psycopg.connect(db) as conn:
            row = conn.execute(
                """SELECT execution_key,status,started_at,finished_at,message,results
                   FROM research_methodology_execution_runs
                   ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()
            edge = conn.execute(
                """SELECT id,status,started_at,finished_at,message
                   FROM research_edge_audit_runs
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        payload = {"methodology": None, "edge_audit": None}
        if row:
            results = row[5] or {}
            payload["methodology"] = {
                "execution_key": row[0],
                "status": row[1],
                "started_at": row[2],
                "finished_at": row[3],
                "message": row[4],
                "topn": _compact_topn(((results.get("steps") or {}).get("topn_sensitivity"))),
                "error": results.get("error"),
            }
        if edge:
            payload["edge_audit"] = {
                "id": edge[0], "status": edge[1], "started_at": edge[2],
                "finished_at": edge[3], "message": edge[4],
            }
        print("TOPN_COMPACT_STATUS " + json.dumps(payload, default=str, separators=(",", ":")), flush=True)
    except Exception as exc:
        print("TOPN_COMPACT_STATUS " + json.dumps({"status": "read_failed", "error": str(exc)[:500]}), flush=True)


if os.getenv("PORT") and os.getenv("DATABASE_URL"):
    threading.Thread(target=_worker, name="topn-compact-reporter", daemon=True).start()
