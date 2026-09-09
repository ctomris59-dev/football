#!/usr/bin/env python3
"""Free Football-Data.co.uk odds fallback mapped directly to ESPN upcoming events.

Purpose
-------
Raise current Big-Five total-goals/Asian-odds coverage without a paid provider.
The weekly Football-Data fixture CSV is free and contains total-goals and Asian
handicap prices. We map rows to canonical ESPN upcoming events, preserve raw rows,
and publish a per-event consensus that the advanced context layer can use whenever
OddsPapi is missing.

This bridge is deliberately honest about source quality:
- named bookmaker O/U pairs are counted as bookmakers;
- market-average O/U is a fallback consensus, not a bookmaker;
- Asian handicap home/away prices are stored for audit but are not mislabeled as
  total-goals/corner O/U;
- no corner odds are fabricated.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import statistics
import time
import unicodedata
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg
import requests
from psycopg.types.json import Jsonb

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SOURCE_URL = os.getenv("FOOTBALL_DATA_FIXTURES_URL", "https://www.football-data.co.uk/fixtures.csv")
REFRESH_HOURS = float(os.getenv("FOOTBALL_DATA_ASIAN_REFRESH_HOURS", "6"))
LOOKAHEAD_DAYS = int(os.getenv("FOOTBALL_DATA_ASIAN_LOOKAHEAD_DAYS", "10"))
REQUEST_DELAY = float(os.getenv("FOOTBALL_DATA_ASIAN_REQUEST_DELAY_SECONDS", "0.25"))
TIMEOUT = float(os.getenv("FOOTBALL_DATA_ASIAN_TIMEOUT_SECONDS", "30"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

DIV_BY_LEAGUE = {
    "eng.1": "E0",
    "esp.1": "SP1",
    "ita.1": "I1",
    "ger.1": "D1",
    "fra.1": "F1",
}
BIG5_DIVS = set(DIV_BY_LEAGUE.values())

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("football-data-asian-bridge")

SCHEMA = """
CREATE TABLE IF NOT EXISTS football_data_event_market_snapshots(
    event_id TEXT NOT NULL,
    snapshot_hour TIMESTAMPTZ NOT NULL,
    division TEXT,
    match_date DATE,
    home_team TEXT,
    away_team TEXT,
    mapping_score DOUBLE PRECISION,
    goal_line DOUBLE PRECISION,
    goal_p_over DOUBLE PRECISION,
    goal_over_price DOUBLE PRECISION,
    goal_under_price DOUBLE PRECISION,
    named_bookmaker_count INTEGER NOT NULL DEFAULT 0,
    goal_books JSONB NOT NULL DEFAULT '{}'::jsonb,
    asian_handicap_home_line DOUBLE PRECISION,
    asian_home_price DOUBLE PRECISION,
    asian_away_price DOUBLE PRECISION,
    source_url TEXT NOT NULL,
    raw JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(event_id, snapshot_hour)
);
CREATE INDEX IF NOT EXISTS idx_fd_event_market_latest
    ON football_data_event_market_snapshots(event_id, snapshot_hour DESC);
CREATE TABLE IF NOT EXISTS football_data_asian_runs(
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    rows_fetched INTEGER NOT NULL DEFAULT 0,
    big5_rows INTEGER NOT NULL DEFAULT 0,
    mapped_events INTEGER NOT NULL DEFAULT 0,
    goal_events INTEGER NOT NULL DEFAULT 0,
    handicap_events INTEGER NOT NULL DEFAULT 0,
    unmapped_rows INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""

ALIASES = {
    "man united": "manchester united",
    "man utd": "manchester united",
    "man city": "manchester city",
    "nott m forest": "nottingham forest",
    "nottingham": "nottingham forest",
    "newcastle": "newcastle united",
    "tottenham": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "west ham": "west ham united",
    "leeds": "leeds united",
    "sunderland": "sunderland",
    "ath madrid": "atletico madrid",
    "atletico": "atletico madrid",
    "ath bilbao": "athletic club",
    "athletic bilbao": "athletic club",
    "sociedad": "real sociedad",
    "betis": "real betis",
    "vallecano": "rayo vallecano",
    "celta": "celta vigo",
    "osasuna": "osasuna",
    "inter": "internazionale",
    "inter milan": "internazionale",
    "roma": "as roma",
    "milan": "ac milan",
    "verona": "hellas verona",
    "paris sg": "paris saint germain",
    "psg": "paris saint germain",
    "marseille": "olympique marseille",
    "lyon": "olympique lyonnais",
    "monaco": "as monaco",
    "st etienne": "saint etienne",
    "m gladbach": "borussia monchengladbach",
    "gladbach": "borussia monchengladbach",
    "dortmund": "borussia dortmund",
    "leverkusen": "bayer leverkusen",
    "fc koln": "cologne",
    "koln": "cologne",
    "bayern munich": "bayern munich",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clean(v: Any) -> str:
    return str(v or "").strip()


def canon(v: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(v)).encode("ascii", "ignore").decode("ascii").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\b(fc|cf|afc|calcio|football club)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return ALIASES.get(text, text)


def sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    token = len(ta & tb) / max(1, len(ta | tb))
    seq = SequenceMatcher(None, a, b).ratio()
    return max(seq, 0.65 * seq + 0.35 * token)


def fnum(v: Any) -> Optional[float]:
    try:
        if v in (None, ""):
            return None
        return float(str(v).replace(",", ".").strip())
    except Exception:
        return None


def parse_date(v: Any) -> Optional[date]:
    s = clean(v)
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


def no_vig(over: float, under: float) -> Optional[float]:
    if over <= 1.001 or under <= 1.001:
        return None
    a, b = 1.0 / over, 1.0 / under
    return a / (a + b) if (a + b) else None


def first_pair(row: Dict[str, Any], over_keys: Iterable[str], under_keys: Iterable[str]) -> Tuple[Optional[float], Optional[float]]:
    ov = next((fnum(row.get(k)) for k in over_keys if fnum(row.get(k)) is not None), None)
    un = next((fnum(row.get(k)) for k in under_keys if fnum(row.get(k)) is not None), None)
    return ov, un


def goal_consensus(row: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float], Dict[str, Any], int]:
    books: Dict[str, Any] = {}
    named = [
        ("bet365", ("B365>2.5", "B365C>2.5"), ("B365<2.5", "B365C<2.5")),
        ("pinnacle", ("P>2.5", "PC>2.5", "P C>2.5"), ("P<2.5", "PC<2.5", "P C<2.5")),
        ("gamebookers", ("GB>2.5",), ("GB<2.5",)),
    ]
    probs: List[float] = []
    for name, oks, uks in named:
        ov, un = first_pair(row, oks, uks)
        if ov is None or un is None:
            continue
        p = no_vig(ov, un)
        if p is None:
            continue
        books[name] = {"over": ov, "under": un, "p_over": p, "aggregate": False}
        probs.append(p)

    avg_ov, avg_un = first_pair(row, ("Avg>2.5", "AvgC>2.5"), ("Avg<2.5", "AvgC<2.5"))
    avg_p = no_vig(avg_ov, avg_un) if avg_ov is not None and avg_un is not None else None
    if avg_p is not None:
        books["market_average"] = {"over": avg_ov, "under": avg_un, "p_over": avg_p, "aggregate": True}
    use = probs if probs else ([avg_p] if avg_p is not None else [])
    if not use:
        return None, None, None, books, len(probs)
    p = statistics.median(use)
    display_ov = statistics.median([v["over"] for v in books.values() if v.get("over") is not None]) if books else None
    display_un = statistics.median([v["under"] for v in books.values() if v.get("under") is not None]) if books else None
    return p, display_ov, display_un, books, len(probs)


def handicap_values(row: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    line = fnum(row.get("AHh"))
    h = next((fnum(row.get(k)) for k in ("B365AHH", "PAHH", "AvgAHH", "MaxAHH") if fnum(row.get(k)) is not None), None)
    a = next((fnum(row.get(k)) for k in ("B365AHA", "PAHA", "AvgAHA", "MaxAHA") if fnum(row.get(k)) is not None), None)
    return line, h, a


class Bridge:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.db = (database_url or DATABASE_URL).strip()
        if not self.db:
            raise RuntimeError("Missing DATABASE_URL")
        self.conn = psycopg.connect(self.db, autocommit=True)
        self.conn.execute(SCHEMA)
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; FootballDatasetCollector/1.0)",
            "Accept": "text/csv,text/plain,*/*",
        })

    def close(self) -> None:
        self.conn.close()

    def fresh(self) -> bool:
        if REFRESH_HOURS <= 0:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM football_data_asian_runs WHERE status='success' AND finished_at>=NOW()-(%s||' hours')::interval LIMIT 1",
            (REFRESH_HOURS,),
        ).fetchone()
        return bool(row)

    def fetch_rows(self) -> List[Dict[str, str]]:
        if REQUEST_DELAY:
            time.sleep(REQUEST_DELAY)
        r = self.s.get(SOURCE_URL, timeout=(5.0, TIMEOUT))
        r.raise_for_status()
        text = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = r.content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        text = text if text is not None else r.text
        return [
            {str(k).strip().lstrip("\ufeff"): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
            for row in csv.DictReader(io.StringIO(text))
            if row
        ]

    def upcoming(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT event_id,league_slug,match_date,home_team,away_team
               FROM espn_upcoming
               WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '6 hours'
                 AND match_date<=NOW()+(%s||' days')::interval""",
            (LOOKAHEAD_DAYS,),
        ).fetchall()
        out = []
        for eid, league, dt, home, away in rows:
            out.append({
                "event_id": str(eid), "league": str(league), "division": DIV_BY_LEAGUE.get(str(league)),
                "date": dt.date() if dt else None, "home": str(home), "away": str(away),
                "home_c": canon(home), "away_c": canon(away),
            })
        return out

    def match_event(self, row: Dict[str, Any], events: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], float]:
        div = clean(row.get("Div"))
        rd = parse_date(row.get("Date"))
        hc, ac = canon(row.get("HomeTeam")), canon(row.get("AwayTeam"))
        best = None
        best_score = 0.0
        for e in events:
            if e.get("division") != div:
                continue
            if rd and e.get("date") and abs((e["date"] - rd).days) > 2:
                continue
            hs, ass = sim(hc, e["home_c"]), sim(ac, e["away_c"])
            if hs < 0.68 or ass < 0.68:
                continue
            date_bonus = 1.0 if (rd and e.get("date") == rd) else 0.92
            score = (0.48 * hs + 0.48 * ass + 0.04 * date_bonus)
            if score > best_score:
                best, best_score = e, score
        return (best, round(best_score, 4)) if best_score >= 0.76 else (None, round(best_score, 4))

    def run(self) -> Dict[str, Any]:
        if self.fresh():
            row = self.conn.execute(
                "SELECT rows_fetched,big5_rows,mapped_events,goal_events,handicap_events,unmapped_rows FROM football_data_asian_runs WHERE status='success' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            res = {"status": "fresh_skip", "rows_fetched": int(row[0]), "big5_rows": int(row[1]), "mapped_events": int(row[2]),
                   "goal_events": int(row[3]), "handicap_events": int(row[4]), "unmapped_rows": int(row[5])}
            print("FOOTBALL_DATA_ASIAN_RESULT", json.dumps(res, separators=(",", ":")), flush=True)
            return res

        rid = self.conn.execute("INSERT INTO football_data_asian_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        rows_fetched = big5 = mapped = goals = handicaps = unmapped = 0
        try:
            rows = self.fetch_rows(); rows_fetched = len(rows)
            events = self.upcoming()
            hour = utcnow().replace(minute=0, second=0, microsecond=0)
            for row in rows:
                if clean(row.get("Div")) not in BIG5_DIVS:
                    continue
                big5 += 1
                event, score = self.match_event(row, events)
                if not event:
                    unmapped += 1
                    continue
                gp, gov, gun, books, book_count = goal_consensus(row)
                ah_line, ah_h, ah_a = handicap_values(row)
                mapped += 1
                goals += int(gp is not None)
                handicaps += int(ah_line is not None and ah_h is not None and ah_a is not None)
                self.conn.execute(
                    """INSERT INTO football_data_event_market_snapshots(
                       event_id,snapshot_hour,division,match_date,home_team,away_team,mapping_score,
                       goal_line,goal_p_over,goal_over_price,goal_under_price,named_bookmaker_count,goal_books,
                       asian_handicap_home_line,asian_home_price,asian_away_price,source_url,raw)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,2.5,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(event_id,snapshot_hour) DO UPDATE SET
                       division=EXCLUDED.division,match_date=EXCLUDED.match_date,home_team=EXCLUDED.home_team,away_team=EXCLUDED.away_team,
                       mapping_score=EXCLUDED.mapping_score,goal_p_over=EXCLUDED.goal_p_over,goal_over_price=EXCLUDED.goal_over_price,
                       goal_under_price=EXCLUDED.goal_under_price,named_bookmaker_count=EXCLUDED.named_bookmaker_count,goal_books=EXCLUDED.goal_books,
                       asian_handicap_home_line=EXCLUDED.asian_handicap_home_line,asian_home_price=EXCLUDED.asian_home_price,
                       asian_away_price=EXCLUDED.asian_away_price,source_url=EXCLUDED.source_url,raw=EXCLUDED.raw,fetched_at=NOW()""",
                    (event["event_id"], hour, clean(row.get("Div")), parse_date(row.get("Date")), clean(row.get("HomeTeam")), clean(row.get("AwayTeam")),
                     score, gp, gov, gun, book_count, Jsonb(books), ah_line, ah_h, ah_a, SOURCE_URL, Jsonb(row)),
                )
            status = "success"
            msg = json.dumps({"events_in_horizon": len(events), "source": SOURCE_URL}, separators=(",", ":"))
            self.conn.execute(
                """UPDATE football_data_asian_runs SET finished_at=NOW(),status=%s,rows_fetched=%s,big5_rows=%s,mapped_events=%s,
                   goal_events=%s,handicap_events=%s,unmapped_rows=%s,message=%s WHERE id=%s""",
                (status, rows_fetched, big5, mapped, goals, handicaps, unmapped, msg[:1000], rid),
            )
            res = {"status": status, "rows_fetched": rows_fetched, "big5_rows": big5, "mapped_events": mapped,
                   "goal_events": goals, "handicap_events": handicaps, "unmapped_rows": unmapped, "events_in_horizon": len(events)}
            print("FOOTBALL_DATA_ASIAN_RESULT", json.dumps(res, separators=(",", ":")), flush=True)
            return res
        except Exception as exc:
            self.conn.execute("UPDATE football_data_asian_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s", (str(exc)[:1000], rid))
            raise


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    b = Bridge(database_url)
    try:
        return b.run()
    finally:
        b.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
