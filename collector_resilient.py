#!/usr/bin/env python3
"""Plan-aware wrapper around the API-Football bulk collector.

The base collector intentionally fails on most API errors. That is useful for data
integrity, but API-Football Free currently exposes 2024 while rejecting 2025. The
original all-seasons preflight therefore prevented usable 2024 fixture details,
lineups, injuries and season-player data from ever being collected.

This wrapper skips only explicit subscription/season-access errors. Network,
parsing, HTTP and other unexpected failures still fail closed. All successfully
accessible league-season pairs continue through the normal detail/injury/player
phases using the existing idempotent storage methods.
"""
from __future__ import annotations

import logging
import sys
from typing import Dict, List, Tuple

from collector import Collector, LEAGUES, SEASONS, QuotaStop

log = logging.getLogger("football-collector-resilient")


def is_plan_restriction(exc: Exception) -> bool:
    text = str(exc).lower()
    explicit = (
        "free plans do not have access to this season",
        "plan does not have access",
        "subscription" ,
    )
    if any(x in text for x in explicit):
        return True
    # Keep this conservative: skip only when the error clearly combines a plan
    # restriction with season/access wording. Everything else must still fail.
    return "plan" in text and "season" in text and "access" in text


def run_resilient(c: Collector) -> None:
    available: List[Tuple[int, str, int]] = []
    fixture_map: Dict[Tuple[int, int], List[int]] = {}
    skipped: List[dict] = []

    try:
        # Phase 1: preflight each league-season independently. A subscription
        # restriction on one season must not suppress usable seasons.
        for season in SEASONS:
            for league_id, league_name in LEAGUES:
                try:
                    c.store_coverage(league_id, league_name, season)
                    ids = c.collect_fixture_list(league_id, league_name, season)
                    fixture_map[(league_id, season)] = ids
                    available.append((league_id, league_name, season))
                    log.info(
                        "ACCESSIBLE league=%s season=%s fixtures=%s",
                        league_name, season, len(ids),
                    )
                except QuotaStop:
                    raise
                except Exception as exc:
                    if not is_plan_restriction(exc):
                        raise
                    skipped.append(
                        {"league_id": league_id, "league": league_name, "season": season, "reason": str(exc)[:300]}
                    )
                    log.warning(
                        "PLAN_RESTRICTED league=%s season=%s; skipping this pair only: %s",
                        league_name, season, exc,
                    )

        if not available:
            raise RuntimeError("No accessible league-season pairs remained after preflight")

        # Phase 2: completed fixture details include events/lineups/statistics/players.
        for league_id, league_name, season in available:
            c.collect_details(fixture_map[(league_id, season)], league_id, season)

        # Phase 3: injuries/suspensions for accessible league-seasons only.
        for league_id, league_name, season in available:
            c.collect_injuries(league_id, league_name, season)

        # Phase 4: season player statistics for accessible league-seasons only.
        for league_id, league_name, season in available:
            c.collect_season_players(league_id, league_name, season)

        c.print_summary()
        msg = f"Collection completed for {len(available)} accessible league-season pairs; plan-restricted={len(skipped)}"
        c.close_run("success", msg)
        log.info("RESILIENT_COLLECTION_RESULT accessible=%s skipped=%s api_calls=%s", len(available), len(skipped), c.api_calls)

    except QuotaStop as exc:
        log.warning("%s", exc)
        c.print_summary()
        c.close_run("paused_quota", str(exc))
        sys.exit(0)
    except Exception as exc:
        log.exception("Resilient collection failed")
        c.close_run("failed", str(exc))
        raise


def main() -> None:
    collector = Collector()
    try:
        run_resilient(collector)
    finally:
        collector.conn.close()


if __name__ == "__main__":
    main()
