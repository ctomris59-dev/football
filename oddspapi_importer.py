#!/usr/bin/env python3
"""Optional OddsPapi importer for current Big Five bookmaker market snapshots.

Purpose
-------
Collect actual pre-match market prices for the three markets used by the model:
- Over/Under 2.5 goals
- BTTS Yes/No
- Over/Under 8.5 total corners

The importer is deliberately optional. If ODDSPAPI_API_KEY is not configured it
creates no external dependency and exits successfully with a `not_configured`
result. Once a key is added, it dynamically resolves Big Five tournament IDs and
soccer market metadata instead of hard-coding fragile market IDs.

Raw fixture/odds payloads and normalized prices are both kept. Repeated runs use
hourly snapshot keys so line movement can be reconstructed over time.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb
import requests

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
API_KEY = os.getenv("ODDSPAPI_API_KEY", "").strip()
BASE_URL = os.getenv("ODDSPAPI_BASE_URL", "https://api.oddspapi.io").rstrip("/")
BOOKMAKER = os.getenv("ODDSPAPI_BOOKMAKER", "pinnacle").strip() or "pinnacle"
LOOKAHEAD_DAYS = int(os.getenv("ODDSPAPI_LOOKAHEAD_DAYS", "7"))
REQUEST_DELAY = float(os.getenv("ODDSPAPI_REQUEST_DELAY_SECONDS", "2.1"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("oddspapi-importer")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS oddspapi_tournaments (
    tournament_id BIGINT PRIMARY KEY,
    tournament_name TEXT NOT NULL,
    tournament_slug TEXT,
    category_name TEXT,
    category_slug TEXT,
    is_big5 BOOLEAN NOT NULL DEFAULT FALSE,
    raw JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS oddspapi_market_catalog (
    market_id BIGINT PRIMARY KEY,
    market_name TEXT,
    sport_id INTEGER,
    handicap DOUBLE PRECISION,
    period TEXT,
    market_type TEXT,
    player_prop BOOLEAN,
    outcomes JSONB,
    raw JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS oddspapi_fixture_snapshots (
    fixture_id TEXT NOT NULL,
    snapshot_hour TIMESTAMPTZ NOT NULL,
    tournament_id BIGINT,
    league_name TEXT,
    start_time TIMESTAMPTZ,
    home_team TEXT,
    away_team TEXT,
    participant1_id BIGINT,
    participant2_id BIGINT,
    bookmaker TEXT NOT NULL,
    has_odds BOOLEAN,
    raw_fixture JSONB,
    raw_odds JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (fixture_id, snapshot_hour, bookmaker)
);
CREATE INDEX IF NOT EXISTS idx_oddspapi_fixture_time
    ON oddspapi_fixture_snapshots(start_time, bookmaker, snapshot_hour DESC);

CREATE TABLE IF NOT EXISTS oddspapi_market_prices (
    fixture_id TEXT NOT NULL,
    snapshot_hour TIMESTAMPTZ NOT NULL,
    bookmaker TEXT NOT NULL,
    market_id BIGINT NOT NULL,
    market_name TEXT,
    handicap DOUBLE PRECISION,
    period TEXT,
    market_type TEXT,
    outcome_id BIGINT NOT NULL,
    outcome_name TEXT,
    bookmaker_outcome_id TEXT,
    price DOUBLE PRECISION,
    active BOOLEAN,
    main_line BOOLEAN,
    bookmaker_changed_at TIMESTAMPTZ,
    changed_at TIMESTAMPTZ,
    raw JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (fixture_id, snapshot_hour, bookmaker, market_id, outcome_id)
);
CREATE INDEX IF NOT EXISTS idx_oddspapi_prices_lookup
    ON oddspapi_market_prices(fixture_id, market_id, snapshot_hour DESC);
CREATE INDEX IF NOT EXISTS idx_oddspapi_prices_name_line
    ON oddspapi_market_prices(market_name, handicap, snapshot_hour DESC);

CREATE TABLE IF NOT EXISTS oddspapi_import_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    api_calls INTEGER NOT NULL DEFAULT 0,
    tournament_count INTEGER NOT NULL DEFAULT 0,
    fixture_count INTEGER NOT NULL DEFAULT 0,
    price_rows INTEGER NOT NULL DEFAULT 0,
    ou25_fixtures INTEGER NOT NULL DEFAULT 0,
    btts_fixtures INTEGER NOT NULL DEFAULT 0,
    corner85_fixtures INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""

BIG5_TARGETS: List[Tuple[str, str, str]] = [
    ("england", "premier league", "Premier League"),
    ("spain", "laliga", "La Liga"),
    ("italy", "serie a", "Serie A"),
    ("germany", "bundesliga", "Bundesliga"),
    ("france", "ligue 1", "Ligue 1"),
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def norm(value: Any) -> str:
    text = str(value or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def as_list(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("items", "fixtures", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        if payload.get("fixtureId"):
            return [payload]
    return []


def fixture_objects(payload: Any) -> List[Dict[str, Any]]:
    """Find fixture objects even if the API wraps them one level differently."""
    direct = as_list(payload)
    if direct:
        return direct
    found: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, dict) and value.get("fixtureId"):
                found.append(value)
            elif isinstance(value, list):
                found.extend(x for x in value if isinstance(x, dict) and x.get("fixtureId"))
    return found


class OddsPapiImporter:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db:
            raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True)
        self.conn.execute(SCHEMA_SQL)
        self.session = requests.Session()
        self.api_calls = 0
        self.last_call_at = 0.0

    def close(self) -> None:
        self.conn.close()

    def api_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not API_KEY:
            raise RuntimeError("ODDSPAPI_API_KEY is not configured")
        elapsed = time.monotonic() - self.last_call_at
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        query = dict(params or {})
        query["apiKey"] = API_KEY
        last: Optional[Exception] = None
        for attempt in range(4):
            try:
                response = self.session.get(f"{BASE_URL}{path}", params=query, timeout=40)
                self.last_call_at = time.monotonic()
                self.api_calls += 1
                if response.status_code == 429:
                    time.sleep(3 * (attempt + 1))
                    continue
                if response.status_code >= 500:
                    time.sleep(min(15, 2 ** attempt))
                    continue
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last = exc
                time.sleep(min(10, 2 ** attempt))
        raise RuntimeError(f"OddsPapi request failed for {path}: {last}")

    def store_tournaments(self, payload: Any) -> Dict[int, Dict[str, Any]]:
        selected: Dict[int, Dict[str, Any]] = {}
        rows = as_list(payload)
        for item in rows:
            tid = to_int(item.get("tournamentId"))
            if tid is None:
                continue
            category = norm(item.get("categoryName") or item.get("categorySlug"))
            tname = norm(item.get("tournamentName") or item.get("tournamentSlug"))
            big5_name: Optional[str] = None
            for want_category, want_name, display in BIG5_TARGETS:
                category_ok = want_category in category or category in want_category
                name_ok = want_name == tname or want_name in tname or tname in want_name
                if category_ok and name_ok:
                    big5_name = display
                    break
            self.conn.execute(
                """
                INSERT INTO oddspapi_tournaments(
                    tournament_id,tournament_name,tournament_slug,category_name,category_slug,is_big5,raw,updated_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT(tournament_id) DO UPDATE SET
                    tournament_name=EXCLUDED.tournament_name,
                    tournament_slug=EXCLUDED.tournament_slug,
                    category_name=EXCLUDED.category_name,
                    category_slug=EXCLUDED.category_slug,
                    is_big5=EXCLUDED.is_big5,
                    raw=EXCLUDED.raw,
                    updated_at=NOW()
                """,
                (
                    tid,
                    str(item.get("tournamentName") or ""),
                    item.get("tournamentSlug"),
                    item.get("categoryName"),
                    item.get("categorySlug"),
                    bool(big5_name),
                    Jsonb(item),
                ),
            )
            if big5_name:
                selected[tid] = {**item, "_league_name": big5_name}
        return selected

    def store_market_catalog(self, payload: Any) -> Dict[int, Dict[str, Any]]:
        catalog: Dict[int, Dict[str, Any]] = {}
        for item in as_list(payload):
            if to_int(item.get("sportId")) != 10:
                continue
            market_id = to_int(item.get("marketId"))
            if market_id is None:
                continue
            catalog[market_id] = item
            self.conn.execute(
                """
                INSERT INTO oddspapi_market_catalog(
                    market_id,market_name,sport_id,handicap,period,market_type,player_prop,outcomes,raw,updated_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT(market_id) DO UPDATE SET
                    market_name=EXCLUDED.market_name,
                    sport_id=EXCLUDED.sport_id,
                    handicap=EXCLUDED.handicap,
                    period=EXCLUDED.period,
                    market_type=EXCLUDED.market_type,
                    player_prop=EXCLUDED.player_prop,
                    outcomes=EXCLUDED.outcomes,
                    raw=EXCLUDED.raw,
                    updated_at=NOW()
                """,
                (
                    market_id,
                    item.get("marketName"),
                    10,
                    to_float(item.get("handicap")),
                    item.get("period"),
                    item.get("marketType"),
                    bool(item.get("playerProp")) if item.get("playerProp") is not None else None,
                    Jsonb(item.get("outcomes") or []),
                    Jsonb(item),
                ),
            )
        return catalog

    def fetch_fixture_map(self, start: datetime, end: datetime, selected: Dict[int, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        payload = self.api_get(
            "/v4/fixtures",
            {
                "sportId": 10,
                "from": start.isoformat().replace("+00:00", "Z"),
                "to": end.isoformat().replace("+00:00", "Z"),
                "statusId": 0,
                "language": "en",
            },
        )
        out: Dict[str, Dict[str, Any]] = {}
        for item in as_list(payload):
            fixture_id = str(item.get("fixtureId") or "")
            tid = to_int(item.get("tournamentId"))
            if fixture_id and tid in selected:
                out[fixture_id] = item
        return out

    def market_outcome_names(self, meta: Dict[str, Any]) -> Dict[int, str]:
        names: Dict[int, str] = {}
        for outcome in meta.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            oid = to_int(outcome.get("outcomeId"))
            if oid is not None:
                names[oid] = str(outcome.get("outcomeName") or "")
        return names

    def store_prices(
        self,
        fixture: Dict[str, Any],
        fixture_meta: Optional[Dict[str, Any]],
        selected: Dict[int, Dict[str, Any]],
        catalog: Dict[int, Dict[str, Any]],
        snapshot_hour: datetime,
        start: datetime,
        end: datetime,
    ) -> int:
        fixture_id = str(fixture.get("fixtureId") or "")
        if not fixture_id:
            return 0
        start_time = parse_dt(fixture.get("startTime"))
        if start_time is None or start_time < start or start_time > end:
            return 0
        tid = to_int(fixture.get("tournamentId"))
        if tid not in selected:
            return 0
        bookmaker_odds = fixture.get("bookmakerOdds") or {}
        book = bookmaker_odds.get(BOOKMAKER) if isinstance(bookmaker_odds, dict) else None
        if not isinstance(book, dict):
            return 0

        meta = fixture_meta or {}
        league_name = selected[tid].get("_league_name")
        home_team = meta.get("participant1ShortName") or meta.get("participant1Name")
        away_team = meta.get("participant2ShortName") or meta.get("participant2Name")
        participant1_id = to_int(meta.get("participant1Id") or fixture.get("participant1Id"))
        participant2_id = to_int(meta.get("participant2Id") or fixture.get("participant2Id"))

        self.conn.execute(
            """
            INSERT INTO oddspapi_fixture_snapshots(
                fixture_id,snapshot_hour,tournament_id,league_name,start_time,
                home_team,away_team,participant1_id,participant2_id,
                bookmaker,has_odds,raw_fixture,raw_odds,fetched_at
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT(fixture_id,snapshot_hour,bookmaker) DO UPDATE SET
                tournament_id=EXCLUDED.tournament_id,
                league_name=EXCLUDED.league_name,
                start_time=EXCLUDED.start_time,
                home_team=COALESCE(EXCLUDED.home_team,oddspapi_fixture_snapshots.home_team),
                away_team=COALESCE(EXCLUDED.away_team,oddspapi_fixture_snapshots.away_team),
                raw_fixture=COALESCE(EXCLUDED.raw_fixture,oddspapi_fixture_snapshots.raw_fixture),
                raw_odds=EXCLUDED.raw_odds,
                fetched_at=NOW()
            """,
            (
                fixture_id,snapshot_hour,tid,league_name,start_time,
                home_team,away_team,participant1_id,participant2_id,
                BOOKMAKER,bool(fixture.get("hasOdds")),Jsonb(meta) if meta else None,Jsonb(fixture),
            ),
        )

        stored = 0
        markets = book.get("markets") or {}
        if not isinstance(markets, dict):
            return 0
        for market_key, market_data in markets.items():
            if not isinstance(market_data, dict):
                continue
            market_id = to_int(market_key)
            if market_id is None:
                continue
            market_meta = catalog.get(market_id, {})
            outcome_names = self.market_outcome_names(market_meta)
            market_name = market_meta.get("marketName")
            handicap = to_float(market_meta.get("handicap"))
            period = market_meta.get("period")
            market_type = market_meta.get("marketType")
            outcomes = market_data.get("outcomes") or {}
            if not isinstance(outcomes, dict):
                continue
            for outcome_key, outcome_data in outcomes.items():
                if not isinstance(outcome_data, dict):
                    continue
                outcome_id = to_int(outcome_key)
                if outcome_id is None:
                    continue
                players = outcome_data.get("players") or {}
                player_rows: Iterable[Any]
                if isinstance(players, dict):
                    player_rows = players.values()
                elif isinstance(players, list):
                    player_rows = players
                else:
                    player_rows = []
                # Standard football markets normally have one player slot (key 0).
                # Prefer active/main-line record if multiple entries are present.
                options = [p for p in player_rows if isinstance(p, dict)]
                if not options:
                    continue
                options.sort(key=lambda p: (bool(p.get("active")), bool(p.get("mainLine"))), reverse=True)
                price_obj = options[0]
                self.conn.execute(
                    """
                    INSERT INTO oddspapi_market_prices(
                        fixture_id,snapshot_hour,bookmaker,market_id,market_name,handicap,period,market_type,
                        outcome_id,outcome_name,bookmaker_outcome_id,price,active,main_line,
                        bookmaker_changed_at,changed_at,raw,fetched_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT(fixture_id,snapshot_hour,bookmaker,market_id,outcome_id) DO UPDATE SET
                        market_name=EXCLUDED.market_name,
                        handicap=EXCLUDED.handicap,
                        outcome_name=EXCLUDED.outcome_name,
                        bookmaker_outcome_id=EXCLUDED.bookmaker_outcome_id,
                        price=EXCLUDED.price,
                        active=EXCLUDED.active,
                        main_line=EXCLUDED.main_line,
                        bookmaker_changed_at=EXCLUDED.bookmaker_changed_at,
                        changed_at=EXCLUDED.changed_at,
                        raw=EXCLUDED.raw,
                        fetched_at=NOW()
                    """,
                    (
                        fixture_id,snapshot_hour,BOOKMAKER,market_id,market_name,handicap,period,market_type,
                        outcome_id,outcome_names.get(outcome_id),price_obj.get("bookmakerOutcomeId"),
                        to_float(price_obj.get("price")),
                        bool(price_obj.get("active")) if price_obj.get("active") is not None else None,
                        bool(price_obj.get("mainLine")) if price_obj.get("mainLine") is not None else None,
                        parse_dt(price_obj.get("bookmakerChangedAt")),parse_dt(price_obj.get("changedAt")),Jsonb(price_obj),
                    ),
                )
                stored += 1
        return stored

    def quality_counts(self, snapshot_hour: datetime) -> Dict[str, int]:
        # Catalog-driven: no hard-coded IDs required.
        row = self.conn.execute(
            """
            SELECT
              COUNT(DISTINCT p.fixture_id) FILTER (
                WHERE lower(COALESCE(p.market_name,'')) = 'over under full time'
                  AND abs(COALESCE(p.handicap,-999)-2.5) < 0.001
                  AND p.price IS NOT NULL
              ) AS ou25,
              COUNT(DISTINCT p.fixture_id) FILTER (
                WHERE lower(COALESCE(p.market_name,'')) LIKE '%both teams to score%'
                  AND p.price IS NOT NULL
              ) AS btts,
              COUNT(DISTINCT p.fixture_id) FILTER (
                WHERE lower(COALESCE(p.market_name,'')) LIKE '%corner%'
                  AND abs(COALESCE(p.handicap,-999)-8.5) < 0.001
                  AND p.price IS NOT NULL
              ) AS corner85
            FROM oddspapi_market_prices p
            WHERE p.snapshot_hour=%s AND p.bookmaker=%s
            """,
            (snapshot_hour, BOOKMAKER),
        ).fetchone()
        return {"ou25": int(row[0] or 0), "btts": int(row[1] or 0), "corner85": int(row[2] or 0)}

    def run(self) -> Dict[str, Any]:
        run_id = self.conn.execute(
            "INSERT INTO oddspapi_import_runs(status,message) VALUES('running','started') RETURNING id"
        ).fetchone()[0]
        if not API_KEY:
            self.conn.execute(
                "UPDATE oddspapi_import_runs SET finished_at=NOW(),status='not_configured',message='ODDSPAPI_API_KEY is not configured' WHERE id=%s",
                (run_id,),
            )
            result = {"status": "not_configured", "message": "Add ODDSPAPI_API_KEY to enable Pinnacle market snapshots."}
            log.info("ODDSPAPI_RESULT %s", json.dumps(result, separators=(",", ":")))
            return result

        snapshot_hour = utcnow().replace(minute=0, second=0, microsecond=0)
        start = utcnow() - timedelta(hours=3)
        end = utcnow() + timedelta(days=LOOKAHEAD_DAYS)
        try:
            tournaments_payload = self.api_get("/v4/tournaments", {"sportId": 10, "language": "en"})
            selected = self.store_tournaments(tournaments_payload)
            if len(selected) != 5:
                raise RuntimeError(f"Could not resolve all Big Five tournaments; resolved={[(k,v.get('_league_name')) for k,v in selected.items()]}")

            markets_payload = self.api_get("/v4/markets", {"language": "en"})
            catalog = self.store_market_catalog(markets_payload)
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
            odds_fixtures = fixture_objects(odds_payload)
            price_rows = 0
            stored_fixtures = set()
            for fixture in odds_fixtures:
                before = price_rows
                price_rows += self.store_prices(
                    fixture,
                    fixture_map.get(str(fixture.get("fixtureId") or "")),
                    selected,catalog,snapshot_hour,start,end,
                )
                if price_rows > before:
                    stored_fixtures.add(str(fixture.get("fixtureId")))

            quality = self.quality_counts(snapshot_hour)
            self.conn.execute(
                """
                UPDATE oddspapi_import_runs SET
                    finished_at=NOW(),status='success',api_calls=%s,tournament_count=%s,
                    fixture_count=%s,price_rows=%s,ou25_fixtures=%s,btts_fixtures=%s,corner85_fixtures=%s,
                    message=%s WHERE id=%s
                """,
                (
                    self.api_calls,len(selected),len(stored_fixtures),price_rows,
                    quality["ou25"],quality["btts"],quality["corner85"],
                    json.dumps(quality,separators=(",",":")),run_id,
                ),
            )
            result = {
                "status":"success","api_calls":self.api_calls,"tournaments":len(selected),
                "fixtures":len(stored_fixtures),"price_rows":price_rows,
                "ou25_fixtures":quality["ou25"],"btts_fixtures":quality["btts"],"corner85_fixtures":quality["corner85"],
            }
            log.info("ODDSPAPI_RESULT %s", json.dumps(result,separators=(",",":")))
            return result
        except Exception as exc:
            self.conn.execute(
                "UPDATE oddspapi_import_runs SET finished_at=NOW(),status='failed',api_calls=%s,message=%s WHERE id=%s",
                (self.api_calls,str(exc)[:1000],run_id),
            )
            raise


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    importer = OddsPapiImporter(database_url)
    try:
        return importer.run()
    finally:
        importer.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
