from incremental_feature_policy import (
    elo_factor,
    factor_from_live_environment,
    lineup_stability_factor,
    pace_factor,
    venue_factor,
    xg_regression_factor,
)


def test_xg_regression_direction():
    h = {"matches": 6, "attack_xg_minus_goals": .5, "defence_goals_minus_xg": -.1}
    a = {"matches": 6, "attack_xg_minus_goals": .4, "defence_goals_minus_xg": -.1}
    over, ok = xg_regression_factor(h, a, "over_2_5", "2.5 ÜST")
    under, ok2 = xg_regression_factor(h, a, "over_2_5", "2.5 ALT")
    assert ok and ok2 and over > 1.0 and under < 1.0


def test_pace_direction_and_bounds():
    over, ok = pace_factor(90, "over_2_5", "2.5 ÜST")
    under, ok2 = pace_factor(90, "over_2_5", "2.5 ALT")
    assert ok and ok2
    assert 1.0 < over <= 1.04
    assert .96 <= under < 1.0


def test_elo_and_venue_only_apply_to_btts():
    close, ok = elo_factor(20, "btts", "KG VAR")
    far, ok2 = elo_factor(380, "btts", "KG VAR")
    assert ok and ok2 and close > far
    noop, available = elo_factor(20, "over_2_5", "2.5 ÜST")
    assert noop == 1.0 and available is False

    close_v, vok = venue_factor(1.02, 1.00, "btts", "KG VAR")
    far_v, vok2 = venue_factor(1.45, .80, "btts", "KG VAR")
    assert vok and vok2 and close_v > far_v


def test_lineup_stability_penalizes_uncertainty():
    low, ok = lineup_stability_factor(.50, .80)
    high, ok2 = lineup_stability_factor(.90, .88)
    assert ok and ok2 and low < 1.0 < high


def test_live_environment_parser_uses_same_formula():
    env = {
        "xg_regression": {
            "home": {"matches": 6, "attack_xg_minus_goals": .4, "defence_goals_minus_xg": 0.0},
            "away": {"matches": 6, "attack_xg_minus_goals": .4, "defence_goals_minus_xg": 0.0},
        },
        "elo": {"gap": 25},
        "pace": {"score": 80},
        "venue": {"home_index": 1.05, "away_index": 1.00},
        "lineup": {
            "home": {"starter_continuity": .90},
            "away": {"starter_continuity": .88},
            "home_stability": .10,
            "away_stability": .10,
        },
    }
    fx, _ = factor_from_live_environment("xg_regression", env, "over_2_5", "2.5 ÜST")
    direct, _ = xg_regression_factor(env["xg_regression"]["home"], env["xg_regression"]["away"], "over_2_5", "2.5 ÜST")
    assert fx == direct
    fl, _ = factor_from_live_environment("lineup_stability", env, "btts", "KG VAR")
    assert fl > 1.0
