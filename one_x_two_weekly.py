#!/usr/bin/env python3
"""Weekly 1X2 coupon builder using the frozen V1 core and existing safety context.

The probability engine is independent of the regular goal/BTTS/corner Top-10. V2
coupon thresholds may be used only when the policy registry contains a holdout-safe
passed V2 audit with the frozen selected_coupon_policy.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg

from model_engine_v1 import predict_match
from one_x_two_engine import (
    ACTIVE_MODE,
    POLICY_KEY,
    V2_ACTIVE_MODE,
    coupon_selection,
    coupon_selection_with_thresholds,
    from_v1_prediction,
)
from production_predictor import canon
from research_change_control import registry_activation_mode
from schedule_context import team_schedule_context
from thursday_decision_engine import (
    DATABASE_URL,
    _early_gate,
    _history_rows,
    _last_rest_days,
    _player_context,
    weekend_bounds,
)


def _registry_metrics(conn) -> Dict[str, Any]:
    try:
        row = conn.execute(
            "SELECT metrics FROM policy_activation_registry WHERE policy_key=%s",
            (POLICY_KEY,),
        ).fetchone()
    except Exception:
        return {}
    return row[0] if row and isinstance(row[0], dict) else {}


def build(database_url: str = DATABASE_URL, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    as_of = now or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    week_key, start, end = weekend_bounds(as_of)

    with psycopg.connect(database_url, autocommit=True) as conn:
        mode = registry_activation_mode(conn, POLICY_KEY)
        metrics = _registry_metrics(conn)
        v2_policy = metrics.get("selected_coupon_policy") if isinstance(metrics, dict) else None
        v2_ready = mode == V2_ACTIVE_MODE and isinstance(v2_policy, dict)
        active = mode == ACTIVE_MODE or v2_ready

        fixtures = conn.execute(
            """SELECT event_id,match_date,league_name,home_team,away_team
                 FROM espn_upcoming
                WHERE is_current=TRUE AND match_date>=%s AND match_date<%s
                ORDER BY match_date,league_name,home_team""",
            (start, end),
        ).fetchall()

        histories: Dict[str, List[Dict[str, Any]]] = {}
        rows: List[Dict[str, Any]] = []
        exclusions: Dict[str, int] = defaultdict(int)

        for event_id, match_date, league, home, away in fixtures:
            league_s = str(league)
            if league_s not in histories:
                histories[league_s] = _history_rows(conn, league_s, match_date)
            history = histories[league_s]
            if not history:
                exclusions["no_history"] += 1
                continue

            pred = predict_match(history, canon(home), canon(away), recent_matches=18)
            one = from_v1_prediction(pred)
            if v2_ready:
                coupon = coupon_selection_with_thresholds(
                    one,
                    single_min_prob=float(v2_policy["single_min_prob"]),
                    single_min_margin=float(v2_policy["single_min_margin"]),
                    triple_max_top=float(v2_policy["triple_max_top"]),
                    triple_min_bottom=float(v2_policy["triple_min_bottom"]),
                )
            else:
                coupon = coupon_selection(one)

            home_ctx, away_ctx = _player_context(conn, str(home)), _player_context(conn, str(away))
            home_rest_fallback = _last_rest_days(history, str(home), match_date)
            away_rest_fallback = _last_rest_days(history, str(away), match_date)
            home_sched = team_schedule_context(conn, str(home), match_date, as_of=as_of, fallback_rest_days=home_rest_fallback)
            away_sched = team_schedule_context(conn, str(away), match_date, as_of=as_of, fallback_rest_days=away_rest_fallback)
            eligible, blockers, gate_diag = _early_gate(
                pred,
                home_ctx,
                away_ctx,
                home_sched.get("rest_days"),
                away_sched.get("rest_days"),
            )
            schedule_factor = min(float(home_sched.get("rank_factor") or 0.0), float(away_sched.get("rank_factor") or 0.0))
            top_prob = float(coupon["top_probability"])
            quality = float(gate_diag.get("model_data_quality") or pred.data_quality)
            ranking_score = top_prob * (0.75 + 0.25 * quality) * schedule_factor

            rows.append({
                "event_id": str(event_id),
                "match_date": match_date,
                "league": league_s,
                "home": str(home),
                "away": str(away),
                "p1": round(one.p1, 6),
                "p0": round(one.px, 6),
                "p2": round(one.p2, 6),
                "top_outcome": coupon["top_outcome"],
                "top_probability": round(top_prob, 6),
                "selection": coupon["selection"],
                "tier": coupon["tier"],
                "selection_count": coupon["selection_count"],
                "coverage_probability": round(float(coupon["coverage_probability"]), 6),
                "ranking_score": round(ranking_score, 6),
                "data_quality": round(quality, 6),
                "schedule_rank_factor": round(schedule_factor, 6),
                "context_eligible": bool(eligible),
                "context_blockers": blockers,
                "home_schedule": home_sched,
                "away_schedule": away_sched,
            })

        rows.sort(key=lambda r: (-float(r["ranking_score"]), int(r["selection_count"]), -float(r["top_probability"])))
        return {
            "week_key": week_key,
            "generated_at": as_of.isoformat(),
            "status": "active" if active else "research_only",
            "policy_key": POLICY_KEY,
            "registry_mode": mode,
            "active_coupon_policy": v2_policy if v2_ready else None,
            "probability_semantics": "1X2 probabilities derived from frozen V1 home/away Poisson goal intensities",
            "coupon_policy_semantics": (
                "2425-developed, untouched-2526-validated V2 thresholds; 2627 excluded"
                if v2_ready else
                "V1 research thresholds only; no validated 1X2 coupon policy active"
            ),
            "fixture_count": len(fixtures),
            "scored_count": len(rows),
            "exclusions": dict(exclusions),
            "matches": rows,
        }


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2, default=str))
