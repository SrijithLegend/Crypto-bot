"""scanner.py - detect the 4 stages from the slide on OHLC candles.

Stages: (1) Trend Identifier  (2) Break of Structure (contact levels)
        (3) Moving Averages (EMA 20/50/200)  (4) Continuation or Reversal

Rules that keep it accurate:
  * Everything at bar t uses only bars <= t (no look-ahead, no repainting).
  * Swing points are confirmed `pivot_right` bars AFTER they form.
  * Breaks are decided on candle CLOSES, never wicks, with an ATR buffer.
  * Feed CLOSED candles only (fetch_candles() drops the forming one).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

INF = 10**12


@dataclass(frozen=True)
class Config:
    # swings
    pivot_left: int = 5
    pivot_right: int = 5          # confirmation delay in bars
    trend_swings: int = 2         # consecutive HH+HL (or LH+LL) needed
    # moving averages
    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    slope_bars: int = 5
    atr_period: int = 14
    # contact levels / break of structure
    contact_tol_atr: float = 0.25   # pivots this close (in ATR) = same level
    break_buf_atr: float = 0.10     # close must clear the level by this much
    level_max_age: int = 300        # bars a level stays alive after last contact
    # signal filters
    min_contacts_cont: int = 2      # continuation needs a real "contact" level
    min_contacts_rev: int = 1       # reversal may break the latest swing
    require_ma_cont: bool = True
    cooldown: int = 20              # min bars between signals, same direction
    # trade geometry
    stop_buf_atr: float = 0.2
    fallback_stop_atr: float = 1.5
    rr: float = 2.0
    # fees: taker in + taker out incl. 18% GST (derived from the order CSV)
    fee_rt_pct: float = 0.11
    min_target_fee_mult: float = 3.0
    drop_fee_fail: bool = True


# ----------------------------------------------------------------- indicators
def ema(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()


def pivots(high: np.ndarray, low: np.ndarray, left: int, right: int):
    """Indices of swing highs / lows. A swing at i is only KNOWN at i+right."""
    if left < 1 or right < 1:
        raise ValueError("pivot_left and pivot_right must be >= 1")
    w = left + right + 1
    if len(high) < w:
        e = np.array([], dtype=np.int64)
        return e, e
    wh = sliding_window_view(high, w)
    ctr = wh[:, left]
    is_hi = (ctr > wh[:, :left].max(axis=1)) & (ctr >= wh[:, left + 1:].max(axis=1))
    wl = sliding_window_view(low, w)
    ctl = wl[:, left]
    is_lo = (ctl < wl[:, :left].min(axis=1)) & (ctl <= wl[:, left + 1:].min(axis=1))
    return np.flatnonzero(is_hi) + left, np.flatnonzero(is_lo) + left


# ------------------------------------------------------- stage 1: trend
def trend_state(n, hi_idx, hi_px, lo_idx, lo_px, right, k, atr_, tol_atr):
    """+1 uptrend (k consecutive HH and HL), -1 downtrend (LH and LL), else 0.
    A swing within tol_atr*ATR of the previous one is EQUAL (a flat level), not
    higher/lower - it breaks the chain, so ranges stay 'unclear'."""
    conf = np.r_[hi_idx + right, lo_idx + right]
    typ = np.r_[np.ones(len(hi_idx), np.int8), -np.ones(len(lo_idx), np.int8)]
    px = np.r_[hi_px, lo_px]
    order = np.argsort(conf, kind="stable")
    trend = np.zeros(n, dtype=np.int8)
    last_h = last_l = np.nan
    hh = lh = hl = ll = 0
    cur, start = 0, 0
    for j in order:
        c = int(conf[j])
        if c >= n:
            break
        trend[start:c] = cur
        start = c
        p = px[j]
        tol = tol_atr * atr_[c]
        if typ[j] == 1:
            if not np.isnan(last_h):
                if p > last_h + tol:
                    hh, lh = hh + 1, 0
                elif p < last_h - tol:
                    lh, hh = lh + 1, 0
                else:
                    hh = lh = 0
            last_h = p
        else:
            if not np.isnan(last_l):
                if p > last_l + tol:
                    hl, ll = hl + 1, 0
                elif p < last_l - tol:
                    ll, hl = ll + 1, 0
                else:
                    hl = ll = 0
            last_l = p
        cur = 1 if (hh >= k and hl >= k) else (-1 if (lh >= k and ll >= k) else 0)
    trend[start:] = cur
    return trend


# ------------------------------------------------- stage 3: moving averages
def ma_alignment(close, e1, e2, e3, slope_bars, warmup):
    """+1 close>EMA20>EMA50>EMA200 with EMA20/50 rising; -1 mirror; else 0."""
    pad = np.full(slope_bars, np.nan)
    s1 = e1 - np.r_[pad, e1[:-slope_bars]]
    s2 = e2 - np.r_[pad, e2[:-slope_bars]]
    bull = (close > e1) & (e1 > e2) & (e2 > e3) & (s1 > 0) & (s2 > 0)
    bear = (close < e1) & (e1 < e2) & (e2 < e3) & (s1 < 0) & (s2 < 0)
    out = np.where(bull, 1, np.where(bear, -1, 0)).astype(np.int8)
    out[:warmup] = 0
    return out


# --------------------------------------- stage 2: contact levels + breaks
def _first_break(close, side, price, buf, start, end):
    if end < start:
        return INF
    seg = close[start:end + 1]
    m = seg > price + buf if side == 1 else seg < price - buf
    return start + int(np.argmax(m)) if m.any() else INF


def find_breaks(close, atr_, hi_idx, hi_px, lo_idx, lo_px, cfg: Config):
    """Cluster swing points into levels ("contacts"), then find the first CLOSE
    beyond each level. side=+1 resistance (break up), -1 support (break down)."""
    n = len(close)
    r, age = cfg.pivot_right, cfg.level_max_age
    conf = np.r_[hi_idx + r, lo_idx + r]
    side = np.r_[np.ones(len(hi_idx), np.int8), -np.ones(len(lo_idx), np.int8)]
    px = np.r_[hi_px, lo_px]
    order = np.argsort(conf, kind="stable")

    live, events = [], []

    def make(sd, price, contacts, c):
        buf = cfg.break_buf_atr * atr_[c]
        bt = _first_break(close, sd, price, buf, c, min(n - 1, c + age))
        return {"side": sd, "price": price, "contacts": contacts,
                "last_conf": c, "buf": buf, "bt": bt}

    for j in order:
        c = int(conf[j])
        if c >= n:
            break
        keep = []
        for lv in live:                      # settle levels that ended before c
            if lv["bt"] < c:
                events.append((lv["bt"], lv["side"], lv["price"], lv["contacts"]))
            elif lv["last_conf"] + age >= c:
                keep.append(lv)
        live = keep
        sd, p = int(side[j]), float(px[j])
        tol = cfg.contact_tol_atr * atr_[c]
        best, bd = None, None
        for lv in live:                      # nearest live same-side level
            if lv["side"] == sd:
                d = abs(lv["price"] - p)
                if d <= tol and (bd is None or d < bd):
                    best, bd = lv, d
        if best is None:
            live.append(make(sd, p, 1, c))
        else:
            k = best["contacts"]
            new = make(sd, (best["price"] * k + p) / (k + 1), k + 1, c)
            live[live.index(best)] = new

    final_live = []
    for lv in live:                          # flush at end of data
        if lv["bt"] < INF:
            events.append((lv["bt"], lv["side"], lv["price"], lv["contacts"]))
        elif lv["last_conf"] + age >= n - 1:
            final_live.append({k: lv[k] for k in ("side", "price", "contacts", "last_conf")})
    events.sort(key=lambda e: (e[0], e[1], e[2], e[3]))
    ev = pd.DataFrame(events, columns=["bar", "dir", "level", "contacts"])
    return ev.astype({"bar": "int64", "dir": "int64", "contacts": "int64"}), final_live


# ------------------------------------- stage 4: continuation / reversal
def make_signals(df, cfg, events, trend, ms, e2, a, hi_idx, hi_px, lo_idx, lo_px):
    c = df["close"].to_numpy(float)
    times = df["time"].to_numpy() if "time" in df.columns else np.arange(len(df))
    hi_conf, lo_conf = hi_idx + cfg.pivot_right, lo_idx + cfg.pivot_right
    warm = cfg.ema_slow * 3
    last = {1: -INF, -1: -INF}
    rows = []
    for t, d, lvl, contacts in events.itertuples(index=False, name=None):
        if t < warm:
            continue
        tp = int(trend[t - 1])
        if tp == 0:
            continue                          # no confirmed trend -> no signal
        if d == tp:
            kind = "continuation"
            if contacts < cfg.min_contacts_cont:
                continue
            if cfg.require_ma_cont and ms[t] != d:
                continue
        else:
            kind = "reversal"
            if contacts < cfg.min_contacts_rev:
                continue
            if not (c[t] < e2[t] if d == -1 else c[t] > e2[t]):
                continue
        if t - last[d] < cfg.cooldown:
            continue
        entry, stop, target, risk, target_pct = geometry(d, t, c, a, hi_conf, hi_px, lo_conf, lo_px, cfg)
        fee_ok = target_pct >= cfg.min_target_fee_mult * cfg.fee_rt_pct
        if cfg.drop_fee_fail and not fee_ok:
            continue
        last[d] = t
        rows.append({
            "time": times[t], "bar": int(t), "side": "LONG" if d == 1 else "SHORT",
            "kind": kind, "entry": entry, "stop": stop, "target": target,
            "risk_pct": risk / entry * 100.0, "target_pct": target_pct, "fee_ok": bool(fee_ok),
            "trend_prev": tp, "ma_state": int(ms[t]), "level": float(lvl),
            "contacts": int(contacts), "score": 1 + int(ms[t] == d) + int(contacts >= 2),
        })
    cols = ["time", "bar", "side", "kind", "entry", "stop", "target", "risk_pct",
            "target_pct", "fee_ok", "trend_prev", "ma_state", "level", "contacts", "score"]
    return pd.DataFrame(rows, columns=cols)


def geometry(d, t, c, a, hi_conf, hi_px, lo_conf, lo_px, cfg):
    """Entry/stop/target for a trade in direction d on bar t (stop beyond the last confirmed swing)."""
    entry = float(c[t])
    stop = np.nan
    if d == 1:
        k = int(np.searchsorted(lo_conf, t, side="right")) - 1
        if k >= 0:
            stop = lo_px[k] - cfg.stop_buf_atr * a[t]
    else:
        k = int(np.searchsorted(hi_conf, t, side="right")) - 1
        if k >= 0:
            stop = hi_px[k] + cfg.stop_buf_atr * a[t]
    if not np.isfinite(stop) or (d == 1 and stop >= entry) or (d == -1 and stop <= entry):
        stop = entry - d * cfg.fallback_stop_atr * a[t]
    risk = abs(entry - stop)
    return entry, float(stop), entry + d * cfg.rr * risk, risk, cfg.rr * risk / entry * 100.0


# ------------------------------------------------------------------ driver
@dataclass
class Result:
    cfg: Config
    trend: np.ndarray
    ma_state: np.ndarray
    ema_fast: np.ndarray
    ema_mid: np.ndarray
    ema_slow: np.ndarray
    atr: np.ndarray
    pivot_highs: np.ndarray
    pivot_lows: np.ndarray
    events: pd.DataFrame
    signals: pd.DataFrame
    live_levels: list


def scan(df: pd.DataFrame, cfg: Config = Config()) -> Result:
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            raise ValueError(f"missing column: {col}")
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    if np.isnan(h).any() or np.isnan(l).any() or np.isnan(c).any():
        raise ValueError("NaN in OHLC - clean the data first")
    n = len(c)
    e1, e2, e3 = ema(c, cfg.ema_fast), ema(c, cfg.ema_mid), ema(c, cfg.ema_slow)
    a = atr(h, l, c, cfg.atr_period)
    hi_idx, lo_idx = pivots(h, l, cfg.pivot_left, cfg.pivot_right)
    hi_px, lo_px = h[hi_idx], l[lo_idx]
    trend = trend_state(n, hi_idx, hi_px, lo_idx, lo_px, cfg.pivot_right,
                        cfg.trend_swings, a, cfg.contact_tol_atr)
    ms = ma_alignment(c, e1, e2, e3, cfg.slope_bars, cfg.ema_slow * 3)
    events, live = find_breaks(c, a, hi_idx, hi_px, lo_idx, lo_px, cfg)
    sig = make_signals(df, cfg, events, trend, ms, e2, a, hi_idx, hi_px, lo_idx, lo_px)
    return Result(cfg, trend, ms, e1, e2, e3, a, hi_idx, lo_idx, events, sig, live)


# ------------------------------------------------ 5-stage consensus entry
STAGES = ("trend", "bos", "ma", "signal", "funding")


def stage_votes(df, res, i, funding=0):
    """Direction (+1/-1/0) each of the 5 stages points to on bar i. Funding is passed in (contrarian vote)."""
    ev = res.events[res.events["bar"] == i]["dir"].sum() if len(res.events) else 0
    sg = res.signals[res.signals["bar"] == i]
    return {"trend": int(res.trend[i]), "bos": int(np.sign(ev)), "ma": int(res.ma_state[i]),
            "signal": 0 if sg.empty else (1 if sg["side"].iloc[-1] == "LONG" else -1), "funding": funding}


def _agree(v, d, need):
    n, opp = sum(x == d for x in v.values()), sum(x == -d for x in v.values())
    return n >= need and n > opp


def consensus(df, res, funding=0, min_votes=3, mode="scanner"):
    """Entry decision on the LAST closed bar. Stage 4 (the scanner signal) is always the anchor.
      scanner: the signal alone (the backtested baseline).
      gated  : the signal, unless funding votes the OPPOSITE side (funding abstaining = allowed).
      vote   : >= min_votes stages agree and outvote the other side, stage 4 is one of them, and the
               vote set was not already agreeing on the previous bar (edge trigger).
    Returns the signal dict (with "votes") or None."""
    i = len(df) - 1
    sg = res.signals[res.signals["bar"] == i]
    if sg.empty:
        return None
    row = sg.iloc[-1].to_dict()
    d = 1 if row["side"] == "LONG" else -1
    now = stage_votes(df, res, i, funding)
    if mode == "gated" and funding == -d:
        return None
    if mode == "vote":
        if not (_agree(now, d, min_votes) and now["signal"] == d):
            return None
        if _agree(stage_votes(df, res, i - 1, funding), d, min_votes):
            return None
    row["votes"] = ",".join(k for k, x in now.items() if x == d)
    return row


def summarize(df: pd.DataFrame, res: Result) -> dict:
    """What the slide's four boxes say about the LAST closed candle."""
    i = len(df) - 1
    name = {1: "UP", -1: "DOWN", 0: "UNCLEAR/RANGE"}
    close = float(df["close"].iloc[i])
    above = [lv["price"] for lv in res.live_levels if lv["side"] == 1 and lv["price"] > close]
    below = [lv["price"] for lv in res.live_levels if lv["side"] == -1 and lv["price"] < close]
    last_sig = res.signals.iloc[-1].to_dict() if len(res.signals) else None
    return {
        "close": close,
        "trend": name[int(res.trend[i])],
        "ma_alignment": name[int(res.ma_state[i])],
        "nearest_resistance": min(above) if above else None,
        "nearest_support": max(below) if below else None,
        "last_signal": last_sig,
    }


# --------------------------------------------------------------- data feed
RES_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
               "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400}


def fetch_candles(symbol="BTCUSD", resolution="5m", bars=1500,
                  base="https://api.india.delta.exchange") -> pd.DataFrame:
    """Public Delta India candles (no auth). Returns CLOSED candles only."""
    import time

    import requests

    secs = RES_SECONDS[resolution]
    now = int(time.time())
    end, left, rows = now, bars, []
    while left > 0:
        n = min(500, left)
        start = end - n * secs
        r = requests.get(f"{base}/v2/history/candles", timeout=15,
                         params={"symbol": symbol, "resolution": resolution,
                                 "start": start, "end": end})
        r.raise_for_status()
        j = r.json()
        rows += j.get("result", j) if isinstance(j, dict) else j
        end, left = start, left - n
        time.sleep(0.25)                     # stay well under rate limits
    df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time")
    df = df[df["time"] + secs <= now].reset_index(drop=True)   # drop forming candle
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df[["time", "open", "high", "low", "close", "volume"]].astype(
        {"open": float, "high": float, "low": float, "close": float, "volume": float})


if __name__ == "__main__":
    data = fetch_candles()
    result = scan(data)
    print(summarize(data, result))
    print(result.signals.tail(10).to_string())
