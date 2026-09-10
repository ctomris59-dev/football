#!/usr/bin/env python3
"""Build one auditable, pre-match match-environment snapshot per upcoming fixture.

The purpose is to close useful context gaps (xG regression, pace, venue, Elo,
all-competition fatigue and lineup stability) while preserving the frozen V1
production core. Nothing in this module changes model probabilities or pick
ranking. The snapshot is explicitly shadow/challenger data until the existing
leakage-safe two-fold gate approves a future policy.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

from match_environment_features import (
    MATCH_ENVIRONMENT_VERSION,
    coverage_score,
    exact_scoreline_distribution,
    fatigue_asymmetry,
    lineup_stability,
    mean,
    pace_band,
    pace_proxy_score,
    projected_goal_environment,
    safe_float,
    venue_strength_index,
    xg_regression_profile,
)
from model_engine_v1 import predict_match
from production_predictor import canon
from schedule_context import team_schedule_context
from thursday_decision_engine import _history_rows, _last_rest_days

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
XG_RECENT = int(os.getenv("MATCH_ENV_XG_RECENT", "6"))
VENUE_RECENT = int(os.getenv("MATCH_ENV_VENUE_RECENT", "12"))

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS match_environment_snapshots(
  event_id TEXT NOT NULL,
  snapshot_hour TIMESTAMPTZ NOT NULL,
  match_date TIMESTAMPTZ NOT NULL,
  league_name TEXT NOT NULL,
  home_team TEXT NOT NULL,
  away_team TEXT NOT NULL,
  projected_total_goals DOUBLE PRECISION,
  pace_proxy_score DOUBLE PRECISION,
  pace_band TEXT,
  home_xg_attack_delta DOUBLE PRECISION,
  away_xg_attack_delta DOUBLE PRECISION,
  home_xg_defence_delta DOUBLE PRECISION,
  away_xg_defence_delta DOUBLE PRECISION,
  home_venue_index DOUBLE PRECISION,
  away_venue_index DOUBLE PRECISION,
  venue_index_gap DOUBLE PRECISION,
  home_elo DOUBLE PRECISION,
  away_elo DOUBLE PRECISION,
  elo_gap DOUBLE PRECISION,
  home_rest_days DOUBLE PRECISION,
  away_rest_days DOUBLE PRECISION,
  fatigue_asymmetry_days DOUBLE PRECISION,
  home_lineup_stability DOUBLE PRECISION,
  away_lineup_stability DOUBLE PRECISION,
  feature_coverage DOUBLE PRECISION NOT NULL DEFAULT 0,
  context JSONB NOT NULL DEFAULT '{}'::jsonb,
  built_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(event_id,snapshot_hour)
);
CREATE INDEX IF NOT EXISTS idx_match_environment_latest
  ON match_environment_snapshots(match_date,snapshot_hour DESC);
CREATE TABLE IF NOT EXISTS match_environment_runs(
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  fixtures INTEGER NOT NULL DEFAULT 0,
  xg_mapped INTEGER NOT NULL DEFAULT 0,
  pace_mapped INTEGER NOT NULL DEFAULT 0,
  elo_mapped INTEGER NOT NULL DEFAULT 0,
  all_comp_schedule_mapped INTEGER NOT NULL DEFAULT 0,
  player_mapped INTEGER NOT NULL DEFAULT 0,
  message TEXT
);
"""


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
    keys = (
        "expected_xi_strength", "top11_strength", "injury_impact", "goalkeeper_injured",
        "retained_minutes_share", "starter_continuity", "player_coverage", "key_absences", "source_meta",
    )
    return dict(zip(keys, row))


def _pressure_context(conn, event_id: str) -> Dict[str, Any]:
    try:
        row = conn.execute(
            """SELECT goal_pressure_signal,corner_pressure_signal,home_threat_share,away_threat_share,coverage,raw
                 FROM fixture_pressure_snapshots
                WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    return dict(zip(("goal_pressure_signal", "corner_pressure_signal", "home_threat_share", "away_threat_share", "coverage", "raw"), row))


def _elo_for(conn, team: str, league: str, match_date: datetime) -> Optional[float]:
    try:
        maps = conn.execute(
            "SELECT system_team,clubelo_club FROM clubelo_team_map WHERE league_name=%s",
            (league,),
        ).fetchall()
    except Exception:
        return None
    ct = canon(team)
    club = next((str(club) for label, club in maps if canon(label) == ct), None)
    if not club:
        return None
    d = match_date.date()
    try:
        row = conn.execute(
            """SELECT elo FROM clubelo_history
                WHERE clubelo_club=%s AND from_date<=%s
                  AND (to_date IS NULL OR to_date>=%s)
                ORDER BY from_date DESC LIMIT 1""",
            (club, d, d),
        ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
        row = conn.execute(
            """SELECT elo FROM clubelo_daily_snapshots
                WHERE club=%s AND snapshot_date<=%s
                ORDER BY snapshot_date DESC LIMIT 1""",
            (club, d),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def _xg_recent(conn, league: str, team: str, before: datetime, limit: int = XG_RECENT) -> Dict[str, Any]:
    ct = canon(team)
    try:
        rows = conn.execute(
            """SELECT home_team,away_team,home_goals,away_goals,home_xg,away_xg
                 FROM understat_matches
                WHERE league_name=%s AND is_result=TRUE AND match_date<%s
                  AND home_goals IS NOT NULL AND away_goals IS NOT NULL
                  AND home_xg IS NOT NULL AND away_xg IS NOT NULL
                ORDER BY match_date DESC LIMIT 240""",
            (league, before),
        ).fetchall()
    except Exception:
        return xg_regression_profile(None, None, None, None, matches=0)
    gf: List[float] = []
    ga: List[float] = []
    xf: List[float] = []
    xa: List[float] = []
    for home, away, hg, ag, hx, ax in rows:
        if canon(home) == ct:
            gf.append(float(hg)); ga.append(float(ag)); xf.append(float(hx)); xa.append(float(ax))
        elif canon(away) == ct:
            gf.append(float(ag)); ga.append(float(hg)); xf.append(float(ax)); xa.append(float(hx))
        else:
            continue
        if len(gf) >= max(1, int(limit)):
            break
    return xg_regression_profile(mean(gf), mean(xf), mean(ga), mean(xa), matches=len(gf))


def _venue_profile(history: List[Dict[str, Any]], team: str, venue: str, limit: int = VENUE_RECENT) -> Dict[str, Any]:
    ct = canon(team)
    gf: List[float] = []
    ga: List[float] = []
    sf: List[float] = []
    sa: List[float] = []
    used = 0
    for row in reversed(history):
        if venue == "home" and row.get("home_team") == ct:
            own_goal, opp_goal = row.get("home_goals"), row.get("away_goals")
            own_sot, opp_sot = row.get("home_shots_on_target"), row.get("away_shots_on_target")
        elif venue == "away" and row.get("away_team") == ct:
            own_goal, opp_goal = row.get("away_goals"), row.get("home_goals")
            own_sot, opp_sot = row.get("away_shots_on_target"), row.get("home_shots_on_target")
        else:
            continue
        if own_goal is not None and opp_goal is not None:
            gf.append(float(own_goal)); ga.append(float(opp_goal))
        if own_sot is not None and opp_sot is not None:
            sf.append(float(own_sot)); sa.append(float(opp_sot))
        used += 1
        if used >= max(1, int(limit)):
            break
    return {
        "matches": used,
        "goals_for": mean(gf),
        "goals_against": mean(ga),
        "sot_for": mean(sf),
        "sot_against": mean(sa),
    }


def _league_goal_baselines(history: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    hg = [safe_float(r.get("home_goals")) for r in history]
    ag = [safe_float(r.get("away_goals")) for r in history]
    return {"home": mean(hg), "away": mean(ag)}


def latest_environment(conn, event_id: str) -> Optional[Dict[str, Any]]:
    try:
        row = conn.execute(
            """SELECT context FROM match_environment_snapshots
                WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
    except Exception:
        return None
    return dict(row[0]) if row and isinstance(row[0], dict) else None


def build(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
        rid = conn.execute("INSERT INTO match_environment_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        fixtures = xg_mapped = pace_mapped = elo_mapped = schedule_mapped = player_mapped = 0
        try:
            upcoming = conn.execute(
                """SELECT event_id,match_date,league_name,home_team,away_team
                     FROM espn_upcoming
                    WHERE is_current=TRUE
                      AND match_date>=NOW()-INTERVAL '2 hours'
                      AND match_date<=NOW()+INTERVAL '8 days'
                    ORDER BY match_date"""
            ).fetchall()
            hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            histories: Dict[str, List[Dict[str, Any]]] = {}
            for event_id, match_date, league, home, away in upcoming:
                fixtures += 1
                league_s, home_s, away_s = str(league), str(home), str(away)
                history = _history_rows(conn, league_s, match_date)
                histories[league_s] = history
                if not history:
                    continue
                pred = predict_match(history, canon(home_s), canon(away_s), recent_matches=18)
                pge = projected_goal_environment(pred.lambda_home_goals, pred.lambda_away_goals)

                pressure = _pressure_context(conn, str(event_id))
                pace = pace_proxy_score(pressure.get("goal_pressure_signal"), pressure.get("corner_pressure_signal"))
                if pace is not None:
                    pace_mapped += 1

                hxg = _xg_recent(conn, league_s, home_s, match_date)
                axg = _xg_recent(conn, league_s, away_s, match_date)
                if int(hxg.get("matches") or 0) > 0 and int(axg.get("matches") or 0) > 0:
                    xg_mapped += 1

                base = _league_goal_baselines(history)
                hv = _venue_profile(history, home_s, "home")
                av = _venue_profile(history, away_s, "away")
                hvi = venue_strength_index(
                    hv.get("goals_for"), hv.get("goals_against"), hv.get("sot_for"), hv.get("sot_against"),
                    league_goals_for=base.get("home"), league_goals_against=base.get("away"),
                )
                avi = venue_strength_index(
                    av.get("goals_for"), av.get("goals_against"), av.get("sot_for"), av.get("sot_against"),
                    league_goals_for=base.get("away"), league_goals_against=base.get("home"),
                )
                venue_gap = (hvi - avi) if hvi is not None and avi is not None else None

                helo, aelo = _elo_for(conn, home_s, league_s, match_date), _elo_for(conn, away_s, league_s, match_date)
                elo_gap = (helo - aelo) if helo is not None and aelo is not None else None
                if elo_gap is not None:
                    elo_mapped += 1

                legacy_h = _last_rest_days(history, home_s, match_date)
                legacy_a = _last_rest_days(history, away_s, match_date)
                hsched = team_schedule_context(conn, home_s, match_date, fallback_rest_days=legacy_h)
                asched = team_schedule_context(conn, away_s, match_date, fallback_rest_days=legacy_a)
                if hsched.get("scope") == "all_competitions" and asched.get("scope") == "all_competitions":
                    schedule_mapped += 1
                hrest, arest = hsched.get("rest_days"), asched.get("rest_days")

                hp, ap = _player_context(conn, home_s), _player_context(conn, away_s)
                hstab = lineup_stability(hp.get("starter_continuity"), hp.get("retained_minutes_share"), hp.get("injury_impact"), hp.get("goalkeeper_injured"))
                astab = lineup_stability(ap.get("starter_continuity"), ap.get("retained_minutes_share"), ap.get("injury_impact"), ap.get("goalkeeper_injured"))
                if hstab is not None and astab is not None:
                    player_mapped += 1

                scorelines = exact_scoreline_distribution(pred.lambda_home_goals, pred.lambda_away_goals)
                coverage = coverage_score([
                    pace,
                    hxg.get("attack_xg_minus_goals"), axg.get("attack_xg_minus_goals"),
                    hvi, avi, elo_gap, hrest, arest, hstab, astab,
                ])
                context = {
                    "version": MATCH_ENVIRONMENT_VERSION,
                    "activation": "shadow_only_two_fold_validation_required",
                    "production_probability_changed": False,
                    "projected_goal_environment": pge,
                    "pge_note": "our transparent metric; not DataGaffer proprietary XoG",
                    "pace": {"score": pace, "band": pace_band(pace), "source": "goal+corner pressure proxy"},
                    "xg_regression": {"home": hxg, "away": axg, "window_matches": XG_RECENT},
                    "venue": {"home": hv, "away": av, "home_index": hvi, "away_index": avi, "index_gap": venue_gap, "window_matches": VENUE_RECENT},
                    "elo": {"home": helo, "away": aelo, "gap": elo_gap, "source": "internal/ClubElo-compatible snapshot"},
                    "schedule": {"home": hsched, "away": asched, "fatigue_asymmetry_days": fatigue_asymmetry(hrest, arest)},
                    "lineup": {"home": hp, "away": ap, "home_stability": hstab, "away_stability": astab},
                    "score_distribution": scorelines,
                    "market_independence": "bookmaker prices are not blended into these model features",
                    "dixon_coles": "not_active_until_rho_is_time-causally_fitted_and_two-fold_validated",
                    "feature_coverage": coverage,
                }
                conn.execute(
                    """INSERT INTO match_environment_snapshots(
                        event_id,snapshot_hour,match_date,league_name,home_team,away_team,
                        projected_total_goals,pace_proxy_score,pace_band,
                        home_xg_attack_delta,away_xg_attack_delta,home_xg_defence_delta,away_xg_defence_delta,
                        home_venue_index,away_venue_index,venue_index_gap,home_elo,away_elo,elo_gap,
                        home_rest_days,away_rest_days,fatigue_asymmetry_days,
                        home_lineup_stability,away_lineup_stability,feature_coverage,context)
                      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT(event_id,snapshot_hour) DO UPDATE SET
                        match_date=EXCLUDED.match_date,league_name=EXCLUDED.league_name,
                        home_team=EXCLUDED.home_team,away_team=EXCLUDED.away_team,
                        projected_total_goals=EXCLUDED.projected_total_goals,
                        pace_proxy_score=EXCLUDED.pace_proxy_score,pace_band=EXCLUDED.pace_band,
                        home_xg_attack_delta=EXCLUDED.home_xg_attack_delta,away_xg_attack_delta=EXCLUDED.away_xg_attack_delta,
                        home_xg_defence_delta=EXCLUDED.home_xg_defence_delta,away_xg_defence_delta=EXCLUDED.away_xg_defence_delta,
                        home_venue_index=EXCLUDED.home_venue_index,away_venue_index=EXCLUDED.away_venue_index,
                        venue_index_gap=EXCLUDED.venue_index_gap,home_elo=EXCLUDED.home_elo,away_elo=EXCLUDED.away_elo,
                        elo_gap=EXCLUDED.elo_gap,home_rest_days=EXCLUDED.home_rest_days,away_rest_days=EXCLUDED.away_rest_days,
                        fatigue_asymmetry_days=EXCLUDED.fatigue_asymmetry_days,
                        home_lineup_stability=EXCLUDED.home_lineup_stability,away_lineup_stability=EXCLUDED.away_lineup_stability,
                        feature_coverage=EXCLUDED.feature_coverage,context=EXCLUDED.context,built_at=NOW()""",
                    (
                        str(event_id), hour, match_date, league_s, home_s, away_s,
                        pge.get("projected_total_goals"), pace, pace_band(pace),
                        hxg.get("attack_xg_minus_goals"), axg.get("attack_xg_minus_goals"),
                        hxg.get("defence_goals_minus_xg"), axg.get("defence_goals_minus_xg"),
                        hvi, avi, venue_gap, helo, aelo, elo_gap,
                        hrest, arest, fatigue_asymmetry(hrest, arest), hstab, astab, coverage, Jsonb(context),
                    ),
                )
            conn.execute(
                """UPDATE match_environment_runs SET finished_at=NOW(),status='success',fixtures=%s,xg_mapped=%s,
                   pace_mapped=%s,elo_mapped=%s,all_comp_schedule_mapped=%s,player_mapped=%s,
                   message=%s WHERE id=%s""",
                (
                    fixtures, xg_mapped, pace_mapped, elo_mapped, schedule_mapped, player_mapped,
                    "shadow context only; exact Poisson score grid; no proprietary XoG copy; no production ranking change", rid,
                ),
            )
            result = {
                "status": "success", "version": MATCH_ENVIRONMENT_VERSION, "fixtures": fixtures,
                "xg_mapped": xg_mapped, "pace_mapped": pace_mapped, "elo_mapped": elo_mapped,
                "all_comp_schedule_mapped": schedule_mapped, "player_mapped": player_mapped,
            }
            print("MATCH_ENVIRONMENT_RESULT", json.dumps(result, separators=(",", ":")), flush=True)
            return result
        except Exception as exc:
            conn.execute(
                "UPDATE match_environment_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                (str(exc)[:1200], rid),
            )
            raise


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2, default=str))
