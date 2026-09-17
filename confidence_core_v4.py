#!/usr/bin/env python3
"""Evidence-complete six-layer weekly football decision engine (V4).

This is the production-facing decision layer requested after the first V3 live run.
It keeps reliability and value as separate products and does not tune any weight or
threshold on 2026/27 outcomes.

Six layers used for Confidence Core4:
1) conservative market-anchored probability consensus + uncertainty floor;
2) opponent-adjusted xG/xGA attack/defence model from past-only Understat rows;
3) expected-XI / injury / goalkeeper adjustments applied directly to goal lambdas;
4) opponent-adjusted realised-goal attack/defence strength;
5) fresh multi-book same-market no-vig international consensus;
6) past-only Dixon-Coles low-score dependence correction.

A Confidence Core4 row must actually have all evidence layers available. Missing xG
or multi-book coverage does not get silently treated as zero risk. Such rows may be
shown only as fallbacks/value candidates, never as a high-confidence pick.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import psycopg

from advanced_goal_models import (
    fit_dc_rho,
    fit_opponent_strengths,
    opponent_adjusted_lambdas,
    opponent_adjusted_probabilities,
)
from confidence_core_v3 import (
    _binary_ref,
    _clip,
    _day,
    _diversify,
    _enrich_xg,
    _injury_covered,
    _is_multi_book,
    _lineup_lambdas,
    _player_context,
    _price_metrics,
    _schedule_factor,
    _score_probs,
)
from international_market_reference import latest_ref
from model_engine_v1 import predict_match as predict_match_v1
from one_x_two_market_reference import (
    latest_ref as latest_1x2_ref,
    selected_probability as selected_1x2_probability,
)
from production_predictor import canon
from schedule_context import SCHEDULE_CONTEXT_VERSION, team_schedule_context
from thursday_decision_engine import (
    DATABASE_URL,
    LIST_LIMIT,
    TURKEY_PRICE_DDL,
    _history_rows,
    _last_rest_days,
    _latest_import_coverage,
    _price_payload,
    weekend_bounds,
)

POLICY_VERSION = "confidence-core-v4-evidence-complete-2026-09-17"
CORE4_SIZE = 4
MIN_BINARY_MODEL = 0.54
MIN_1X2_MODEL = 0.38
MIN_EVIDENCE = 0.64
MIN_PLAYER_COVERAGE = 0.45
MAX_MODEL_MARKET_GAP = 0.10
MIN_DIRECTION_AGREEMENT = 0.75
VALUE_MIN_EV = 0.02
VALUE_MIN_EVIDENCE = 0.55
MAX_SAME_SELECTION_IN_CORE = 2
MARKET_WEIGHT = 0.55
MODEL_WEIGHT = 0.45
MODEL_SPREAD_PENALTY = 0.30
MARKET_DISPERSION_PENALTY = 0.45
EVIDENCE_PENALTY = 0.06
MIN_XG_MODEL_MATCHES = 35
MIN_MODEL_FAMILIES_FOR_CONFIDENCE = 4


def _xg_strength_model(xg_history: Sequence[Mapping[str, Any]]):
    """Fit the existing opponent-strength machinery to xG instead of goals.

    This gives a distinct chance-quality family: expected scoring/conceding strength
    is adjusted for opponent quality before being converted to match lambdas. It is
    deliberately past-only and does not learn weights from the live 2026/27 results.
    """
    pseudo: List[Dict[str, Any]] = []
    for raw in xg_history:
        hx, ax = raw.get("home_xg"), raw.get("away_xg")
        if hx is None or ax is None:
            continue
        row = dict(raw)
        try:
            row["home_goals"] = float(hx)
            row["away_goals"] = float(ax)
        except (TypeError, ValueError):
            continue
        pseudo.append(row)
    if len(pseudo) < MIN_XG_MODEL_MATCHES:
        return None, len(pseudo)
    try:
        return fit_opponent_strengths(pseudo), len(pseudo)
    except Exception:
        return None, len(pseudo)


def _evidence(
    pred_quality: float,
    xg_available: bool,
    xg_coverage: float,
    hctx: Mapping[str, Any],
    actx: Mapping[str, Any],
    ref: Optional[Mapping[str, Any]],
    injury_complete: bool,
    hs: Mapping[str, Any],
    ass: Mapping[str, Any],
) -> Tuple[float, Dict[str, float]]:
    sample = _clip(float(pred_quality or 0.0), 0.0, 1.0)
    xg = _clip((1.0 if xg_available else 0.0) * (0.60 + 0.40 * _clip(xg_coverage, 0.0, 1.0)), 0.0, 1.0)
    player = (
        _clip(float(hctx.get("player_coverage") or 0.0), 0.0, 1.0)
        + _clip(float(actx.get("player_coverage") or 0.0), 0.0, 1.0)
    ) / 2.0
    books = int((ref or {}).get("bookmaker_count") or 0)
    market = _clip(books / 4.0, 0.0, 1.0)
    if ref and "multi_book" in str(ref.get("quality") or ""):
        market = max(market, 0.80)
    sched = 0.0
    if hs.get("rest_days") is not None and ass.get("rest_days") is not None:
        sched += 0.45
    if hs.get("scope") == "all_competitions" and ass.get("scope") == "all_competitions":
        sched += 0.30
    if injury_complete:
        sched += 0.25
    sched = _clip(sched, 0.0, 1.0)
    score = _clip(0.23 * sample + 0.22 * xg + 0.20 * player + 0.20 * market + 0.15 * sched, 0.0, 1.0)
    return score, {
        "sample": sample,
        "opponent_adjusted_xg": xg,
        "player": player,
        "market": market,
        "schedule_injury": sched,
    }


def _consensus_probability(
    model_probs: Sequence[float],
    market_p: Optional[float],
    evidence: float,
    market_dispersion: Optional[float],
) -> Dict[str, float]:
    """Produce a conservative decision probability and uncertainty floor.

    This is intentionally labelled a consensus probability rather than pretending to
    be a statistically perfect calibrated probability. The international no-vig
    market gets a larger weight because our historical O/U audit beat V1 on Brier.
    The lower bound explicitly subtracts model disagreement, bookmaker dispersion,
    and incomplete evidence.
    """
    vals = [float(x) for x in model_probs if x is not None and 0.0 <= float(x) <= 1.0]
    if not vals:
        return {"model_median": 0.5, "calibrated": 0.5, "lower": 0.0, "spread": 1.0}
    median = statistics.median(vals)
    model_spread = max(vals) - min(vals) if len(vals) > 1 else 0.0
    dispersion = _clip(float(market_dispersion or 0.0), 0.0, 0.25)
    if market_p is not None:
        mp = float(market_p)
        calibrated = MARKET_WEIGHT * mp + MODEL_WEIGHT * median
        conservative_anchor = min(calibrated, median, mp)
        total_spread = max(vals + [mp]) - min(vals + [mp])
    else:
        calibrated = median
        conservative_anchor = median
        total_spread = model_spread
    uncertainty = (
        MODEL_SPREAD_PENALTY * total_spread
        + MARKET_DISPERSION_PENALTY * dispersion
        + EVIDENCE_PENALTY * (1.0 - _clip(evidence, 0.0, 1.0))
    )
    lower = conservative_anchor - uncertainty
    return {
        "model_median": _clip(median, 0.0, 1.0),
        "calibrated": _clip(calibrated, 0.0, 1.0),
        "lower": _clip(lower, 0.0, 1.0),
        "spread": _clip(total_spread, 0.0, 1.0),
        "uncertainty_penalty": _clip(uncertainty, 0.0, 1.0),
    }


def _public(r: Mapping[str, Any], rank: int, tier: str) -> Dict[str, Any]:
    return {
        "rank": rank,
        "list_tier": tier,
        "event_id": r["event_id"],
        "match_date": r["match_date"],
        "league": r["league"],
        "home": r["home"],
        "away": r["away"],
        "market": r["market"],
        "selection": r["selection"],
        "tr_price": r["tr_price"],
        "tr_opening_price": r.get("tr_opening_price"),
        "tr_source": r.get("tr_source"),
        "model_probability_estimate": r["model_median"],
        "confidence": r["model_median"],
        "consensus_probability": r["calibrated"],
        "calibrated_probability": r["calibrated"],
        "confidence_lower_bound": r["lower"],
        "uncertainty_penalty": r.get("uncertainty_penalty"),
        "model_probability_spread": r["spread"],
        "direction_agreement": r["agreement"],
        "model_families": r.get("model_families") or [],
        "model_family_count": len(r.get("model_families") or []),
        "evidence_quality": r["evidence"],
        "evidence_components": r["components"],
        "international_fair_probability": r.get("market_p"),
        "international_quality": r.get("market_quality"),
        "international_bookmakers": r.get("books"),
        "international_dispersion": r.get("dispersion"),
        "model_market_gap": r.get("market_gap"),
        "model_edge_vs_tr": r.get("edge"),
        "model_ev_vs_tr": r.get("ev"),
        "value_ev_calibrated": r.get("ev"),
        "data_quality": r.get("data_quality"),
        "xg_used": r.get("xg_available"),
        "xg_history_coverage": r.get("xg_coverage"),
        "xg_model_matches": r.get("xg_model_matches"),
        "opponent_model_available": r.get("opp_available"),
        "dixon_coles_available": r.get("dc_available"),
        "dixon_coles_rho": r.get("rho"),
        "lineup_adjustment": r.get("lineup_diag"),
        "player_coverage": r.get("player_coverage"),
        "schedule_rank_factor": r.get("schedule_factor"),
        "injury_feed_complete": r.get("injury_complete"),
        "confidence_semantics": "multi-book market anchored four-model consensus with conservative uncertainty floor; not a guarantee",
        "policy_version": POLICY_VERSION,
    }


def _best_per_fixture(rows: Sequence[Dict[str, Any]], key_fields: Tuple[str, ...]) -> List[Dict[str, Any]]:
    best: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cur = best.get(row["event_id"])
        k = tuple(float(row.get(x) or -9.0) for x in key_fields)
        ck = tuple(float(cur.get(x) or -9.0) for x in key_fields) if cur else None
        if cur is None or k > ck:
            best[row["event_id"]] = row
    return list(best.values())


def build(
    database_url: str = DATABASE_URL,
    *,
    now: Optional[datetime] = None,
    limit: int = LIST_LIMIT,
    strict_picks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    as_of = now or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    week_key, start, end = weekend_bounds(as_of)

    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(TURKEY_PRICE_DDL)
        injury_covered = _injury_covered(conn)
        fixtures = conn.execute(
            """SELECT event_id,match_date,league_name,home_team,away_team
                 FROM espn_upcoming
                WHERE is_current=TRUE AND match_date>=%s AND match_date<%s
                ORDER BY match_date""",
            (start, end),
        ).fetchall()

        histories: Dict[Tuple[str, date], List[Dict[str, Any]]] = {}
        xg_histories: Dict[Tuple[str, date], Tuple[List[Dict[str, Any]], int]] = {}
        goal_opp_models: Dict[Tuple[str, date], Any] = {}
        xg_opp_models: Dict[Tuple[str, date], Tuple[Any, int]] = {}
        dc_models: Dict[Tuple[str, date], Tuple[float, bool, Dict[str, Any]]] = {}
        rows: List[Dict[str, Any]] = []
        excluded: Dict[str, int] = defaultdict(int)
        xg_active_fixtures = multibook_binary_rows = multibook_1x2_rows = 0

        for eid, match_date, league, home, away in fixtures:
            league, home, away = str(league), str(home), str(away)
            key = (league, match_date.date())
            histories.setdefault(key, _history_rows(conn, league, match_date))
            history = histories[key]
            if not history:
                excluded["no_history"] += 1
                continue
            try:
                v1 = predict_match_v1(history, canon(home), canon(away), recent_matches=18)
            except Exception:
                excluded["v1_failed"] += 1
                continue

            if key not in xg_histories:
                xg_histories[key] = _enrich_xg(conn, league, match_date, history)
            xgh, xg_rows = xg_histories[key]
            if key not in xg_opp_models:
                xg_opp_models[key] = _xg_strength_model(xgh)
            xg_model, xg_model_matches = xg_opp_models[key]
            try:
                xg_lh, xg_la, xg_available = (
                    opponent_adjusted_lambdas(xg_model, home, away) if xg_model else (0.0, 0.0, False)
                )
            except Exception:
                xg_lh, xg_la, xg_available = 0.0, 0.0, False
            if xg_available:
                xg_active_fixtures += 1

            if key not in goal_opp_models:
                try:
                    goal_opp_models[key] = fit_opponent_strengths(history)
                except Exception:
                    goal_opp_models[key] = None
            goal_opp_model = goal_opp_models[key]
            try:
                opp = opponent_adjusted_probabilities(v1, goal_opp_model, home, away) if goal_opp_model else None
            except Exception:
                opp = None

            if key not in dc_models:
                try:
                    dc_models[key] = fit_dc_rho(history)
                except Exception:
                    dc_models[key] = (0.0, False, {})
            rho, dc_available, _dc_diag = dc_models[key]

            hctx, actx = _player_context(conn, home), _player_context(conn, away)
            hrest, arest = _last_rest_days(history, home, match_date), _last_rest_days(history, away, match_date)
            hs = team_schedule_context(conn, home, match_date, as_of=as_of, fallback_rest_days=hrest)
            ass = team_schedule_context(conn, away, match_date, as_of=as_of, fallback_rest_days=arest)
            if hs.get("pending_pre_fixture_match") or ass.get("pending_pre_fixture_match"):
                excluded["pending_intervening_match"] += 1
                continue
            injury_complete = canon(home) in injury_covered and canon(away) in injury_covered
            player_coverage = (
                _clip(float(hctx.get("player_coverage") or 0.0), 0.0, 1.0)
                + _clip(float(actx.get("player_coverage") or 0.0), 0.0, 1.0)
            ) / 2.0
            sched_factor = min(_schedule_factor(hs), _schedule_factor(ass))

            vlh, vla, lineup_diag = _lineup_lambdas(v1.lambda_home_goals, v1.lambda_away_goals, hctx, actx)
            variants: Dict[str, Dict[str, float]] = {"v1": _score_probs(vlh, vla)}
            if xg_available:
                xlh, xla, _ = _lineup_lambdas(xg_lh, xg_la, hctx, actx)
                variants["opponent_adjusted_xg"] = _score_probs(xlh, xla)
            opp_available = bool(opp and opp.get("available"))
            if opp_available:
                olh, ola, _ = _lineup_lambdas(float(opp["lambda_home_goals"]), float(opp["lambda_away_goals"]), hctx, actx)
                variants["opponent_adjusted_goals"] = _score_probs(olh, ola)
            if dc_available:
                variants["dixon_coles"] = _score_probs(vlh, vla, rho)
            model_families = list(variants)
            xg_cov = min(1.0, xg_rows / max(1, len(history)))

            def common(ref: Optional[Mapping[str, Any]]) -> Tuple[float, Dict[str, float]]:
                return _evidence(v1.data_quality, bool(xg_available), xg_cov, hctx, actx, ref, injury_complete, hs, ass)

            for market, pkey, yes_label, no_label in (
                ("over_2_5", "p_over_2_5", "2.5 ÜST", "2.5 ALT"),
                ("btts", "p_btts", "KG VAR", "KG YOK"),
            ):
                yvals = [float(v[pkey]) for v in variants.values()]
                median_yes = statistics.median(yvals)
                yes = median_yes >= 0.5
                selection = yes_label if yes else no_label
                model_p = median_yes if yes else 1.0 - median_yes
                price = _price_payload(conn, str(eid), market, selection)
                if not price:
                    excluded["turkey_price_missing"] += 1
                    continue
                ref = latest_ref(conn, str(eid), market)
                if _is_multi_book(ref):
                    multibook_binary_rows += 1
                market_p = _binary_ref(ref, yes)
                evidence, components = common(ref)
                selected = [p if yes else 1.0 - p for p in yvals]
                cal = _consensus_probability(selected, market_p, evidence, (ref or {}).get("dispersion"))
                agreement = sum(1 for p in yvals if (p >= 0.5) == yes) / len(yvals)
                edge, ev = _price_metrics(cal["calibrated"], price["tr_price"])
                gap = abs(cal["model_median"] - market_p) if market_p is not None else None
                row = {
                    "event_id": str(eid), "match_date": match_date, "league": league,
                    "home": home, "away": away, "market": market, "selection": selection,
                    "tr_price": float(price["tr_price"]), "tr_opening_price": price.get("tr_opening_price"),
                    "tr_source": price.get("tr_source"), **cal, "agreement": agreement,
                    "evidence": evidence, "components": components, "market_p": market_p,
                    "market_quality": (ref or {}).get("quality"), "books": (ref or {}).get("bookmaker_count"),
                    "dispersion": (ref or {}).get("dispersion"), "market_gap": gap, "edge": edge, "ev": ev,
                    "data_quality": float(v1.data_quality), "xg_available": bool(xg_available),
                    "xg_coverage": xg_cov, "xg_model_matches": xg_model_matches,
                    "opp_available": opp_available, "dc_available": bool(dc_available),
                    "rho": float(rho) if dc_available else None, "lineup_diag": lineup_diag,
                    "player_coverage": player_coverage, "schedule_factor": sched_factor,
                    "injury_complete": injury_complete, "model_families": model_families,
                }
                row["trust"] = bool(
                    model_p >= MIN_BINARY_MODEL
                    and bool(xg_available)
                    and _is_multi_book(ref)
                    and evidence >= MIN_EVIDENCE
                    and player_coverage >= MIN_PLAYER_COVERAGE
                    and agreement >= MIN_DIRECTION_AGREEMENT
                    and gap is not None and gap <= MAX_MODEL_MARKET_GAP
                    and len(model_families) >= MIN_MODEL_FAMILIES_FOR_CONFIDENCE
                )
                # Value is separate: a sharp single-book reference may inform value,
                # but it can never promote a row into Confidence Core4.
                row["value"] = bool(
                    evidence >= VALUE_MIN_EVIDENCE and market_p is not None
                    and ev >= VALUE_MIN_EV and agreement >= 0.50
                )
                rows.append(row)

            omap = {"1": "p_home", "0": "p_draw", "2": "p_away"}
            med = {s: statistics.median([float(v[k]) for v in variants.values()]) for s, k in omap.items()}
            sel = max(med, key=med.get)
            model_p = float(med[sel])
            price = _price_payload(conn, str(eid), "match_result", sel)
            if price:
                ref = latest_1x2_ref(conn, str(eid))
                if _is_multi_book(ref, True):
                    multibook_1x2_rows += 1
                market_p = selected_1x2_probability(ref, sel)
                selected = [float(v[omap[sel]]) for v in variants.values()]
                picks = [max(omap, key=lambda s: float(v[omap[s]])) for v in variants.values()]
                evidence, components = common(ref)
                cal = _consensus_probability(selected, market_p, evidence, (ref or {}).get("dispersion"))
                agreement = sum(1 for p in picks if p == sel) / len(picks)
                edge, ev = _price_metrics(cal["calibrated"], price["tr_price"])
                gap = abs(cal["model_median"] - market_p) if market_p is not None else None
                row = {
                    "event_id": str(eid), "match_date": match_date, "league": league,
                    "home": home, "away": away, "market": "match_result", "selection": sel,
                    "tr_price": float(price["tr_price"]), "tr_opening_price": price.get("tr_opening_price"),
                    "tr_source": price.get("tr_source"), **cal, "agreement": agreement,
                    "evidence": evidence, "components": components, "market_p": market_p,
                    "market_quality": (ref or {}).get("quality"), "books": (ref or {}).get("bookmaker_count"),
                    "dispersion": (ref or {}).get("dispersion"), "market_gap": gap, "edge": edge, "ev": ev,
                    "data_quality": float(v1.data_quality), "xg_available": bool(xg_available),
                    "xg_coverage": xg_cov, "xg_model_matches": xg_model_matches,
                    "opp_available": opp_available, "dc_available": bool(dc_available),
                    "rho": float(rho) if dc_available else None, "lineup_diag": lineup_diag,
                    "player_coverage": player_coverage, "schedule_factor": sched_factor,
                    "injury_complete": injury_complete, "model_families": model_families,
                }
                row["trust"] = bool(
                    model_p >= MIN_1X2_MODEL
                    and bool(xg_available)
                    and _is_multi_book(ref, True)
                    and evidence >= MIN_EVIDENCE
                    and player_coverage >= MIN_PLAYER_COVERAGE
                    and agreement >= MIN_DIRECTION_AGREEMENT
                    and gap is not None and gap <= MAX_MODEL_MARKET_GAP
                    and len(model_families) >= MIN_MODEL_FAMILIES_FOR_CONFIDENCE
                )
                row["value"] = bool(
                    evidence >= VALUE_MIN_EVIDENCE and market_p is not None
                    and ev >= VALUE_MIN_EV and agreement >= 0.50
                )
                rows.append(row)
            else:
                excluded["turkey_1x2_price_missing"] += 1

        trust_rows = [r for r in rows if r.get("trust")]
        value_rows = [r for r in rows if r.get("value")]
        trust = _best_per_fixture(trust_rows, ("lower", "evidence", "agreement", "calibrated"))
        trust.sort(key=lambda r: (r["lower"], r["evidence"], r["agreement"], r["calibrated"]), reverse=True)
        core = _diversify(trust, CORE4_SIZE)
        values = _best_per_fixture(value_rows, ("ev", "evidence", "lower"))
        values.sort(key=lambda r: (r["ev"], r["evidence"], r["lower"]), reverse=True)

        core_public = [_public(r, i + 1, "confidence_core4") for i, r in enumerate(core)]
        trust_public = [_public(r, i + 1, "confidence_ranked") for i, r in enumerate(trust[:limit])]
        value_public = [_public(r, i + 1, "value") for i, r in enumerate(values[:limit])]

        used = {r["event_id"] for r in core}
        fallback_pool = _best_per_fixture(
            [r for r in rows if r["event_id"] not in used],
            ("lower", "evidence", "agreement"),
        )
        fallback_pool.sort(key=lambda r: (r["lower"], r["evidence"], r["agreement"]), reverse=True)
        fallback_public = [
            _public(r, i + 1, "evidence_fallback")
            for i, r in enumerate(fallback_pool[:max(4, CORE4_SIZE - len(core))])
        ]

        coverage = _latest_import_coverage(conn, len(fixtures))
        official = float(coverage.get("fixture_coverage") or 0.0)
        ready = bool(official >= 0.90 and len(core_public) >= CORE4_SIZE)
        result = {
            "status": "success",
            "policy_version": POLICY_VERSION,
            "week_key": week_key,
            "horizon_start": start,
            "horizon_end": end,
            "fixture_count": len(fixtures),
            "official_fixture_coverage": official,
            "candidate_market_rows": len(rows),
            "trust_fixture_candidates": len(trust),
            "value_fixture_candidates": len(values),
            "xg_active_fixtures": xg_active_fixtures,
            "multi_book_binary_rows": multibook_binary_rows,
            "multi_book_1x2_rows": multibook_1x2_rows,
            "ready": ready,
            "core4": core_public,
            "confidence_core4": core_public,
            "ranked_picks": trust_public,
            "confidence_ranked": trust_public,
            "value_picks": value_public,
            "fallback_candidates": fallback_public,
            "excluded_counts": dict(excluded),
            "policy": {
                "selection_semantics": "confidence_core4_separate_from_value",
                "six_layers": [
                    "market_anchored_conservative_consensus_with_uncertainty_floor",
                    "opponent_adjusted_xg_xga_strength",
                    "expected_xi_injury_lambda_adjustment",
                    "opponent_adjusted_goal_strength",
                    "multi_book_no_vig_consensus",
                    "dixon_coles_low_score_correction",
                ],
                "current_season_result_tuning": False,
                "confidence_core_requires_xg": True,
                "confidence_core_requires_multi_book": True,
                "confidence_core_requires_four_model_families": True,
                "confidence_core_min_direction_agreement": MIN_DIRECTION_AGREEMENT,
                "confidence_core_min_evidence": MIN_EVIDENCE,
                "confidence_core_not_forced_when_evidence_missing": True,
                "value_min_ev": VALUE_MIN_EV,
                "turkey_executable_price_required": True,
                "max_same_selection_in_core": MAX_SAME_SELECTION_IN_CORE,
                "schedule_context_version": SCHEDULE_CONTEXT_VERSION,
                "probability_note": "market-anchored conservative consensus; lower bound includes model spread, bookmaker dispersion and evidence completeness; not a statistical guarantee",
            },
        }
        print("WEEKLY_CORE4_V4_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
        return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2, default=str))
