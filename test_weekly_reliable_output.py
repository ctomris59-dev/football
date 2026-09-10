from weekly_trusted_predictions import _selected_market_probability, _public
from turkey_two_sided_odds import _side


def _row(confidence):
    return {
        'event_id':'x','match_date':None,'league':'L','home':'A','away':'B',
        'market':'over_2_5','selection':'2.5 ÜST','confidence':confidence,
        'ranking_score':confidence,'price':{'tr_price':1.70,'tr_opening_price':1.72,'tr_source':'iddaa_official'},
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
