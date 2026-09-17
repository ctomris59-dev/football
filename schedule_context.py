#!/usr/bin/env python3
"""Operational schedule/fatigue context for weekly selection ranking.

v2 fixes a critical ambiguity in the old implementation: an incomplete/stale event
before the target fixture is never treated as the team's last completed match.
Rest is calculated only from completed official matches. Separately, an incomplete
official event between ``now`` and the target fixture blocks finalization until the
context can refresh after that event.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from production_predictor import canon

SCHEDULE_CONTEXT_VERSION = "all_comp_v2_completed_only"


def schedule_rank_factor(rest_days: Optional[float], *, pending_pre_fixture_match: bool = False) -> float:
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
                LIMIT 500""",
            (fixture_dt, fixture_dt),
        ).fetchall()
    except Exception:
        rows = []

    target = canon(team)
    team_rows = [row for row in rows if canon(row[1]) == target]

    # An upcoming official event before the target fixture means the current rest
    # and squad context can change; do not finalize through it.
    pending_row = None
    for row in sorted(team_rows, key=lambda r: r[0]):
        match_date = row[0]
        completed = bool(row[7])
        if now < match_date < fixture_dt and not completed:
            pending_row = row
            break

    # Last-match/rest context must come from a genuinely completed event only.
    completed_rows = [row for row in team_rows if bool(row[7]) and row[0] <= now]
    selected = max(completed_rows, key=lambda r: r[0]) if completed_rows else None

    if not selected:
        factor = schedule_rank_factor(fallback_rest_days, pending_pre_fixture_match=bool(pending_row))
        return {
            "version": SCHEDULE_CONTEXT_VERSION,
            "scope": "domestic_fallback",
            "rest_days": fallback_rest_days,
            "rank_factor": factor,
            "pending_pre_fixture_match": bool(pending_row),
            "pending_match_date": pending_row[0] if pending_row else None,
            "pending_event_id": pending_row[8] if pending_row else None,
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
    pending = bool(pending_row)
    return {
        "version": SCHEDULE_CONTEXT_VERSION,
        "scope": "all_competitions_completed_only",
        "rest_days": round(rest_days, 3),
        "rank_factor": schedule_rank_factor(rest_days, pending_pre_fixture_match=pending),
        "pending_pre_fixture_match": pending,
        "pending_match_date": pending_row[0] if pending_row else None,
        "pending_event_id": pending_row[8] if pending_row else None,
        "last_match_date": match_date,
        "last_competition": comp_name,
        "last_competition_slug": comp_slug,
        "last_opponent": opponent,
        "last_home_away": home_away,
        "last_status": status,
        "last_completed": bool(completed),
        "event_id": event_id,
    }
