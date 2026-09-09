#!/usr/bin/env python3
"""TLS-independent second-tier history importer v3.

Uses GitHub-hosted open football data as the resilient source used by promotion
validation. Data is intentionally results-only when the rich Football-Data host is
unreachable; unavailable shots/corners remain NULL and are never imputed as zero.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from second_tier_importer import SecondTierImporter

SEASONS = [x.strip() for x in os.getenv("SECOND_TIER_SEASONS", "2324,2425,2526").split(",") if x.strip()]
REQUEST_DELAY = float(os.getenv("SECOND_TIER_GITHUB_DELAY_SECONDS", "0.20"))

LEAGUES: List[Tuple[str, str, str, str]] = [
    ("E1", "Championship", "Premier League", "en.2"),
    ("SP2", "Segunda Division", "La Liga", "es.2"),
    ("I2", "Serie B", "Serie A", "it.2"),
    ("D2", "2. Bundesliga", "Bundesliga", "de.2"),
    ("F2", "Ligue 2", "Ligue 1", "fr.2"),
]
SEASON_FOLDER = {"2324": "2023-24", "2425": "2024-25", "2526": "2025-26"}
CACHE_2324 = {"E1": "eng.2", "SP2": "es.2", "I2": "it.2", "D2": "de.2", "F2": "fr.2"}


def score_pair(value: Any) -> Optional[Tuple[int, int]]:
    if isinstance(value, dict):
        value = value.get("ft")
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return int(value[0]), int(value[1])
        except Exception:
            return None
    if isinstance(value, str) and "-" in value:
        try:
            a, b = value.split("-", 1)
            return int(a.strip()), int(b.strip())
        except Exception:
            return None
    return None


def openfootball_rows(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    out: List[Dict[str, Any]] = []
    for m in payload.get("matches") or []:
        if not isinstance(m, dict):
            continue
        ft = score_pair(m.get("score"))
        if not ft:
            continue
        score = m.get("score")
        ht = score_pair(score.get("ht")) if isinstance(score, dict) else None
        date = str(m.get("date") or "").strip()
        home = str(m.get("team1") or "").strip()
        away = str(m.get("team2") or "").strip()
        if not date or not home or not away:
            continue
        out.append({
            "Date": date,
            "Time": str(m.get("time") or "").strip(),
            "HomeTeam": home,
            "AwayTeam": away,
            "FTHG": ft[0],
            "FTAG": ft[1],
            "HTHG": ht[0] if ht else None,
            "HTAG": ht[1] if ht else None,
            "_source_round": m.get("round"),
            "_source_status": m.get("status"),
        })
    return out


def cache_csv_rows(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in csv.DictReader(io.StringIO(text)):
        ft = score_pair(m.get("FT"))
        if not ft:
            continue
        ht = score_pair(m.get("HT"))
        raw_date = str(m.get("Date") or "").strip()
        try:
            iso = datetime.strptime(raw_date, "%a %b %d %Y").date().isoformat()
        except ValueError:
            try:
                iso = datetime.strptime(raw_date, "%a %b %d %Y").date().isoformat()
            except Exception:
                continue
        home = str(m.get("Team 1") or "").strip()
        away = str(m.get("Team 2") or "").strip()
        if not home or not away:
            continue
        out.append({
            "Date": iso,
            "HomeTeam": home,
            "AwayTeam": away,
            "FTHG": ft[0],
            "FTAG": ft[1],
            "HTHG": ht[0] if ht else None,
            "HTAG": ht[1] if ht else None,
        })
    return out


class ResilientSecondTierImporter(SecondTierImporter):
    def __init__(self, database_url: Optional[str] = None) -> None:
        super().__init__(database_url)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "FootballPredictionDataset/3.0", "Accept": "application/json,text/csv,*/*"})
        self.http_calls = 0

    def existing_count(self, season: str, division: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM second_tier_matches WHERE season_code=%s AND division=%s",
            (season, division),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def get(self, url: str, *, json_mode: bool) -> Any:
        last = ""
        for attempt in range(3):
            if REQUEST_DELAY:
                time.sleep(REQUEST_DELAY)
            try:
                r = self.session.get(url, timeout=40)
                self.http_calls += 1
                if r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                    time.sleep(1 + attempt)
                    continue
                r.raise_for_status()
                return r.json() if json_mode else r.text
            except requests.RequestException as exc:
                last = exc.__class__.__name__
                time.sleep(1 + attempt)
            except ValueError:
                raise RuntimeError("Invalid second-tier JSON payload")
        raise RuntimeError(f"Second-tier mirror unavailable: {last}")

    def source_rows(self, season: str, division: str, slug: str) -> Tuple[List[Dict[str, Any]], str]:
        folder = SEASON_FOLDER[season]
        if season == "2324":
            cache_slug = CACHE_2324[division]
            url = f"https://raw.githubusercontent.com/footballcsv/cache.footballdata/master/{folder}/{cache_slug}.csv"
            return cache_csv_rows(self.get(url, json_mode=False)), url
        url = f"https://raw.githubusercontent.com/openfootball/football.json/master/{folder}/{slug}.json"
        return openfootball_rows(self.get(url, json_mode=True)), url

    def run(self) -> Dict[str, Any]:
        rid = self.conn.execute("INSERT INTO second_tier_import_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        total = 0
        details: Dict[str, Any] = {}
        failures: Dict[str, str] = {}
        try:
            for season in SEASONS:
                if season not in SEASON_FOLDER:
                    continue
                for division, league, parent, slug in LEAGUES:
                    key = f"{season}:{division}"
                    existing = self.existing_count(season, division)
                    if existing >= 20:
                        details[key] = {"rows": existing, "source": "database", "fresh": True}
                        total += existing
                        continue
                    try:
                        rows, url = self.source_rows(season, division, slug)
                        stored = sum(self.store_row(r, season, division, league, parent, url) for r in rows)
                        if stored < 20:
                            raise RuntimeError(f"insufficient rows: {stored}")
                        self.set_state(season, division, url, "success", stored, "resilient GitHub result mirror")
                        details[key] = {"rows": stored, "source": url}
                        total += stored
                    except Exception as exc:
                        failures[key] = str(exc)[:300]
                        details[key] = {"rows": 0, "error": failures[key]}
            status = "success" if not failures else ("partial" if total else "failed")
            self.conn.execute(
                """UPDATE second_tier_import_runs SET finished_at=NOW(),status=%s,source_calls=%s,
                   rows_stored=%s,message=%s WHERE id=%s""",
                (status, self.http_calls, total, json.dumps({"sources": details, "failures": failures}, separators=(",", ":")), rid),
            )
            result = {"status": status, "http_calls": self.http_calls, "rows": total, "by_source": details, "failures": failures}
            print("SECOND_TIER_V3_RESULT", json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            if status == "failed":
                raise RuntimeError("All second-tier mirror sources failed")
            return result
        except Exception as exc:
            self.conn.execute(
                "UPDATE second_tier_import_runs SET finished_at=NOW(),status='failed',source_calls=%s,rows_stored=%s,message=%s WHERE id=%s",
                (self.http_calls, total, str(exc)[:1000], rid),
            )
            raise


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    imp = ResilientSecondTierImporter(database_url)
    try:
        return imp.run()
    finally:
        imp.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
