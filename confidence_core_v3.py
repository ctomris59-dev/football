#!/usr/bin/env python3
"""Six-layer evidence-driven weekly football decision engine.

Production decision architecture:
1. Frozen V1 historical baseline.
2. Past-only Understat xG/xGA challenger.
3. Expected-XI/injury adjustment applied directly to goal lambdas.
4. Opponent-adjusted attack/defence strength challenger.
5. Multi-book same-market no-vig international consensus as probability anchor.
6. Past-only Dixon-Coles low-score correction challenger.

Reliability and value are deliberately separate products. 2026/27 outcomes are not
used to tune any threshold or weight in this module.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import psycopg

from advanced_goal_models import dixon_coles_probabilities, fit_dc_rho, fit_opponent_strengths, opponent_adjusted_probabilities
from international_market_reference import latest_ref
from model_engine import predict_match as predict_match_xg
from model_engine_v1 import predict_match as predict_match_v1
from one_x_two_market_reference import latest_ref as latest_1x2_ref, selected_probability as selected_1x2_probability
from production_predictor import canon
from schedule_context import SCHEDULE_CONTEXT_VERSION, team_schedule_context
from thursday_decision_engine import DATABASE_URL, LIST_LIMIT, TURKEY_PRICE_DDL, _history_rows, _last_rest_days, _latest_import_coverage, _price_payload, weekend_bounds

POLICY_VERSION = "confidence-core-v3-six-layer-2026-09-17"
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
MIN_MULTI_BOOKS = 2
MARKET_WEIGHT = 0.55
MODEL_WEIGHT = 0.45
SPREAD_PENALTY = 0.35
EVIDENCE_PENALTY = 0.05


def _clip(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, float(v)))


def _day(v: Any) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except Exception:
        return None


def _score_probs(lam_h: float, lam_a: float, rho: float = 0.0) -> Dict[str, float]:
    out = dixon_coles_probabilities(float(lam_h), float(lam_a), float(rho))
    return {k: float(out[k]) for k in ("p_over_2_5", "p_btts", "p_home", "p_draw", "p_away")}


def _player_context(conn, team: str) -> Dict[str, Any]:
    try:
        row = conn.execute(
            """SELECT expected_xi_strength,top11_strength,injury_impact,goalkeeper_injured,
                      retained_minutes_share,starter_continuity,player_coverage,key_absences,source_meta
                 FROM player_team_context_snapshots
                WHERE team_name=%s ORDER BY snapshot_hour DESC LIMIT 1""",
            (team,),
        ).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    keys = ("expected_xi_strength", "top11_strength", "injury_impact", "goalkeeper_injured", "retained_minutes_share", "starter_continuity", "player_coverage", "key_absences", "source_meta")
    return dict(zip(keys, row))


def _injury_covered(conn) -> Set[str]:
    try:
        hour = conn.execute("SELECT MAX(snapshot_hour) FROM fotmob_fixture_availability_snapshots").fetchone()[0]
        rows = conn.execute("SELECT home_team,away_team FROM fotmob_fixture_availability_snapshots WHERE snapshot_hour=%s", (hour,)).fetchall() if hour else []
    except Exception:
        return set()
    return {canon(t) for row in rows for t in row if t}


def _enrich_xg(conn, league: str, before: datetime, history: Sequence[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    try:
        rows = conn.execute(
            """SELECT match_date,home_team,away_team,home_xg,away_xg FROM understat_matches
                WHERE league_name=%s AND is_result=TRUE AND match_date<%s
                  AND home_xg IS NOT NULL AND away_xg IS NOT NULL ORDER BY match_date""",
            (league, before),
        ).fetchall()
    except Exception:
        rows = []
    idx: Dict[Tuple[date, str, str], Tuple[float, float]] = {}
    for dt, h, a, hx, ax in rows:
        d = _day(dt)
        if d:
            idx[(d, canon(h), canon(a))] = (float(hx), float(ax))
    out: List[Dict[str, Any]] = []
    matched = 0
    for raw in history:
        row = dict(raw)
        d = _day(row.get("match_date"))
        pair = idx.get((d, canon(row.get("home_team")), canon(row.get("away_team")))) if d else None
        if pair:
            row["home_xg"], row["away_xg"] = pair
            matched += 1
        out.append(row)
    return out, matched


def _availability_factor(ctx: Mapping[str, Any]) -> float:
    ratio = 1.0
    try:
        exp, top = ctx.get("expected_xi_strength"), ctx.get("top11_strength")
        if exp is not None and top is not None and float(top) > 1e-9:
            ratio = _clip(float(exp) / float(top), 0.82, 1.04)
    except Exception:
        pass
    try:
        injury = _clip(float(ctx.get("injury_impact") or 0.0), 0.0, 0.55)
    except Exception:
        injury = 0.0
    return _clip(math.sqrt(ratio) * (1.0 - 0.08 * injury), 0.88, 1.03)


def _lineup_lambdas(lh: float, la: float, hctx: Mapping[str, Any], actx: Mapping[str, Any]) -> Tuple[float, float, Dict[str, float]]:
    hf, af = _availability_factor(hctx), _availability_factor(actx)
    hgk = 1.035 if bool(actx.get("goalkeeper_injured")) else 1.0
    agk = 1.035 if bool(hctx.get("goalkeeper_injured")) else 1.0
    return (
        _clip(float(lh) * hf * hgk, 0.15, 4.75),
        _clip(float(la) * af * agk, 0.12, 4.25),
        {"home_availability_factor": hf, "away_availability_factor": af, "away_gk_factor_on_home": hgk, "home_gk_factor_on_away": agk},
    )


def _schedule_factor(ctx: Mapping[str, Any]) -> float:
    try:
        return _clip(float(ctx.get("rank_factor") or 0.96), 0.88, 1.02)
    except Exception:
        return 0.96


def _is_multi_book(ref: Optional[Mapping[str, Any]], one_x_two: bool = False) -> bool:
    if not ref:
        return False
    q = str(ref.get("quality") or "")
    books = int(ref.get("bookmaker_count") or 0)
    expected = "multi_book_three_way_consensus" if one_x_two else "multi_book_consensus"
    return books >= MIN_MULTI_BOOKS and q == expected


def _binary_ref(ref: Optional[Dict[str, Any]], yes: bool) -> Optional[float]:
    if not ref or ref.get("reference_p_yes") is None:
        return None
    p = float(ref["reference_p_yes"])
    return p if yes else 1.0 - p


def _evidence(pred_quality: float, xg_used: bool, xg_coverage: float, hctx: Mapping[str, Any], actx: Mapping[str, Any], ref: Optional[Mapping[str, Any]], injury_complete: bool, hs: Mapping[str, Any], ass: Mapping[str, Any]) -> Tuple[float, Dict[str, float]]:
    sample = _clip(float(pred_quality or 0.0), 0.0, 1.0)
    xg = _clip((1.0 if xg_used else 0.35) * (0.70 + 0.30 * _clip(xg_coverage, 0.0, 1.0)), 0.0, 1.0)
    player = (_clip(float(hctx.get("player_coverage") or 0.0), 0.0, 1.0) + _clip(float(actx.get("player_coverage") or 0.0), 0.0, 1.0)) / 2.0
    books = int((ref or {}).get("bookmaker_count") or 0)
    market = _clip(books / 4.0, 0.0, 1.0)
    if ref and "multi_book" in str(ref.get("quality") or ""):
        market = max(market, 0.75)
    sched = 0.0
    if hs.get("rest_days") is not None and ass.get("rest_days") is not None:
        sched += 0.45
    if hs.get("scope") == "all_competitions" and ass.get("scope") == "all_competitions":
        sched += 0.30
    if injury_complete:
        sched += 0.25
    sched = _clip(sched, 0.0, 1.0)
    score = _clip(0.25 * sample + 0.20 * xg + 0.20 * player + 0.20 * market + 0.15 * sched, 0.0, 1.0)
    return score, {"sample": sample, "xg": xg, "player": player, "market": market, "schedule_injury": sched}


def _calibrate(model_probs: Sequence[float], market_p: Optional[float], evidence: float) -> Dict[str, float]:
    vals = [float(x) for x in model_probs if x is not None and 0.0 <= float(x) <= 1.0]
    median = statistics.median(vals)
    all_vals = list(vals)
    if market_p is not None:
        market_p = float(market_p)
        calibrated = MARKET_WEIGHT * market_p + MODEL_WEIGHT * median
        anchor = min(median, market_p)
        all_vals.append(market_p)
    else:
        calibrated, anchor = median, median
    spread = max(all_vals) - min(all_vals) if len(all_vals) > 1 else 0.0
    lower = anchor - SPREAD_PENALTY * spread - EVIDENCE_PENALTY * (1.0 - evidence)
    return {"model_median": _clip(median, 0.0, 1.0), "calibrated": _clip(calibrated, 0.0, 1.0), "lower": _clip(lower, 0.0, 1.0), "spread": _clip(spread, 0.0, 1.0)}


def _price_metrics(p: float, price: float) -> Tuple[float, float]:
    implied = 1.0 / float(price)
    return float(p) - implied, float(p) * float(price) - 1.0


def _public(r: Mapping[str, Any], rank: int, tier: str) -> Dict[str, Any]:
    return {
        "rank": rank, "list_tier": tier, "event_id": r["event_id"], "match_date": r["match_date"], "league": r["league"], "home": r["home"], "away": r["away"],
        "market": r["market"], "selection": r["selection"], "tr_price": r["tr_price"], "tr_opening_price": r.get("tr_opening_price"), "tr_source": r.get("tr_source"),
        "model_probability_estimate": r["model_median"], "confidence": r["model_median"], "calibrated_probability": r["calibrated"], "confidence_lower_bound": r["lower"],
        "model_probability_spread": r["spread"], "direction_agreement": r["agreement"], "evidence_quality": r["evidence"], "evidence_components": r["components"],
        "international_fair_probability": r.get("market_p"), "international_quality": r.get("market_quality"), "international_bookmakers": r.get("books"), "international_dispersion": r.get("dispersion"),
        "model_market_gap": r.get("market_gap"), "model_edge_vs_tr": r.get("edge"), "model_ev_vs_tr": r.get("ev"), "value_ev_calibrated": r.get("ev"),
        "data_quality": r.get("data_quality"), "xg_used": r.get("xg_used"), "xg_history_coverage": r.get("xg_coverage"), "opponent_model_available": r.get("opp_available"),
        "dixon_coles_available": r.get("dc_available"), "dixon_coles_rho": r.get("rho"), "lineup_adjustment": r.get("lineup_diag"), "player_coverage": r.get("player_coverage"),
        "schedule_rank_factor": r.get("schedule_factor"), "injury_feed_complete": r.get("injury_complete"),
        "confidence_semantics": "market-anchored model consensus; lower bound is a conservative agreement floor, not a guarantee", "policy_version": POLICY_VERSION,
    }


def _diversify(rows: Sequence[Dict[str, Any]], size: int) -> List[Dict[str, Any]]:
    chosen: List[Dict[str, Any]] = []
    counts: Dict[str, int] = defaultdict(int)
    used: Set[str] = set()
    for row in rows:
        key = f"{row['market']}|{row['selection']}"
        if row["event_id"] in used or counts[key] >= MAX_SAME_SELECTION_IN_CORE:
            continue
        chosen.append(row); used.add(row["event_id"]); counts[key] += 1
        if len(chosen) >= size:
            return chosen
    for row in rows:
        if row["event_id"] not in used:
            chosen.append(row); used.add(row["event_id"])
            if len(chosen) >= size:
                break
    return chosen


def build(database_url: str = DATABASE_URL, *, now: Optional[datetime] = None, limit: int = LIST_LIMIT, strict_picks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    as_of = now or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    week_key, start, end = weekend_bounds(as_of)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(TURKEY_PRICE_DDL)
        injury_covered = _injury_covered(conn)
        fixtures = conn.execute("SELECT event_id,match_date,league_name,home_team,away_team FROM espn_upcoming WHERE is_current=TRUE AND match_date>=%s AND match_date<%s ORDER BY match_date", (start, end)).fetchall()
        histories: Dict[Tuple[str, date], List[Dict[str, Any]]] = {}
        xg_histories: Dict[Tuple[str, date], Tuple[List[Dict[str, Any]], int]] = {}
        opp_models: Dict[Tuple[str, date], Any] = {}
        dc_models: Dict[Tuple[str, date], Tuple[float, bool, Dict[str, Any]]] = {}
        rows: List[Dict[str, Any]] = []
        excluded: Dict[str, int] = defaultdict(int)

        for eid, match_date, league, home, away in fixtures:
            league, home, away = str(league), str(home), str(away)
            key = (league, match_date.date())
            histories.setdefault(key, _history_rows(conn, league, match_date))
            history = histories[key]
            if not history:
                excluded["no_history"] += 1; continue
            try:
                v1 = predict_match_v1(history, canon(home), canon(away), recent_matches=18)
            except Exception:
                excluded["v1_failed"] += 1; continue
            if key not in xg_histories:
                xg_histories[key] = _enrich_xg(conn, league, match_date, history)
            xgh, xg_rows = xg_histories[key]
            try:
                xgp = predict_match_xg(xgh, canon(home), canon(away), recent_matches=18)
            except Exception:
                xgp = None
            if key not in opp_models:
                try: opp_models[key] = fit_opponent_strengths(history)
                except Exception: opp_models[key] = None
            opp_model = opp_models[key]
            try: opp = opponent_adjusted_probabilities(v1, opp_model, home, away) if opp_model else None
            except Exception: opp = None
            if key not in dc_models:
                try: dc_models[key] = fit_dc_rho(history)
                except Exception: dc_models[key] = (0.0, False, {})
            rho, dc_available, _ = dc_models[key]

            hctx, actx = _player_context(conn, home), _player_context(conn, away)
            hrest, arest = _last_rest_days(history, home, match_date), _last_rest_days(history, away, match_date)
            hs = team_schedule_context(conn, home, match_date, as_of=as_of, fallback_rest_days=hrest)
            ass = team_schedule_context(conn, away, match_date, as_of=as_of, fallback_rest_days=arest)
            if hs.get("pending_pre_fixture_match") or ass.get("pending_pre_fixture_match"):
                excluded["pending_intervening_match"] += 1; continue
            injury_complete = canon(home) in injury_covered and canon(away) in injury_covered
            player_coverage = (_clip(float(hctx.get("player_coverage") or 0.0), 0, 1) + _clip(float(actx.get("player_coverage") or 0.0), 0, 1)) / 2
            sched_factor = min(_schedule_factor(hs), _schedule_factor(ass))

            vlh, vla, lineup_diag = _lineup_lambdas(v1.lambda_home_goals, v1.lambda_away_goals, hctx, actx)
            variants: Dict[str, Dict[str, float]] = {"v1": _score_probs(vlh, vla)}
            xg_used = bool(xgp and getattr(xgp, "xg_used", False))
            if xg_used:
                xlh, xla, _ = _lineup_lambdas(xgp.lambda_home_goals, xgp.lambda_away_goals, hctx, actx)
                variants["xg"] = _score_probs(xlh, xla)
            opp_available = bool(opp and opp.get("available"))
            if opp_available:
                olh, ola, _ = _lineup_lambdas(opp["lambda_home_goals"], opp["lambda_away_goals"], hctx, actx)
                variants["opponent"] = _score_probs(olh, ola)
            if dc_available:
                variants["dixon_coles"] = _score_probs(vlh, vla, rho)
            xg_cov = min(1.0, xg_rows / max(1, len(history)))

            def common(ref: Optional[Mapping[str, Any]]) -> Tuple[float, Dict[str, float]]:
                return _evidence(v1.data_quality, xg_used, xg_cov, hctx, actx, ref, injury_complete, hs, ass)

            for market, pkey, yes_label, no_label in (("over_2_5", "p_over_2_5", "2.5 ÜST", "2.5 ALT"), ("btts", "p_btts", "KG VAR", "KG YOK")):
                yvals = [float(v[pkey]) for v in variants.values()]
                median_yes = statistics.median(yvals)
                yes = median_yes >= 0.5
                selection = yes_label if yes else no_label
                model_p = median_yes if yes else 1.0 - median_yes
                price = _price_payload(conn, str(eid), market, selection)
                if not price:
                    excluded["turkey_price_missing"] += 1; continue
                ref = latest_ref(conn, str(eid), market)
                market_p = _binary_ref(ref, yes)
                evidence, components = common(ref)
                selected = [p if yes else 1.0-p for p in yvals]
                cal = _calibrate(selected, market_p, evidence)
                agreement = sum(1 for p in yvals if (p >= 0.5) == yes) / len(yvals)
                edge, ev = _price_metrics(cal["calibrated"], price["tr_price"])
                gap = abs(cal["model_median"] - market_p) if market_p is not None else None
                row = {"event_id":str(eid),"match_date":match_date,"league":league,"home":home,"away":away,"market":market,"selection":selection,"tr_price":float(price["tr_price"]),"tr_opening_price":price.get("tr_opening_price"),"tr_source":price.get("tr_source"),**cal,"agreement":agreement,"evidence":evidence,"components":components,"market_p":market_p,"market_quality":(ref or {}).get("quality"),"books":(ref or {}).get("bookmaker_count"),"dispersion":(ref or {}).get("dispersion"),"market_gap":gap,"edge":edge,"ev":ev,"data_quality":float(v1.data_quality),"xg_used":xg_used,"xg_coverage":xg_cov,"opp_available":opp_available,"dc_available":bool(dc_available),"rho":float(rho) if dc_available else None,"lineup_diag":lineup_diag,"player_coverage":player_coverage,"schedule_factor":sched_factor,"injury_complete":injury_complete}
                row["trust"] = bool(model_p >= MIN_BINARY_MODEL and _is_multi_book(ref) and evidence >= MIN_EVIDENCE and player_coverage >= MIN_PLAYER_COVERAGE and agreement >= MIN_DIRECTION_AGREEMENT and gap is not None and gap <= MAX_MODEL_MARKET_GAP and len(selected) >= 3)
                row["value"] = bool(evidence >= VALUE_MIN_EVIDENCE and market_p is not None and ev >= VALUE_MIN_EV and agreement >= 0.50)
                rows.append(row)

            omap = {"1":"p_home","0":"p_draw","2":"p_away"}
            med = {s:statistics.median([float(v[k]) for v in variants.values()]) for s,k in omap.items()}
            sel = max(med, key=med.get); model_p = float(med[sel]); price = _price_payload(conn, str(eid), "match_result", sel)
            if price:
                ref = latest_1x2_ref(conn, str(eid)); market_p = selected_1x2_probability(ref, sel)
                selected = [float(v[omap[sel]]) for v in variants.values()]
                picks = [max(omap, key=lambda s:float(v[omap[s]])) for v in variants.values()]
                evidence, components = common(ref); cal = _calibrate(selected, market_p, evidence)
                agreement = sum(1 for p in picks if p == sel) / len(picks); edge, ev = _price_metrics(cal["calibrated"], price["tr_price"])
                gap = abs(cal["model_median"] - market_p) if market_p is not None else None
                row = {"event_id":str(eid),"match_date":match_date,"league":league,"home":home,"away":away,"market":"match_result","selection":sel,"tr_price":float(price["tr_price"]),"tr_opening_price":price.get("tr_opening_price"),"tr_source":price.get("tr_source"),**cal,"agreement":agreement,"evidence":evidence,"components":components,"market_p":market_p,"market_quality":(ref or {}).get("quality"),"books":(ref or {}).get("bookmaker_count"),"dispersion":(ref or {}).get("dispersion"),"market_gap":gap,"edge":edge,"ev":ev,"data_quality":float(v1.data_quality),"xg_used":xg_used,"xg_coverage":xg_cov,"opp_available":opp_available,"dc_available":bool(dc_available),"rho":float(rho) if dc_available else None,"lineup_diag":lineup_diag,"player_coverage":player_coverage,"schedule_factor":sched_factor,"injury_complete":injury_complete}
                row["trust"] = bool(model_p >= MIN_1X2_MODEL and _is_multi_book(ref, True) and evidence >= MIN_EVIDENCE and player_coverage >= MIN_PLAYER_COVERAGE and agreement >= MIN_DIRECTION_AGREEMENT and gap is not None and gap <= MAX_MODEL_MARKET_GAP and len(selected) >= 3)
                row["value"] = bool(evidence >= VALUE_MIN_EVIDENCE and market_p is not None and ev >= VALUE_MIN_EV and agreement >= 0.50)
                rows.append(row)
            else:
                excluded["turkey_1x2_price_missing"] += 1

        trust_best: Dict[str, Dict[str, Any]] = {}; value_best: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            if r.get("trust"):
                cur = trust_best.get(r["event_id"]); k = (r["lower"],r["evidence"],r["agreement"],r["calibrated"])
                if cur is None or k > (cur["lower"],cur["evidence"],cur["agreement"],cur["calibrated"]): trust_best[r["event_id"]] = r
            if r.get("value"):
                cur = value_best.get(r["event_id"]); k = (r["ev"],r["evidence"],r["lower"])
                if cur is None or k > (cur["ev"],cur["evidence"],cur["lower"]): value_best[r["event_id"]] = r
        trust = sorted(trust_best.values(), key=lambda r:(r["lower"],r["evidence"],r["agreement"],r["calibrated"]), reverse=True)
        core = _diversify(trust, CORE4_SIZE)
        values = sorted(value_best.values(), key=lambda r:(r["ev"],r["evidence"],r["lower"]), reverse=True)
        core_public = [_public(r,i+1,"confidence_core4") for i,r in enumerate(core)]
        trust_public = [_public(r,i+1,"confidence_ranked") for i,r in enumerate(trust[:limit])]
        value_public = [_public(r,i+1,"value") for i,r in enumerate(values[:limit])]
        used = {r["event_id"] for r in core}; fallbacks = sorted([r for r in rows if r["event_id"] not in used], key=lambda r:(r["lower"],r["evidence"],r["agreement"]), reverse=True)
        fallback_public = [_public(r,i+1,"evidence_fallback") for i,r in enumerate(fallbacks[:max(0, CORE4_SIZE-len(core))])]
        coverage = _latest_import_coverage(conn, len(fixtures)); official = float(coverage.get("fixture_coverage") or 0.0); ready = bool(official >= 0.90 and len(core_public) >= CORE4_SIZE)
        result = {"status":"success","policy_version":POLICY_VERSION,"week_key":week_key,"horizon_start":start,"horizon_end":end,"fixture_count":len(fixtures),"official_fixture_coverage":official,"candidate_market_rows":len(rows),"trust_fixture_candidates":len(trust),"value_fixture_candidates":len(values),"ready":ready,"core4":core_public,"confidence_core4":core_public,"ranked_picks":trust_public,"confidence_ranked":trust_public,"value_picks":value_public,"fallback_candidates":fallback_public,"excluded_counts":dict(excluded),"policy":{"selection_semantics":"confidence_core4_separate_from_value","six_layers":["market_anchored_conservative_calibration","xg_xga_challenger","expected_xi_lambda_adjustment","opponent_adjusted_strength","multi_book_no_vig_consensus","dixon_coles_low_score_correction"],"current_season_result_tuning":False,"confidence_core_requires_multi_book":True,"confidence_core_requires_three_model_families":True,"confidence_core_min_direction_agreement":MIN_DIRECTION_AGREEMENT,"confidence_core_min_evidence":MIN_EVIDENCE,"confidence_core_not_forced_when_evidence_missing":True,"value_min_ev":VALUE_MIN_EV,"turkey_executable_price_required":True,"max_same_selection_in_core":MAX_SAME_SELECTION_IN_CORE,"schedule_context_version":SCHEDULE_CONTEXT_VERSION,"calibration_note":"market-anchored consensus plus conservative lower-bound floor; not a statistical guarantee"}}
        print("WEEKLY_CORE4_RESULT",json.dumps(result,ensure_ascii=False,default=str,separators=(",",":")),flush=True)
        return result

if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2, default=str))
