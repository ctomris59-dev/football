from pathlib import Path

from thursday_decision_engine import market_metrics


def test_value_ev_uses_turkey_executable_price_only():
    # Foreign market contributes a fair probability reference, not an executable price.
    m = market_metrics(model_p=0.60, tr_price=1.50, international_p=0.58)
    assert round(m["model_ev_vs_tr"], 6) == -0.10
    assert round(m["international_ev_vs_tr"], 6) == -0.13
    assert round(m["model_market_gap"], 6) == 0.02


def test_weekly_reliability_does_not_penalize_low_turkey_odds_vs_foreign_odds():
    src = Path("weekly_trusted_predictions_v2.py").read_text(encoding="utf-8")
    # Reliability ranking is probability/data/schedule based. Turkish price is required
    # to ensure the market is playable, but raw price parity with foreign books must
    # never enter the ranking key or contradiction gate.
    assert 'price = _price_payload(conn, str(eid), "match_result", selection)' in src
    assert 'abs(float(confidence) - ref_selected) > INTERNATIONAL_MAX_MODEL_DIVERGENCE' in src
    assert 'ranking_score = float(confidence)' in src
    ranking_block = src[src.index('candidate_key = ('):src.index('current_key = (')]
    assert 'tr_price' not in ranking_block
    assert 'international_fair_odds' not in ranking_block
    assert 'median_price' not in ranking_block


def test_international_market_is_probability_reference_not_price_parity_gate():
    src = Path("thursday_decision_engine.py").read_text(encoding="utf-8")
    assert "official Turkish İddaa price = the only executable price" in src
    assert '"model_ev_vs_tr": model_p * tr_price - 1.0' in src
    assert '"international_ev_vs_tr": international_p * tr_price - 1.0' in src
    assert '"model_market_gap": model_p - international_p' in src


def test_1x2_reference_is_three_way_novig_probability():
    src = Path("one_x_two_market_reference.py").read_text(encoding="utf-8")
    assert 'raw = {k: 1.0 / p for k, p in prices.items()}' in src
    assert 'probs = {k: raw[k] / overround for k in ("1", "0", "2")}' in src
    # Raw international odds can be stored for diagnostics, but they are not a
    # requirement that the Turkish payout must equal or beat them.
    weekly = Path("weekly_trusted_predictions_v2.py").read_text(encoding="utf-8")
    assert 'tr_price >=' not in weekly
    assert 'tr_price <=' not in weekly
