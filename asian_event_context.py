#!/usr/bin/env python3
"""Resolve one event's best available Asian/total-goals market context.

Priority is source-by-field, not source-by-event:
1) OddsPapi for any field it actually has;
2) Football-Data.co.uk free fixture consensus for missing goal O/U 2.5;
3) ESPN free explicit over/under prices for any remaining missing goal O/U 2.5.

Corner O/U is never backfilled from a source that does not provide corner prices.
"""
from __future__ import annotations
from typing import Any, Dict


def _rowdict(row, keys):
    return dict(zip(keys, row)) if row else {}


def resolve_event_market(conn, event_id: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "goal_lines": {}, "corner_lines": {}, "goal_p": None, "corner_p": None,
        "goal_move": None, "corner_move": None, "books": 0,
        "goal_source": None, "corner_source": None, "sources": [],
    }

    # Primary: OddsPapi normalized per-book no-vig snapshot.
    try:
        fm = conn.execute(
            "SELECT oddspapi_fixture_id FROM prematch_feature_snapshots WHERE event_id=%s AND oddspapi_fixture_id IS NOT NULL ORDER BY snapshot_hour DESC LIMIT 1",
            (event_id,),
        ).fetchone()
    except Exception:
        fm = None
    if fm:
        try:
            ar = conn.execute(
                """SELECT goal_lines,corner_lines,goal_p_over_2_5,corner_p_over_8_5,
                          goal_open_to_latest_delta,corner_open_to_latest_delta,bookmaker_count
                   FROM asian_market_fixture_snapshots WHERE fixture_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",
                (str(fm[0]),),
            ).fetchone()
        except Exception:
            ar = None
        A = _rowdict(ar, ["goal_lines","corner_lines","goal_p","corner_p","goal_move","corner_move","books"])
        if A:
            result.update({k: A.get(k) for k in ("goal_lines","corner_lines","goal_p","corner_p","goal_move","corner_move")})
            result["books"] = int(A.get("books") or 0)
            if A.get("goal_p") is not None:
                result["goal_source"] = "oddspapi"
            if A.get("corner_p") is not None:
                result["corner_source"] = "oddspapi"
            result["sources"].append("oddspapi")
            result["oddspapi_fixture_id"] = str(fm[0])

    # Free fallback 1: Football-Data. Do not overwrite real OddsPapi goal data.
    try:
        fd = conn.execute(
            """SELECT goal_p_over,goal_over_price,goal_under_price,named_bookmaker_count,goal_books,
                      asian_handicap_home_line,asian_home_price,asian_away_price,mapping_score,snapshot_hour
               FROM football_data_event_market_snapshots WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
    except Exception:
        fd = None
    F = _rowdict(fd, ["goal_p","over","under","books","goal_books","ah_line","ah_home","ah_away","mapping_score","snapshot_hour"])
    if F:
        result["football_data"] = F
        result["sources"].append("football-data")
        if result.get("goal_p") is None and F.get("goal_p") is not None:
            result["goal_p"] = float(F["goal_p"])
            result["goal_source"] = "football-data"
            result["goal_lines"] = {"2.5": {"p_over": float(F["goal_p"]), "source": "football-data", "books": F.get("goal_books") or {}}}
            # Bookmaker count remains named books only; a market-average consensus
            # contributes one evidence source without pretending to be a bookmaker.
            result["books"] = max(int(result.get("books") or 0), int(F.get("books") or 0), 1)

    # Free fallback 2: ESPN explicit over/under prices.
    try:
        er = conn.execute(
            """SELECT provider,goal_p_over,over_price,under_price,snapshot_hour
               FROM espn_event_total_market_snapshots WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
    except Exception:
        er = None
    E = _rowdict(er, ["provider","goal_p","over","under","snapshot_hour"])
    if E:
        result["espn"] = E
        result["sources"].append("espn")
        if result.get("goal_p") is None and E.get("goal_p") is not None:
            result["goal_p"] = float(E["goal_p"])
            result["goal_source"] = "espn"
            result["goal_lines"] = {"2.5": {"p_over": float(E["goal_p"]), "source": "espn", "provider": E.get("provider")}}
            result["books"] = max(int(result.get("books") or 0), 1)

    result["sources"] = list(dict.fromkeys(result["sources"]))
    result["has_any"] = bool(result.get("goal_p") is not None or result.get("corner_p") is not None)
    return result
