#!/usr/bin/env python3
"""Resilient Football-Data importer.

Uses a public GitHub mirror for the two completed Big Five seasons when
football-data.co.uk blocks cloud datacenter IPs. The current 2026/27 season is
still attempted from the official source, but a temporary failure there does
not prevent the completed seasons from being imported.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from football_data_importer import (
    FD_REFRESH_HOURS,
    FD_SEASONS,
    FootballDataImporter,
)

log = logging.getLogger("football-data-mirror-importer")

MIRROR_BASE = "https://raw.githubusercontent.com/tmorcinek/ekstraklasa-analysis/main/data"


def mirror_url(season_code: str, division: str) -> Optional[str]:
    # Repository documentation identifies the unsuffixed files as 2025/26
    # and the -2 files as 2024/25.
    if season_code == "2526":
        return f"{MIRROR_BASE}/{division}.csv"
    if season_code == "2425":
        return f"{MIRROR_BASE}/{division}-2.csv"
    return None


class ResilientFootballDataImporter(FootballDataImporter):
    def import_historical_source(
        self,
        season_code: str,
        division: str,
        league_name: str,
    ) -> int:
        key = f"history:{season_code}:{division}"
        is_current = season_code == max(FD_SEASONS)
        refresh_hours = FD_REFRESH_HOURS if is_current else 24 * 365 * 20

        if self.recently_succeeded(key, refresh_hours):
            row = self.conn.execute(
                "SELECT row_count FROM football_data_source_state WHERE source_key=%s",
                (key,),
            ).fetchone()
            return int(row[0] or 0) if row else 0

        mirror = mirror_url(season_code, division)
        official = (
            f"https://www.football-data.co.uk/mmz4281/"
            f"{season_code}/{division}.csv"
        )
        candidates = [mirror, official] if mirror else [official]
        candidates = [u for u in candidates if u]

        last_error: Optional[Exception] = None
        for url in candidates:
            try:
                source_type = "GitHub mirror" if url == mirror else "official"
                log.info(
                    "Football-Data %s: %s %s",
                    source_type,
                    league_name,
                    season_code,
                )
                rows = self.fetch_csv(url, retries=3 if url == official else 5)
                stored = 0
                for row in rows:
                    if self.upsert_match(row, season_code, division, league_name, url):
                        stored += 1

                if stored == 0:
                    raise RuntimeError(f"No completed matches found in {url}")

                self.set_state(
                    key,
                    url,
                    "success",
                    row_count=stored,
                    message=f"Imported via {source_type}.",
                )
                log.info(
                    "Football-Data stored: %s %s -> %s matches (%s)",
                    league_name,
                    season_code,
                    stored,
                    source_type,
                )
                return stored
            except Exception as exc:
                last_error = exc
                log.warning("Source failed for %s %s: %s", division, season_code, exc)

        # Crucial for 2026/27: do not throw away the completed-season import
        # just because the official current-season endpoint temporarily blocks
        # Render/GitHub cloud IPs.
        message = str(last_error or "No source available")
        self.set_state(
            key,
            candidates[-1],
            "failed",
            row_count=0,
            message=message,
        )
        log.warning(
            "Skipping unavailable source for %s %s; completed seasons remain usable.",
            league_name,
            season_code,
        )
        return 0

    def import_upcoming(self) -> int:
        try:
            return super().import_upcoming()
        except Exception as exc:
            log.warning("Upcoming Football-Data CSV unavailable for now: %s", exc)
            return 0


def run_import(database_url: Optional[str] = None) -> Dict[str, int]:
    importer = ResilientFootballDataImporter(database_url)
    try:
        result = importer.run()
        log.info("Resilient Football-Data import summary: %s", result)
        return result
    finally:
        importer.close()


def main() -> None:
    run_import()


if __name__ == "__main__":
    main()
