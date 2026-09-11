#!/usr/bin/env python3
"""Strict Thursday value engine with guarded 1X2 integration.

Binary goal/BTTS/corner behavior remains delegated to v2. A separately validated
1X2 probability engine may add full-time match-result candidates only when official
Turkey prices and a fresh same-book three-way no-vig international reference exist.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg

from model_engine_v1 import predict_match
from one_x_two_engine import from_v1_prediction
from one_x_two_market_audit import ACTIVE_MODE as ONE_X_TWO_ACTIVE_MODE, POLICY_KEY as ONE_X_TWO_POLICY_KEY
from one_x_two_market_reference import latest_ref as latest_1x2_ref, selected_probability as selected_1x2_probability
from production_predictor import canon
from research_change_control import registry_activation_mode
from thursday_decision_engine import (
    DATABASE_URL,
    HIGH_CONFIDENCE_MIN,
    INTERNATIONAL_MAX_MODEL_DIVERGENCE,
    INTERNATIONAL_MIN_TR_EDGE,
    INTERNATIONAL_MIN_TR_EV,
    LIST_LIMIT,
    VALUE_MIN_CONFIDENCE,
    VALUE_MIN_EDGE,
    VALUE_MIN_EV,
    _early_gate,
    _history_rows,
    _last_rest_days,
    _player_context,
    _price_payload,
    market_metrics,
    weekend_bounds,
)
from thursday_decision_engine_v2 import build_decision as build_base, playable_decision_ready


def _public(row: Dict[str, Any], *, include_value: bool) -> Dict[str, Any]:
    price = row["price"]
    ref = row["international"]
    item = {
        "event_id": row["event_id"], "match_date": row["match_date"], "league": row["league"],
        "home": row["home"], "away": row["away"], "market": "match_result", "selection": row["selection"],
        "confidence": row["confidence"], "model_probability_estimate": row["confidence"],
        "confidence_semantics": "selected_1x2_probability_from_frozen_v1_home_away_poisson_lambdas",
        "tr_price": price.get("tr_price"), "tr_opening_price": price.get("tr_opening_price"),
        "tr_source": price.get("tr_source"),
        "international_fair_probability": row["international_selected_probability"],
        "international_fair_odds": (1.0 / row["international_selected_probability"]) if row["international_selected_probability"] else None,
        "international_bookmakers": ref.get("bookmaker_count"),
        "international_dispersion": ref.get("dispersion"),
        "international_sharp_bookmaker": ref.get("sharp_bookmaker"),
        "international_quality": ref.get("quality"),
        "model_market_gap": row["metrics"].get("model_market_gap") if row.get("metrics") else None,
        "early_context": row["early_context"],
        "one_x_two_probabilities": row["one_x_two_probabilities"],
        "one_x_two_registry_mode": ONE_X_TWO_ACTIVE_MODE,
    }
    if include_value and row.get("metrics"):
        metrics = row["metrics"]
        item.update({
            "tr_implied_probability": metrics["tr_implied_probability"],
            "model_edge_vs_tr": metrics["model_edge_vs_tr"],
            "model_ev_vs_tr": metrics["model_ev_vs_tr"],
            "international_edge_vs_tr": metrics["international_edge_vs_tr"],
            "international_ev_vs_tr": metrics["international_ev_vs_tr"],
        })
    return item


def _build_1x2(database_url: str, *, now: Optional[datetime]) -> Dict[str, Any]:
    week_key, start, end = weekend_bounds(now)
    with psycopg.connect(database_url, autocommit=True) as conn:
        mode = registry_activation_mode(conn, ONE_X_TWO_POLICY_KEY)
        if mode != ONE_X_TWO_ACTIVE_MODE:
            return {"active": False, "registry_mode": mode, "candidate_rows": [], "playable": 0, "intl": 0, "aligned": 0}

        fixtures = conn.execute(
            """SELECT event_id,match_date,league_name,home_team,away_team
                 FROM espn_upcoming WHERE is_current=TRUE AND match_date>=%s AND match_date<%s ORDER BY match_date""",
            (start, end),
        ).fetchall()
        histories: Dict[str, List[Dict[str, Any]]] = {}
        rows: List[Dict[str, Any]] = []
        for eid, match_date, league, home, away in fixtures:
            league_s = str(league)
            if league_s not in histories:
                histories[league_s] = _history_rows(conn, league_s, match_date)
            history = histories[league_s]
            if not history:
                continue
            pred = predict_match(history, canon(home), canon(away), recent_matches=18)
            home_ctx, away_ctx = _player_context(conn, str(home)), _player_context(conn, str(away))
            eligible, blockers, gate_diag = _early_gate(
                pred, home_ctx, away_ctx,
                _last_rest_days(history, str(home), match_date),
                _last_rest_days(history, str(away), match_date),
            )
            one = from_v1_prediction(pred)
            probs = {"1": float(one.p1), "0": float(one.px), "2": float(one.p2)}
            ref = latest_1x2_ref(conn, str(eid))
            for selection, model_p in probs.items():
                if model_p < VALUE_MIN_CONFIDENCE and model_p < HIGH_CONFIDENCE_MIN:
                    continue
                price = _price_payload(conn, str(eid), "match_result", selection)
                intl_p = selected_1x2_probability(ref, selection)
                aligned = bool(ref and intl_p is not None and abs(model_p - intl_p) <= INTERNATIONAL_MAX_MODEL_DIVERGENCE)
                metrics = market_metrics(model_p, price["tr_price"], intl_p) if price and intl_p is not None else None
                rows.append({
                    "event_id": str(eid), "match_date": match_date, "league": league_s,
                    "home": str(home), "away": str(away), "selection": selection,
                    "confidence": model_p, "eligible": bool(eligible), "blockers": blockers,
                    "early_context": gate_diag, "price": price, "international": ref,
                    "international_selected_probability": intl_p,
                    "international_aligned": aligned,
                    "alignment_reason": None if aligned else ("international_reference_missing" if intl_p is None else "model_market_divergence_high"),
                    "metrics": metrics,
                    "one_x_two_probabilities": {"1": round(one.p1, 6), "0": round(one.px, 6), "2": round(one.p2, 6)},
                })

        candidates = [r for r in rows if r["eligible"] and r["confidence"] >= VALUE_MIN_CONFIDENCE]
        playable = [r for r in candidates if r["price"]]
        intl_rows = [r for r in playable if r["international"] and r["international_selected_probability"] is not None]
        aligned = [r for r in intl_rows if r["international_aligned"]]
        return {
            "active": True, "registry_mode": mode, "candidate_rows": rows,
            "playable": len(playable), "intl": len(intl_rows), "aligned": len(aligned),
        }


def build_decision(database_url: str = DATABASE_URL, *, now: Optional[datetime] = None, limit: int = LIST_LIMIT) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    base = build_base(database_url, now=now, limit=1000)
    x12 = _build_1x2(database_url, now=now)
    if not x12["active"]:
        out = dict(base)
        out["high_confidence"] = (base.get("high_confidence") or [])[:limit]
        out["high_confidence_value"] = (base.get("high_confidence_value") or [])[:limit]
        diagnostics = dict(out.get("diagnostics") or {})
        policy = dict(diagnostics.get("policy") or {})
        policy.update({
            "one_x_two_policy_key": ONE_X_TWO_POLICY_KEY,
            "one_x_two_registry_mode": x12["registry_mode"],
            "one_x_two_market_active": False,
            "one_x_two_fail_closed": True,
        })
        diagnostics["policy"] = policy
        out["diagnostics"] = diagnostics
        return out

    rows = x12["candidate_rows"]
    high_candidates = [
        r for r in rows
        if r["eligible"] and r["confidence"] >= HIGH_CONFIDENCE_MIN and r["price"] and r["international_aligned"]
    ]
    # Only one 1X2 outcome can be the high-confidence representative for a fixture.
    x12_high_by_fixture: Dict[str, Dict[str, Any]] = {}
    for r in high_candidates:
        cur = x12_high_by_fixture.get(r["event_id"])
        if cur is None or float(r["confidence"]) > float(cur["confidence"]):
            x12_high_by_fixture[r["event_id"]] = r
    x12_high = [_public(r, include_value=False) for r in x12_high_by_fixture.values()]

    value_candidates = []
    for r in rows:
        metrics = r.get("metrics")
        if not (r["eligible"] and r["confidence"] >= VALUE_MIN_CONFIDENCE and r["price"] and r["international_aligned"] and metrics):
            continue
        if (
            metrics["model_edge_vs_tr"] >= VALUE_MIN_EDGE
            and metrics["model_ev_vs_tr"] >= VALUE_MIN_EV
            and metrics["international_edge_vs_tr"] >= INTERNATIONAL_MIN_TR_EDGE
            and metrics["international_ev_vs_tr"] >= INTERNATIONAL_MIN_TR_EV
        ):
            value_candidates.append(r)
    x12_value_by_fixture: Dict[str, Dict[str, Any]] = {}
    for r in value_candidates:
        key = min(float(r["metrics"]["model_ev_vs_tr"]), float(r["metrics"]["international_ev_vs_tr"]))
        cur = x12_value_by_fixture.get(r["event_id"])
        cur_key = min(float(cur["metrics"]["model_ev_vs_tr"]), float(cur["metrics"]["international_ev_vs_tr"])) if cur else None
        if cur is None or key > cur_key:
            x12_value_by_fixture[r["event_id"]] = r
    x12_value = [_public(r, include_value=True) for r in x12_value_by_fixture.values()]

    high_by_fixture: Dict[str, Dict[str, Any]] = {}
    for item in list(base.get("high_confidence") or []) + x12_high:
        eid = str(item.get("event_id"))
        cur = high_by_fixture.get(eid)
        if cur is None or float(item.get("confidence") or 0.0) > float(cur.get("confidence") or 0.0):
            high_by_fixture[eid] = item
    high = list(high_by_fixture.values())
    high.sort(key=lambda x: float(x.get("confidence") or 0.0), reverse=True)

    value_by_fixture: Dict[str, Dict[str, Any]] = {}
    def value_key(item: Dict[str, Any]) -> tuple[float, float]:
        return (
            min(float(item.get("model_ev_vs_tr") or -9.0), float(item.get("international_ev_vs_tr") or -9.0)),
            float(item.get("confidence") or 0.0),
        )
    for item in list(base.get("high_confidence_value") or []) + x12_value:
        eid = str(item.get("event_id"))
        cur = value_by_fixture.get(eid)
        if cur is None or value_key(item) > value_key(cur):
            value_by_fixture[eid] = item
    value = list(value_by_fixture.values())
    value.sort(key=value_key, reverse=True)

    diagnostics = dict(base.get("diagnostics") or {})
    base_candidate = int(diagnostics.get("candidate_market_rows") or base.get("candidate_rows") or 0)
    base_playable = int(diagnostics.get("playable_candidate_rows") or base.get("playable_candidate_rows") or 0)
    base_intl = int(diagnostics.get("international_candidate_rows") or 0)
    combined_playable = base_playable + int(x12["playable"])
    combined_intl = base_intl + int(x12["intl"])
    intl_coverage = combined_intl / combined_playable if combined_playable else 0.0
    decision_ready = playable_decision_ready(
        float(base.get("official_fixture_coverage") or 0.0), combined_playable, intl_coverage
    )
    policy = dict(diagnostics.get("policy") or {})
    policy.update({
        "one_x_two_policy_key": ONE_X_TWO_POLICY_KEY,
        "one_x_two_registry_mode": x12["registry_mode"],
        "one_x_two_market_active": True,
        "one_x_two_fail_closed": True,
        "one_x_two_reference": "three_way_same_book_no_vig",
        "one_x_two_value_thresholds": "same_as_existing_strict_value_engine",
    })
    diagnostics.update({
        "policy": policy,
        "one_x_two_candidate_rows": len(rows),
        "one_x_two_playable_candidate_rows": x12["playable"],
        "one_x_two_international_rows": x12["intl"],
        "one_x_two_aligned_rows": x12["aligned"],
        "combined_candidate_market_rows": base_candidate + len(rows),
        "combined_playable_candidate_rows": combined_playable,
        "combined_international_candidate_rows": combined_intl,
        "combined_international_candidate_coverage": intl_coverage,
    })

    result = dict(base)
    result.update({
        "decision_engine": "playable_market_v3_1x2_guarded",
        "decision_ready": decision_ready,
        "high_confidence": high[:limit],
        "high_confidence_value": value[:limit],
        "candidate_rows": base_candidate + len(rows),
        "playable_candidate_rows": combined_playable,
        "international_candidate_coverage": intl_coverage,
        "diagnostics": diagnostics,
    })
    print("THURSDAY_DECISION_V3_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
    return result


if __name__ == "__main__":
    print(json.dumps(build_decision(), ensure_ascii=False, indent=2, default=str))
