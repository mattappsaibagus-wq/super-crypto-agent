from supercrypto.agents.advisor import ticker_conflict


def test_two_ids_conflict():
    assert "hood-inu" in ticker_conflict({"hood-inu", "hi-dollar"}, [])


def test_price_gap_conflict():
    assert ticker_conflict(set(), [0.024, 3.841e-05]).startswith("prices")


def test_normal_volatility_is_fine():
    assert ticker_conflict({"hi-dollar"}, [1.0, 1.4, 2.5]) == ""
    assert ticker_conflict(set(), []) == ""
