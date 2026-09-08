#!/usr/bin/env python3
"""Canonical Big Five wrapper around oddspapi_importer."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from oddspapi_importer import BOOKMAKER, OddsPapiImporter


class CanonicalOddsPapiImporter(OddsPapiImporter):
    def store_tournaments(self, payload: Any) -> Dict[int, Dict[str, Any]]:
        matched = super().store_tournaments(payload)
        per_league: Dict[str, tuple[int, Dict[str, Any]]] = {}
        for tid, item in matched.items():
            league = str(item.get("_league_name") or "")
            current = per_league.get(league)
            if current is None or tid < current[0]:
                per_league[league] = (tid, item)

        chosen = {tid: item for tid, item in per_league.values()}
        self.conn.execute("UPDATE oddspapi_tournaments SET is_big5=FALSE WHERE is_big5=TRUE")
        for tid in chosen:
            self.conn.execute(
                "UPDATE oddspapi_tournaments SET is_big5=TRUE, updated_at=NOW() WHERE tournament_id=%s",
                (tid,),
            )
        return chosen

    def quality_counts(self, snapshot_hour: datetime) -> Dict[str, int]:
        # Psycopg uses %s placeholders, so literal SQL wildcard percent signs
        # must be doubled when parameters are supplied.
        row = self.conn.execute(
            """
            SELECT
              COUNT(DISTINCT p.fixture_id) FILTER (
                WHERE lower(COALESCE(p.market_name,'')) = 'over under full time'
                  AND abs(COALESCE(p.handicap,-999)-2.5) < 0.001
                  AND p.price IS NOT NULL
              ) AS ou25,
              COUNT(DISTINCT p.fixture_id) FILTER (
                WHERE lower(COALESCE(p.market_name,'')) LIKE '%%both teams to score%%'
                  AND p.price IS NOT NULL
              ) AS btts,
              COUNT(DISTINCT p.fixture_id) FILTER (
                WHERE lower(COALESCE(p.market_name,'')) LIKE '%%corner%%'
                  AND abs(COALESCE(p.handicap,-999)-8.5) < 0.001
                  AND p.price IS NOT NULL
              ) AS corner85
            FROM oddspapi_market_prices p
            WHERE p.snapshot_hour=%s AND p.bookmaker=%s
            """,
            (snapshot_hour, BOOKMAKER),
        ).fetchone()
        return {
            "ou25": int(row[0] or 0),
            "btts": int(row[1] or 0),
            "corner85": int(row[2] or 0),
        }


def run_import(database_url: Optional[str] = None):
    importer = CanonicalOddsPapiImporter(database_url)
    try:
        return importer.run()
    finally:
        importer.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
