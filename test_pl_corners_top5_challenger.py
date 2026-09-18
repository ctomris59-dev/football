import unittest

from pl_corners_top5_challenger import activation_gate, select_policy_rows


class PLCornersTop5ChallengerTests(unittest.TestCase):
    def test_statistically_stable_but_missing_market_and_clv_stays_blocked(self):
        overall = {"n": 259, "hit_rate": 0.7066, "calibration_gap": 0.042}
        folds = {
            "2425": {"n": 153, "hit_rate": 0.7059},
            "2526": {"n": 106, "hit_rate": 0.7075},
        }
        gate = activation_gate(overall, folds, market_benchmark_available=False, clv_available=False)
        self.assertTrue(gate["statistical_stability_pass"])
        self.assertFalse(gate["production_activation_pass"])
        self.assertEqual(gate["decision"], "BLOCKED")
        self.assertIn("missing_corners_market_benchmark", gate["blockers"])
        self.assertIn("missing_corners_clv", gate["blockers"])

    def test_complete_evidence_can_pass(self):
        overall = {"n": 259, "hit_rate": 0.7066, "calibration_gap": 0.042}
        folds = {
            "2425": {"n": 153, "hit_rate": 0.7059},
            "2526": {"n": 106, "hit_rate": 0.7075},
        }
        gate = activation_gate(
            overall, folds,
            market_benchmark_available=True,
            clv_available=True,
            market_edge_pass=True,
            clv_not_degraded=True,
        )
        self.assertTrue(gate["production_activation_pass"])
        self.assertEqual(gate["decision"], "PASS")

    def test_small_or_unstable_fold_blocks(self):
        overall = {"n": 259, "hit_rate": 0.72, "calibration_gap": 0.01}
        folds = {
            "2425": {"n": 40, "hit_rate": 0.75},
            "2526": {"n": 219, "hit_rate": 0.67},
        }
        gate = activation_gate(
            overall, folds,
            market_benchmark_available=True,
            clv_available=True,
            market_edge_pass=True,
            clv_not_degraded=True,
        )
        self.assertFalse(gate["statistical_stability_pass"])
        self.assertFalse(gate["production_activation_pass"])
        self.assertIn("each_fold_sample", gate["blockers"])
        self.assertIn("each_fold_hit_rate", gate["blockers"])

    def test_policy_filters_only_pl_corners_after_weekly_top5(self):
        candidates = []
        specs = [
            (0.90, "Premier League", "corners_over_8_5"),
            (0.89, "La Liga", "corners_over_8_5"),
            (0.88, "Premier League", "over_2_5"),
            (0.87, "Premier League", "corners_over_8_5"),
            (0.86, "Bundesliga", "corners_over_8_5"),
            (0.85, "Premier League", "corners_over_8_5"),
        ]
        for rank, league, market in specs:
            candidates.append({
                "fold": "2425", "week": "2025-W01", "ranking": rank,
                "confidence": rank, "league": league, "market": market,
            })
        selected = select_policy_rows(candidates)
        self.assertEqual([r["ranking"] for r in selected], [0.90, 0.87])


if __name__ == "__main__":
    unittest.main()
