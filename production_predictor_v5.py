#!/usr/bin/env python3
"""Production v5: validation-gated model plus high-confidence value selection.

Displayed probabilities remain the frozen/validated V1 probabilities. Four-layer
penalties may alter ranking only when the policy registry contains evidence from the
predeclared two-fold, holdout-safe validation protocol. Otherwise activation fails
closed to v1_only. Closing prices and 2026/27 outcomes never enter this ranking path.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb as PsyJsonb

import production_predictor_v4 as v4
import production_predictor_v3 as v3
from research_change_control import registry_activation_mode

MODEL_VERSION = "production-poisson-form-v1-advanced-v5-value"
POLICY_BASE = "high-confidence-value-validation-gated-v5"
VALUE_MIN_CONFIDENCE = float(os.getenv("VALUE_MIN_CONFIDENCE", "0.65"))
VALUE_MIN_EDGE = float(os.getenv("VALUE_MIN_EDGE", "0.015"))
VALUE_MIN_EV = float(os.getenv("VALUE_MIN_EV", "0.02"))


def _default(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


def _safe(v):
    return PsyJsonb(v, dumps=lambda x: json.dumps(x, default=_default, separators=(",", ":")))


def _activation_mode(conn) -> str:
    # Fail closed unless registry metrics prove the new two-fold holdout-safe gate.
    return registry_activation_mode(conn, "four-layer-v5")


def _pressure_penalty(ctx: Dict[str, Any], market: str, selection: str) -> float:
    pressure = (
        ctx.get("goal_pressure_signal")
        if market in ("over_2_5", "btts")
        else (ctx.get("corner_pressure_signal") if market == "corners_over_8_5" else None)
    )
    if pressure is None:
        return 1.0
    ps = float(pressure)
    over = "ÜST" in str(selection).upper() or "VAR" in str(selection).upper() or "OVER" in str(selection).upper()
    if over and ps < .88:
        return .985
    if not over and ps > 1.12:
        return .985
    return 1.0


def _continuity_penalty(ctx: Dict[str, Any]) -> float:
    vals = [x for x in (ctx.get("home_starter_continuity"), ctx.get("away_starter_continuity")) if x is not None]
    return .985 if vals and min(float(x) for x in vals) < .55 else 1.0


def _shadow_diagnostic_factor(ctx: Dict[str, Any], market: str, selection: str, model_p: float) -> float:
    factor = 1.0
    cov = float(ctx.get("total_coverage") or 0)
    if cov < .35:
        factor *= .985
    hi = max(float(ctx.get("home_injury_impact") or 0), float(ctx.get("away_injury_impact") or 0))
    if hi >= .20:
        factor *= .985
    if ctx.get("home_goalkeeper_injured") or ctx.get("away_goalkeeper_injured"):
        factor *= .99
    vals = [x for x in (ctx.get("home_starter_continuity"), ctx.get("away_starter_continuity")) if x is not None]
    if vals and min(float(x) for x in vals) < .55:
        factor *= .985
    retained = [x for x in (ctx.get("home_retained_minutes_share"), ctx.get("away_retained_minutes_share")) if x is not None]
    if retained and min(float(x) for x in retained) < .55:
        factor *= .985
    books = int(ctx.get("asian_bookmakers") or 0)
    asian = (
        ctx.get("asian_goal_p_over_2_5")
        if market == "over_2_5"
        else (ctx.get("asian_corner_p_over_8_5") if market == "corners_over_8_5" else None)
    )
    if asian is not None and books >= 2:
        pa = float(asian)
        under = "ALT" in str(selection).upper() or "UNDER" in str(selection).upper()
        if under:
            pa = 1 - pa
        if model_p - pa > .12:
            factor *= .975
        elif model_p - pa > .07:
            factor *= .988
    factor *= _pressure_penalty(ctx, market, selection)
    return max(.80, min(1.0, factor))


def penalty_factor(ctx: Dict[str, Any], market: str, selection: str, model_p: float, active_mode: str = "v1_only") -> float:
    factor = 1.0
    if active_mode in ("pressure", "pressure_continuity"):
        factor *= _pressure_penalty(ctx, market, selection)
    if active_mode in ("continuity", "pressure_continuity"):
        factor *= _continuity_penalty(ctx)
    return max(.90, min(1.0, factor))


def _ctx(conn, event_id):
    try:
        r = conn.execute(
            """SELECT home_injury_impact,away_injury_impact,home_goalkeeper_injured,away_goalkeeper_injured,
                      home_retained_minutes_share,away_retained_minutes_share,home_starter_continuity,
                      away_starter_continuity,goal_pressure_signal,corner_pressure_signal,
                      asian_goal_p_over_2_5,asian_corner_p_over_8_5,asian_bookmakers,total_coverage,context
               FROM advanced_fixture_context_v4
               WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
    except Exception:
        return {}
    if not r:
        return {}
    keys = [
        "home_injury_impact", "away_injury_impact", "home_goalkeeper_injured", "away_goalkeeper_injured",
        "home_retained_minutes_share", "away_retained_minutes_share", "home_starter_continuity",
        "away_starter_continuity", "goal_pressure_signal", "corner_pressure_signal", "asian_goal_p_over_2_5",
        "asian_corner_p_over_8_5", "asian_bookmakers", "total_coverage", "context",
    ]
    return dict(zip(keys, r))


def _value_metrics(model_p, market_p, price):
    p = float(model_p)
    q = float(market_p) if market_p is not None else (1.0 / float(price) if price else None)
    o = float(price) if price else None
    edge = (p - q) if q is not None else None
    ev = (p * o - 1.0) if o is not None else None
    return q, edge, ev


def rerank(db: str, run_id: int) -> Dict[str, Any]:
    with psycopg.connect(db, autocommit=True) as c:
        mode = _activation_mode(c)
        rows = c.execute(
            "SELECT event_id,market,selection,model_probability,market_no_vig_probability,market_price,ranking_score,model_details "
            "FROM production_predictions WHERE run_id=%s AND provisional_ready=TRUE",
            (run_id,),
        ).fetchall()
        for eid, market, sel, p, market_p, price, rank, details in rows:
            ctx = _ctx(c, eid)
            active = penalty_factor(ctx, str(market), str(sel), float(p), mode)
            shadow = _shadow_diagnostic_factor(ctx, str(market), str(sel), float(p))
            q, edge, ev = _value_metrics(p, market_p, price)
            md = dict(details or {}) if isinstance(details, dict) else {}
            md["four_layer_context"] = ctx.get("context") or {}
            md["v5_activation_mode"] = mode
            md["v5_active_penalty_factor"] = round(active, 6)
            md["v5_shadow_diagnostic_factor"] = round(shadow, 6)
            md["value"] = {
                "market_fair_probability": round(q, 6) if q is not None else None,
                "edge": round(edge, 6) if edge is not None else None,
                "ev": round(ev, 6) if ev is not None else None,
                "min_confidence": VALUE_MIN_CONFIDENCE,
                "min_edge": VALUE_MIN_EDGE,
                "min_ev": VALUE_MIN_EV,
            }
            c.execute(
                "UPDATE production_predictions SET ranking_score=%s,model_details=%s WHERE run_id=%s AND event_id=%s AND market=%s",
                (round(float(rank) * active, 6), _safe(md), run_id, eid, market),
            )

        raw = c.execute(
            "SELECT event_id,market,ranking_score,model_probability,market_no_vig_probability,market_price "
            "FROM production_predictions WHERE run_id=%s AND provisional_ready=TRUE AND model_probability>=%s "
            "AND market_price IS NOT NULL AND market_price>=%s",
            (run_id, VALUE_MIN_CONFIDENCE, v3.core.PREDICTION_MIN_PRICE),
        ).fetchall()
        best = {}
        qualified = 0
        for eid, m, r, p, mp, price in raw:
            q, edge, ev = _value_metrics(p, mp, price)
            if edge is None or ev is None or edge < VALUE_MIN_EDGE or ev < VALUE_MIN_EV:
                continue
            qualified += 1
            val = (m, float(r), float(p), float(edge), float(ev), float(price))
            cur = best.get(eid)
            if cur is None or (val[2], val[4], val[3], val[1]) > (cur[2], cur[4], cur[3], cur[1]):
                best[eid] = val

        ordered = sorted(best.items(), key=lambda kv: (kv[1][2], kv[1][4], kv[1][3], kv[1][1]), reverse=True)[:10]
        c.execute("UPDATE production_predictions SET top10_rank=NULL WHERE run_id=%s", (run_id,))
        for i, (eid, (m, *_)) in enumerate(ordered, 1):
            c.execute(
                "UPDATE production_predictions SET top10_rank=%s WHERE run_id=%s AND event_id=%s AND market=%s",
                (i, run_id, eid, m),
            )

        policy = f"{POLICY_BASE}:{mode}"
        msg = (
            f"high-confidence value gate p>={VALUE_MIN_CONFIDENCE:.3f}, edge>={VALUE_MIN_EDGE:.3f}, "
            f"EV>={VALUE_MIN_EV:.3f}; v5 gate={mode}; no forced picks; two-fold evidence required"
        )
        c.execute(
            "UPDATE production_prediction_runs SET model_version=%s,policy_version=%s,candidate_matches=%s,top10_count=%s,message=%s WHERE id=%s",
            (MODEL_VERSION, policy, len(best), len(ordered), msg, run_id),
        )
        top = []
        for rank, (eid, (m, _r, _p, edge, ev, price)) in enumerate(ordered, 1):
            row = c.execute(
                "SELECT match_date,league_name,home_team,away_team,selection,model_probability,ranking_score,final_context_ready "
                "FROM production_predictions WHERE run_id=%s AND event_id=%s AND market=%s",
                (run_id, eid, m),
            ).fetchone()
            if row:
                top.append({
                    "rank": rank, "event_id": eid, "market": m, "match_date": row[0], "league": row[1],
                    "home": row[2], "away": row[3], "selection": row[4], "confidence": float(row[5]),
                    "price": price, "edge": edge, "ev": ev, "ranking": float(row[6]), "final": bool(row[7]),
                })
        return {
            "candidate_matches": len(best),
            "qualified_market_rows": qualified,
            "top10_count": len(ordered),
            "top10": top,
            "activation_mode": mode,
            "value_gate": {"min_confidence": VALUE_MIN_CONFIDENCE, "min_edge": VALUE_MIN_EDGE, "min_ev": VALUE_MIN_EV},
        }


def run_predictions(database_url: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    db = (database_url or v3.core.DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    res = v4.run_predictions(db, **kwargs)
    rr = rerank(db, int(res["run_id"]))
    res.update(rr)
    res["model_version"] = MODEL_VERSION
    res["policy_version"] = f"{POLICY_BASE}:{rr['activation_mode']}"
    res["four_layer_shadow"] = rr["activation_mode"] == "v1_only"
    return res


if __name__ == "__main__":
    print(json.dumps(run_predictions(), ensure_ascii=False, indent=2, default=_default))
