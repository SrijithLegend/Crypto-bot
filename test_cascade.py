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


def _bar(px):
    return dict(high=px, low=px, close=px)


def test_heatmap_projects_longs_below_and_shorts_above():
    longs, shorts = C.build_heatmap([1000.0, 2000.0], [_bar(100_000)] * 2)
    assert longs and shorts
    assert max(longs) < 100_000 < min(shorts)
    assert abs(sum(longs.values()) - 500) < 1e-6    # d=1000 * long_share 0.5


def test_heatmap_deletes_swept_and_shrinks_on_closes():
    flat = [_bar(100_000)] * 3
    longs, _ = C.build_heatmap([1000.0, 2000.0, 1000.0], flat)   # OI halves -> notional halves
    assert abs(sum(longs.values()) - 250) < 1e-6
    swept = [_bar(100_000), _bar(100_000), dict(high=100_000, low=50_000, close=100_000)]
    longs, _ = C.build_heatmap([1000.0, 2000.0, 2000.0], swept)  # crash bar clears all long liqs
    assert longs == {}


def test_cluster_signal_dense_near_zone():
    px = 100_000
    longs = {99_000: 500.0, 91_000: 10.0}                         # dense inside 2%, thin in 2-10%
    s = C.cluster_signal(longs, {}, px, oi_total=10_000)
    assert s["long_cluster_below"] and not s["short_cluster_above"]
    s = C.cluster_signal(longs, {}, px, oi_total=1e9)             # below 1% OI-share floor
    assert not s["long_cluster_below"]


def _oi(spike=False, price_ret=0.0):
    return dict(spike=spike, price_ret=price_ret)


def test_combine_mirror_logic():
    f_long, f_none = dict(crowded="long"), dict(crowded=None)
    c_down = dict(long_cluster_below=True, short_cluster_above=False)
    r = C.combine(_oi(True, 0.001), f_long, c_down)
    assert r == {"DOWN_CASCADE_RISK": 3, "UP_SQUEEZE_RISK": 1}    # spike counts for both flat-band sides
    r = C.combine(_oi(True, -0.01), f_long, c_down)               # price fell past band: OI leg fails for longs
    assert r["DOWN_CASCADE_RISK"] == 2 and r["UP_SQUEEZE_RISK"] == 1
    assert C.combine(_oi(), f_none, dict(long_cluster_below=False, short_cluster_above=False)) == \
        {"DOWN_CASCADE_RISK": 0, "UP_SQUEEZE_RISK": 0}


def test_log_row_writes_header_once(tmp_path):
    p = str(tmp_path / "l.csv")
    C.log_row({"a": 1, "b": 2}, p)
    C.log_row({"a": 3, "b": 4}, p)
    assert open(p).read().split() == ["a,b", "1,2", "3,4"]
