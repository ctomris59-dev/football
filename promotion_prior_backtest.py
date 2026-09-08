#!/usr/bin/env python3
"""Leakage-safe validation of promoted-team priors.

Train transfer coefficients on 2023/24 second tier -> 2024/25 top flight,
then test them on promoted teams entering the 2025/26 Big Five. Only the first
8 league appearances per promoted club are evaluated, where the prior matters
most. Production may use the prior only when this test improves mean Brier.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import psycopg

from model_engine_v1 import predict_match
from promotion_prior_builder import (
    PARENTS, METRICS, aggregate, baselines, canon, learn_beta, relative, rows_for_table,
)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
VERSION = "promotion-prior-oos-v1"
TEST_MATCHES_PER_TEAM = int(os.getenv("PROMOTION_PRIOR_TEST_MATCHES", "8"))

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS promotion_prior_backtest_runs(
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  version TEXT NOT NULL,
  status TEXT NOT NULL,
  matches INTEGER NOT NULL DEFAULT 0,
  promoted_teams INTEGER NOT NULL DEFAULT 0,
  raw_brier DOUBLE PRECISION,
  prior_brier DOUBLE PRECISION,
  raw_goals_brier DOUBLE PRECISION,
  prior_goals_brier DOUBLE PRECISION,
  raw_btts_brier DOUBLE PRECISION,
  prior_btts_brier DOUBLE PRECISION,
  raw_corners_brier DOUBLE PRECISION,
  prior_corners_brier DOUBLE PRECISION,
  raw_high_n INTEGER NOT NULL DEFAULT 0,
  raw_high_hit DOUBLE PRECISION,
  prior_high_n INTEGER NOT NULL DEFAULT 0,
  prior_high_hit DOUBLE PRECISION,
  use_prior BOOLEAN NOT NULL DEFAULT FALSE,
  message TEXT
);
"""


def _top_rows(conn, league: str, season: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in conn.execute(
        """SELECT match_date,home_team,away_team,home_goals,away_goals,
                  home_shots_on_target,away_shots_on_target,home_corners,away_corners
           FROM football_data_matches
           WHERE league_name=%s AND season_code=%s
             AND home_goals IS NOT NULL AND away_goals IS NOT NULL
           ORDER BY match_date""",
        (league, season),
    ).fetchall():
        out.append({
            "match_date": r[0], "home_team": canon(r[1]), "away_team": canon(r[2]),
            "home_goals": float(r[3]), "away_goals": float(r[4]),
            "home_shots_on_target": None if r[5] is None else float(r[5]),
            "away_shots_on_target": None if r[6] is None else float(r[6]),
            "home_corners": None if r[7] is None else float(r[7]),
            "away_corners": None if r[8] is None else float(r[8]),
        })
    return out


def _match_baseline(rows: List[Dict[str, Any]], key: str, default: float) -> float:
    vals = [float(r[key]) for r in rows if r.get(key) is not None]
    return sum(vals) / len(vals) if vals else default


def _transfer_priors(conn, parent: str) -> Dict[str, Dict[str, float]]:
    # Strictly earlier training cycle: 23/24 lower -> 24/25 top.
    lower_train, _ = aggregate(rows_for_table(conn, "second_tier_matches", "2324", parent))
    top_train, _ = aggregate(rows_for_table(conn, "football_data_matches", "2425", parent))
    lr = relative(lower_train, baselines(lower_train))
    tr = relative(top_train, baselines(top_train))
    promoted_train = sorted(set(lr).intersection(tr))
    betas: Dict[str, float] = {}
    for metric in METRICS:
        betas[metric] = learn_beta([(lr[t][metric], tr[t][metric]) for t in promoted_train])

    # Apply the learned coefficients to 24/25 lower-tier clubs that appear in 25/26 top flight.
    lower_test, _ = aggregate(rows_for_table(conn, "second_tier_matches", "2425", parent))
    lower_test_rel = relative(lower_test, baselines(lower_test))
    top_test, _ = aggregate(rows_for_table(conn, "football_data_matches", "2526", parent))
    promoted_test = sorted(set(lower_test_rel).intersection(top_test))
    return {
        team: {
            metric: max(0.55, min(1.65, 1.0 + betas[metric] * (lower_test_rel[team][metric] - 1.0)))
            for metric in METRICS
        }
        for team in promoted_test
    }


def _pseudo_rows(team: str, prior: Dict[str, float], baseline: Dict[str, float]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    start = date(2025, 7, 1)
    for i in range(3):
        out.append({
            "match_date": start + timedelta(days=i),
            "home_team": team, "away_team": "__promotion_prior__",
            "home_goals": baseline["home_goals"] * prior["goals_for"],
            "away_goals": baseline["away_goals"] * prior["goals_against"],
            "home_shots_on_target": baseline["home_sot"] * prior["sot_for"],
            "away_shots_on_target": baseline["away_sot"] * prior["sot_against"],
            "home_corners": baseline["home_corners"] * prior["corners_for"],
            "away_corners": baseline["away_corners"] * prior["corners_against"],
        })
        out.append({
            "match_date": start + timedelta(days=3 + i),
            "home_team": "__promotion_prior__", "away_team": team,
            "home_goals": baseline["home_goals"] * prior["goals_against"],
            "away_goals": baseline["away_goals"] * prior["goals_for"],
            "home_shots_on_target": baseline["home_sot"] * prior["sot_against"],
            "away_shots_on_target": baseline["away_sot"] * prior["sot_for"],
            "home_corners": baseline["home_corners"] * prior["corners_against"],
            "away_corners": baseline["away_corners"] * prior["corners_for"],
        })
    return out


def run_backtest(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
        rid = conn.execute(
            "INSERT INTO promotion_prior_backtest_runs(version,status) VALUES(%s,'running') RETURNING id",
            (VERSION,),
        ).fetchone()[0]
        n = promoted_total = raw_high_n = prior_high_n = raw_high_hit = prior_high_hit = 0
        rb = pb = 0.0
        rg = pg = rbt = pbt = rc = pc = 0.0
        try:
            for league in PARENTS:
                priors = _transfer_priors(conn, league)
                if not priors:
                    continue
                promoted_total += len(priors)
                train = _top_rows(conn, league, "2425")
                test = _top_rows(conn, league, "2526")
                if not train or not test:
                    continue
                baseline = {
                    "home_goals": _match_baseline(train, "home_goals", 1.5),
                    "away_goals": _match_baseline(train, "away_goals", 1.2),
                    "home_sot": _match_baseline(train, "home_shots_on_target", 4.8),
                    "away_sot": _match_baseline(train, "away_shots_on_target", 4.0),
                    "home_corners": _match_baseline(train, "home_corners", 5.3),
                    "away_corners": _match_baseline(train, "away_corners", 4.4),
                }
                raw_hist = list(train)
                prior_hist = list(train)
                for team, prior in priors.items():
                    prior_hist.extend(_pseudo_rows(team, prior, baseline))
                prior_hist.sort(key=lambda x: x["match_date"])
                appearances = defaultdict(int)

                for row in test:
                    home, away = row["home_team"], row["away_team"]
                    relevant = [t for t in (home, away) if t in priors and appearances[t] < TEST_MATCHES_PER_TEAM]
                    if relevant:
                        pr = predict_match(raw_hist, home, away)
                        pp = predict_match(prior_hist, home, away)
                        y_g = 1.0 if row["home_goals"] + row["away_goals"] > 2 else 0.0
                        y_b = 1.0 if row["home_goals"] > 0 and row["away_goals"] > 0 else 0.0
                        corners = (row.get("home_corners") or 0.0) + (row.get("away_corners") or 0.0)
                        y_c = 1.0 if corners > 8 else 0.0
                        raw_ps = [pr.p_over_2_5, pr.p_btts, pr.p_corners_over_8_5]
                        prior_ps = [pp.p_over_2_5, pp.p_btts, pp.p_corners_over_8_5]
                        ys = [y_g, y_b, y_c]
                        rg += (raw_ps[0] - y_g) ** 2; pg += (prior_ps[0] - y_g) ** 2
                        rbt += (raw_ps[1] - y_b) ** 2; pbt += (prior_ps[1] - y_b) ** 2
                        rc += (raw_ps[2] - y_c) ** 2; pc += (prior_ps[2] - y_c) ** 2
                        rb += sum((p - y) ** 2 for p, y in zip(raw_ps, ys)) / 3.0
                        pb += sum((p - y) ** 2 for p, y in zip(prior_ps, ys)) / 3.0
                        for ps, which in ((raw_ps, "raw"), (prior_ps, "prior")):
                            idx = max(range(3), key=lambda j: max(ps[j], 1.0 - ps[j]))
                            conf = max(ps[idx], 1.0 - ps[idx])
                            correct = int((ps[idx] >= 0.5) == bool(ys[idx]))
                            if conf >= 0.65:
                                if which == "raw": raw_high_n += 1; raw_high_hit += correct
                                else: prior_high_n += 1; prior_high_hit += correct
                        n += 1
                    for t in (home, away):
                        if t in priors:
                            appearances[t] += 1
                    raw_hist.append(row); prior_hist.append(row)

            raw_brier = rb / n if n else None
            prior_brier = pb / n if n else None
            use = bool(n >= 40 and raw_brier is not None and prior_brier is not None and prior_brier < raw_brier)
            vals = {
                "matches": n, "promoted_teams": promoted_total,
                "raw_brier": raw_brier, "prior_brier": prior_brier,
                "raw_goals_brier": rg / n if n else None, "prior_goals_brier": pg / n if n else None,
                "raw_btts_brier": rbt / n if n else None, "prior_btts_brier": pbt / n if n else None,
                "raw_corners_brier": rc / n if n else None, "prior_corners_brier": pc / n if n else None,
                "raw_high_n": raw_high_n, "raw_high_hit": raw_high_hit / raw_high_n if raw_high_n else None,
                "prior_high_n": prior_high_n, "prior_high_hit": prior_high_hit / prior_high_n if prior_high_n else None,
                "use_prior": use,
            }
            conn.execute(
                """UPDATE promotion_prior_backtest_runs SET finished_at=NOW(),status='success',matches=%s,promoted_teams=%s,
                   raw_brier=%s,prior_brier=%s,raw_goals_brier=%s,prior_goals_brier=%s,raw_btts_brier=%s,prior_btts_brier=%s,
                   raw_corners_brier=%s,prior_corners_brier=%s,raw_high_n=%s,raw_high_hit=%s,prior_high_n=%s,prior_high_hit=%s,
                   use_prior=%s,message='OOS: train 2324->2425, test first promoted-team matches in 2526' WHERE id=%s""",
                (n,promoted_total,raw_brier,prior_brier,vals["raw_goals_brier"],vals["prior_goals_brier"],
                 vals["raw_btts_brier"],vals["prior_btts_brier"],vals["raw_corners_brier"],vals["prior_corners_brier"],
                 raw_high_n,vals["raw_high_hit"],prior_high_n,vals["prior_high_hit"],use,rid),
            )
            print("PROMOTION_PRIOR_BACKTEST_RESULT", json.dumps(vals, separators=(",", ":")))
            return {"status": "success", **vals}
        except Exception as exc:
            conn.execute(
                "UPDATE promotion_prior_backtest_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                (str(exc)[:1000], rid),
            )
            raise


if __name__ == "__main__":
    print(json.dumps(run_backtest(), ensure_ascii=False, indent=2, default=str))
