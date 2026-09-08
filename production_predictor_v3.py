#!/usr/bin/env python3
"""Production predictor v3.

Principles:
- keep the historically stronger v1 Poisson/form probabilities as the primary model;
- use per-bookmaker no-vig consensus instead of cross-book margin removal;
- activate score-state or promoted-team priors only when their leakage-safe
  validation tables explicitly approve them;
- record player strength, injury impact, style and Elo as shadow features and use
  coverage/uncertainty only as a small Top-10 tie-breaker, never to inflate the
  displayed model probability without validation.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

import production_predictor as core
from model_engine import Prediction as CompatiblePrediction
from model_engine_v1 import predict_match as predict_v1

MODEL_VERSION = "production-poisson-form-v1-advanced-v3"
POLICY_VERSION = "consensus-validated-enrichment-v3"

_FALLBACK_HISTORY = core.history_rows
_FALLBACK_MARKET_PRICES = core.market_prices
_FALLBACK_NO_VIG = core.no_vig_selected
_USE_SCORE_STATE = False
_USE_PROMOTION_PRIOR = False


def _primary_predict(history, home_team, away_team, *, recent_matches=18):
    p = predict_v1(history, home_team, away_team, recent_matches=recent_matches)
    return CompatiblePrediction(
        p.p_over_2_5, p.p_btts, p.p_corners_over_8_5,
        p.lambda_home_goals, p.lambda_away_goals, p.lambda_total_corners,
        p.home_sample, p.away_sample, p.data_quality, False,
    )


def _latest_bool(conn, table: str, column: str) -> bool:
    allowed = {
        ("score_state_backtest_runs", "use_adjusted"),
        ("promotion_prior_backtest_runs", "use_prior"),
    }
    if (table, column) not in allowed:
        return False
    try:
        row = conn.execute(
            f"SELECT {column} FROM {table} WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def _avg(rows: List[Dict[str, Any]], key: str, default: float) -> float:
    vals = []
    for r in rows:
        try:
            if r.get(key) is not None:
                vals.append(float(r[key]))
        except Exception:
            pass
    return sum(vals) / len(vals) if vals else default


def _ratio(prior: Dict[str, Any], key: str) -> float:
    try:
        return max(0.55, min(1.65, float(prior.get(key, 1.0))))
    except Exception:
        return 1.0


def _promotion_pseudo_rows(conn, league: str, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """SELECT team_name,transferred_relative FROM promotion_priors
           WHERE target_season='2627' AND parent_league_name=%s""",
        (league,),
    ).fetchall()
    if not rows:
        return []
    base = {
        "hg": _avg(history, "home_goals", 1.50), "ag": _avg(history, "away_goals", 1.20),
        "hs": _avg(history, "home_shots_on_target", 4.8), "as": _avg(history, "away_shots_on_target", 4.0),
        "hc": _avg(history, "home_corners", 5.3), "ac": _avg(history, "away_corners", 4.4),
    }
    out: List[Dict[str, Any]] = []
    start = date(2026, 7, 1)
    for team_name, transferred in rows:
        t = core.canon(team_name)
        p = transferred if isinstance(transferred, dict) else {}
        for i in range(3):
            out.append({
                "match_date": start + timedelta(days=i), "home_team": t, "away_team": "__promotion_prior__",
                "home_goals": base["hg"] * _ratio(p, "goals_for"),
                "away_goals": base["ag"] * _ratio(p, "goals_against"),
                "home_shots_on_target": base["hs"] * _ratio(p, "sot_for"),
                "away_shots_on_target": base["as"] * _ratio(p, "sot_against"),
                "home_corners": base["hc"] * _ratio(p, "corners_for"),
                "away_corners": base["ac"] * _ratio(p, "corners_against"),
                "home_xg": None, "away_xg": None,
            })
            out.append({
                "match_date": start + timedelta(days=3 + i), "home_team": "__promotion_prior__", "away_team": t,
                "home_goals": base["hg"] * _ratio(p, "goals_against"),
                "away_goals": base["ag"] * _ratio(p, "goals_for"),
                "home_shots_on_target": base["hs"] * _ratio(p, "sot_against"),
                "away_shots_on_target": base["as"] * _ratio(p, "sot_for"),
                "home_corners": base["hc"] * _ratio(p, "corners_against"),
                "away_corners": base["ac"] * _ratio(p, "corners_for"),
                "home_xg": None, "away_xg": None,
            })
    return out


def _history_rows_v3(conn, league_name: str, before) -> List[Dict[str, Any]]:
    history = _FALLBACK_HISTORY(conn, league_name, before)
    if _USE_SCORE_STATE and history:
        try:
            adjusted = {}
            for dt, home, away, hc, ac in conn.execute(
                """SELECT match_date,home_team,away_team,adjusted_home_corners,adjusted_away_corners
                   FROM score_state_adjusted_matches
                   WHERE league_name=%s AND match_date<%s AND adjustment_applied=TRUE""",
                (league_name, before.date()),
            ).fetchall():
                adjusted[(dt, core.canon(home), core.canon(away))] = (hc, ac)
            for row in history:
                d = core.as_date(row.get("match_date"))
                key = (d, core.canon(row.get("home_team")), core.canon(row.get("away_team")))
                if key in adjusted:
                    row["home_corners"], row["away_corners"] = adjusted[key]
        except Exception:
            pass
    if _USE_PROMOTION_PRIOR and history:
        try:
            history.extend(_promotion_pseudo_rows(conn, league_name, history))
            history.sort(key=lambda x: (core.as_date(x.get("match_date")) or date.min, str(x.get("home_team")), str(x.get("away_team"))))
        except Exception:
            pass
    return history


def _consensus_market_prices(conn, fixture_id: Optional[str]) -> Dict[str, Dict[Any, Any]]:
    if not fixture_id:
        return {}
    try:
        rows = conn.execute(
            """SELECT DISTINCT ON (market) market,consensus_p_yes,bookmaker_count,dispersion,sharp_bookmaker,sharp_p_yes,
                      best_price_yes,best_price_no,snapshot_hour
               FROM market_consensus_snapshots WHERE fixture_id=%s
               ORDER BY market,snapshot_hour DESC""",
            (fixture_id,),
        ).fetchall()
    except Exception:
        return _FALLBACK_MARKET_PRICES(conn, fixture_id)
    if not rows:
        return _FALLBACK_MARKET_PRICES(conn, fixture_id)
    out: Dict[str, Dict[Any, Any]] = {}
    for market, py, books, dispersion, sharp_book, sharp_py, yes_price, no_price, snapshot in rows:
        if py is None:
            continue
        out[str(market)] = {
            True: float(yes_price) if yes_price is not None else None,
            False: float(no_price) if no_price is not None else None,
            "_consensus_p_yes": float(py),
            "_bookmaker_count": int(books or 0),
            "_dispersion": float(dispersion or 0.0),
            "_sharp_bookmaker": sharp_book,
            "_sharp_p_yes": float(sharp_py) if sharp_py is not None else None,
            "_snapshot_hour": snapshot,
        }
    return out or _FALLBACK_MARKET_PRICES(conn, fixture_id)


def _consensus_no_vig(prices: Dict[Any, Any], selected: bool):
    if "_consensus_p_yes" in prices:
        selected_price = prices.get(selected)
        py = float(prices["_consensus_p_yes"])
        return selected_price, py if selected else 1.0 - py
    return _FALLBACK_NO_VIG(prices, selected)


def _latest_enrichment(conn, event_id: str) -> Dict[str, Any]:
    try:
        row = conn.execute(
            """SELECT enrichment_coverage,player_coverage,home_injury_impact,away_injury_impact,
                      home_goalkeeper_injured,away_goalkeeper_injured,corner_style_index,home_elo,away_elo,elo_diff,
                      home_promoted,away_promoted,home_key_injuries,away_key_injuries
               FROM fixture_enrichment_snapshots WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    keys = ["coverage","player_coverage","home_injury_impact","away_injury_impact","home_gk_injured","away_gk_injured",
            "corner_style_index","home_elo","away_elo","elo_diff","home_promoted","away_promoted","home_key_injuries","away_key_injuries"]
    return dict(zip(keys, row))


def _consensus_for_prediction(conn, event_id: str, market: str) -> Dict[str, Any]:
    try:
        f = conn.execute(
            """SELECT oddspapi_fixture_id FROM prematch_feature_snapshots
               WHERE event_id=%s AND oddspapi_fixture_id IS NOT NULL ORDER BY snapshot_hour DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
        if not f:
            return {}
        row = conn.execute(
            """SELECT bookmaker_count,consensus_p_yes,dispersion,sharp_bookmaker,sharp_p_yes,snapshot_hour
               FROM market_consensus_snapshots WHERE fixture_id=%s AND market=%s
               ORDER BY snapshot_hour DESC LIMIT 1""",
            (str(f[0]), market),
        ).fetchone()
        if not row:
            return {}
        return {"bookmaker_count": int(row[0] or 0), "consensus_p_yes": row[1], "dispersion": row[2],
                "sharp_bookmaker": row[3], "sharp_p_yes": row[4], "snapshot_hour": row[5]}
    except Exception:
        return {}


def _rerank_and_annotate(db: str, run_id: int) -> List[Dict[str, Any]]:
    with psycopg.connect(db, autocommit=True) as conn:
        rows = conn.execute(
            """SELECT event_id,market,ranking_score,model_probability,match_date,league_name,home_team,away_team,selection,
                      final_context_ready,model_details
               FROM production_predictions WHERE run_id=%s AND provisional_ready=TRUE
                 AND model_probability>=%s AND market_price IS NOT NULL AND market_price>=%s""",
            (run_id, core.PREDICTION_MIN_CONFIDENCE, core.PREDICTION_MIN_PRICE),
        ).fetchall()
        candidates: List[Dict[str, Any]] = []
        for event_id, market, ranking, confidence, match_date, league, home, away, selection, final_ready, details in rows:
            enrich = _latest_enrichment(conn, event_id)
            consensus = _consensus_for_prediction(conn, event_id, market)
            coverage = float(enrich.get("coverage") or 0.0)
            factor = 0.97 + 0.03 * max(0.0, min(1.0, coverage))
            books = int(consensus.get("bookmaker_count") or 0)
            if books and books < 3:
                factor *= 0.985
            dispersion = float(consensus.get("dispersion") or 0.0)
            factor *= max(0.97, 1.0 - min(0.03, dispersion * 1.5))
            severe_uncertainty = max(float(enrich.get("home_injury_impact") or 0.0), float(enrich.get("away_injury_impact") or 0.0)) >= 0.25
            if severe_uncertainty and not final_ready:
                factor *= 0.98
            if (enrich.get("home_promoted") or enrich.get("away_promoted")) and not _USE_PROMOTION_PRIOR:
                factor *= 0.98
            advanced = float(ranking) * factor
            md = dict(details or {}) if isinstance(details, dict) else {}
            md["advanced_features"] = enrich
            md["market_consensus"] = consensus
            md["v3_activation"] = {"score_state": _USE_SCORE_STATE, "promotion_prior": _USE_PROMOTION_PRIOR}
            conn.execute(
                "UPDATE production_predictions SET ranking_score=%s,model_details=%s WHERE run_id=%s AND event_id=%s AND market=%s",
                (round(advanced, 6), Jsonb(md), run_id, event_id, market),
            )
            candidates.append({
                "event_id": event_id, "market": market, "ranking": advanced, "confidence": float(confidence),
                "match_date": match_date, "league": league, "home": home, "away": away, "selection": selection,
                "final": bool(final_ready),
            })

        by_fixture: Dict[str, Dict[str, Any]] = {}
        for item in candidates:
            cur = by_fixture.get(item["event_id"])
            if cur is None or (item["ranking"], item["confidence"]) > (cur["ranking"], cur["confidence"]):
                by_fixture[item["event_id"]] = item
        ranked = sorted(by_fixture.values(), key=lambda x: (x["ranking"], x["confidence"]), reverse=True)[:10]
        conn.execute("UPDATE production_predictions SET top10_rank=NULL WHERE run_id=%s", (run_id,))
        for rank, item in enumerate(ranked, 1):
            conn.execute(
                "UPDATE production_predictions SET top10_rank=%s WHERE run_id=%s AND event_id=%s AND market=%s",
                (rank, run_id, item["event_id"], item["market"]),
            )
        conn.execute(
            """UPDATE production_prediction_runs SET model_version=%s,policy_version=%s,candidate_matches=%s,top10_count=%s,
               message=%s WHERE id=%s""",
            (MODEL_VERSION, POLICY_VERSION, len(by_fixture), len(ranked),
             "v3: per-book consensus; validated priors only; enrichment used conservatively for ranking", run_id),
        )
        return ranked


def run_predictions(database_url: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    global _USE_SCORE_STATE, _USE_PROMOTION_PRIOR
    db = (database_url or core.DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db) as conn:
        _USE_SCORE_STATE = _latest_bool(conn, "score_state_backtest_runs", "use_adjusted")
        _USE_PROMOTION_PRIOR = _latest_bool(conn, "promotion_prior_backtest_runs", "use_prior")

    previous = {
        "predict": core.predict_match,
        "history": core.history_rows,
        "prices": core.market_prices,
        "novig": core.no_vig_selected,
        "model_version": core.MODEL_VERSION,
        "policy_version": core.POLICY_VERSION,
    }
    try:
        core.predict_match = _primary_predict
        core.history_rows = _history_rows_v3
        core.market_prices = _consensus_market_prices
        core.no_vig_selected = _consensus_no_vig
        core.MODEL_VERSION = MODEL_VERSION
        core.POLICY_VERSION = POLICY_VERSION
        result = core.run_predictions(db, **kwargs)
        ranked = _rerank_and_annotate(db, int(result["run_id"]))
        result["model_version"] = MODEL_VERSION
        result["policy_version"] = POLICY_VERSION
        result["score_state_active"] = _USE_SCORE_STATE
        result["promotion_prior_active"] = _USE_PROMOTION_PRIOR
        result["top10_count"] = len(ranked)
        result["top10"] = [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in item.items()} for item in ranked
        ]
        print("PRODUCTION_V3_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")))
        return result
    finally:
        core.predict_match = previous["predict"]
        core.history_rows = previous["history"]
        core.market_prices = previous["prices"]
        core.no_vig_selected = previous["novig"]
        core.MODEL_VERSION = previous["model_version"]
        core.POLICY_VERSION = previous["policy_version"]


if __name__ == "__main__":
    print(json.dumps(run_predictions(), ensure_ascii=False, indent=2, default=str))
