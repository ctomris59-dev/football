#!/usr/bin/env python3
"""Operational schedule/fatigue context for weekly selection ranking.

This module does NOT change frozen V1 probabilities. It only reads the broader
club schedule collected from ESPN and produces an auditable rest/pending-match
context plus a conservative ranking factor. A scheduled official match between
"now" and the target domestic fixture is treated as pending and therefore not
eligible for finalization until that match is played and context can refresh.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from production_predictor import canon

SCHEDULE_CONTEXT_VERSION = "all_comp_v1"


def schedule_rank_factor(rest_days: Optional[float], *, pending_pre_fixture_match: bool = False) -> float:
    """Conservative operational ranking modifier; raw V1 probability is untouched."""
    if pending_pre_fixture_match:
        return 0.0
    if rest_days is None:
        return 1.0
    r = float(rest_days)
    if r < 2.5:
        return 0.0
    if r < 3.5:
        return 0.93
    if r < 4.5:
        return 0.97
    if r < 5.5:
        return 0.99
    return 1.0


def team_schedule_context(
    conn,
    team: str,
    fixture_dt: datetime,
    *,
    as_of: Optional[datetime] = None,
    fallback_rest_days: Optional[float] = None,
) -> Dict[str, Any]:
    """Return the latest official club event before fixture across competitions.

    ``espn_team_schedule_events`` is populated from domestic + UEFA team schedule
    pages. If unavailable, the caller-provided domestic rest value is preserved.
    """
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if fixture_dt.tzinfo is None:
        fixture_dt = fixture_dt.replace(tzinfo=timezone.utc)

    rows = []
    try:
        rows = conn.execute(
            """SELECT match_date,team_name,competition_name,competition_slug,
                      opponent_name,home_away,status,completed,event_id
                 FROM espn_team_schedule_events
                WHERE match_date IS NOT NULL
                  AND match_date < %s
                  AND match_date >= %s-INTERVAL '21 days'
                ORDER BY match_date DESC
                LIMIT 300""",
            (fixture_dt, fixture_dt),
        ).fetchall()
    except Exception:
        rows = []

    target = canon(team)
    selected = None
    for row in rows:
        if canon(row[1]) == target:
            selected = row
            break

    if not selected:
        factor = schedule_rank_factor(fallback_rest_days)
        return {
            "version": SCHEDULE_CONTEXT_VERSION,
            "scope": "domestic_fallback",
            "rest_days": fallback_rest_days,
            "rank_factor": factor,
            "pending_pre_fixture_match": False,
            "last_match_date": None,
            "last_competition": None,
            "last_competition_slug": None,
            "last_opponent": None,
            "last_home_away": None,
            "last_status": None,
            "last_completed": None,
            "event_id": None,
        }

    match_date, _team_name, comp_name, comp_slug, opponent, home_away, status, completed, event_id = selected
    rest_days = max(0.0, (fixture_dt - match_date).total_seconds() / 86400.0)
    pending = bool(match_date > now and not bool(completed))
    return {
        "version": SCHEDULE_CONTEXT_VERSION,
        "scope": "all_competitions",
        "rest_days": round(rest_days, 3),
        "rank_factor": schedule_rank_factor(rest_days, pending_pre_fixture_match=pending),
        "pending_pre_fixture_match": pending,
        "last_match_date": match_date,
        "last_competition": comp_name,
        "last_competition_slug": comp_slug,
        "last_opponent": opponent,
        "last_home_away": home_away,
        "last_status": status,
        "last_completed": completed,
        "event_id": event_id,
    }
