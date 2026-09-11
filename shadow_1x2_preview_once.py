#!/usr/bin/env python3
"""Run a one-shot current-week shadow preview with guarded 1X2 enabled.

This utility deliberately does NOT touch thursday_final_decisions. It can refresh
price/reference inputs, builds the current guarded weekly list in memory, compares it
with the already-frozen list, stores the diagnostic result in its own shadow table,
and prints a compact log payload.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import psycopg
from psycopg.types.json import Jsonb

from thursday_decision_engine import weekend_bounds

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RUN_KEY = os.getenv("SHADOW_1X2_RUN_KEY", "").strip()
REFRESH_INPUTS = os.getenv("SHADOW_1X2_REFRESH_INPUTS", "true").strip().lower() in {"1", "true", "yes"}

DDL = """
CREATE TABLE IF NOT EXISTS shadow_1x2_preview_runs(
 run_key TEXT PRIMARY KEY,
 week_key DATE NOT NULL,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL,
 result JSONB,
 message TEXT
);
"""


def _compact(item: Dict[str, Any], rank: int) -> Dict[str, Any]:
    return {
        "rank": rank,
        "event_id": str(item.get("event_id") or ""),
        "match": f"{item.get('home')} - {item.get('away')}",
        "market": item.get("market"),
        "selection": item.get("selection"),
        "confidence": round(float(item.get("confidence") or item.get("model_probability_estimate") or 0.0), 6),
        "ranking_score": round(float(item.get("ranking_score") or 0.0), 6),
        "tr_price": item.get("tr_price"),
        "market_check": item.get("market_check"),
    }


def compare_lists(frozen: Iterable[Dict[str, Any]], shadow: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    frozen_rows = [_compact(x, i + 1) for i, x in enumerate(frozen)]
    shadow_rows = [_compact(x, i + 1) for i, x in enumerate(shadow)]
    frozen_by_id = {x["event_id"]: x for x in frozen_rows}
    shadow_by_id = {x["event_id"]: x for x in shadow_rows}

    entered = [x for x in shadow_rows if x["event_id"] not in frozen_by_id]
    exited = [x for x in frozen_rows if x["event_id"] not in shadow_by_id]
    changed = []
    rank_moves = []
    for event_id in sorted(set(frozen_by_id) & set(shadow_by_id)):
        a, b = frozen_by_id[event_id], shadow_by_id[event_id]
        if (a["market"], a["selection"]) != (b["market"], b["selection"]):
            changed.append({"event_id": event_id, "match": b["match"], "frozen": a, "shadow": b})
        if a["rank"] != b["rank"]:
            rank_moves.append({"event_id": event_id, "match": b["match"], "from": a["rank"], "to": b["rank"]})

    return {
        "frozen": frozen_rows,
        "shadow": shadow_rows,
        "entered": entered,
        "exited": exited,
        "changed_same_fixture": changed,
        "rank_moves": rank_moves,
        "one_x_two_picks": [x for x in shadow_rows if x["market"] == "match_result"],
    }


def _safe(label: str, fn) -> Dict[str, Any]:
    try:
        result = fn()
        return {"status": "ok", "result": result}
    except Exception as exc:
        return {"status": "failed_optional", "error": str(exc)[:1200]}


def run(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")

    now = datetime.now(timezone.utc)
    week_key, start, end = weekend_bounds(now)
    run_key = RUN_KEY or f"shadow-1x2-{week_key.isoformat()}"

    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(DDL)
        prior = conn.execute(
            "SELECT status,result FROM shadow_1x2_preview_runs WHERE run_key=%s",
            (run_key,),
        ).fetchone()
        if prior and prior[0] == "success" and prior[1]:
            result = dict(prior[1])
            print("SHADOW_1X2_PREVIEW_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return result
        conn.execute(
            """INSERT INTO shadow_1x2_preview_runs(run_key,week_key,status)
               VALUES(%s,%s,'running')
               ON CONFLICT(run_key) DO UPDATE SET week_key=EXCLUDED.week_key,status='running',started_at=NOW(),finished_at=NULL,result=NULL,message=NULL""",
            (run_key, week_key),
        )

    try:
        from thursday_opening_watch import latest_final
        frozen = latest_final(db, week_key)
        if not frozen:
            raise RuntimeError(f"No frozen Thursday decision exists for week {week_key}")
        payload = frozen.get("payload") or {}
        frozen_list: List[Dict[str, Any]] = list(payload.get("weekly_reliable") or payload.get("high_confidence") or [])
        if not frozen_list:
            raise RuntimeError("Frozen decision exists but contains no weekly list")

        if REFRESH_INPUTS:
            from turkey_iddaa_odds_collector import run_import as run_turkey
            from turkey_two_sided_odds import run_import as run_turkey_all_sides
            turkey = _safe("turkey", lambda: run_turkey(db))
            turkey_all = _safe("turkey_all_sides", lambda: run_turkey_all_sides(db))

            # One current all-book snapshot is enough for both existing binary references
            # and the new same-book three-way 1X2 reference. Failure is non-blocking;
            # weekly reliability already permits missing international reference.
            from oddspapi_allbooks_importer import run_import as run_allbooks
            allbooks = _safe("allbooks", lambda: run_allbooks(db))

            from one_x_two_market_reference import build_refs as build_1x2_refs
            refs_1x2 = _safe("one_x_two_refs", lambda: build_1x2_refs(db, start=start, end=end))
        else:
            reused = {"status": "skipped_reuse_current_snapshots"}
            turkey = dict(reused)
            turkey_all = dict(reused)
            allbooks = dict(reused)
            refs_1x2 = dict(reused)

        from weekly_trusted_predictions_v2 import build as build_shadow
        shadow_result = build_shadow(db, now=now, limit=10)
        shadow_list: List[Dict[str, Any]] = list(shadow_result.get("picks") or [])

        comparison = compare_lists(frozen_list[:10], shadow_list[:10])
        result = {
            "status": "success",
            "run_key": run_key,
            "week_key": week_key,
            "frozen_final_untouched": True,
            "frozen_count": len(frozen_list[:10]),
            "shadow_count": len(shadow_list[:10]),
            "one_x_two_registry_mode": (shadow_result.get("one_x_two_diagnostics") or {}).get("registry_mode"),
            "one_x_two_candidate_rows": (shadow_result.get("one_x_two_diagnostics") or {}).get("candidate_rows"),
            "one_x_two_in_shadow_top10": (shadow_result.get("one_x_two_diagnostics") or {}).get("one_x_two_in_ranked_picks"),
            "one_x_two_excluded": (shadow_result.get("one_x_two_diagnostics") or {}).get("excluded_counts") or {},
            "input_refresh": {
                "turkey": turkey,
                "turkey_all_sides": turkey_all,
                "allbooks": allbooks,
                "one_x_two_refs": refs_1x2,
            },
            "comparison": comparison,
        }

        with psycopg.connect(db, autocommit=True) as conn:
            conn.execute(DDL)
            conn.execute(
                "UPDATE shadow_1x2_preview_runs SET finished_at=NOW(),status='success',result=%s,message=%s WHERE run_key=%s",
                (
                    Jsonb(result, dumps=lambda obj: json.dumps(obj, ensure_ascii=False, default=str)),
                    "Shadow preview completed; frozen final was not modified.",
                    run_key,
                ),
            )
        print("SHADOW_1X2_PREVIEW_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
        return result
    except Exception as exc:
        with psycopg.connect(db, autocommit=True) as conn:
            conn.execute(DDL)
            conn.execute(
                "UPDATE shadow_1x2_preview_runs SET finished_at=NOW(),status='failed',message=%s WHERE run_key=%s",
                (str(exc)[:2000], run_key),
            )
        raise


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
