from thursday_opening_watch import _decorate_ranked_picks, _rank_tier
from thursday_page import render_page


def _pick(i: int):
    return {
        "event_id": f"e{i}",
        "market": "btts",
        "selection": "KG VAR",
        "home": f"H{i}",
        "away": f"A{i}",
        "confidence": 0.70 - i * 0.01,
    }


def test_rank_tiers_are_position_based():
    assert [_rank_tier(i) for i in range(1, 11)] == [
        "core4", "core4", "core4", "core4",
        "strong", "strong", "strong", "strong",
        "other_reliable", "other_reliable",
    ]


def test_value_badge_metadata_never_reorders_or_replaces_top10():
    picks = [_pick(i) for i in range(1, 11)]
    value = [{
        **picks[5],
        "model_ev_vs_tr": 0.045,
        "model_edge_vs_tr": 0.03,
    }]
    out = _decorate_ranked_picks(picks, value)
    assert [p["event_id"] for p in out] == [p["event_id"] for p in picks]
    assert [p["rank"] for p in out] == list(range(1, 11))
    assert [p["list_tier"] for p in out[:4]] == ["core4"] * 4
    assert [p["list_tier"] for p in out[4:8]] == ["strong"] * 4
    assert [p["list_tier"] for p in out[8:]] == ["other_reliable"] * 2
    assert sum(bool(p["is_value"]) for p in out) == 1
    assert out[5]["is_value"] is True
    assert out[5]["model_ev_vs_tr"] == 0.045
    assert out[0]["is_value"] is False


def test_value_match_requires_same_fixture_market_and_selection():
    picks = [_pick(1)]
    wrong_selection = [{**picks[0], "selection": "KG YOK", "model_ev_vs_tr": 0.10}]
    out = _decorate_ranked_picks(picks, wrong_selection)
    assert out[0]["is_value"] is False
    assert "model_ev_vs_tr" not in out[0]


def test_dashboard_has_one_ranked_structure_and_no_separate_value_list():
    html = render_page()
    assert "🛡️ Core 4" in html
    assert "⭐ Güçlü Seçimler" in html
    assert "🎯 Diğer Güvenilir Seçimler" in html
    assert "💰 VALUE" in html
    assert 'id="valueList"' not in html
    assert "value yalnız rozet" in html.lower()
