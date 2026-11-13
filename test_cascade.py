import cascade as C


def test_zscore():
    assert C.zscore(5, [1, 1, 1]) == 0.0            # zero std -> 0, no div-by-zero
    assert C.zscore(3, [1, 2, 3]) > 1


def test_oi_spike_flags_only_a_jump():
    flat = [100.0 + (i % 5) * 0.1 for i in range(60)]
    bars = [dict(high=1, low=1, close=100.0)] * 60
    assert not C.oi_signal(flat, bars)["spike"]
    assert C.oi_signal(flat[:-1] + [130.0], bars)["spike"]


def test_funding_needs_abs_floor_and_z():
    hist = [0.0001] * 50 + [0.00011] * 50
    assert C.funding_signal(0.0005, hist)["crowded"] == "long"
    assert C.funding_signal(-0.0005, hist)["crowded"] == "short"
    assert C.funding_signal(0.00012, hist)["crowded"] is None   # high z but under abs floor
