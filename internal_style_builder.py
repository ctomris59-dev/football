#!/usr/bin/env python3
"""Guaranteed DB-only team-style fallback for current FotMob-mapped teams.

Builds conservative relative corner and shots-on-target style features from stored
match history. It writes into the same style table used by fixture enrichment, but
never invents unavailable crossing/pressing/xG fields.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

from fotmob_strength_style_importer import SCHEMA_SQL as FOTMOB_STRENGTH_SCHEMA

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RECENT_MATCHES = int(os.getenv("INTERNAL_STYLE_RECENT_MATCHES", "20"))
LEAGUE_IDS = {"Premier League": 47, "La Liga": 87, "Serie A": 55, "Bundesliga": 54, "Ligue 1": 53}

SCHEMA_SQL = FOTMOB_STRENGTH_SCHEMA + """
CREATE TABLE IF NOT EXISTS internal_style_runs(
 id BIGSERIAL PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 finished_at TIMESTAMPTZ,
 status TEXT NOT NULL,
 teams INTEGER NOT NULL DEFAULT 0,
 mapped_ids INTEGER NOT NULL DEFAULT 0,
 message TEXT
);
"""

ALIASES = {
    "man utd": "manchester united", "man united": "manchester united", "man city": "manchester city",
    "nottm forest": "nottingham forest", "wolves": "wolverhampton wanderers",
    "spurs": "tottenham hotspur", "tottenham": "tottenham hotspur",
    "milan": "ac milan", "inter": "inter milan", "psg": "paris saint germain", "paris sg": "paris saint germain",
    "mgladbach": "borussia monchengladbach", "borussia m gladbach": "borussia monchengladbach",
}


def canon(v: Any) -> str:
    s = unicodedata.normalize("NFKD", str(v or "")).encode("ascii", "ignore").decode().lower().replace("'", "")
    s = re.sub(r"\b(fc|cf|ssc|ac|club|football club|afc)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip(); s = re.sub(r"\s+", " ", s)
    return ALIASES.get(s, s)


def avg(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def team_metrics(rows: List[Tuple], team: str) -> Dict[str, Optional[float]]:
    ct = canon(team)
    corners: List[float] = []
    sot: List[float] = []
    goals: List[float] = []
    used = 0
    for _dt, home, away, hg, ag, hsot, asot, hc, ac in reversed(rows):
        ch, ca = canon(home), canon(away)
        if ct == ch:
            if hc is not None: corners.append(float(hc))
            if hsot is not None: sot.append(float(hsot))
            if hg is not None: goals.append(float(hg))
        elif ct == ca:
            if ac is not None: corners.append(float(ac))
            if asot is not None: sot.append(float(asot))
            if ag is not None: goals.append(float(ag))
        else:
            continue
        used += 1
        if used >= RECENT_MATCHES:
            break
    return {"corner_taken_team": avg(corners), "ontarget_scoring_att_team": avg(sot), "goals_team": avg(goals), "matches": used}


def build(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
        rid = conn.execute("INSERT INTO internal_style_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        teams_written = mapped_ids = 0
        try:
            snap = datetime.now(timezone.utc).date()
            for league, league_id in LEAGUE_IDS.items():
                mapping: Dict[str, Tuple[str, str]] = {}
                fm_rows = conn.execute(
                    """SELECT DISTINCT ON (espn_event_id) home_team,home_fotmob_team_id,away_team,away_fotmob_team_id
                       FROM fotmob_fixture_availability_snapshots WHERE league_name=%s
                       ORDER BY espn_event_id,snapshot_hour DESC""",
                    (league,),
                ).fetchall()
                for h, hid, a, aid in fm_rows:
                    if hid: mapping[canon(h)] = (str(hid), str(h))
                    if aid: mapping[canon(a)] = (str(aid), str(a))
                mapped_ids += len(mapping)
                hist = conn.execute(
                    """SELECT match_date,home_team,away_team,home_goals,away_goals,home_shots_on_target,away_shots_on_target,home_corners,away_corners
                       FROM football_data_matches WHERE league_name=%s AND season_code IN ('2425','2526')
                       UNION ALL
                       SELECT match_date::date,home_team,away_team,home_goals,away_goals,home_shots_on_target,away_shots_on_target,home_corners,away_corners
                       FROM espn_current_matches WHERE league_name=%s
                       ORDER BY 1""",
                    (league, league),
                ).fetchall()
                metrics_by_team: Dict[str, Dict[str, Optional[float]]] = {}
                for ct, (_tid, label) in mapping.items():
                    metrics_by_team[ct] = team_metrics(hist, label)
                baselines: Dict[str, Optional[float]] = {}
                for key in ("corner_taken_team", "ontarget_scoring_att_team", "goals_team"):
                    vals = [float(m[key]) for m in metrics_by_team.values() if m.get(key) is not None]
                    baselines[key] = avg(vals)
                for ct, (tid, label) in mapping.items():
                    m = metrics_by_team.get(ct, {})
                    relative = {}
                    for key in ("corner_taken_team", "ontarget_scoring_att_team", "goals_team"):
                        v, b = m.get(key), baselines.get(key)
                        if v is not None and b not in (None, 0):
                            relative[key] = max(0.4, min(2.5, float(v) / float(b)))
                    if not relative:
                        continue
                    stored_metrics = {k: v for k, v in m.items() if k != "matches" and v is not None}
                    conn.execute(
                        """INSERT INTO fotmob_team_style_snapshots(snapshot_date,league_name,league_id,season_label,season_id,
                           team_id,team_name,metrics,relative_metrics,top11_strength,player_coverage,fetched_at)
                           VALUES(%s,%s,%s,'internal-history','internal',%s,%s,%s,%s,NULL,0,NOW())
                           ON CONFLICT(snapshot_date,league_id,team_id) DO UPDATE SET
                             team_name=EXCLUDED.team_name,
                             metrics=CASE WHEN fotmob_team_style_snapshots.player_coverage>0 THEN fotmob_team_style_snapshots.metrics ELSE EXCLUDED.metrics END,
                             relative_metrics=CASE WHEN fotmob_team_style_snapshots.player_coverage>0 THEN fotmob_team_style_snapshots.relative_metrics ELSE EXCLUDED.relative_metrics END,
                             season_label=CASE WHEN fotmob_team_style_snapshots.player_coverage>0 THEN fotmob_team_style_snapshots.season_label ELSE EXCLUDED.season_label END,
                             season_id=CASE WHEN fotmob_team_style_snapshots.player_coverage>0 THEN fotmob_team_style_snapshots.season_id ELSE EXCLUDED.season_id END,
                             fetched_at=NOW()""",
                        (snap, league, league_id, tid, label, Jsonb(stored_metrics), Jsonb(relative)),
                    )
                    teams_written += 1
            conn.execute(
                "UPDATE internal_style_runs SET finished_at=NOW(),status='success',teams=%s,mapped_ids=%s,message='corners+sot+goals; no invented fields' WHERE id=%s",
                (teams_written, mapped_ids, rid),
            )
            result = {"status": "success", "teams": teams_written, "mapped_ids": mapped_ids}
            print("INTERNAL_STYLE_RESULT", json.dumps(result, separators=(",", ":")))
            return result
        except Exception as exc:
            conn.execute("UPDATE internal_style_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s", (str(exc)[:1000], rid))
            raise


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
