#!/usr/bin/env python3
"""Quota-safe multi-bookmaker OddsPapi importer v2.

Fixes two production issues discovered during the live bootstrap:
- always sends an explicit, verified bookmaker list to odds-by-tournaments;
- never includes API keys, request URLs or query strings in exceptions/logs.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import requests

from oddspapi_allbooks_importer import AllBooksImporter
from oddspapi_importer import (
    API_KEY, BASE_URL, LOOKAHEAD_DAYS, REQUEST_DELAY, as_list, fixture_objects,
    parse_dt, to_int, utcnow,
)

PREFERRED = [
    x.strip().lower()
    for x in os.getenv(
        "ODDSPAPI_CONSENSUS_BOOKMAKERS",
        "pinnacle,betfair_ex_eu,betfair,bet365,williamhill,unibet",
    ).split(",")
    if x.strip()
]
MAX_BOOKS = max(1, min(10, int(os.getenv("ODDSPAPI_MAX_CONSENSUS_BOOKMAKERS", "6"))))


def redact(text: Any) -> str:
    """Remove common secret/query representations from provider diagnostics."""
    s = str(text or "")
    if API_KEY:
        s = s.replace(API_KEY, "[REDACTED]")
    # Never preserve query strings because providers commonly put secrets there.
    if "?" in s:
        s = s.split("?", 1)[0] + "?[REDACTED_QUERY]"
    return s[:500]


def safe_http_error(path: str, status: Optional[int], body: Any = "") -> RuntimeError:
    clean_body = redact(body).replace("\n", " ")[:240]
    suffix = f": {clean_body}" if clean_body else ""
    return RuntimeError(f"OddsPapi HTTP {status or 'network'} for {path}{suffix}")


def bookmaker_slug(row: Dict[str, Any]) -> Optional[str]:
    for key in ("slug", "bookmakerSlug", "bookmaker", "id"):
        v = row.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def discover_bookmaker_slugs(payload: Any, preferred: Iterable[str] = PREFERRED, limit: int = MAX_BOOKS) -> List[str]:
    rows = as_list(payload)
    available: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = bookmaker_slug(row)
        if not slug or slug in available:
            continue
        # Clones add little independent information to a consensus.
        if row.get("cloneOf"):
            continue
        available.append(slug)
    ordered = [x for x in preferred if x in available]
    ordered.extend(x for x in available if x not in ordered)
    return ordered[: max(1, limit)]


class SafeAllBooksImporter(AllBooksImporter):
    def api_get(self, path: str, params: Optional[Dict[str, Any]] = None, *, retries: int = 4) -> Any:
        if not API_KEY:
            raise RuntimeError("ODDSPAPI_API_KEY missing")
        query = dict(params or {})
        query["apiKey"] = API_KEY
        last_status: Optional[int] = None
        last_body = ""
        for attempt in range(retries):
            elapsed = time.monotonic() - self.last_call_at
            if elapsed < REQUEST_DELAY:
                time.sleep(REQUEST_DELAY - elapsed)
            try:
                response = self.session.get(BASE_URL + path, params=query, timeout=45)
                self.last_call_at = time.monotonic()
                self.api_calls += 1
                last_status = response.status_code
                last_body = response.text[:500]
                if response.status_code == 429:
                    time.sleep(min(60, 5 * (attempt + 1)))
                    continue
                if response.status_code >= 500:
                    time.sleep(min(30, 2 ** attempt))
                    continue
                if response.status_code >= 400:
                    raise safe_http_error(path, response.status_code, response.text)
                return response.json()
            except RuntimeError:
                raise
            except requests.RequestException:
                # Intentionally omit exception text: requests may include response.url
                # and therefore the apiKey query parameter.
                if attempt + 1 < retries:
                    time.sleep(min(20, 2 ** attempt))
                    continue
                raise safe_http_error(path, last_status, last_body)
            except ValueError:
                raise RuntimeError(f"OddsPapi invalid JSON for {path}")
        raise safe_http_error(path, last_status, last_body)

    def run(self) -> Dict[str, Any]:
        selected, catalog = self.cached_catalog()
        if len(selected) != 5 or len(catalog) < 5:
            # Parent bootstrap uses our overridden safe api_get.
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
        fixtures = prices = 0
        seen_books = set()
        try:
            books_payload = self.api_get("/v4/bookmakers", {"language": "en"})
            requested = discover_bookmaker_slugs(books_payload)
            if not requested:
                raise RuntimeError("OddsPapi returned no usable bookmakers")

            fixture_map = self.fetch_fixture_map(start, end, selected)
            tids = ",".join(str(x) for x in sorted(selected))
            params = {
                "tournamentIds": tids,
                "bookmakers": ",".join(requested),
                "language": "en",
                "verbosity": 3,
                "oddsFormat": "decimal",
            }
            payload = self.api_get("/v4/odds-by-tournaments", params)
            for fixture in fixture_objects(payload):
                n, books = self.store_target_prices(
                    fixture,
                    fixture_map.get(str(fixture.get("fixtureId") or "")),
                    selected,
                    catalog,
                    snapshot,
                    start,
                    end,
                )
                if n:
                    fixtures += 1
                    prices += n
                    seen_books.update(books)

            self.conn.execute(
                """UPDATE oddspapi_allbooks_runs SET finished_at=NOW(),status='success',api_calls=%s,
                   fixtures=%s,bookmakers=%s,price_rows=%s,message=%s WHERE id=%s""",
                (
                    self.api_calls,
                    fixtures,
                    len(seen_books),
                    prices,
                    json.dumps({"requested": requested, "returned": sorted(seen_books)}, separators=(",", ":")),
                    rid,
                ),
            )
            result = {
                "status": "success",
                "api_calls": self.api_calls,
                "fixtures": fixtures,
                "bookmakers": len(seen_books),
                "price_rows": prices,
                "requested_bookmakers": requested,
            }
            print("ODDSPAPI_ALLBOOKS_V2_RESULT", json.dumps(result, separators=(",", ":")))
            return result
        except Exception as exc:
            msg = redact(exc)
            self.conn.execute(
                "UPDATE oddspapi_allbooks_runs SET finished_at=NOW(),status='failed',api_calls=%s,message=%s WHERE id=%s",
                (self.api_calls, msg[:1000], rid),
            )
            raise RuntimeError(msg) from None


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    imp = SafeAllBooksImporter(database_url)
    try:
        return imp.run()
    finally:
        imp.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
