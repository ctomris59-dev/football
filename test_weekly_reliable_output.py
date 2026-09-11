from types import SimpleNamespace

from weekly_trusted_predictions import _fixture_market_specs, _selected_market_probability, _public
from turkey_two_sided_odds import _side
from schedule_context import schedule_rank_factor


def _row(confidence):
    return {
        'event_id':'x','match_date':None,'league':'L','home':'A','away':'B',
        'market':'over_2_5','selection':'2.5 ÜST','confidence':confidence,
        'ranking_score':confidence,'schedule_rank_factor':1.0,
        'price':{'tr_price':1.70,'tr_opening_price':1.72,'tr_source':'iddaa_official'},
        'international':{},'international_selected_probability':None,
        'market_check':'reference_unavailable','early_context':{}
    }


def test_two_sided_market_classification():
    assert _side('over_2_5', 'Üst') == 'yes'
    assert _side('over_2_5', 'Alt') == 'no'
    assert _side('btts', 'Var') == 'yes'
    assert _side('btts', 'Yok') == 'no'


def test_selected_market_probability_inverts_for_no_side():
    assert abs(_selected_market_probability({'reference_p_yes': .62}, False) - .38) < 1e-12


def test_strict_high_label_is_not_faked():
    low = _public(_row(.66))
    assert low['strict_high_confidence'] is False
    assert low['confidence_tier'] == 'Haftanın En Güvenilirleri'
    high = _public(_row(.71))
    assert high['strict_high_confidence'] is True
    assert high['confidence_tier'] == 'Yüksek Güven'


def test_schedule_rank_factor_only_penalizes_short_rest():
    assert schedule_rank_factor(6.0) == 1.0
    assert schedule_rank_factor(5.0) == 0.99
    assert schedule_rank_factor(4.0) == 0.97
    assert schedule_rank_factor(3.0) == 0.93
    assert schedule_rank_factor(2.0) == 0.0


def test_pending_intervening_match_is_not_finalizable():
    assert schedule_rank_factor(4.0, pending_pre_fixture_match=True) == 0.0


def test_fresh_preview_evaluates_all_supported_corner_lines():
    pred = SimpleNamespace(p_over_2_5=.61, p_btts=.58, lambda_total_corners=10.0)
    specs = _fixture_market_specs(pred, {'p_over_2_5': .61, 'p_btts': .58})
    corner_specs = [s for s in specs if s['corner_line'] is not None]
    assert [s['market'] for s in corner_specs] == [
        'corners_over_7_5',
        'corners_over_8_5',
        'corners_over_9_5',
        'corners_over_10_5',
    ]
    assert [s['corner_line'] for s in corner_specs] == [7.5, 8.5, 9.5, 10.5]
    assert all(0.0 < s['p_yes'] < 1.0 for s in corner_specs)
    assert all(s['confidence_semantics'] == 'selected_side_raw_v1_probability_from_same_frozen_corner_lambda' for s in corner_specs)
