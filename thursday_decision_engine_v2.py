#!/usr/bin/env python3
"""Playable-market aware Thursday decision engine.

This is deliberately a thin production policy layer over the validated V1 model.
It does NOT change model probabilities. A candidate for a market which official
Turkish İddaa does not actually price is classified as non-executable.

Final selections require:
- the existing V1 eligibility gates;
- an executable official Turkish price;
- a fresh international paired same-book no-vig probability reference;
- model/international probability alignment;
- for value, model edge and EV calculated only from the executable Turkey price.

Raw international prices are never compared with Turkish prices.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

from thursday_decision_engine import (
    DATABASE_URL,
    HIGH_CONFIDENCE_MIN,
    VALUE_MIN_CONFIDENCE,
    VALUE_MIN_EDGE,
    VALUE_MIN_EV,
    CANDIDATE_COVERAGE_MIN,
    NO_CANDIDATE_BULLETIN_COVERAGE_MIN,
    LIST_LIMIT,
    MARKETS,
    DDL,
    TURKEY_PRICE_DDL,
    json_default,
    weekend_bounds,
    market_metrics,
    _history_rows,
    _last_rest_days,
    _player_context,
    _early_gate,
    _latest_import_coverage,
    _price_payload,
    _market_alignment,
    _one_per_fixture,
    _public_item,
)
from model_engine_v1 import predict_match
from production_predictor import canon
from international_market_reference import latest_ref

PLAYABLE_BULLETIN_COVERAGE_MIN = float(
    os.getenv("THURSDAY_PLAYABLE_BULLETIN_COVERAGE_MIN", "0.90")
)


def playable_decision_ready(
    official_fixture_coverage: float,
    playable_candidate_count: int,
    international_candidate_coverage: float,
) -> bool:
    """Readiness based on executable markets and probability-reference coverage."""
    if float(official_fixture_coverage or 0.0) < PLAYABLE_BULLETIN_COVERAGE_MIN:
        return False
    if int(playable_candidate_count or 0) <= 0:
        return True
    return float(international_candidate_coverage or 0.0) >= CANDIDATE_COVERAGE_MIN


def _preview(row: Dict[str, Any]) -> Dict[str, Any]:
    price = row.get("price") or {}
    ref = row.get("international") or {}
    return {
        "event_id": row["event_id"],
        "match_date": row["match_date"],
        "league": row["league"],
        "home": row["home"],
        "away": row["away"],
        "market": row["market"],
        "selection": row["selection"],
        "model_probability_estimate": row["confidence"],
        "tr_price": price.get("tr_price"),
        "tr_opening_price": price.get("tr_opening_price"),
        "has_turkey_price": bool(price),
        "has_international_reference": bool(ref),
        "international_fair_probability": ref.get("reference_p_yes"),
        "international_aligned": bool(row.get("international_aligned")),
        "alignment_reason": row.get("alignment_reason"),
    }


def build_decision(
    database_url: str = DATABASE_URL,
    *,
    now: Optional[datetime] = None,
    limit: int = LIST_LIMIT,
) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")

    week_key, start, end = weekend_bounds(now)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(TURKEY_PRICE_DDL)
        conn.execute(DDL)
        run_id = int(
            conn.execute(
                "INSERT INTO thursday_decision_runs(week_key,horizon_start,horizon_end) VALUES(%s,%s,%s) RETURNING id",
                (week_key, start, end),
            ).fetchone()[0]
        )
        try:
            fixtures = conn.execute(
                """SELECT event_id,match_date,league_name,home_team,away_team
                     FROM espn_upcoming
                    WHERE is_current=TRUE AND match_date>=%s AND match_date<%s
                    ORDER BY match_date""",
                (start, end),
            ).fetchall()

            histories: Dict[str, List[Dict[str, Any]]] = {}
            market_rows: List[Dict[str, Any]] = []
            excluded_counts: Dict[str, int] = defaultdict(int)

            for eid, match_date, league, home, away in fixtures:
                league_s = str(league)
                if league_s not in histories:
                    histories[league_s] = _history_rows(conn, league_s, match_date)
                history = histories[league_s]
                if not history:
                    excluded_counts["no_history"] += 1
                    continue

                pred = predict_match(history, canon(home), canon(away), recent_matches=18)
                home_ctx = _player_context(conn, str(home))
                away_ctx = _player_context(conn, str(away))
                home_rest = _last_rest_days(history, str(home), match_date)
                away_rest = _last_rest_days(history, str(away), match_date)
                eligible, blockers, gate_diag = _early_gate(
                    pred, home_ctx, away_ctx, home_rest, away_rest
                )
                for blocker in blockers:
                    excluded_counts[blocker] += 1

                probs = {
                    "p_over_2_5": float(pred.p_over_2_5),
                    "p_btts": float(pred.p_btts),
                    "p_corners_over_8_5": float(pred.p_corners_over_8_5),
                }
                for market, selection, attr in MARKETS:
                    model_p = probs[attr]
                    price = _price_payload(conn, str(eid), market, selection)
                    ref = latest_ref(conn, str(eid), market)
                    aligned, alignment_reason = _market_alignment(model_p, ref)
                    metrics = (
                        market_metrics(model_p, price["tr_price"], ref["reference_p_yes"])
                        if price and ref
                        else None
                    )
                    market_rows.append(
                        {
                            "event_id": str(eid),
                            "match_date": match_date,
                            "league": league_s,
                            "home": str(home),
                            "away": str(away),
                            "market": market,
                            "selection": selection,
                            "confidence": model_p,
                            "eligible": bool(eligible),
                            "blockers": blockers,
                            "early_context": gate_diag,
                            "price": price,
                            "international": ref,
                            "international_aligned": aligned,
                            "alignment_reason": alignment_reason,
                            "metrics": metrics,
                        }
                    )

            candidate_rows = [
                r
                for r in market_rows
                if r["eligible"] and r["confidence"] >= VALUE_MIN_CONFIDENCE
            ]
            playable_candidate_rows = [r for r in candidate_rows if r["price"]]
            intl_candidate_rows = [r for r in playable_candidate_rows if r["international"]]
            aligned_candidate_rows = [
                r for r in intl_candidate_rows if r["international_aligned"]
            ]

            raw_high = [
                r
                for r in market_rows
                if r["eligible"] and r["confidence"] >= HIGH_CONFIDENCE_MIN
            ]
            priced_high = [r for r in raw_high if r["price"]]
            verified_high = [
                r for r in priced_high if r["international"] and r["international_aligned"]
            ]

            raw_high_fixtures = _one_per_fixture(raw_high, lambda r: (r["confidence"],))
            priced_high_fixtures = _one_per_fixture(priced_high, lambda r: (r["confidence"],))
            verified_high_fixtures = _one_per_fixture(verified_high, lambda r: (r["confidence"],))
            verified_high_fixtures.sort(key=lambda r: r["confidence"], reverse=True)
            high_list = [
                _public_item(r, include_value=False)
                for r in verified_high_fixtures[:limit]
            ]

            value_candidates: List[Dict[str, Any]] = []
            for r in aligned_candidate_rows:
                metrics = r.get("metrics")
                if not metrics:
                    continue
                if (
                    metrics["model_edge_vs_tr"] >= VALUE_MIN_EDGE
                    and metrics["model_ev_vs_tr"] >= VALUE_MIN_EV
                ):
                    value_candidates.append(r)

            value_rows = _one_per_fixture(
                value_candidates,
                lambda r: (
                    r["metrics"]["model_ev_vs_tr"],
                    r["confidence"],
                    r["metrics"]["model_edge_vs_tr"],
                ),
            )
            value_rows.sort(
                key=lambda r: (
                    r["metrics"]["model_ev_vs_tr"],
                    r["confidence"],
                    r["metrics"]["model_edge_vs_tr"],
                ),
                reverse=True,
            )
            value_list = [_public_item(r, include_value=True) for r in value_rows[:limit]]

            high_keys = {(x["event_id"], x["market"]) for x in high_list}
            value_keys = {(x["event_id"], x["market"]) for x in value_list}
            for item in high_list:
                item["also_value"] = (item["event_id"], item["market"]) in value_keys
            for item in value_list:
                item["also_high_confidence"] = (item["event_id"], item["market"]) in high_keys

            coverage = _latest_import_coverage(conn, len(fixtures))
            official_coverage = float(coverage.get("fixture_coverage") or 0.0)
            legacy_tr_candidate_coverage = (
                len(playable_candidate_rows) / len(candidate_rows) if candidate_rows else 0.0
            )
            international_candidate_coverage = (
                len(intl_candidate_rows) / len(playable_candidate_rows)
                if playable_candidate_rows else 0.0
            )
            aligned_candidate_coverage = (
                len(aligned_candidate_rows) / len(playable_candidate_rows)
                if playable_candidate_rows else 0.0
            )

            decision_ready = playable_decision_ready(
                official_coverage,
                len(playable_candidate_rows),
                international_candidate_coverage,
            )

            alignment_rejections = defaultdict(int)
            for r in playable_candidate_rows:
                if r.get("alignment_reason"):
                    alignment_rejections[r["alignment_reason"]] += 1

            offered_market_counts = {
                market: sum(1 for r in market_rows if r["market"] == market and r["price"])
                for market, _, _ in MARKETS
            }
            candidate_preview = [
                _preview(r)
                for r in sorted(candidate_rows, key=lambda x: x["confidence"], reverse=True)
            ]
            raw_high_preview = [
                _preview(r)
                for r in sorted(raw_high_fixtures, key=lambda x: x["confidence"], reverse=True)
            ]

            diagnostics = {
                "policy": {
                    "markets": [m[0] for m in MARKETS],
                    "high_confidence_min": HIGH_CONFIDENCE_MIN,
                    "value_min_confidence": VALUE_MIN_CONFIDENCE,
                    "model_edge_vs_tr_min": VALUE_MIN_EDGE,
                    "model_ev_vs_tr_min": VALUE_MIN_EV,
                    "candidate_coverage_min": CANDIDATE_COVERAGE_MIN,
                    "playable_bulletin_coverage_min": PLAYABLE_BULLETIN_COVERAGE_MIN,
                    "legacy_no_candidate_bulletin_coverage_min": NO_CANDIDATE_BULLETIN_COVERAGE_MIN,
                    "candidate_universe": "official_turkey_priced_markets_only",
                    "model_source": "validated_v1_probability_estimate",
                    "model_probability_semantics": "not_assumed_perfectly_calibrated; international market remains mandatory probability sanity filter",
                    "international_role": "paired_same-book_no-vig_probability_sanity_only",
                    "international_vs_turkey_price_comparison": "disabled_all_markets",
                    "value_role": "model_probability_x_turkey_executable_price_only",
                    "executable_price_source": "iddaa_official_turkey",
                    "confirmed_lineup_required": False,
                    "t_minus_reselection": False,
                },
                "excluded_counts": dict(excluded_counts),
                "alignment_rejections": dict(alignment_rejections),
                "turkey_import": coverage,
                "offered_market_counts": offered_market_counts,
                "candidate_market_rows": len(candidate_rows),
                "playable_candidate_rows": len(playable_candidate_rows),
                "unpriced_candidate_rows": len(candidate_rows) - len(playable_candidate_rows),
                "international_candidate_rows": len(intl_candidate_rows),
                "aligned_candidate_rows": len(aligned_candidate_rows),
                "legacy_tr_candidate_coverage": legacy_tr_candidate_coverage,
                "international_candidate_coverage": international_candidate_coverage,
                "aligned_candidate_coverage": aligned_candidate_coverage,
                "raw_high_market_candidates": len(raw_high),
                "priced_high_market_candidates": len(priced_high),
                "verified_high_market_candidates": len(verified_high),
                "raw_high_fixture_candidates": len(raw_high_fixtures),
                "priced_high_fixture_candidates": len(priced_high_fixtures),
                "verified_high_fixture_candidates": len(verified_high_fixtures),
                "candidate_preview": candidate_preview,
                "raw_high_preview": raw_high_preview,
            }

            result = {
                "status": "success",
                "decision_engine": "playable_market_v2",
                "decision_run_id": run_id,
                "week_key": week_key,
                "horizon_start": start,
                "horizon_end": end,
                "fixture_count": len(fixtures),
                "model_market_rows": len(market_rows),
                "eligible_market_rows": sum(1 for r in market_rows if r["eligible"]),
                "official_events": int(coverage.get("official_events") or 0),
                "matched_fixtures": int(coverage.get("matched_fixtures") or 0),
                "official_fixture_coverage": official_coverage,
                "raw_high_candidates": len(raw_high_fixtures),
                "priced_high_candidates": len(priced_high_fixtures),
                "verified_high_candidates": len(verified_high_fixtures),
                "candidate_rows": len(candidate_rows),
                "playable_candidate_rows": len(playable_candidate_rows),
                "tr_candidate_coverage": legacy_tr_candidate_coverage,
                "international_candidate_coverage": international_candidate_coverage,
                "decision_ready": decision_ready,
                "high_confidence": high_list,
                "high_confidence_value": value_list,
                "candidate_preview": candidate_preview,
                "raw_high_preview": raw_high_preview,
                "diagnostics": diagnostics,
                "generated_at": datetime.now().astimezone(),
            }

            conn.execute(
                """UPDATE thursday_decision_runs SET finished_at=NOW(),status='success',fixture_count=%s,
                   model_market_rows=%s,eligible_market_rows=%s,official_events=%s,matched_fixtures=%s,
                   official_fixture_coverage=%s,raw_high_candidates=%s,priced_high_candidates=%s,
                   high_confidence_count=%s,value_count=%s,decision_ready=%s,high_confidence=%s,
                   high_confidence_value=%s,diagnostics=%s,message=%s WHERE id=%s""",
                (
                    len(fixtures),
                    len(market_rows),
                    sum(1 for r in market_rows if r["eligible"]),
                    int(coverage.get("official_events") or 0),
                    int(coverage.get("matched_fixtures") or 0),
                    official_coverage,
                    len(raw_high_fixtures),
                    len(priced_high_fixtures),
                    len(high_list),
                    len(value_list),
                    decision_ready,
                    Jsonb(high_list, dumps=lambda x: json.dumps(x, default=json_default, ensure_ascii=False)),
                    Jsonb(value_list, dumps=lambda x: json.dumps(x, default=json_default, ensure_ascii=False)),
                    Jsonb(diagnostics, dumps=lambda x: json.dumps(x, default=json_default, ensure_ascii=False)),
                    "playable-market-v2: model+international probability sanity+Turkey executable EV",
                    run_id,
                ),
            )
            print(
                "THURSDAY_DECISION_RESULT",
                json.dumps(result, ensure_ascii=False, default=json_default, separators=(",", ":")),
                flush=True,
            )
            return result
        except Exception as exc:
            conn.execute(
                "UPDATE thursday_decision_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                (str(exc)[:1200], run_id),
            )
            raise


if __name__ == "__main__":
    print(json.dumps(build_decision(), ensure_ascii=False, indent=2, default=json_default))
