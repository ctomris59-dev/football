from lineup_stability_v2_policy import continuity_v2_from_lineups, lineup_stability_v2_factor


def test_continuity_requires_two_prior_lineups():
    assert continuity_v2_from_lineups([set(range(11))]) is None


def test_continuity_identical_lineups_is_one():
    xi = {f"p{i}" for i in range(11)}
    assert continuity_v2_from_lineups([xi, xi, xi]) == 1.0


def test_continuity_recency_weighting():
    a = {f"p{i}" for i in range(11)}
    b = {f"q{i}" for i in range(11)}
    c = set(b)
    # first overlap=0, latest overlap=1; weights 1 and 2 => 2/3
    assert abs(continuity_v2_from_lineups([a, b, c]) - (2/3)) < 1e-6


def test_factor_is_bounded_and_requires_both_teams():
    assert lineup_stability_v2_factor(None, 0.9) == (1.0, False)
    factor, ok = lineup_stability_v2_factor(0.3, 0.9)
    assert ok and factor == 0.97
    factor, ok = lineup_stability_v2_factor(0.9, 0.9)
    assert ok and factor == 1.01


def test_factor_mid_band_is_neutral():
    factor, ok = lineup_stability_v2_factor(0.78, 0.82)
    assert ok and factor == 1.0
