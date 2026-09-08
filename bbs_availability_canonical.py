#!/usr/bin/env python3
"""Canonical Big Five league slugs and tolerant soccer absence parsing for BBS."""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import bbs_availability_importer as base

base.LEAGUES = [
    ("epl", "Premier League"),
    ("laliga", "La Liga"),
    ("seriea", "Serie A"),
    ("bundesliga", "Bundesliga"),
    ("ligue1", "Ligue 1"),
]


def _candidate_score(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return -1
    score = 0
    for r in rows[:20]:
        keys = {str(k).lower() for k in r.keys()}
        if "player" in keys or "player_id" in keys or "playerid" in keys:
            score += 3
        if "fixture" in keys or "fixture_id" in keys or "fixtureid" in keys or "match_id" in keys:
            score += 2
        if "reason" in keys or "status" in keys or "injury_type" in keys or "absence_reason" in keys:
            score += 2
        if "team" in keys or "team_id" in keys or "teamname" in keys or "team_name" in keys:
            score += 1
    return score


def tolerant_rows_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[List[Dict[str, Any]]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, list):
            dicts = [x for x in obj if isinstance(x, dict)]
            if dicts:
                candidates.append(dicts)
            for x in obj:
                walk(x)
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)

    walk(payload)
    if not candidates:
        return []
    candidates.sort(key=lambda rows: (_candidate_score(rows), len(rows)), reverse=True)
    best = candidates[0]
    return best if _candidate_score(best) > 0 else []


base.rows_from_payload = tolerant_rows_from_payload


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    return base.run_import(database_url)


if __name__ == "__main__":
    import json
    print(json.dumps(run_import(), ensure_ascii=False, indent=2, default=str))
