#!/usr/bin/env python3
"""Bridge DB-v3 player context into current fixture-enrichment snapshots.

fixture_enrichment_builder historically counted player_mapped only from FotMob deep
player-strength rows. The production player-context layer now lives in
player_team_context_snapshots, so this bridge copies those validated pre-match team
features into the fixture-level enrichment record and creates an explicit
player_mapped verification run.

Fail closed: zero fully mapped fixtures raises and prevents coverage/backtest/Top-10.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

from fixture_enrichment_builder import SCHEMA_SQL

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def latest_context(conn, team: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """SELECT expected_xi_strength,top11_strength,injury_impact,goalkeeper_injured,
                  retained_minutes_share,starter_continuity,player_coverage,key_absences,source_meta,snapshot_hour
           FROM player_team_context_snapshots
           WHERE team_name=%s ORDER BY snapshot_hour DESC LIMIT 1""",
        (team,),
    ).fetchone()
    if not row:
        return None
    keys = [
        "expected", "top11", "impact", "gk", "retained", "continuity",
        "coverage", "key", "meta", "snapshot_hour",
    ]
    out = dict(zip(keys, row))
    if out.get("expected") is None or float(out.get("coverage") or 0.0) <= 0:
        return None
    return out


def run_bridge(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
        rid = conn.execute("INSERT INTO fixture_enrichment_runs(status,message) VALUES('running','db-v3-player-context-bridge') RETURNING id").fetchone()[0]
        fixtures = player_mapped = any_player_mapped = style_mapped = elo_mapped = promotion_mapped = 0
        try:
            upcoming = conn.execute(
                """SELECT event_id,home_team,away_team FROM espn_upcoming
                   WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours'
                     AND match_date<=NOW()+INTERVAL '8 days' ORDER BY match_date"""
            ).fetchall()
            for event_id, home, away in upcoming:
                fixtures += 1
                snap = conn.execute(
                    """SELECT snapshot_hour,home_style,away_style,home_elo,away_elo,
                              home_promotion_prior,away_promotion_prior
                       FROM fixture_enrichment_snapshots
                       WHERE event_id=%s ORDER BY snapshot_hour DESC LIMIT 1""",
                    (event_id,),
                ).fetchone()
                if not snap:
                    continue
                snapshot_hour, home_style, away_style, home_elo, away_elo, home_promo, away_promo = snap
                hc = latest_context(conn, str(home))
                ac = latest_context(conn, str(away))
                any_player_mapped += int(bool(hc or ac))
                both = bool(hc and ac)
                player_mapped += int(both)
                style_mapped += int(home_style is not None and away_style is not None)
                elo_mapped += int(home_elo is not None and away_elo is not None)
                promotion_mapped += int(home_promo is not None or away_promo is not None)

                player_parts = [bool(hc), bool(ac)]
                style_parts = [home_style is not None, away_style is not None]
                elo_parts = [home_elo is not None, away_elo is not None]
                enrichment_coverage = sum(int(x) for x in player_parts + style_parts + elo_parts) / 6.0
                player_coverages = [float(x["coverage"] or 0.0) for x in (hc, ac) if x]
                player_coverage = sum(player_coverages) / len(player_coverages) if player_coverages else 0.0

                conn.execute(
                    """UPDATE fixture_enrichment_snapshots SET
                         home_expected_xi_strength=%s,away_expected_xi_strength=%s,
                         home_top11_strength=%s,away_top11_strength=%s,
                         home_injury_impact=%s,away_injury_impact=%s,
                         home_key_injuries=%s,away_key_injuries=%s,
                         home_goalkeeper_injured=%s,away_goalkeeper_injured=%s,
                         player_coverage=%s,enrichment_coverage=%s,built_at=NOW()
                       WHERE event_id=%s AND snapshot_hour=%s""",
                    (
                        hc.get("expected") if hc else None,
                        ac.get("expected") if ac else None,
                        hc.get("top11") if hc else None,
                        ac.get("top11") if ac else None,
                        hc.get("impact") if hc else None,
                        ac.get("impact") if ac else None,
                        Jsonb(hc.get("key") or []) if hc else Jsonb([]),
                        Jsonb(ac.get("key") or []) if ac else Jsonb([]),
                        hc.get("gk") if hc else None,
                        ac.get("gk") if ac else None,
                        player_coverage,
                        enrichment_coverage,
                        event_id,
                        snapshot_hour,
                    ),
                )

            status = "success" if player_mapped > 0 else "failed"
            message = {
                "source": "db-v3-player-context-bridge",
                "player_mapped": player_mapped,
                "any_player_mapped": any_player_mapped,
                "fixtures": fixtures,
            }
            conn.execute(
                """UPDATE fixture_enrichment_runs SET finished_at=NOW(),status=%s,fixtures=%s,player_mapped=%s,
                   style_mapped=%s,elo_mapped=%s,promotion_mapped=%s,message=%s WHERE id=%s""",
                (status, fixtures, player_mapped, style_mapped, elo_mapped, promotion_mapped,
                 json.dumps(message, separators=(",", ":")), rid),
            )
            result = {
                "status": status,
                "fixtures": fixtures,
                "player_mapped": player_mapped,
                "any_player_mapped": any_player_mapped,
                "style_mapped": style_mapped,
                "elo_mapped": elo_mapped,
                "promotion_mapped": promotion_mapped,
                "player_mapped_rate": round(player_mapped / fixtures, 4) if fixtures else 0.0,
            }
            print("PLAYER_CONTEXT_BRIDGE_RESULT", json.dumps(result, separators=(",", ":")))
            if player_mapped <= 0:
                raise RuntimeError("DB-v3 bridge failed closed: player_mapped=0")
            return result
        except Exception as exc:
            try:
                conn.execute(
                    "UPDATE fixture_enrichment_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",
                    (str(exc)[:700], rid),
                )
            except Exception:
                pass
            raise


if __name__ == "__main__":
    print(json.dumps(run_bridge(), indent=2))
