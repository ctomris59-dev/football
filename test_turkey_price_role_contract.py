from pathlib import Path

from thursday_decision_engine import market_metrics


PRODUCTION_MARKET_FILES = (
    "thursday_decision_engine.py",
    "thursday_decision_engine_v2.py",
    "thursday_decision_engine_v3.py",
    "weekly_trusted_predictions.py",
    "weekly_trusted_predictions_v2.py",
)

FORBIDDEN_CROSS_MARKET_TOKENS = (
    "international_edge_vs_tr",
    "international_ev_vs_tr",
    "INTERNATIONAL_MIN_TR_EDGE",
    "INTERNATIONAL_MIN_TR_EV",
)


def test_value_ev_uses_model_and_turkey_executable_price_only():
    m = market_metrics(model_p=0.60, tr_price=1.50, international_p=0.58)
    assert round(m["model_ev_vs_tr"], 6) == -0.10
    assert round(m["model_edge_vs_tr"], 6) == round(0.60 - 1 / 1.50, 6)
    assert round(m["model_market_gap"], 6) == 0.02
    assert "international_edge_vs_tr" not in m
    assert "international_ev_vs_tr" not in m


def test_foreign_probability_cannot_change_turkey_value_math():
    low_ref = market_metrics(model_p=0.60, tr_price=1.50, international_p=0.40)
    high_ref = market_metrics(model_p=0.60, tr_price=1.50, international_p=0.80)
    for key in ("tr_implied_probability", "model_edge_vs_tr", "model_ev_vs_tr"):
        assert low_ref[key] == high_ref[key]
    assert low_ref["model_market_gap"] != high_ref["model_market_gap"]


def test_no_production_market_family_crosses_international_probability_with_turkey_price():
    for filename in PRODUCTION_MARKET_FILES:
        src = Path(filename).read_text(encoding="utf-8")
        for token in FORBIDDEN_CROSS_MARKET_TOKENS:
            assert token not in src, f"{filename} still contains forbidden cross-market token {token}"


def test_weekly_reliability_does_not_penalize_low_turkey_odds_vs_foreign_odds():
    src = Path("weekly_trusted_predictions_v2.py").read_text(encoding="utf-8")
    assert 'price = _price_payload(conn, str(eid), "match_result", selection)' in src
    assert 'abs(float(confidence) - ref_selected) > INTERNATIONAL_MAX_MODEL_DIVERGENCE' in src
    assert 'ranking_score = float(confidence)' in src
    ranking_block = src[src.index('candidate_key = ('):src.index('current_key = (')]
    assert 'tr_price' not in ranking_block
    assert 'international_fair_odds' not in ranking_block
    assert 'median_price' not in ranking_block


def test_international_market_is_probability_sanity_only():
    core = Path("thursday_decision_engine.py").read_text(encoding="utf-8")
    v2 = Path("thursday_decision_engine_v2.py").read_text(encoding="utf-8")
    v3 = Path("thursday_decision_engine_v3.py").read_text(encoding="utf-8")
    assert "official Turkish İddaa price = the only executable price" in core
    assert '"model_ev_vs_tr": model_p * tr_price - 1.0' in core
    assert '"model_market_gap": model_p - international_p' in core
    assert '"international_vs_turkey_price_comparison": "disabled_all_markets"' in v2
    assert '"international_vs_turkey_price_comparison": "disabled_all_markets"' in v3


def test_1x2_reference_is_three_way_novig_probability():
    src = Path("one_x_two_market_reference.py").read_text(encoding="utf-8")
    assert 'raw = {k: 1.0 / p for k, p in prices.items()}' in src
    assert 'probs = {k: raw[k] / overround for k in ("1", "0", "2")}' in src
    weekly = Path("weekly_trusted_predictions_v2.py").read_text(encoding="utf-8")
    assert 'tr_price >=' not in weekly
    assert 'tr_price <=' not in weekly
