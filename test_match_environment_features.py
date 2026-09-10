from match_environment_features import (
    exact_scoreline_distribution,
    fatigue_asymmetry,
    lineup_stability,
    pace_band,
    pace_proxy_score,
    projected_goal_environment,
    venue_strength_index,
    xg_regression_profile,
)


def test_projected_goal_environment_is_interpretable():
    g = projected_goal_environment(1.7, 1.2)
    assert g["projected_total_goals"] == 2.9
    assert 0 <= g["goal_balance"] <= 1


def test_neutral_pressure_maps_to_neutral_pace():
    score = pace_proxy_score(1.0, 1.0)
    assert score == 50.0
    assert pace_band(score) == "neutral"
    assert pace_proxy_score(1.45, 1.45) == 100.0
    assert pace_proxy_score(0.55, 0.55) == 0.0


def test_xg_regression_signs_are_descriptive_not_predictions():
    p = xg_regression_profile(1.2, 1.7, 1.5, 1.1, matches=6)
    assert p["attack_xg_minus_goals"] == 0.5
    assert p["defence_goals_minus_xg"] == 0.4
    assert p["matches"] == 6


def test_venue_index_centres_near_one_for_league_average_team():
    idx = venue_strength_index(1.5, 1.2, 5.0, 4.0, league_goals_for=1.5, league_goals_against=1.2)
    assert idx is not None
    assert 0.95 <= idx <= 1.1


def test_lineup_stability_and_fatigue_asymmetry():
    stable = lineup_stability(0.80, 0.82, 0.05, False)
    unstable = lineup_stability(0.50, 0.55, 0.30, True)
    assert stable is not None and unstable is not None and stable > unstable
    assert fatigue_asymmetry(4.0, 6.5) == -2.5


def test_exact_poisson_grid_is_normalized_and_matches_core_formula():
    d = exact_scoreline_distribution(1.6, 1.1, max_goals=10, top_n=5)
    assert d["method"] == "exact_independent_poisson"
    assert d["captured_mass"] > 0.9999
    assert abs(d["home_win"] + d["draw"] + d["away_win"] - 1.0) < 2e-6
    assert 0 < d["btts"] < 1
    assert 0 < d["over_2_5"] < 1
    assert len(d["top_scorelines"]) == 5
