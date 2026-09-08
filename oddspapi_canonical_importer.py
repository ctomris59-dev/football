#!/usr/bin/env python3
"""Canonical Big Five wrapper around oddspapi_importer.

OddsPapi's tournament catalog contains historical/youth/alternate competitions
with similar names. The base importer intentionally matches broadly; this wrapper
collapses matches to one canonical tournament per Big Five league before odds are
requested. Current canonical competitions have the stable low IDs in the catalog,
so the smallest matching tournament id per league is selected.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from oddspapi_importer import OddsPapiImporter


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
        # Keep DB classification truthful: only the canonical five are Big Five.
        self.conn.execute("UPDATE oddspapi_tournaments SET is_big5=FALSE WHERE is_big5=TRUE")
        for tid in chosen:
            self.conn.execute(
                "UPDATE oddspapi_tournaments SET is_big5=TRUE, updated_at=NOW() WHERE tournament_id=%s",
                (tid,),
            )
        return chosen


def run_import(database_url: Optional[str] = None):
    importer = CanonicalOddsPapiImporter(database_url)
    try:
        return importer.run()
    finally:
        importer.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
