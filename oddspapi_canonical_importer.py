#!/usr/bin/env python3
"""Canonical, quota-efficient Big Five wrapper around oddspapi_importer."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from oddspapi_importer import (
    API_KEY,
    BOOKMAKER,
    LOOKAHEAD_DAYS,
    OddsPapiImporter,
    fixture_objects,
    utcnow,
)


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
        return {"ou25": int(row[0] or 0), "btts": int(row[1] or 0), "corner85": int(row[2] or 0)}

    def cached_catalog(self) -> tuple[Dict[int, Dict[str, Any]], Dict[int, Dict[str, Any]]]:
        """Return canonical tournaments and market metadata discovered previously."""
        selected: Dict[int, Dict[str, Any]] = {}
        try:
            for tid, name, raw in self.conn.execute(
                "SELECT tournament_id,tournament_name,raw FROM oddspapi_tournaments WHERE is_big5=TRUE ORDER BY tournament_id"
            ).fetchall():
                item = dict(raw or {}) if isinstance(raw, dict) else {}
                item["_league_name"] = str(name or item.get("tournamentName") or "")
                selected[int(tid)] = item
        except Exception:
            selected = {}

        catalog: Dict[int, Dict[str, Any]] = {}
        try:
            for market_id, raw in self.conn.execute(
                "SELECT market_id,raw FROM oddspapi_market_catalog WHERE sport_id=10"
            ).fetchall():
                if isinstance(raw, dict):
                    catalog[int(market_id)] = dict(raw)
        except Exception:
            catalog = {}
        return selected, catalog

    def run(self) -> Dict[str, Any]:
        # First ever run uses the base discovery path (4 calls). Once the five
        # canonical tournaments + soccer market catalog are cached, refreshes use
        # only fixtures + odds (2 calls), preserving the free monthly quota.
        selected, catalog = self.cached_catalog()
        if len(selected) != 5 or len(catalog) < 5:
            return super().run()

        run_id = self.conn.execute(
            "INSERT INTO oddspapi_import_runs(status,message) VALUES('running','cached-catalog refresh') RETURNING id"
        ).fetchone()[0]
        if not API_KEY:
            self.conn.execute(
                "UPDATE oddspapi_import_runs SET finished_at=NOW(),status='not_configured',message='ODDSPAPI_API_KEY is not configured' WHERE id=%s",
                (run_id,),
            )
            return {"status": "not_configured", "message": "ODDSPAPI_API_KEY is not configured"}

        snapshot_hour = utcnow().replace(minute=0, second=0, microsecond=0)
        start = utcnow() - timedelta(hours=3)
        end = utcnow() + timedelta(days=LOOKAHEAD_DAYS)
        try:
            fixture_map = self.fetch_fixture_map(start, end, selected)
            tournament_ids = ",".join(str(x) for x in sorted(selected))
            odds_payload = self.api_get(
                "/v4/odds-by-tournaments",
                {
                    "tournamentIds": tournament_ids,
                    "bookmakers": BOOKMAKER,
                    "language": "en",
                    "verbosity": 3,
                    "oddsFormat": "decimal",
                },
            )
            price_rows = 0
            stored_fixtures = set()
            for fixture in fixture_objects(odds_payload):
                before = price_rows
                price_rows += self.store_prices(
                    fixture,
                    fixture_map.get(str(fixture.get("fixtureId") or "")),
                    selected,
                    catalog,
                    snapshot_hour,
                    start,
                    end,
                )
                if price_rows > before:
                    stored_fixtures.add(str(fixture.get("fixtureId")))

            quality = self.quality_counts(snapshot_hour)
            self.conn.execute(
                """
                UPDATE oddspapi_import_runs SET finished_at=NOW(),status='success',api_calls=%s,
                    tournament_count=%s,fixture_count=%s,price_rows=%s,
                    ou25_fixtures=%s,btts_fixtures=%s,corner85_fixtures=%s,message=%s
                WHERE id=%s
                """,
                (
                    self.api_calls,
                    len(selected),
                    len(stored_fixtures),
                    price_rows,
                    quality["ou25"],
                    quality["btts"],
                    quality["corner85"],
                    json.dumps({**quality, "catalog": "cached"}, separators=(",", ":")),
                    run_id,
                ),
            )
            result = {
                "status": "success",
                "api_calls": self.api_calls,
                "catalog": "cached",
                "tournaments": len(selected),
                "fixtures": len(stored_fixtures),
                "price_rows": price_rows,
                "ou25_fixtures": quality["ou25"],
                "btts_fixtures": quality["btts"],
                "corner85_fixtures": quality["corner85"],
            }
            print("ODDSPAPI_CACHED_RESULT", json.dumps(result, separators=(",", ":")))
            return result
        except Exception as exc:
            self.conn.execute(
                "UPDATE oddspapi_import_runs SET finished_at=NOW(),status='failed',api_calls=%s,message=%s WHERE id=%s",
                (self.api_calls, str(exc)[:1000], run_id),
            )
            raise


def run_import(database_url: Optional[str] = None):
    importer = CanonicalOddsPapiImporter(database_url)
    try:
        return importer.run()
    finally:
        importer.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
