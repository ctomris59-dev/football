from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta, timezone

from research_change_control import activation_evidence_is_safe, guarded_activation_mode
from research_evaluation import confidence_band, shrink_rate, stratified_week_sample, week_block_bootstrap
from clv_backtest import reconstruct_clv, split_evaluation_scope


class ResearchEvaluationTests(unittest.TestCase):
    def test_confidence_bands(self):
        self.assertEqual(confidence_band(0.62), "0.60-0.65")
        self.assertEqual(confidence_band(0.76), "0.75-1.00")

    def test_small_bucket_is_pulled_to_parent(self):
        raw = 4 / 5
        shrunk = shrink_rate(4, 5, 0.60, 30)
        self.assertGreater(shrunk, 0.60)
        self.assertLess(shrunk, raw)

    def test_week_resampling_is_stratified_by_fold(self):
        rows = [
            {"fold": "2425", "week": "A", "x": 1},
            {"fold": "2425", "week": "B", "x": 2},
            {"fold": "2526", "week": "C", "x": 3},
            {"fold": "2526", "week": "D", "x": 4},
            {"fold": "2526", "week": "E", "x": 5},
        ]
        sample = stratified_week_sample(rows, rng=random.Random(7))
        counts = {fold: sum(r["fold"] == fold for r in sample) for fold in ("2425", "2526")}
        self.assertEqual(counts["2425"], 2)
        self.assertEqual(counts["2526"], 3)

    def test_bootstrap_returns_interval(self):
        rows = [
            {"fold": "2425", "week": "A", "x": 1.0},
            {"fold": "2425", "week": "B", "x": 2.0},
            {"fold": "2526", "week": "C", "x": 4.0},
            {"fold": "2526", "week": "D", "x": 5.0},
        ]
        ci = week_block_bootstrap(rows, lambda rs: {"mean": sum(r["x"] for r in rs) / len(rs)}, iterations=200, seed=11)
        self.assertIn("mean", ci)
        self.assertLess(ci["mean"][0], ci["mean"][1])

    def test_challenger_fails_closed_without_new_gate(self):
        legacy = {"variants": {"v1": {"hit_rate": 0.68}}}
        self.assertFalse(activation_evidence_is_safe(legacy))
        self.assertEqual(guarded_activation_mode("pressure", legacy), "v1_only")

    def test_challenger_can_pass_only_predeclared_gate(self):
        evidence = {
            "gate_passed": True,
            "validation_protocol": {
                "gate_version": "two-fold-week-block-v1",
                "test_seasons": ["2425", "2526"],
                "live_holdout_season": "2627",
                "holdout_excluded": True,
            },
        }
        self.assertTrue(activation_evidence_is_safe(evidence))
        self.assertEqual(guarded_activation_mode("elo", evidence), "elo")

    def test_clv_uses_decision_then_pre_kickoff_close(self):
        tz = timezone.utc
        kickoff = datetime(2026, 9, 20, 18, 0, tzinfo=tz)
        prediction = kickoff - timedelta(days=2)
        rows = [
            (prediction - timedelta(hours=1), "bookA", "Total Goals Over Under", 2.5, "Over", 2.00),
            (prediction - timedelta(hours=1), "bookA", "Total Goals Over Under", 2.5, "Under", 1.90),
            (prediction + timedelta(hours=1), "bookA", "Total Goals Over Under", 2.5, "Over", 1.90),
            (prediction + timedelta(hours=1), "bookA", "Total Goals Over Under", 2.5, "Under", 2.00),
            (kickoff - timedelta(minutes=20), "bookA", "Total Goals Over Under", 2.5, "Over", 1.80),
            (kickoff - timedelta(minutes=20), "bookA", "Total Goals Over Under", 2.5, "Under", 2.10),
            (kickoff + timedelta(minutes=5), "bookA", "Total Goals Over Under", 2.5, "Over", 1.50),
            (kickoff + timedelta(minutes=5), "bookA", "Total Goals Over Under", 2.5, "Under", 2.50),
        ]
        result = reconstruct_clv(rows, "over_2_5", True, prediction, kickoff)
        self.assertIsNotNone(result)
        self.assertGreater(result["probability_clv"], 0)
        self.assertGreater(result["log_price_clv"], 0)
        self.assertAlmostEqual(result["closing_price"], 1.80, places=6)

    def test_clv_live_holdout_is_never_pooled_into_validation(self):
        rows = [
            {"fold": "2425", "id": 1},
            {"fold": "2526", "id": 2},
            {"fold": "2627", "id": 3},
            {"fold": "2223", "id": 4},
        ]
        scopes = split_evaluation_scope(rows)
        self.assertEqual([r["id"] for r in scopes["historical_validation"]], [1, 2])
        self.assertEqual([r["id"] for r in scopes["live_holdout"]], [3])
        self.assertEqual([r["id"] for r in scopes["other"]], [4])


if __name__ == "__main__":
    unittest.main()
