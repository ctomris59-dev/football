import math

from one_x_two_engine import (
    actual_outcome,
    coupon_selection,
    probabilities_from_lambdas,
    selection_contains,
)


def test_probabilities_sum_to_one():
    pred = probabilities_from_lambdas(1.65, 1.10)
    assert abs(pred.p1 + pred.px + pred.p2 - 1.0) < 1e-12
    assert all(0.0 <= x <= 1.0 for x in (pred.p1, pred.px, pred.p2))


def test_equal_lambdas_are_symmetric():
    pred = probabilities_from_lambdas(1.35, 1.35)
    assert abs(pred.p1 - pred.p2) < 1e-12
    assert pred.px > 0.20


def test_stronger_home_side_is_favoured():
    pred = probabilities_from_lambdas(2.40, 0.65)
    assert pred.p1 > pred.px > pred.p2
    coupon = coupon_selection(pred)
    assert coupon["selection"] == "1"
    assert coupon["tier"] == "single"


def test_stronger_away_side_can_be_single():
    pred = probabilities_from_lambdas(0.55, 2.25)
    coupon = coupon_selection(pred)
    assert coupon["selection"] == "2"
    assert coupon["tier"] == "single"


def test_balanced_match_is_not_forced_single():
    pred = probabilities_from_lambdas(1.30, 1.20)
    coupon = coupon_selection(pred)
    assert coupon["selection_count"] >= 2
    assert coupon["tier"] in {"double", "triple"}


def test_outcome_and_selection_membership():
    assert actual_outcome(2, 1) == "1"
    assert actual_outcome(1, 1) == "0"
    assert actual_outcome(0, 2) == "2"
    assert selection_contains("02", "0")
    assert selection_contains("02", "2")
    assert not selection_contains("02", "1")
