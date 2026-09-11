from ranking_challenger_stack_policy import (
    FEATURE_CORNER,
    FEATURE_LINEUP,
    FEATURE_MISSING,
    MODE_V1,
    corner_specific_factor,
    features_from_mode,
    live_factors,
    missing_player_factor,
    mode_from_features,
)


def test_mode_round_trip_preserves_fixed_order():
    mode = mode_from_features([FEATURE_CORNER, FEATURE_LINEUP, FEATURE_MISSING])
    assert features_from_mode(mode) == (FEATURE_LINEUP, FEATURE_MISSING, FEATURE_CORNER)
    assert mode_from_features([]) == MODE_V1


def test_missing_player_factor_is_bounded_and_fail_closed():
    factor, available = missing_player_factor(None, {"injury_impact": 0.2})
    assert factor == 1.0 and available is False

    factor, available = missing_player_factor(
        {"injury_impact": 0.32}, {"injury_impact": 0.04}
    )
    assert available is True
    assert factor == 0.95

    factor, available = missing_player_factor(
        {"injury_impact": 0.01}, {"injury_impact": 0.02}
    )
    assert available is True
    assert factor == 1.005


def test_expected_xi_drop_can_supply_missing_player_signal():
    home = {
        "injury_impact": None,
        "top11_strength": 0.80,
        "expected_xi_strength": 0.64,
        "source_meta": {"expected_xi_data_informed": True},
    }
    away = {
        "injury_impact": 0.02,
        "top11_strength": 0.75,
        "expected_xi_strength": 0.74,
        "source_meta": {"expected_xi_data_informed": True},
    }
    factor, available = missing_player_factor(home, away)
    assert available is True
    assert factor < 1.0


def test_corner_factor_respects_selection_direction():
    over_high, ok1 = corner_specific_factor("corners_over_9_5", True, 1.30)
    under_high, ok2 = corner_specific_factor("corners_over_9_5", False, 1.30)
    over_low, ok3 = corner_specific_factor("corners_over_9_5", True, 0.70)
    assert ok1 and ok2 and ok3
    assert over_high > 1.0
    assert under_high < 1.0
    assert over_low < 1.0
    assert corner_specific_factor("btts", True, 1.30) == (1.0, False)


def test_live_stack_combines_only_enabled_features():
    env = {
        "lineup": {
            "home": {"starter_continuity": 0.90, "injury_impact": 0.02},
            "away": {"starter_continuity": 0.86, "injury_impact": 0.02},
        },
        "pressure": {"corner_pressure_signal": 1.25},
    }
    mode = mode_from_features([FEATURE_LINEUP, FEATURE_MISSING, FEATURE_CORNER])
    result = live_factors(env, "corners_over_9_5", True, mode)
    assert result["lineup_available"] is True
    assert result["missing_player_available"] is True
    assert result["corner_specific_available"] is True
    expected = (
        result["lineup_factor"]
        * result["missing_player_factor"]
        * result["corner_specific_factor"]
    )
    assert abs(result["combined_factor"] - expected) < 1e-12


def test_v1_mode_is_neutral():
    result = live_factors({}, "corners_over_8_5", True, MODE_V1)
    assert result["combined_factor"] == 1.0
    assert result["lineup_available"] is False
    assert result["missing_player_available"] is False
    assert result["corner_specific_available"] is False
