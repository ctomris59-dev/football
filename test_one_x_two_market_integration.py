from one_x_two_market_audit import _fold_gate
from one_x_two_market_reference import no_vig_three, outcome_label
from oddspapi_allbooks_importer import market_kind
from turkey_iddaa_odds_collector import classify_market
from turkey_two_sided_odds import _result_selection
from turkey_value_workflow import valid_market_price, valid_price


def test_provider_market_classification_is_three_way_only():
    assert market_kind("Match Result", None) == "match_result"
    assert market_kind("1X2", None) == "match_result"
    assert market_kind("Both Teams To Score", None) == "btts"
    assert market_kind("Over Under Full Time", 2.5) == "over_2_5"


def test_three_way_no_vig_normalizes_all_outcomes():
    result = no_vig_three(2.0, 3.5, 4.0)
    assert result is not None
    assert abs(result["p1"] + result["p0"] + result["p2"] - 1.0) < 1e-12
    assert result["p1"] > result["p0"] > result["p2"]
    assert 0.95 <= result["overround"] <= 1.40


def test_three_way_no_vig_rejects_malformed_prices():
    assert no_vig_three(1.01, 1.01, 1.01) is None
    assert no_vig_three(2.0, 3.0, 100.0) is None


def test_international_outcome_labels_support_symbols_and_team_names():
    assert outcome_label("Home", "Arsenal", "Chelsea") == "1"
    assert outcome_label("Draw", "Arsenal", "Chelsea") == "0"
    assert outcome_label("Away", "Arsenal", "Chelsea") == "2"
    assert outcome_label("Arsenal", "Arsenal", "Chelsea") == "1"
    assert outcome_label("Chelsea", "Arsenal", "Chelsea") == "2"


def test_iddaa_classifier_recognizes_match_result_without_changing_legacy_targets():
    assert classify_market("Maç Sonucu", {}) == "match_result"
    assert classify_market("Match Result", {}) == "match_result"
    assert classify_market("İlk Yarı Maç Sonucu", {}) is None


def test_iddaa_result_outcomes_are_normalized():
    assert _result_selection("1", "Beşiktaş", "Fenerbahçe") == "1"
    assert _result_selection("X", "Beşiktaş", "Fenerbahçe") == "0"
    assert _result_selection("2", "Beşiktaş", "Fenerbahçe") == "2"
    assert _result_selection("Beşiktaş", "Beşiktaş", "Fenerbahçe") == "1"
    assert _result_selection("Fenerbahçe", "Beşiktaş", "Fenerbahçe") == "2"


def test_1x2_has_wider_safe_price_bound_without_relaxing_existing_markets():
    assert not valid_price(8.0)
    assert valid_market_price(8.0, "match_result")
    assert not valid_market_price(25.0, "match_result")


def test_probability_only_market_gate_accepts_known_quality_shape():
    good = {
        "matches": 1750,
        "top1_accuracy": 0.51,
        "multiclass_brier": 0.617,
        "log_loss": 1.03,
        "ece_top_probability": 0.068,
    }
    assert _fold_gate("2526", good) == []
    bad = dict(good)
    bad["top1_accuracy"] = 0.45
    assert "fold_2526_top1_accuracy_low" in _fold_gate("2526", bad)
