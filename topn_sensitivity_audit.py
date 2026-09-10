#!/usr/bin/env python3
"""Leakage-safe Top-3/5/10 sensitivity audit for the frozen V1 model.

Purpose: test whether a shorter weekly list materially improves realized hit rate,
and expose fold x league x market stability without changing production predictions.
The 2026/27 live season is never used as an outcome/tuning fold here.
"""
from __future__ import annotations

import json
from collections import defaultdict
from statistics import mean
from typing import Any, Dict, List, Sequence, Tuple

import psycopg

from edge_structure_audit import (
    DATABASE_URL,
    MIN_HISTORY_MATCHES,
    TEST_SEASONS,
    _load,
    _order,
    _outcome,
    _week,
)
from model_engine_v1 import best_market, predict_match
from research_evaluation import selection_metrics, stable_seed, week_block_bootstrap

TOP_NS = (3, 5, 10)
PRIOR_N = 30.0
VERSION = "v1-topn-sensitivity-two-fold-v1"


def _r(v: Any, digits: int = 4):
    return None if v is None else round(float(v), digits)


def _rate(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    hits = sum(bool(r.get("hit")) for r in rows)
    return {
        "n": n,
        "hits": hits,
        "hit_rate": _r(hits / n) if n else None,
        "avg_confidence": _r(mean(float(r["confidence"]) for r in rows)) if n else None,
    }


def _selection_report(rows: Sequence[Dict[str, Any]], label: str) -> Dict[str, Any]:
    if not rows:
        return {"n": 0, "hits": 0, "hit_rate": None, "bootstrap95": {}}
    point = selection_metrics(rows)
    ci = week_block_bootstrap(
        rows,
        selection_metrics,
        iterations=2000,
        seed=stable_seed("topn-sensitivity:" + label),
    )
    base = _rate(rows)
    base.update({
        "model_brier": _r(point.get("model_brier"), 5),
        "bootstrap95": ci,
    })
    return base


def _weekly_top(candidates: Sequence[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    weekly: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        weekly[(str(row["fold"]), str(row["week"]))].append(row)
    out: List[Dict[str, Any]] = []
    for _, rows in sorted(weekly.items()):
        rows = sorted(
            rows,
            key=lambda r: (float(r["ranking"]), float(r["confidence"])),
            reverse=True,
        )
        out.extend(rows[:n])
    return out


def _shrink_rate(hits: int, n: int, parent: float | None, prior_n: float = PRIOR_N) -> float | None:
    if parent is None:
        return hits / n if n else None
    return (hits + prior_n * parent) / (n + prior_n) if n + prior_n else None


def _structure(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    market_rows: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    lm_rows: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        market_rows[str(r["market"])].append(r)
        lm_rows[(str(r["league"]), str(r["market"]))].append(r)

    market_parent = {m: _rate(part) for m, part in market_rows.items()}
    league_market: Dict[str, Any] = {}
    for (league, market), part in sorted(lm_rows.items()):
        raw = _rate(part)
        parent = market_parent.get(market, {}).get("hit_rate")
        fold_parts = {
            fold: _rate([r for r in part if str(r["fold"]) == str(fold)])
            for fold in TEST_SEASONS
        }
        nonempty = [v for v in fold_parts.values() if v["n"]]
        shrunk = _shrink_rate(raw["hits"], raw["n"], parent)
        league_market[f"{league}|{market}"] = {
            "raw": raw,
            "shrunk_hit_rate_to_market": _r(shrunk),
            "by_fold": fold_parts,
            "min_nonempty_fold_n": min((x["n"] for x in nonempty), default=0),
            "min_nonempty_fold_hit_rate": _r(min((x["hit_rate"] for x in nonempty if x["hit_rate"] is not None), default=0.0)),
            "folds_present": len(nonempty),
        }
    return {
        "market_parent": market_parent,
        "league_market": league_market,
        "shrinkage": f"empirical-Bayes style rate shrinkage to market parent, prior equivalent n={PRIOR_N:g}",
    }


def _score_candidates(database_url: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    with psycopg.connect(database_url) as conn:
        all_matches = _load(conn)
    by_div: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for m in all_matches:
        by_div[str(m["division"])].append(m)

    candidates: List[Dict[str, Any]] = []
    fold_meta: Dict[str, Any] = {}
    for test_season in sorted(TEST_SEASONS, key=_order):
        scored = skipped = 0
        for division, matches in by_div.items():
            train = [m for m in matches if _order(m["season_code"]) < _order(test_season)]
            tests = [m for m in matches if str(m["season_code"]) == str(test_season)]
            train.sort(key=lambda x: (x["match_date"], x["home_team"], x["away_team"]))
            tests.sort(key=lambda x: (x["match_date"], x["home_team"], x["away_team"]))
            if len(train) < MIN_HISTORY_MATCHES:
                skipped += len(tests)
                continue
            history = list(train)
            for match in tests:
                pred = predict_match(history, match["home_team"], match["away_team"])
                scored += 1
                bm = best_market(pred)
                outcome = _outcome(match, str(bm["market"]))
                if outcome is not None:
                    selected_yes = bool(bm["selection_yes"])
                    hit = bool(outcome) == selected_yes
                    confidence = float(bm["probability"])
                    quality = float(bm["data_quality"])
                    candidates.append({
                        "fold": str(test_season),
                        "week": _week(match["match_date"]),
                        "league": str(match["league_name"]),
                        "market": str(bm["market"]),
                        "selection": str(bm["selection"]),
                        "confidence": confidence,
                        "data_quality": quality,
                        "ranking": confidence * (0.75 + 0.25 * quality),
                        "hit": hit,
                        "p_selected": confidence,
                        "y_selected": int(hit),
                        "market_selected_p": None,
                        "execution_price": None,
                    })
                history.append(match)
        fold_meta[str(test_season)] = {"matches_scored": scored, "matches_skipped": skipped}
    return candidates, fold_meta


def run(database_url: str = DATABASE_URL) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    candidates, fold_meta = _score_candidates(database_url)
    sensitivity: Dict[str, Any] = {}
    for n in TOP_NS:
        picks = _weekly_top(candidates, n)
        sensitivity[str(n)] = {
            "overall": _selection_report(picks, f"top{n}:overall"),
            "by_fold": {
                fold: _selection_report([r for r in picks if r["fold"] == fold], f"top{n}:fold:{fold}")
                for fold in TEST_SEASONS
            },
            "structure": _structure(picks),
        }

    result = {
        "version": VERSION,
        "purpose": "research_only_no_production_change",
        "protocol": {
            "sequential_history": True,
            "test_seasons": list(TEST_SEASONS),
            "live_2627_outcomes_used": False,
            "top_n_values": list(TOP_NS),
            "bootstrap": "ISO-week block, 2000 iterations",
            "small_n": "league-market rates shrunk toward market parent with prior n=30",
        },
        "folds": fold_meta,
        "candidate_rows": len(candidates),
        "sensitivity": sensitivity,
        "interpretation_rule": "Do not choose a Top-N from pooled hit rate alone; require repeatable fold behavior and adequate sample. This audit never promotes production policy by itself.",
    }
    print("TOPN_SENSITIVITY_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
