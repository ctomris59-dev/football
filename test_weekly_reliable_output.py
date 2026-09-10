from weekly_trusted_predictions import _selected_market_probability, _public
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
