#!/usr/bin/env python3
"""Quota-safe multi-bookmaker OddsPapi importer v3.

OddsPapi's live v4 endpoint currently accepts exactly one `bookmaker` query
parameter per request. This importer discovers supported bookmakers, requests
up to three independently, then stores them at one common snapshot hour so the
consensus builder can compare like-for-like prices. Provider errors are redacted.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import Any, Dict, Optional

from oddspapi_allbooks_importer_v2 import (
    API_KEY,
    SafeAllBooksImporter,
    discover_bookmaker_slugs,
    redact,
)
from oddspapi_importer import LOOKAHEAD_DAYS, fixture_objects, utcnow

MAX_BOOKS = max(1, min(5, int(os.getenv("ODDSPAPI_MAX_CONSENSUS_BOOKMAKERS", "3"))))


class MultiRequestAllBooksImporter(SafeAllBooksImporter):
    def run(self) -> Dict[str, Any]:
        selected, catalog = self.cached_catalog()
        if len(selected) != 5 or len(catalog) < 5:
            base = super().run()
            return {"status": "catalog_bootstrap", "base": base}

        rid = self.conn.execute(
            "INSERT INTO oddspapi_allbooks_runs(status) VALUES('running') RETURNING id"
        ).fetchone()[0]
        if not API_KEY:
            self.conn.execute(
                "UPDATE oddspapi_allbooks_runs SET finished_at=NOW(),status='not_configured',message='ODDSPAPI_API_KEY missing' WHERE id=%s",
                (rid,),
            )
            return {"status": "not_configured"}

        snapshot = utcnow().replace(minute=0, second=0, microsecond=0)
        start = utcnow() - timedelta(hours=3)
        end = utcnow() + timedelta(days=LOOKAHEAD_DAYS)
        seen_fixture_ids = set()
        seen_books = set()
        prices = 0
        failures: Dict[str, str] = {}
        successful_requests = 0
        try:
            books_payload = self.api_get("/v4/bookmakers", {"language": "en"})
            requested = discover_bookmaker_slugs(books_payload, limit=MAX_BOOKS)
            if not requested:
                raise RuntimeError("OddsPapi returned no usable bookmakers")

            fixture_map = self.fetch_fixture_map(start, end, selected)
            tids = ",".join(str(x) for x in sorted(selected))
            for bookmaker in requested:
                try:
                    payload = self.api_get(
                        "/v4/odds-by-tournaments",
                        {
                            "tournamentIds": tids,
                            "bookmaker": bookmaker,
                            "language": "en",
                            "verbosity": 3,
                            "oddsFormat": "decimal",
                        },
                    )
                    successful_requests += 1
                    for fixture in fixture_objects(payload):
                        fid = str(fixture.get("fixtureId") or "")
                        n, books = self.store_target_prices(
                            fixture,
                            fixture_map.get(fid),
                            selected,
                            catalog,
                            snapshot,
                            start,
                            end,
                        )
                        if n:
                            seen_fixture_ids.add(fid)
                            prices += n
                            seen_books.update(books)
                except Exception as exc:
                    failures[bookmaker] = redact(exc)
                    continue

            if successful_requests == 0:
                raise RuntimeError("OddsPapi all bookmaker requests failed")
            if not seen_books:
                # A request can succeed while target markets have not opened yet.
                status = "success"
                message = {
                    "requested": requested,
                    "returned": [],
                    "failures": failures,
                    "note": "requests succeeded but no target market prices were open",
                }
            else:
                status = "success"
                message = {"requested": requested, "returned": sorted(seen_books), "failures": failures}

            self.conn.execute(
                """UPDATE oddspapi_allbooks_runs SET finished_at=NOW(),status=%s,api_calls=%s,
                   fixtures=%s,bookmakers=%s,price_rows=%s,message=%s WHERE id=%s""",
                (
                    status,
                    self.api_calls,
                    len(seen_fixture_ids),
                    len(seen_books),
                    prices,
                    json.dumps(message, separators=(",", ":")),
                    rid,
                ),
            )
            result = {
                "status": status,
                "api_calls": self.api_calls,
                "fixtures": len(seen_fixture_ids),
                "bookmakers": len(seen_books),
                "price_rows": prices,
                "requested_bookmakers": requested,
                "failed_bookmakers": sorted(failures),
            }
            print("ODDSPAPI_ALLBOOKS_V3_RESULT", json.dumps(result, separators=(",", ":")))
            return result
        except Exception as exc:
            msg = redact(exc)
            self.conn.execute(
                "UPDATE oddspapi_allbooks_runs SET finished_at=NOW(),status='failed',api_calls=%s,message=%s WHERE id=%s",
                (self.api_calls, msg[:1000], rid),
            )
            raise RuntimeError(msg) from None


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    imp = MultiRequestAllBooksImporter(database_url)
    try:
        return imp.run()
    finally:
        imp.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
