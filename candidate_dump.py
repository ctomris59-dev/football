#!/usr/bin/env python3
"""Read-only current candidate dump for operational inspection."""
from __future__ import annotations

from typing import Any, Dict

from multiline_corner_upgrade import build_multiline_corner_candidates
from weekly_trusted_predictions import build as build_weekly


def dump(database_url: str) -> Dict[str, Any]:
    ranked = build_weekly(database_url, limit=100)
    corners = build_multiline_corner_candidates(database_url)
    corner_rows = list(corners.get("best_per_fixture") or [])
    return {
        "week_key": ranked.get("week_key"),
        "generated_ranked_count": len(ranked.get("picks") or []),
        "ranked_candidates": ranked.get("picks") or [],
        "corner_candidate_rows": corners.get("candidate_rows"),
        "corner_best_per_fixture_count": len(corner_rows),
        "corner_candidates": corner_rows,
        "offered_market_counts": corners.get("offered_market_counts") or {},
        "corner_excluded_counts": corners.get("excluded_counts") or {},
    }
