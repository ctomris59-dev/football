#!/usr/bin/env python3
"""Leakage-safe weekly Over 2.5 Top-3/5/10 sensitivity audit for frozen V1.

Research-only. This deliberately ignores BTTS/corners and tests only the positive
Over 2.5 side, preserving the same two expanding historical folds and sequential
history used by the structural audit. The live 2026/27 season is never used as an
outcome/tuning fold.
"""
from __future__ import annotations

import json
from collections import defaultdict
from statistics import mean
from typing import Any, Dict, List, Sequence, Tuple

import psycopg

from edge_structure_audit import DATABASE_URL, MIN_HISTORY_MATCHES, TEST_SEASONS, _load, _order, _week
from model_engine_v1 import predict_match
from research_evaluation import selection_metrics, stable_seed, week_block_bootstrap

TOP_NS = (3, 5, 10)
VERSION = "v1-over25-only-two-fold-v1"


def _r(v: Any, digits: int = 4):
    return None if v is None else round(float(v), digits)


def _report(rows: Sequence[Dict[str, Any]], label: str) -> Dict[str, Any]:
    if not rows:
        return {"n": 0, "hits": 0, "hit_rate": None, "avg_confidence": None, "model_brier": None, "bootstrap95": {}}
    point = selection_metrics(rows)
    ci = week_block_bootstrap(
        rows,
        selection_metrics,
        iterations=2000,
        seed=stable_seed("over25-only:" + label),
    )
    n = len(rows)
    hits = sum(bool(r["hit"]) for r in rows)
    return {
        "n": n,
        "hits": hits,
        "hit_rate": _r(hits / n),
        "avg_confidence": _r(mean(float(r["confidence"]) for r in rows)),
        "model_brier": _r(point.get("model_brier"), 5),
        "bootstrap95": ci,
    }


def _weekly_top(rows: Sequence[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    weekly: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        weekly[(str(row["fold"]), str(row["week"]))].append(row)
    out: List[Dict[str, Any]] = []
    for _, part in sorted(weekly.items()):
        part = sorted(part, key=lambda r: (float(r["ranking"]), float(r["confidence"])), reverse=True)
        out.extend(part[:n])
    return out


def _league_report(rows: Sequence[Dict[str, Any]], label: str) -> Dict[str, Any]:
    leagues: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        leagues[str(row["league"])].append(row)
    return {
        league: {
            "overall": _report(part, f"{label}:{league}:overall"),
            "by_fold": {
                fold: _report([r for r in part if str(r["fold"]) == str(fold)], f"{label}:{league}:{fold}")
                for fold in TEST_SEASONS
            },
        }
        for league, part in sorted(leagues.items())
    }


def _score(database_url: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    with psycopg.connect(database_url) as conn:
        all_matches = _load(conn)

    by_div: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for m in all_matches:
        by_div[str(m["division"])].append(m)

    rows: List[Dict[str, Any]] = []
    fold_meta: Dict[str, Any] = {}
    for test_season in sorted(TEST_SEASONS, key=_order):
        scored = skipped = usable = 0
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
                outcome = match.get("over_2_5")
                if outcome is not None:
                    usable += 1
                    p = float(pred.p_over_2_5)
                    q = float(pred.data_quality)
                    rows.append({
                        "fold": str(test_season),
                        "week": _week(match["match_date"]),
                        "league": str(match["league_name"]),
                        "market": "over_2_5",
                        "selection": "2.5 ÜST",
                        "confidence": p,
                        "data_quality": q,
                        "ranking": p * (0.75 + 0.25 * q),
                        "hit": bool(outcome),
                        "p_selected": p,
                        "y_selected": int(bool(outcome)),
                        "market_selected_p": None,
                        "execution_price": None,
                    })
                history.append(match)
        fold_meta[str(test_season)] = {"matches_scored": scored, "matches_usable": usable, "matches_skipped": skipped}
    return rows, fold_meta


def run(database_url: str = DATABASE_URL) -> Dict[str, Any]:
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    candidates, folds = _score(database_url)
    sensitivity: Dict[str, Any] = {}
    for n in TOP_NS:
        picks = _weekly_top(candidates, n)
        sensitivity[str(n)] = {
            "overall": _report(picks, f"top{n}:overall"),
            "by_fold": {
                fold: _report([r for r in picks if str(r["fold"]) == str(fold)], f"top{n}:fold:{fold}")
                for fold in TEST_SEASONS
            },
            "by_league": _league_report(picks, f"top{n}"),
        }

    result = {
        "version": VERSION,
        "purpose": "research_only_over25_positive_side",
        "protocol": {
            "market": "over_2_5",
            "selection": "2.5 ÜST only",
            "sequential_history": True,
            "test_seasons": list(TEST_SEASONS),
            "live_2627_outcomes_used": False,
            "top_n_values": list(TOP_NS),
            "bootstrap": "ISO-week block, 2000 iterations",
        },
        "folds": folds,
        "candidate_rows": len(candidates),
        "sensitivity": sensitivity,
        "interpretation_rule": "Do not call a weekly Over 2.5 list >=70% reliable unless performance is repeatable across both folds and its uncertainty supports that claim.",
    }
    print("OVER25_SENSITIVITY_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
