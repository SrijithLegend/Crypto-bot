import time

import numpy as np
import pandas as pd
import pytest

import scanner as S
from scanner import Config, find_breaks, pivots, scan, trend_state


# ------------------------------------------------------------------ helpers
def mk(close, spread=0.1):
    c = np.asarray(close, float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"time": np.arange(len(c)), "open": o,
                         "high": np.maximum(o, c) + spread,
                         "low": np.minimum(o, c) - spread, "close": c,
                         "volume": 1.0})


def zigzag(vertices, leg=12):
    out = [vertices[0]]
    for a, b in zip(vertices[:-1], vertices[1:]):
        out += list(np.linspace(a, b, leg + 1)[1:])
    return np.array(out)


def synth(n, seed=0):
    rng = np.random.default_rng(seed)
    drift = np.zeros(n)
    i = 0
    while i < n:
        L = int(rng.integers(100, 600))
        drift[i:i + L] = rng.choice([-1, 0, 1]) * 0.02
        i += L
    r = drift + rng.normal(0, 0.15, n)
    c = 100 * np.exp(np.cumsum(r) / 100 * 3) * 100
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.0007, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.0007, n)))
    return pd.DataFrame({"time": np.arange(n), "open": o, "high": h, "low": l,
                         "close": c, "volume": 1.0})


# ------------------------------------------------------------------ pivots
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_pivots_match_bruteforce(seed):
    df = synth(3000, seed)
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    L, R = 5, 4
    hi, lo = pivots(h, l, L, R)
    bh = [i for i in range(L, len(h) - R)
          if h[i] > h[i - L:i].max() and h[i] >= h[i + 1:i + R + 1].max()]
    bl = [i for i in range(L, len(l) - R)
          if l[i] < l[i - L:i].min() and l[i] <= l[i + 1:i + R + 1].min()]
    assert list(hi) == bh and list(lo) == bl


def test_pivots_short_input():
    e = pivots(np.ones(5), np.ones(5), 5, 5)
    assert len(e[0]) == 0 and len(e[1]) == 0


# ------------------------------------------------------------------ trend
def _trend_of(vertices):
    df = mk(zigzag(vertices))
    cfg = Config()
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    hi, lo = pivots(h, l, cfg.pivot_left, cfg.pivot_right)
    a = S.atr(h, l, df["close"].to_numpy(), cfg.atr_period)
    return trend_state(len(df), hi, h[hi], lo, l[lo], cfg.pivot_right,
                       cfg.trend_swings, a, cfg.contact_tol_atr), df


def test_uptrend_detected():
    t, _ = _trend_of([100, 110, 104, 115, 108, 120, 112, 125, 117, 130, 122])
    assert t[-1] == 1 and (t[:20] == 0).all()


def test_downtrend_detected():
    t, _ = _trend_of([130, 120, 126, 115, 122, 110, 118, 105, 113, 100, 108])
    assert t[-1] == -1


def test_range_is_unclear():
    t, _ = _trend_of([100, 110, 100, 110, 100, 110, 100, 110, 100, 110])
    assert t[-1] == 0


def test_trend_flips_on_structure_break():
    up = [100, 110, 104, 115, 108, 120, 112, 125, 117]
    down = [117, 105, 114, 100, 109, 95, 104, 90, 99]
    t, _ = _trend_of(up + down[1:])
    assert t.max() == 1 and t[-1] == -1


# ------------------------------------------------------------ break of structure
def test_break_of_resistance_with_two_contacts():
    # two equal highs at ~110 (two contacts) then a close well above
    c = zigzag([100, 110, 100, 110, 100, 110, 100, 125], leg=12)
    df = mk(c)
    res = scan(df, Config())
    ev = res.events
    up = ev[(ev["dir"] == 1)]
    assert len(up) >= 1
    first = up.iloc[0]
    assert first["contacts"] >= 2
    assert abs(first["level"] - 110.1) < 0.5
    fb = int(first["bar"])
    assert df["close"].iloc[fb] > first["level"]
    # nothing broke earlier
    assert (df["close"].iloc[:fb] <= first["level"]).all()


def test_wick_does_not_break_close_does():
    c = zigzag([100, 110, 100, 110, 100, 110, 100, 108], leg=12)
    df = mk(c)
    df.loc[len(df) - 3, "high"] = 130.0      # huge wick, close stays below
    ev = scan(df, Config()).events
    assert (ev["dir"] == 1).sum() == 0


# -------- slow bar-by-bar reference vs the event-driven implementation
def slow_events(close, atr_, hi_idx, hi_px, lo_idx, lo_px, cfg):
    n, r, age = len(close), cfg.pivot_right, cfg.level_max_age
    pv = {}
    for i, p in zip(hi_idx, hi_px):
        pv.setdefault(int(i) + r, []).append((1, float(p)))
    for i, p in zip(lo_idx, lo_px):
        pv.setdefault(int(i) + r, []).append((-1, float(p)))
    live, out = [], []
    for t in range(n):
        live = [lv for lv in live if lv["lc"] + age >= t]
        for sd, p in pv.get(t, []):
            tol = cfg.contact_tol_atr * atr_[t]
            best = None
            for lv in live:
                if lv["sd"] == sd and abs(lv["px"] - p) <= tol and (
                        best is None or abs(lv["px"] - p) < abs(best["px"] - p)):
                    best = lv
            if best is None:
                live.append({"sd": sd, "px": p, "k": 1, "lc": t, "buf": cfg.break_buf_atr * atr_[t]})
            else:
                live.remove(best)
                k = best["k"]
                live.append({"sd": sd, "px": (best["px"] * k + p) / (k + 1), "k": k + 1,
                             "lc": t, "buf": cfg.break_buf_atr * atr_[t]})
        keep = []
        for lv in live:
            if (lv["sd"] == 1 and close[t] > lv["px"] + lv["buf"]) or \
               (lv["sd"] == -1 and close[t] < lv["px"] - lv["buf"]):
                out.append((t, lv["sd"], lv["px"], lv["k"]))
            else:
                keep.append(lv)
        live = keep
    return sorted(out, key=lambda e: (e[0], e[1], e[2], e[3])), live


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
@pytest.mark.parametrize("age", [300, 40])
def test_events_match_slow_reference(seed, age):
    df = synth(4000, seed)
    cfg = Config(level_max_age=age)
    h, l, c = (df[k].to_numpy() for k in ("high", "low", "close"))
    a = S.atr(h, l, c, cfg.atr_period)
    hi, lo = pivots(h, l, cfg.pivot_left, cfg.pivot_right)
    ev, live = find_breaks(c, a, hi, h[hi], lo, l[lo], cfg)
    sev, slive = slow_events(c, a, hi, h[hi], lo, l[lo], cfg)
    got = [(int(r.bar), int(r.dir), float(r.level), int(r.contacts)) for r in ev.itertuples()]
    assert len(got) == len(sev) and len(got) > 20
    for g, s in zip(got, sev):
        assert g[:2] == s[:2] and g[3] == s[3] and abs(g[2] - s[2]) < 1e-9
    n = len(c)
    slive = [lv for lv in slive if lv["lc"] + cfg.level_max_age >= n - 1]
    assert sorted((x["side"], round(x["price"], 6)) for x in live) == \
        sorted((x["sd"], round(x["px"], 6)) for x in slive)


# ------------------------------------------------- no look-ahead / repainting
@pytest.mark.parametrize("seed", [0, 5])
def test_prefix_stability(seed):
    df = synth(6000, seed)
    cfg = Config()
    full = scan(df, cfg)
    assert len(full.signals) >= 3, "need signals for the test to mean anything"
    for m in (2500, 3333, 4800, 5999):
        part = scan(df.iloc[:m].reset_index(drop=True), cfg)
        a = full.signals[full.signals["bar"] < m].reset_index(drop=True)
        b = part.signals.reset_index(drop=True)
        pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-9)
        assert (full.trend[:m - 1] == part.trend[:m - 1]).all()
        assert (full.ma_state[:m] == part.ma_state[:m]).all()


def test_future_cannot_change_past_signal():
    df = synth(5000, 3)
    base = scan(df, Config()).signals
    df2 = df.copy()
    df2.loc[4000:, ["open", "high", "low", "close"]] *= 3.0   # wreck the future
    alt = scan(df2, Config()).signals
    pd.testing.assert_frame_equal(base[base["bar"] < 4000].reset_index(drop=True),
                                  alt[alt["bar"] < 4000].reset_index(drop=True))


# ------------------------------------------------------------ signal sanity
def test_signal_geometry_and_fees():
    sig = scan(synth(20000, 1), Config()).signals
    assert len(sig) > 5
    L, Sh = sig[sig.side == "LONG"], sig[sig.side == "SHORT"]
    assert (L.stop < L.entry).all() and (L.target > L.entry).all()
    assert (Sh.stop > Sh.entry).all() and (Sh.target < Sh.entry).all()
    assert sig.fee_ok.all()
    assert (sig.target_pct >= 3 * Config().fee_rt_pct - 1e-12).all()
    assert set(sig.kind) <= {"continuation", "reversal"}
    # cooldown respected per direction
    for _, g in sig.groupby("side"):
        assert (g.bar.diff().dropna() >= Config().cooldown).all()


def test_continuation_requires_trend_and_ma():
    cfg = Config()
    res = scan(synth(20000, 2), cfg)
    cont = res.signals[res.signals.kind == "continuation"]
    d = np.where(cont.side == "LONG", 1, -1)
    assert (res.ma_state[cont.bar.to_numpy()] == d).all()
    assert (res.trend[cont.bar.to_numpy() - 1] == d).all()


# --------------------------------------------------------------- performance
def test_speed_100k_bars():
    df = synth(100_000, 7)
    scan(df.iloc[:5000].reset_index(drop=True))     # warm-up
    t0 = time.perf_counter()
    res = scan(df)
    dt = time.perf_counter() - t0
    print(f"\n100k bars scanned in {dt:.2f}s, {len(res.signals)} signals")
    assert dt < 3.0


# ----------------------------------------------------------------- edge cases
def test_edge_cases():
    scan(mk(np.ones(5) * 100))
    scan(mk(np.ones(2000) * 100))                 # flat market
    scan(mk(np.linspace(100, 200, 2000)))         # pure ramp
    r = scan(mk(np.ones(10) * 100))
    assert len(r.signals) == 0
    bad = mk(np.ones(300) * 100)
    bad.loc[10, "close"] = np.nan
    with pytest.raises(ValueError):
        scan(bad)
    with pytest.raises(ValueError):
        scan(mk(np.ones(300))[["open", "high", "low"]])


def test_summarize_runs():
    df = synth(5000, 4)
    s = S.summarize(df, scan(df))
    assert s["trend"] in {"UP", "DOWN", "UNCLEAR/RANGE"}
    if s["nearest_resistance"] is not None:
        assert s["nearest_resistance"] > s["close"]
    if s["nearest_support"] is not None:
        assert s["nearest_support"] < s["close"]
