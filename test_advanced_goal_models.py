import math

from advanced_goal_models import (
    MODE_DC,
    MODE_OPP,
    MODE_V1,
    OPPONENT_BLEND_WEIGHT,
    apply_mode,
    dixon_coles_probabilities,
    fit_dc_rho,
    fit_opponent_strengths,
    opponent_adjusted_lambdas,
)
from model_engine_v1 import Prediction


def _pred():
    return Prediction(
        p_over_2_5=0.55,
        p_btts=0.52,
        p_corners_over_8_5=0.60,
        lambda_home_goals=1.55,
        lambda_away_goals=1.15,
        lambda_total_corners=9.8,
        home_sample=18,
        away_sample=18,
        data_quality=1.0,
    )


def _history(n=80):
    rows = []
    for i in range(n):
        if i % 4 == 0:
            home, away, hg, ag = 'Strong', 'Weak', 3, 0
        elif i % 4 == 1:
            home, away, hg, ag = 'Weak', 'Strong', 0, 2
        elif i % 4 == 2:
            home, away, hg, ag = 'Mid', 'Strong', 1, 2
        else:
            home, away, hg, ag = 'Strong', 'Mid', 2, 1
        rows.append({'home_team':home,'away_team':away,'home_goals':hg,'away_goals':ag})
    return rows


def test_dc_rho_zero_matches_independent_goal_markets_close():
    p = dixon_coles_probabilities(1.55, 1.15, 0.0, max_goals=12)
    independent_btts = (1-math.exp(-1.55)) * (1-math.exp(-1.15))
    assert abs(p['p_btts'] - independent_btts) < 1e-4
    assert 0 < p['p_over_2_5'] < 1
    assert abs(p['p_home'] + p['p_draw'] + p['p_away'] - 1.0) < 1e-9


def test_dc_fit_is_bounded_and_past_data_only_function():
    rho, available, meta = fit_dc_rho(_history())
    assert -0.20 <= rho <= 0.20
    assert available is True
    assert meta['fit_matches'] == 80


def test_opponent_adjusted_strengths_identify_strong_and_weak_teams():
    model = fit_opponent_strengths(_history())
    strong_h, strong_a, ok = opponent_adjusted_lambdas(model, 'Strong', 'Weak')
    weak_h, weak_a, ok2 = opponent_adjusted_lambdas(model, 'Weak', 'Strong')
    assert ok and ok2
    assert strong_h > weak_h
    assert strong_a < weak_a


def test_modes_preserve_corner_probability_and_v1_fail_closed():
    pred = _pred(); history = _history()
    base = apply_mode(pred, history, 'Strong', 'Weak', MODE_V1)
    dc = apply_mode(pred, history, 'Strong', 'Weak', MODE_DC)
    opp = apply_mode(pred, history, 'Strong', 'Weak', MODE_OPP)
    assert base['p_over_2_5'] == pred.p_over_2_5
    assert dc['p_corners_over_8_5'] == pred.p_corners_over_8_5
    assert opp['p_corners_over_8_5'] == pred.p_corners_over_8_5
    assert 0 < OPPONENT_BLEND_WEIGHT < 0.5
