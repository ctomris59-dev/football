#!/usr/bin/env python3
"""Refresh useful ESPN pre-match context without the empty team-injury endpoint."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, Optional

from espn_context_importer import ContextImporter, utcnow


def run_import(database_url: Optional[str] = None) -> Dict[str, int]:
    imp = ContextImporter(database_url)
    rid = imp.conn.execute("INSERT INTO espn_context_runs(status) VALUES('running') RETURNING id").fetchone()[0]
    odds = prematch = xg = 0
    hour = utcnow().replace(minute=0, second=0, microsecond=0)
    try:
        rows = imp.upcoming_rows()
        odds = imp.collect_odds(rows)
        prematch = imp.collect_prematch(rows)
        xg = imp.discover_xg()
        quality = imp.log_quality(hour)
        quality["injury_source_disabled"] = "ESPN team injury endpoint returned 0 useful rows; BBS is the availability source"
        imp.conn.execute(
            """
            UPDATE espn_context_runs
            SET finished_at=NOW(),injury_teams=0,odds_events=%s,prematch_events=%s,
                xg_matches=%s,status='success',message=%s
            WHERE id=%s
            """,
            (odds, prematch, xg, json.dumps(quality, separators=(",", ":"))[:1000], rid),
        )
        return {"injury_teams": 0, "odds_events": odds, "prematch_events": prematch, "xg_matches": xg}
    except Exception as exc:
        imp.conn.execute(
            "UPDATE espn_context_runs SET finished_at=NOW(),injury_teams=0,odds_events=%s,prematch_events=%s,xg_matches=%s,status='failed',message=%s WHERE id=%s",
            (odds, prematch, xg, str(exc)[:1000], rid),
        )
        raise
    finally:
        imp.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
