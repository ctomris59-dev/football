#!/usr/bin/env python3
"""Safe pre-match context builder with cross-competition schedule preference."""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Dict, Optional

from psycopg.types.json import Jsonb

from prematch_context_builder import PrematchContextBuilder, age_hours


class SafePrematchContextBuilder(PrematchContextBuilder):
    def schedule(self, team: str, fixture_dt):
        """Prefer ESPN team-schedule history; fall back to domestic league rows."""
        try:
            team_row = self.conn.execute(
                """SELECT team_id FROM espn_team_schedule_events
                   WHERE lower(team_name)=lower(%s) ORDER BY updated_at DESC LIMIT 1""",
                (team,),
            ).fetchone()
            if team_row and team_row[0]:
                rows = self.conn.execute(
                    """SELECT match_date FROM espn_team_schedule_events
                       WHERE team_id=%s AND match_date < %s
                         AND COALESCE(completed,FALSE)=TRUE
                       ORDER BY match_date DESC LIMIT 20""",
                    (team_row[0], fixture_dt),
                ).fetchall()
                dates = [r[0] for r in rows if r[0]]
                if dates:
                    rest = (fixture_dt-dates[0]).total_seconds()/86400.0
                    seven = sum(1 for d in dates if fixture_dt-timedelta(days=7) <= d < fixture_dt)
                    fourteen = sum(1 for d in dates if fixture_dt-timedelta(days=14) <= d < fixture_dt)
                    return {"days_rest": round(rest,2), "last7": seven, "last14": fourteen, "scope":"team_schedule_endpoint"}
        except Exception:
            pass
        result = super().schedule(team, fixture_dt)
        result["scope"] = "domestic_league_only"
        return result

    def build(self) -> Dict[str, Any]:
        run_id = self.conn.execute("INSERT INTO prematch_context_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        odds_matched = availability_matched = 0
        try:
            matches = self.upcoming()
            avail_as_of, avail_stale, absence_rows = self.latest_absence_rows()
            for m in matches:
                home_sched = self.schedule(m["home_team"], m["match_date"])
                away_sched = self.schedule(m["away_team"], m["match_date"])
                pre = self.latest_prematch(m["event_id"])
                odds_match = self.match_odds_fixture(m["league_name"], m["match_date"], m["home_team"], m["away_team"])
                odds = self.odds_summary(str(odds_match[0])) if odds_match else {}
                if odds_match:
                    odds_matched += 1
                hc = self.team_absence_counts(m["home_team"], absence_rows)
                ac = self.team_absence_counts(m["away_team"], absence_rows)
                if hc["all"] or ac["all"]:
                    availability_matched += 1

                schedule_scope = "team_schedule_endpoint" if home_sched.get("scope")=="team_schedule_endpoint" and away_sched.get("scope")=="team_schedule_endpoint" else "domestic_league_only"
                quality = {
                    "schedule_scope": schedule_scope,
                    "schedule_complete": home_sched["days_rest"] is not None and away_sched["days_rest"] is not None,
                    "prematch_roster_or_lineup": bool((pre.get("lineup") or 0) > 0 or (pre.get("roster") or 0) > 0),
                    "odds_matched": bool(odds_match),
                    "availability_source_present": bool(absence_rows),
                    "availability_is_confirmed_current": False,
                    "availability_stale": avail_stale,
                    "has_ou25": bool(odds.get("ou25")),
                    "has_btts": bool(odds.get("btts")),
                    "has_corner85": bool(odds.get("corner85")),
                }

                params = (
                    m["event_id"], self.hour, m["league_slug"], m["league_name"], m["match_date"], m["home_team"], m["away_team"],
                    home_sched["days_rest"], away_sched["days_rest"], home_sched["last7"], away_sched["last7"], home_sched["last14"], away_sched["last14"],
                    pre.get("lineup"), pre.get("roster"), age_hours(pre.get("snapshot"), self.now),
                    str(odds_match[0]) if odds_match else None, age_hours(odds.get("snapshot"), self.now), int(odds.get("rows") or 0),
                    bool(odds.get("ou25")), bool(odds.get("btts")), bool(odds.get("corner85")), Jsonb(odds.get("movement") or {}),
                    hc["all"], ac["all"], hc["injury"], ac["injury"], hc["suspension"], ac["suspension"],
                    avail_as_of, avail_stale, Jsonb(quality),
                )
                placeholders = ",".join(["%s"] * len(params))
                sql = f"""
                    INSERT INTO prematch_feature_snapshots(
                        event_id,snapshot_hour,league_slug,league_name,match_date,home_team,away_team,
                        home_days_rest,away_days_rest,home_matches_last_7d,away_matches_last_7d,
                        home_matches_last_14d,away_matches_last_14d,
                        lineup_entries,roster_entries,prematch_snapshot_age_hours,
                        oddspapi_fixture_id,odds_snapshot_age_hours,odds_price_rows,has_ou25,has_btts,has_corner85,odds_movement,
                        home_recent_absence_players,away_recent_absence_players,
                        home_recent_injury_players,away_recent_injury_players,
                        home_recent_suspension_players,away_recent_suspension_players,
                        availability_as_of,availability_stale,data_quality,built_at
                    ) VALUES({placeholders},NOW())
                    ON CONFLICT(event_id,snapshot_hour) DO UPDATE SET
                        home_days_rest=EXCLUDED.home_days_rest,away_days_rest=EXCLUDED.away_days_rest,
                        home_matches_last_7d=EXCLUDED.home_matches_last_7d,away_matches_last_7d=EXCLUDED.away_matches_last_7d,
                        home_matches_last_14d=EXCLUDED.home_matches_last_14d,away_matches_last_14d=EXCLUDED.away_matches_last_14d,
                        lineup_entries=EXCLUDED.lineup_entries,roster_entries=EXCLUDED.roster_entries,
                        prematch_snapshot_age_hours=EXCLUDED.prematch_snapshot_age_hours,
                        oddspapi_fixture_id=EXCLUDED.oddspapi_fixture_id,odds_snapshot_age_hours=EXCLUDED.odds_snapshot_age_hours,
                        odds_price_rows=EXCLUDED.odds_price_rows,has_ou25=EXCLUDED.has_ou25,has_btts=EXCLUDED.has_btts,has_corner85=EXCLUDED.has_corner85,
                        odds_movement=EXCLUDED.odds_movement,
                        home_recent_absence_players=EXCLUDED.home_recent_absence_players,away_recent_absence_players=EXCLUDED.away_recent_absence_players,
                        home_recent_injury_players=EXCLUDED.home_recent_injury_players,away_recent_injury_players=EXCLUDED.away_recent_injury_players,
                        home_recent_suspension_players=EXCLUDED.home_recent_suspension_players,away_recent_suspension_players=EXCLUDED.away_recent_suspension_players,
                        availability_as_of=EXCLUDED.availability_as_of,availability_stale=EXCLUDED.availability_stale,
                        data_quality=EXCLUDED.data_quality,built_at=NOW()
                """
                self.conn.execute(sql, params)

            self.conn.execute(
                "UPDATE prematch_context_runs SET finished_at=NOW(),status='success',upcoming_matches=%s,odds_matched=%s,availability_matched=%s,message='ok' WHERE id=%s",
                (len(matches), odds_matched, availability_matched, run_id),
            )
            result = {"status":"success","upcoming":len(matches),"odds_matched":odds_matched,"availability_matched":availability_matched}
            return result
        except Exception as exc:
            self.conn.execute("UPDATE prematch_context_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s", (str(exc)[:1000], run_id))
            raise


def run_build(database_url: Optional[str]=None) -> Dict[str, Any]:
    builder=SafePrematchContextBuilder(database_url)
    try:
        result=builder.build();print("PREMATCH_CONTEXT_RESULT",json.dumps(result,separators=(",",":")));return result
    finally:builder.close()

if __name__=="__main__":print(json.dumps(run_build(),ensure_ascii=False,indent=2))
