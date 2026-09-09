#!/usr/bin/env python3
"""Cache-first DB-v3 Expected-XI and squad-continuity context builder.

The previous league-page builder persisted every Understat player row one transaction
at a time before producing team context. On the smallest Render Postgres plan that
made a startup verification unnecessarily slow. DB-v3 first reuses any player rows
already cached in Postgres, fetches only missing league/season pages into memory, and
writes the compact team-context snapshots required by production.

This module intentionally does not activate any V5 ranking layer. Activation remains
owned by the leakage-safe V1/V5 policy backtest.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from psycopg.types.json import Jsonb

import understat_player_continuity_v2 as v2

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
CURRENT_SEASON = int(os.getenv("PLAYER_CONTEXT_CURRENT_SEASON", "2026"))
PREVIOUS_SEASON = CURRENT_SEASON - 1
LOOKAHEAD_DAYS = int(os.getenv("PLAYER_CONTEXT_LOOKAHEAD_DAYS", "8"))
REQUEST_DELAY = float(os.getenv("PLAYER_CONTEXT_REQUEST_DELAY_SECONDS", "0.55"))
HTTP_TIMEOUT = float(os.getenv("PLAYER_CONTEXT_HTTP_TIMEOUT_SECONDS", "25"))
LEAGUES = v2.LEAGUES


def _row_from_db(row: tuple[Any, ...]) -> Dict[str, Any]:
    return {
        "player_id": str(row[1]),
        "player_name": row[2],
        "games": row[3],
        "starts": row[4],
        "minutes": row[5],
        "goals": row[6],
        "xg": row[7],
        "assists": row[8],
        "xa": row[9],
        "xgchain": row[10],
        "xgbuildup": row[11],
        "raw": row[12] if isinstance(row[12], dict) else {},
        "team_title": row[0],
    }


def load_cached(conn, season: int) -> Dict[str, Tuple[str, List[Dict[str, Any]]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    labels: Dict[str, str] = {}
    try:
        rows = conn.execute(
            """SELECT team_name,player_id,player_name,games,starts,minutes,goals,xg,assists,xa,xgchain,xgbuildup,raw
               FROM understat_player_seasons WHERE season=%s""",
            (season,),
        ).fetchall()
    except Exception:
        return {}
    for row in rows:
        label = str(row[0])
        key = v2.canon(label)
        labels[key] = label
        grouped[key].append(_row_from_db(row))
    return {k: (labels[k], vals) for k, vals in grouped.items() if vals}


def fetch_memory(importer: v2.Importer, code: str, season: int) -> Dict[str, Tuple[str, List[Dict[str, Any]]]]:
    wait = REQUEST_DELAY - (time.monotonic() - importer.last_call)
    if wait > 0:
        time.sleep(wait)
    response = importer.session.get(f"{v2.BASE}/league/{code}/{season}", timeout=HTTP_TIMEOUT)
    importer.last_call = time.monotonic()
    importer.http_calls += 1
    if response.status_code != 200:
        raise RuntimeError(f"Understat league HTTP {response.status_code}: {code}/{season}")
    raw_rows = v2.extract_players_data(response.text)
    if not raw_rows:
        raise RuntimeError(f"Understat playersData empty: {code}/{season}")
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    labels: Dict[str, str] = {}
    for raw in raw_rows:
        label = v2.team_title(raw)
        player = v2.normalized_player(raw)
        if not label or not player:
            continue
        key = v2.canon(label)
        labels[key] = label
        buckets[key].append(player)
    if not buckets:
        raise RuntimeError(f"Understat player rows have no team_title: {code}/{season}")
    return {k: (labels[k], vals) for k, vals in buckets.items() if vals}


def _all_mapped(names: set[str], available: Dict[str, Tuple[str, List[Dict[str, Any]]]]) -> bool:
    return bool(names) and all(v2.match_team(name, available) is not None for name in names)


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")

    imp = v2.Importer(db)
    rid = imp.conn.execute("INSERT INTO player_context_runs(status) VALUES('running') RETURNING id").fetchone()[0]
    try:
        upcoming = imp.conn.execute(
            """SELECT DISTINCT league_name,home_team,away_team FROM espn_upcoming
               WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours'
                 AND match_date<=NOW()+(%s||' days')::interval""",
            (LOOKAHEAD_DAYS,),
        ).fetchall()
        by_league: Dict[str, set[str]] = defaultdict(set)
        for league, home, away in upcoming:
            by_league[str(league)].update((str(home), str(away)))

        current_cache = load_cached(imp.conn, CURRENT_SEASON)
        previous_cache = load_cached(imp.conn, PREVIOUS_SEASON)
        code_by_name = {name: code for code, name in LEAGUES}
        errors: Dict[str, str] = {}
        source_by_key: Dict[str, str] = {}

        for league, names in by_league.items():
            code = code_by_name.get(league)
            if not code:
                errors[f"{league}:league"] = "unsupported league code"
                continue
            if not _all_mapped(names, current_cache):
                try:
                    fresh = fetch_memory(imp, code, CURRENT_SEASON)
                    current_cache.update(fresh)
                    source_by_key[f"{league}:{CURRENT_SEASON}"] = "network-memory"
                except Exception as exc:
                    errors[f"{league}:{CURRENT_SEASON}"] = str(exc)[:300]
            else:
                source_by_key[f"{league}:{CURRENT_SEASON}"] = "postgres-cache"
            if not _all_mapped(names, previous_cache):
                try:
                    fresh = fetch_memory(imp, code, PREVIOUS_SEASON)
                    previous_cache.update(fresh)
                    source_by_key[f"{league}:{PREVIOUS_SEASON}"] = "network-memory"
                except Exception as exc:
                    errors[f"{league}:{PREVIOUS_SEASON}"] = str(exc)[:300]
            else:
                source_by_key[f"{league}:{PREVIOUS_SEASON}"] = "postgres-cache"

        hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        teams = mapped = current = previous = current_only = 0
        coverages: List[float] = []
        for league, names in by_league.items():
            for team in sorted(names):
                teams += 1
                cm = v2.match_team(team, current_cache)
                pm = v2.match_team(team, previous_cache)
                cur = cm[1] if cm else []
                prev = pm[1] if pm else []
                current += int(bool(cur))
                previous += int(bool(prev))
                mapped += int(bool(cur or prev))
                current_only += int(bool(cur and not prev))
                ctx = imp.build_context(
                    team,
                    cur,
                    prev,
                    current_label=cm[0] if cm else None,
                    previous_label=pm[0] if pm else None,
                )
                coverages.append(float(ctx.get("coverage") or 0.0))
                meta = dict(ctx.get("meta") or {})
                meta.update(
                    {
                        "source": "db-v3-cache-first",
                        "league": league,
                        "current_source": source_by_key.get(f"{league}:{CURRENT_SEASON}"),
                        "previous_source": source_by_key.get(f"{league}:{PREVIOUS_SEASON}"),
                        "current_match_score": round(cm[2], 4) if cm else None,
                        "previous_match_score": round(pm[2], 4) if pm else None,
                    }
                )
                imp.conn.execute(
                    """INSERT INTO player_team_context_snapshots(
                       team_name,snapshot_hour,current_season,previous_season,expected_xi_strength,top11_strength,
                       injury_impact,goalkeeper_injured,retained_minutes_share,starter_continuity,player_coverage,key_absences,source_meta)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(team_name,snapshot_hour) DO UPDATE SET
                         current_season=EXCLUDED.current_season,previous_season=EXCLUDED.previous_season,
                         expected_xi_strength=EXCLUDED.expected_xi_strength,top11_strength=EXCLUDED.top11_strength,
                         injury_impact=EXCLUDED.injury_impact,goalkeeper_injured=EXCLUDED.goalkeeper_injured,
                         retained_minutes_share=EXCLUDED.retained_minutes_share,starter_continuity=EXCLUDED.starter_continuity,
                         player_coverage=EXCLUDED.player_coverage,key_absences=EXCLUDED.key_absences,source_meta=EXCLUDED.source_meta""",
                    (
                        team,
                        hour,
                        CURRENT_SEASON,
                        PREVIOUS_SEASON,
                        ctx.get("expected"),
                        ctx.get("top11"),
                        ctx.get("impact"),
                        ctx.get("gk"),
                        ctx.get("retained"),
                        ctx.get("starter_continuity"),
                        ctx.get("coverage"),
                        Jsonb(ctx.get("key_absences") or []),
                        Jsonb(meta),
                    ),
                )

        avg_coverage = round(sum(coverages) / len(coverages), 4) if coverages else 0.0
        status = "success" if current > 0 and mapped > 0 else "failed"
        message = {
            "source": "db-v3-cache-first",
            "mapped": mapped,
            "current_only": current_only,
            "avg_coverage": avg_coverage,
            "errors": errors,
            "sources": source_by_key,
        }
        imp.conn.execute(
            """UPDATE player_context_runs SET finished_at=NOW(),status=%s,teams=%s,teams_with_current=%s,
               teams_with_previous=%s,http_calls=%s,message=%s WHERE id=%s""",
            (status, teams, current, previous, imp.http_calls, json.dumps(message, separators=(",", ":")), rid),
        )
        result = {
            "status": status,
            "teams": teams,
            "mapped": mapped,
            "current": current,
            "previous": previous,
            "current_only": current_only,
            "http_calls": imp.http_calls,
            "avg_coverage": avg_coverage,
            "errors": errors,
        }
        print("PLAYER_CONTEXT_DB_V3_RESULT", json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        if status != "success":
            raise RuntimeError(f"DB-v3 player context failed closed: mapped={mapped}, current={current}")
        return result
    except Exception as exc:
        try:
            imp.conn.execute(
                "UPDATE player_context_runs SET finished_at=NOW(),status='failed',http_calls=%s,message=%s WHERE id=%s",
                (imp.http_calls, str(exc)[:700], rid),
            )
        except Exception:
            pass
        raise
    finally:
        imp.close()


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
