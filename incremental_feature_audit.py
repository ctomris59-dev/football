#!/usr/bin/env python3
"""Leakage-safe one-at-a-time ranking audit for match-environment challengers.

Each candidate feature is compared independently with the frozen V1 weekly Top-10
ranking. No 2026/27 result is read. The feature formulas are shared with production
through incremental_feature_policy.py so a passing audit cannot silently drift from
the live implementation.

Protocol:
- Fold 1: earlier history -> 2024/25
- Fold 2: earlier history -> 2025/26
- sequential within-season updates only
- ISO-week block bootstrap, stratified by fold
- feature availability/coverage reported
- predeclared stability gate; no post-hoc threshold search
"""
from __future__ import annotations

import json
import math
import os
import random
from collections import defaultdict
from datetime import date, datetime
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg
from psycopg.types.json import Jsonb

from incremental_feature_policy import (
    ACTIVATABLE_FEATURES,
    FEATURE_ORDER,
    POLICY_KEY,
    POLICY_VERSION,
    elo_factor,
    lineup_stability_factor,
    pace_factor,
    venue_factor,
    xg_regression_factor,
)
from match_environment_features import (
    lineup_stability,
    pace_proxy_score,
    venue_strength_index,
    xg_regression_profile,
)
from model_engine_v1 import best_market, predict_match
from production_predictor import canon

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SEASONS = [x.strip() for x in os.getenv("INCREMENTAL_AUDIT_SEASONS", "2324,2425,2526").split(",") if x.strip()]
TEST_SEASONS = [x.strip() for x in os.getenv("INCREMENTAL_AUDIT_TEST_SEASONS", "2425,2526").split(",") if x.strip()]
LIVE_HOLDOUT = os.getenv("INCREMENTAL_AUDIT_LIVE_HOLDOUT", "2627").strip()
TOP_N = int(os.getenv("INCREMENTAL_AUDIT_TOP_N", "10"))
MIN_HISTORY_MATCHES = int(os.getenv("INCREMENTAL_AUDIT_MIN_HISTORY_MATCHES", "150"))
BOOTSTRAP_ITERATIONS = int(os.getenv("INCREMENTAL_AUDIT_BOOTSTRAP_ITERATIONS", "2000"))
XG_RECENT = int(os.getenv("INCREMENTAL_AUDIT_XG_RECENT", "6"))
PACE_RECENT = int(os.getenv("INCREMENTAL_AUDIT_PACE_RECENT", "18"))
VENUE_RECENT = int(os.getenv("INCREMENTAL_AUDIT_VENUE_RECENT", "12"))

# Predeclared promotion thresholds. These are intentionally modest but require
# repeatability in both folds and a strong majority of block-bootstrap resamples.
MIN_POOLED_HIT_GAIN = float(os.getenv("INCREMENTAL_AUDIT_MIN_POOLED_HIT_GAIN", "0.005"))
MIN_FOLD_HIT_GAIN = float(os.getenv("INCREMENTAL_AUDIT_MIN_FOLD_HIT_GAIN", "0.0"))
MIN_FOLD_COVERAGE = float(os.getenv("INCREMENTAL_AUDIT_MIN_FOLD_COVERAGE", "0.50"))
MIN_CHANGED_PICKS = int(os.getenv("INCREMENTAL_AUDIT_MIN_CHANGED_PICKS", "20"))
MIN_BOOTSTRAP_P_IMPROVE = float(os.getenv("INCREMENTAL_AUDIT_MIN_BOOTSTRAP_P_IMPROVE", "0.80"))
MAX_SELECTED_BRIER_DELTA = float(os.getenv("INCREMENTAL_AUDIT_MAX_SELECTED_BRIER_DELTA", "0.0"))

VERSION = "incremental-feature-two-fold-week-block-v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS incremental_feature_audit_runs(
 id BIGSERIAL PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 version TEXT NOT NULL,
 status TEXT NOT NULL,
 results JSONB,
 message TEXT
);
CREATE TABLE IF NOT EXISTS policy_activation_registry(
 policy_key TEXT PRIMARY KEY,
 policy_version TEXT NOT NULL,
 active_mode TEXT NOT NULL,
 validated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
 reason TEXT
);
"""


def _order(code: str) -> int:
    s = str(code or "").strip()
    return int(s[:2]) if len(s) == 4 and s.isdigit() else -1


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _week(value: Any) -> str:
    d = _as_date(value)
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _season_year(code: str) -> int:
    return 2000 + int(str(code)[:2])


def _load_matches(conn) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """SELECT season_code,division,league_name,match_date,home_team,away_team,
                  home_goals,away_goals,home_shots,away_shots,
                  home_shots_on_target,away_shots_on_target,
                  home_corners,away_corners,total_corners,over_2_5,btts,corners_over_8_5,
                  odds_over_2_5,odds_under_2_5
             FROM football_data_matches
            WHERE season_code = ANY(%s)
              AND home_goals IS NOT NULL AND away_goals IS NOT NULL
            ORDER BY division,match_date,home_team,away_team""",
        (SEASONS,),
    )
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _load_xg(conn) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    out: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    try:
        rows = conn.execute(
            """SELECT league_name,match_date,home_team,away_team,
                      home_goals,away_goals,home_xg,away_xg
                 FROM understat_matches
                WHERE is_result=TRUE AND match_date IS NOT NULL
                  AND home_goals IS NOT NULL AND away_goals IS NOT NULL
                  AND home_xg IS NOT NULL AND away_xg IS NOT NULL
                ORDER BY match_date"""
        ).fetchall()
    except Exception:
        return out
    for league, dt, home, away, hg, ag, hx, ax in rows:
        d = _as_date(dt)
        lh, la = canon(home), canon(away)
        out[(str(league), lh)].append({"date": d, "gf": float(hg), "ga": float(ag), "xf": float(hx), "xa": float(ax)})
        out[(str(league), la)].append({"date": d, "gf": float(ag), "ga": float(hg), "xf": float(ax), "xa": float(hx)})
    return out


def _xg_profile(
    xg_rows: Dict[Tuple[str, str], List[Dict[str, Any]]],
    league: str,
    team: str,
    before: Any,
) -> Dict[str, Any]:
    d = _as_date(before)
    rows = xg_rows.get((str(league), canon(team)), [])
    prior = [r for r in rows if r["date"] < d][-max(1, XG_RECENT):]
    if not prior:
        return xg_regression_profile(None, None, None, None, matches=0)
    return xg_regression_profile(
        mean(r["gf"] for r in prior),
        mean(r["xf"] for r in prior),
        mean(r["ga"] for r in prior),
        mean(r["xa"] for r in prior),
        matches=len(prior),
    )


def _recent_team_matches(history: Sequence[Dict[str, Any]], team: str, n: int) -> List[Dict[str, Any]]:
    ct = canon(team)
    out = []
    for m in reversed(history):
        if ct in (canon(m.get("home_team")), canon(m.get("away_team"))):
            out.append(m)
        if len(out) >= n:
            break
    return out


def _env_total(rows: Sequence[Dict[str, Any]], a: str, b: str) -> Optional[float]:
    vals = []
    for m in rows:
        x, y = m.get(a), m.get(b)
        if x is not None and y is not None:
            vals.append(float(x) + float(y))
    return mean(vals) if vals else None


def _pace_score(history: Sequence[Dict[str, Any]], home: str, away: str) -> Optional[float]:
    hr = _recent_team_matches(history, home, PACE_RECENT)
    ar = _recent_team_matches(history, away, PACE_RECENT)
    hg = _env_total(hr, "home_goals", "away_goals")
    ag = _env_total(ar, "home_goals", "away_goals")
    hc = _env_total(hr, "home_corners", "away_corners")
    ac = _env_total(ar, "home_corners", "away_corners")
    goal = max(.55, min(1.45, ((hg + ag) / 2.0) / 2.6)) if hg is not None and ag is not None else None
    corner = max(.55, min(1.45, ((hc + ac) / 2.0) / 9.5)) if hc is not None and ac is not None else None
    return pace_proxy_score(goal, corner)


def _avg(values: Iterable[Any]) -> Optional[float]:
    vals = []
    for v in values:
        try:
            if v is not None:
                vals.append(float(v))
        except (TypeError, ValueError):
            pass
    return mean(vals) if vals else None


def _venue_profile(history: Sequence[Dict[str, Any]], team: str, venue: str) -> Dict[str, Any]:
    ct = canon(team)
    gf: List[float] = []
    ga: List[float] = []
    sf: List[float] = []
    sa: List[float] = []
    used = 0
    for row in reversed(history):
        if venue == "home" and canon(row.get("home_team")) == ct:
            og, pg = row.get("home_goals"), row.get("away_goals")
            os_, ps = row.get("home_shots_on_target"), row.get("away_shots_on_target")
        elif venue == "away" and canon(row.get("away_team")) == ct:
            og, pg = row.get("away_goals"), row.get("home_goals")
            os_, ps = row.get("away_shots_on_target"), row.get("home_shots_on_target")
        else:
            continue
        if og is not None and pg is not None:
            gf.append(float(og)); ga.append(float(pg))
        if os_ is not None and ps is not None:
            sf.append(float(os_)); sa.append(float(ps))
        used += 1
        if used >= VENUE_RECENT:
            break
    return {"matches": used, "gf": _avg(gf), "ga": _avg(ga), "sf": _avg(sf), "sa": _avg(sa)}


def _venue_indices(history: Sequence[Dict[str, Any]], home: str, away: str) -> Tuple[Optional[float], Optional[float]]:
    bh = _avg(m.get("home_goals") for m in history)
    ba = _avg(m.get("away_goals") for m in history)
    if bh is None or ba is None:
        return None, None
    hp = _venue_profile(history, home, "home")
    ap = _venue_profile(history, away, "away")
    hi = venue_strength_index(hp["gf"], hp["ga"], hp["sf"], hp["sa"], league_goals_for=bh, league_goals_against=ba)
    ai = venue_strength_index(ap["gf"], ap["ga"], ap["sf"], ap["sa"], league_goals_for=ba, league_goals_against=bh)
    return hi, ai


def _extract_lineup_objects(obj: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        if isinstance(obj.get("team"), dict) and isinstance(obj.get("startXI"), list):
            out.append(obj)
        for v in obj.values():
            out.extend(_extract_lineup_objects(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_extract_lineup_objects(v))
    return out


def _starter_names(obj: Dict[str, Any]) -> List[str]:
    out = []
    for item in obj.get("startXI") or []:
        if not isinstance(item, dict):
            continue
        p = item.get("player") if isinstance(item.get("player"), dict) else item
        name = p.get("name") if isinstance(p, dict) else None
        if name:
            out.append(canon(name))
    return [x for x in out if x]


def _load_lineups(conn) -> Dict[Tuple[int, str], List[Tuple[date, frozenset[str]]]]:
    grouped: Dict[Tuple[int, str], List[Tuple[date, frozenset[str]]]] = defaultdict(list)
    seen = set()
    try:
        rows = conn.execute(
            """SELECT e.season,e.event_id,e.match_date,p.team_name,p.player_name
                 FROM espn_historical_events e
                 JOIN espn_historical_lineup_players p ON p.event_id=e.event_id
                WHERE e.season IN (2024,2025) AND e.summary_status='success' AND p.starter=TRUE
                ORDER BY e.season,e.match_date,e.event_id,p.team_name"""
        ).fetchall()
        temp: Dict[Tuple[int, str, date, str], List[str]] = defaultdict(list)
        for season, eid, dt, team, name in rows:
            temp[(int(season), str(eid), _as_date(dt), str(team))].append(canon(name))
        for (season, _eid, d, team), names in temp.items():
            s = frozenset(x for x in names if x)
            if len(s) < 7:
                continue
            key = (season, d, canon(team), s)
            if key in seen:
                continue
            seen.add(key)
            grouped[(season, canon(team))].append((d, s))
    except Exception:
        pass

    # Cached API-Football lineups are a fallback; dedupe by date/team/starter set.
    try:
        rows = conn.execute(
            """SELECT f.season,f.fixture_date,d.lineups
                 FROM fixtures f JOIN fixture_details d ON d.fixture_id=f.fixture_id
                WHERE f.season IN (2024,2025)
                  AND d.lineups IS NOT NULL AND f.status_short IN ('FT','AET','PEN')
                ORDER BY f.fixture_date"""
        ).fetchall()
        for season, dt, raw in rows:
            d = _as_date(dt)
            for lu in _extract_lineup_objects(raw):
                team = canon((lu.get("team") or {}).get("name"))
                s = frozenset(_starter_names(lu))
                if not team or len(s) < 7:
                    continue
                key = (int(season), d, team, s)
                if key in seen:
                    continue
                seen.add(key)
                grouped[(int(season), team)].append((d, s))
    except Exception:
        pass

    for key in grouped:
        grouped[key].sort(key=lambda x: x[0])
    return grouped


def _lineup_stability_at(
    lineups: Dict[Tuple[int, str], List[Tuple[date, frozenset[str]]]],
    season_year: int,
    team: str,
    before: Any,
) -> Optional[float]:
    d = _as_date(before)
    prior = [s for dt, s in lineups.get((season_year, canon(team)), []) if dt < d][-4:]
    if len(prior) < 3:
        return None
    overlaps = []
    for a, b in zip(prior[:-1], prior[1:]):
        overlaps.append(len(a & b) / max(1, min(11, len(a), len(b))))
    continuity = mean(overlaps) if overlaps else None
    return lineup_stability(continuity, None, None, False) if continuity is not None else None


BASE_ELO = 1500.0
ELO_K = 20.0
ELO_HOME_ADV = 60.0
ELO_CARRY = 0.85


def _elo_expected(home: float, away: float) -> float:
    return 1.0 / (1.0 + 10.0 ** (-(home + ELO_HOME_ADV - away) / 400.0))


def _elo_update(ratings: Dict[str, float], match: Dict[str, Any]) -> None:
    h, a = canon(match["home_team"]), canon(match["away_team"])
    rh, ra = ratings.get(h, BASE_ELO), ratings.get(a, BASE_ELO)
    hg, ag = int(match["home_goals"]), int(match["away_goals"])
    actual = 1.0 if hg > ag else (0.0 if hg < ag else 0.5)
    delta = ELO_K * (actual - _elo_expected(rh, ra))
    ratings[h] = rh + delta
    ratings[a] = ra - delta


def _elo_from_train(train: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    ratings: Dict[str, float] = {}
    current = None
    for m in sorted(train, key=lambda x: (_order(str(x["season_code"])), x["match_date"], x["home_team"], x["away_team"])):
        season = str(m["season_code"])
        if current is None:
            current = season
        elif season != current:
            ratings = {k: BASE_ELO + (v - BASE_ELO) * ELO_CARRY for k, v in ratings.items()}
            current = season
        _elo_update(ratings, m)
    return {k: BASE_ELO + (v - BASE_ELO) * ELO_CARRY for k, v in ratings.items()}


def _outcome(match: Dict[str, Any], market: str) -> Optional[bool]:
    value = match.get(market)
    return None if value is None else bool(value)


def _candidate_id(row: Dict[str, Any]) -> str:
    return "|".join([str(row["fold"]), str(row["division"]), str(row["date"]), str(row["home"]), str(row["away"]), str(row["market"])])


def _metrics(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"n": 0, "hits": 0, "hit_rate": None, "selected_brier": None, "avg_confidence": None}
    hits = sum(bool(r["hit"]) for r in rows)
    return {
        "n": len(rows),
        "hits": hits,
        "hit_rate": round(hits / len(rows), 5),
        "selected_brier": round(mean((float(r["confidence"]) - int(bool(r["hit"]))) ** 2 for r in rows), 6),
        "avg_confidence": round(mean(float(r["confidence"]) for r in rows), 5),
    }


def _select_week(rows: Sequence[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    return sorted(rows, key=lambda r: (float(r[key]), float(r["confidence"])), reverse=True)[:TOP_N]


def _selection_sets(
    weekly: Dict[Tuple[str, str], List[Dict[str, Any]]],
    key: str,
) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str], List[Dict[str, Any]]]]:
    all_rows: List[Dict[str, Any]] = []
    by_week: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for wk, rows in sorted(weekly.items()):
        picked = _select_week(rows, key)
        by_week[wk] = picked
        all_rows.extend(picked)
    return all_rows, by_week


def _quantile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - pos) + xs[hi] * (pos - lo)


def _bootstrap_delta(
    base_by_week: Dict[Tuple[str, str], List[Dict[str, Any]]],
    chal_by_week: Dict[Tuple[str, str], List[Dict[str, Any]]],
    *,
    seed: int,
) -> Dict[str, Any]:
    rng = random.Random(seed)
    by_fold: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for key in base_by_week:
        if key in chal_by_week:
            by_fold[key[0]].append(key)
    deltas: List[float] = []
    for _ in range(max(1, BOOTSTRAP_ITERATIONS)):
        bh = bn = ch = cn = 0
        for fold in TEST_SEASONS:
            keys = by_fold.get(fold, [])
            if not keys:
                continue
            for _j in range(len(keys)):
                key = keys[rng.randrange(len(keys))]
                br = base_by_week[key]
                cr = chal_by_week[key]
                bh += sum(bool(r["hit"]) for r in br); bn += len(br)
                ch += sum(bool(r["hit"]) for r in cr); cn += len(cr)
        if bn and cn:
            deltas.append(ch / cn - bh / bn)
    return {
        "iterations": len(deltas),
        "ci95": [
            round(_quantile(deltas, .025), 6) if deltas else None,
            round(_quantile(deltas, .975), 6) if deltas else None,
        ],
        "p_gain_gt_0": round(sum(x > 0 for x in deltas) / len(deltas), 4) if deltas else None,
        "median_delta": round(_quantile(deltas, .50), 6) if deltas else None,
    }


def _validate_protocol() -> None:
    if LIVE_HOLDOUT in TEST_SEASONS:
        raise RuntimeError(f"Protected live holdout {LIVE_HOLDOUT} cannot be used in incremental feature audit")
    if tuple(TEST_SEASONS) != ("2425", "2526"):
        raise RuntimeError("Incremental audit test seasons must remain exactly 2425,2526 for this gate")
    for test in TEST_SEASONS:
        if not any(_order(s) < _order(test) for s in SEASONS):
            raise RuntimeError(f"No earlier season available for test fold {test}")


def _gate(feature: str, report: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    overall = report["overall"]
    base = report["baseline_overall"]
    gain = (overall["hit_rate"] or 0.0) - (base["hit_rate"] or 0.0)
    brier_delta = (overall["selected_brier"] or 9.0) - (base["selected_brier"] or 0.0)
    if feature not in ACTIVATABLE_FEATURES:
        reasons.append("feature_not_production_compatible")
    if gain < MIN_POOLED_HIT_GAIN:
        reasons.append("pooled_hit_gain_below_threshold")
    for fold in TEST_SEASONS:
        fm, bm = report["by_fold"][fold], report["baseline_by_fold"][fold]
        fgain = (fm["hit_rate"] or 0.0) - (bm["hit_rate"] or 0.0)
        if fgain < MIN_FOLD_HIT_GAIN:
            reasons.append(f"fold_{fold}_regressed")
        if float(report["coverage_by_fold"].get(fold) or 0.0) < MIN_FOLD_COVERAGE:
            reasons.append(f"fold_{fold}_coverage_low")
    if int(report.get("changed_picks") or 0) < MIN_CHANGED_PICKS:
        reasons.append("too_few_changed_picks")
    if float((report.get("bootstrap") or {}).get("p_gain_gt_0") or 0.0) < MIN_BOOTSTRAP_P_IMPROVE:
        reasons.append("bootstrap_support_low")
    if brier_delta > MAX_SELECTED_BRIER_DELTA:
        reasons.append("selected_brier_worse")
    return not reasons, reasons


def run_audit(database_url: Optional[str] = None) -> Dict[str, Any]:
    _validate_protocol()
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        rid = conn.execute(
            "INSERT INTO incremental_feature_audit_runs(version,status) VALUES(%s,'running') RETURNING id",
            (VERSION,),
        ).fetchone()[0]
        try:
            matches = _load_matches(conn)
            xg_rows = _load_xg(conn)
            lineups = _load_lineups(conn)
            by_div: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for m in matches:
                by_div[str(m["division"])].append(m)

            weekly: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
            coverage_counts: Dict[str, Dict[str, List[int]]] = {
                f: {fold: [0, 0] for fold in TEST_SEASONS} for f in FEATURE_ORDER
            }
            fold_meta: Dict[str, Any] = {}

            for test in TEST_SEASONS:
                scored = skipped = 0
                for division, div_rows in by_div.items():
                    train = [m for m in div_rows if _order(str(m["season_code"])) < _order(test)]
                    tests = [m for m in div_rows if str(m["season_code"]) == test]
                    train.sort(key=lambda m: (m["match_date"], m["home_team"], m["away_team"]))
                    tests.sort(key=lambda m: (m["match_date"], m["home_team"], m["away_team"]))
                    if len(train) < MIN_HISTORY_MATCHES:
                        skipped += len(tests)
                        continue
                    history = list(train)
                    ratings = _elo_from_train(train)
                    for match in tests:
                        pred = predict_match(history, match["home_team"], match["away_team"])
                        bm = best_market(pred)
                        y = _outcome(match, str(bm["market"]))
                        d = _as_date(match["match_date"])
                        if y is not None:
                            scored += 1
                            base_rank = float(bm["probability"]) * (0.75 + 0.25 * float(bm["data_quality"]))
                            market = str(bm["market"])
                            selection = str(bm["selection"])

                            hx = _xg_profile(xg_rows, str(match["league_name"]), str(match["home_team"]), d)
                            ax = _xg_profile(xg_rows, str(match["league_name"]), str(match["away_team"]), d)
                            fx, axok = xg_regression_factor(hx, ax, market, selection)

                            eh = ratings.get(canon(match["home_team"]), BASE_ELO)
                            ea = ratings.get(canon(match["away_team"]), BASE_ELO)
                            fe, aeok = elo_factor(eh - ea, market, selection)

                            ps = _pace_score(history, str(match["home_team"]), str(match["away_team"]))
                            fp, apok = pace_factor(ps, market, selection)

                            hi, ai = _venue_indices(history, str(match["home_team"]), str(match["away_team"]))
                            fv, avok = venue_factor(hi, ai, market, selection)

                            sy = _season_year(test)
                            hs = _lineup_stability_at(lineups, sy, str(match["home_team"]), d)
                            away_stab = _lineup_stability_at(lineups, sy, str(match["away_team"]), d)
                            fl, alok = lineup_stability_factor(hs, away_stab)

                            available = {
                                "xg_regression": axok,
                                "elo": aeok,
                                "pace": apok,
                                "venue": avok,
                                "lineup_stability": alok,
                            }
                            factors = {
                                "xg_regression": fx,
                                "elo": fe,
                                "pace": fp,
                                "venue": fv,
                                "lineup_stability": fl,
                            }
                            applicable = {
                                "xg_regression": market in ("over_2_5", "btts"),
                                "elo": market == "btts",
                                "pace": market in ("over_2_5", "btts", "corners_over_8_5"),
                                "venue": market == "btts",
                                "lineup_stability": True,
                            }
                            for feature in FEATURE_ORDER:
                                if applicable[feature]:
                                    coverage_counts[feature][test][1] += 1
                                    coverage_counts[feature][test][0] += int(available[feature])

                            row = {
                                "fold": test,
                                "week": _week(d),
                                "division": division,
                                "league": str(match["league_name"]),
                                "date": d,
                                "home": str(match["home_team"]),
                                "away": str(match["away_team"]),
                                "market": market,
                                "selection": selection,
                                "confidence": float(bm["probability"]),
                                "data_quality": float(bm["data_quality"]),
                                "hit": bool(y) == bool(bm["selection_yes"]),
                                "v1": base_rank,
                                "feature_available": available,
                            }
                            for feature in FEATURE_ORDER:
                                row[feature] = base_rank * float(factors[feature])
                            weekly[(test, row["week"])].append(row)
                        _elo_update(ratings, match)
                        history.append(match)
                fold_meta[test] = {
                    "train_seasons": [s for s in SEASONS if _order(s) < _order(test)],
                    "test_season": test,
                    "matches_scored": scored,
                    "matches_skipped_insufficient_history": skipped,
                }

            baseline_rows, baseline_by_week = _selection_sets(weekly, "v1")
            baseline_by_fold = {
                fold: _metrics([r for r in baseline_rows if r["fold"] == fold])
                for fold in TEST_SEASONS
            }
            baseline_overall = _metrics(baseline_rows)

            feature_reports: Dict[str, Any] = {}
            for idx, feature in enumerate(FEATURE_ORDER):
                rows, by_week = _selection_sets(weekly, feature)
                by_fold = {fold: _metrics([r for r in rows if r["fold"] == fold]) for fold in TEST_SEASONS}
                coverage_by_fold = {}
                for fold in TEST_SEASONS:
                    yes, total = coverage_counts[feature][fold]
                    coverage_by_fold[fold] = round(yes / total, 4) if total else 0.0
                changed = 0
                for key, br in baseline_by_week.items():
                    cr = by_week.get(key, [])
                    bset = {_candidate_id(r) for r in br}
                    cset = {_candidate_id(r) for r in cr}
                    changed += len(cset - bset)
                report = {
                    "feature": feature,
                    "overall": _metrics(rows),
                    "by_fold": by_fold,
                    "baseline_overall": baseline_overall,
                    "baseline_by_fold": baseline_by_fold,
                    "coverage_by_fold": coverage_by_fold,
                    "changed_picks": changed,
                    "bootstrap": _bootstrap_delta(baseline_by_week, by_week, seed=20260910 + idx * 997),
                }
                passed, reasons = _gate(feature, report)
                report["gate_passed"] = passed
                report["gate_fail_reasons"] = reasons
                report["hit_gain"] = round(
                    (report["overall"]["hit_rate"] or 0.0) - (baseline_overall["hit_rate"] or 0.0), 6
                )
                report["selected_brier_delta"] = round(
                    (report["overall"]["selected_brier"] or 0.0) - (baseline_overall["selected_brier"] or 0.0), 6
                )
                feature_reports[feature] = report

            passed = [feature_reports[f] for f in FEATURE_ORDER if feature_reports[f]["gate_passed"]]
            passed.sort(
                key=lambda r: (
                    float(r["hit_gain"]),
                    float((r["bootstrap"] or {}).get("p_gain_gt_0") or 0.0),
                    -float(r["selected_brier_delta"]),
                ),
                reverse=True,
            )
            winner = passed[0]["feature"] if passed else None
            result: Dict[str, Any] = {
                "version": VERSION,
                "policy_key": POLICY_KEY,
                "policy_version": POLICY_VERSION,
                "gate_passed": bool(winner),
                "recommended_activation": winner or "v1_only",
                "validation_protocol": {
                    "gate_version": "two-fold-week-block-v1",
                    "test_seasons": list(TEST_SEASONS),
                    "folds": fold_meta,
                    "sequential_history": True,
                    "week_block_bootstrap": True,
                    "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
                    "holdout_excluded": LIVE_HOLDOUT not in TEST_SEASONS,
                    "live_holdout_season": LIVE_HOLDOUT,
                    "holdout_rule": "2627 outcomes are never read for tuning or this audit",
                },
                "predeclared_gate": {
                    "min_pooled_hit_gain": MIN_POOLED_HIT_GAIN,
                    "min_each_fold_hit_gain": MIN_FOLD_HIT_GAIN,
                    "min_each_fold_feature_coverage": MIN_FOLD_COVERAGE,
                    "min_changed_picks": MIN_CHANGED_PICKS,
                    "min_bootstrap_p_gain_gt_0": MIN_BOOTSTRAP_P_IMPROVE,
                    "max_selected_brier_delta": MAX_SELECTED_BRIER_DELTA,
                },
                "baseline": baseline_overall,
                "baseline_by_fold": baseline_by_fold,
                "features": feature_reports,
                "feature_order": list(FEATURE_ORDER),
                "activation_note": (
                    f"{winner} passed the predeclared two-fold gate; production may read this registry row."
                    if winner
                    else "No feature passed; production must remain v1_only."
                ),
            }
            reason = result["activation_note"]
            conn.execute(
                """INSERT INTO policy_activation_registry(policy_key,policy_version,active_mode,metrics,reason)
                   VALUES(%s,%s,%s,%s,%s)
                   ON CONFLICT(policy_key) DO UPDATE SET
                     policy_version=EXCLUDED.policy_version,
                     active_mode=EXCLUDED.active_mode,
                     validated_at=NOW(),
                     metrics=EXCLUDED.metrics,
                     reason=EXCLUDED.reason""",
                (POLICY_KEY, POLICY_VERSION, winner or "v1_only", Jsonb(result), reason),
            )
            conn.execute(
                "UPDATE incremental_feature_audit_runs SET finished_at=NOW(),status='success',results=%s,message=%s WHERE id=%s",
                (Jsonb(result), reason, rid),
            )
            print("INCREMENTAL_FEATURE_AUDIT_RESULT", json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            conn.execute(
                "UPDATE incremental_feature_audit_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                (str(exc)[:2000], rid),
            )
            raise


if __name__ == "__main__":
    print(json.dumps(run_audit(), ensure_ascii=False, indent=2, default=str))
