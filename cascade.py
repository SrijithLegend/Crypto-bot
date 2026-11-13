"""BTC cascade-risk flag (see claude.md). RISK FLAG, NOT AN ENTRY SIGNAL. Binance public data, no key.
  python cascade.py            # log a reading every 5 min to cascade_log.csv
"""
from __future__ import annotations

import csv
import math
import os
import time

import requests

BINANCE = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"

CFG = dict(
    # Signal 1: OI
    oi_window_bars=12,          # 12 x 5m = 1h change window
    oi_z_min=2.0,               # OI change must be >= 2 std above its own history
    flat_band=0.0025,           # price may move at most 0.25% against the position side
    # Signal 2: funding (per 8h)
    funding_abs_min=0.0003,     # 0.03% per 8h; Binance baseline is ~0.01%
    funding_z_min=2.0,
    # Signal 3: liquidation clusters
    cluster_pct=0.02,           # "just beyond price" = within 2%
    cluster_far_pct=0.10,       # compare against the 2%-10% zone
    cluster_ratio_min=2.0,      # near zone must be >= 2x denser than far zone
    cluster_min_oi_share=0.01,  # near zone must hold >= 1% of total OI notional
    bucket_usd=50,
    # Heatmap model assumptions (NOT measured; guesses)
    leverage_tiers={10: 0.25, 25: 0.30, 50: 0.25, 100: 0.20},
    mmr=0.004,
    long_share=0.5,
)


# ------------------------------------------------------------------ data + signals 1, 2
def _get(path, **params):
    r = requests.get(BINANCE + path, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_data():
    oi = _get("/futures/data/openInterestHist", symbol=SYMBOL, period="5m", limit=500)
    kl = _get("/fapi/v1/klines", symbol=SYMBOL, interval="5m", limit=500)
    fund = _get("/fapi/v1/fundingRate", symbol=SYMBOL, limit=100)
    prem = _get("/fapi/v1/premiumIndex", symbol=SYMBOL)

    n = min(len(oi), len(kl))          # align by taking the most recent n of each
    oi_val = [float(r["sumOpenInterestValue"]) for r in oi][-n:]
    bars = [dict(high=float(k[2]), low=float(k[3]), close=float(k[4])) for k in kl][-n:]
    funding_hist = [float(r["fundingRate"]) for r in fund]
    return oi_val, bars, funding_hist, float(prem["lastFundingRate"])


def zscore(x, series):
    m = sum(series) / len(series)
    sd = (sum((v - m) ** 2 for v in series) / len(series)) ** 0.5
    return 0.0 if sd == 0 else (x - m) / sd


def oi_signal(oi_val, bars, c=CFG):
    w = c["oi_window_bars"]
    chg = [oi_val[i] / oi_val[i - w] - 1 for i in range(w, len(oi_val))]
    cur = chg[-1]
    z = zscore(cur, chg[:-1])
    price_ret = bars[-1]["close"] / bars[-1 - w]["close"] - 1
    return dict(oi_chg=cur, z=z, price_ret=price_ret,
                spike=(cur > 0 and z >= c["oi_z_min"]))


def funding_signal(cur, hist, c=CFG):
    z = zscore(cur, hist)
    crowded = None
    # AND on purpose: the abs floor stops a near-zero std from inflating z
    if cur >= c["funding_abs_min"] and z >= c["funding_z_min"]:
        crowded = "long"
    elif cur <= -c["funding_abs_min"] and z <= -c["funding_z_min"]:
        crowded = "short"
    return dict(rate=cur, z=z, crowded=crowded)
