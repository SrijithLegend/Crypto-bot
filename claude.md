# BTC Cascade-Risk Signals — Implementation Spec

Three signals that flag when BTC is positioned for a liquidation cascade:

1. **Open interest (OI) spike** with price flat or rising
2. **Extreme funding rate** (one side crowded)
3. **Liquidation clusters** just beyond current price

> **This is a risk flag, not an entry signal.** It tells you *where the fuel is*, not *when it ignites*. Expect false positives. Do not trade it live until you've logged data and backtested it.

---

## 0. Design decisions (read first)

| Decision | Choice | Why |
|---|---|---|
| Signal source | **Binance USDT-M BTCUSDT** (public, no key) | Deepest BTC derivatives market. Delta India's BTC OI is a small slice, so its OI/funding says little about where the real cascade fuel is. |
| Execution venue | Delta Exchange India (BTCUSD) | Your bot already trades there. Use the flag as a **filter** before placing orders. |
| Liquidation heatmap | **Estimated** from OI changes (section 4) | Real pending-liquidation maps are not free. Coinglass sells them via paid API. A DIY estimate is a model, not ground truth. |
| Output | Score 0-3 per side | 3/3 = all conditions aligned. 2/3 = watch. |

**Direction logic (the mirror matters):**

| Side | OI | Funding | Cluster | Meaning |
|---|---|---|---|---|
| `DOWN_CASCADE_RISK` | Spiking, price flat/rising (longs piling in) | Extreme **positive** | Long-liq cluster **below** price | Long flush risk |
| `UP_SQUEEZE_RISK` | Spiking, price flat/falling (shorts piling in) | Extreme **negative** | Short-liq cluster **above** price | Short squeeze risk |

---

## 1. Setup

```bash
pip install requests
```

No API keys needed. All endpoints below are public.

Endpoints used (Binance USDT-M futures, base `https://fapi.binance.com`):

- `GET /futures/data/openInterestHist` — OI history (use `period=5m`, `limit=500`)
- `GET /fapi/v1/klines` — price bars
- `GET /fapi/v1/fundingRate` — funding history
- `GET /fapi/v1/premiumIndex` — current funding + mark price

> **Verify before relying on it:** `openInterestHist` only serves a limited recent window (roughly the last 30 days). Check the current Binance docs. This is why section 6 tells you to log your own data.

---

## 2. Config (all thresholds are placeholders — tune by backtest)

```python
import math
import time
import csv
import os
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

    # Heatmap model assumptions (NOT measured; these are guesses)
    leverage_tiers={10: 0.25, 25: 0.30, 50: 0.25, 100: 0.20},  # share of new OI per tier
    mmr=0.004,                  # approx maintenance margin rate
    long_share=0.5,             # share of new OI assumed long
)
```

---

## 3. Data + signals 1 and 2

```python
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
```

---

## 4. Signal 3: estimated liquidation heatmap

**Idea:** every time OI grows, new positions opened near the current price. Project where those positions would be liquidated at assumed leverage tiers, then add up notional per price bucket. Shrink buckets when OI falls. Delete buckets price has already traded through (those got liquidated).

```python
def build_heatmap(oi_val, bars, c=CFG):
    b = c["bucket_usd"]
    longs, shorts = {}, {}             # {bucket_price: notional_usd}

    for i in range(1, len(oi_val)):
        d = oi_val[i] - oi_val[i - 1]
        px = bars[i]["close"]

        if d > 0:                       # new positions opened
            for lev, wt in c["leverage_tiers"].items():
                n = d * wt
                lp = round(px * (1 - 1 / lev + c["mmr"]) / b) * b
                sp = round(px * (1 + 1 / lev - c["mmr"]) / b) * b
                longs[lp] = longs.get(lp, 0.0) + n * c["long_share"]
                shorts[sp] = shorts.get(sp, 0.0) + n * (1 - c["long_share"])
        elif d < 0:                     # positions closed: shrink everything
            k = 1 + d / oi_val[i - 1]
            for book in (longs, shorts):
                for key in book:
                    book[key] *= k

        for key in [k for k in longs if k >= bars[i]["low"]]:    # long liqs already hit
            del longs[key]
        for key in [k for k in shorts if k <= bars[i]["high"]]:  # short liqs already hit
            del shorts[key]

    return longs, shorts


def _zone(book, lo, hi, far_lo, far_hi):
    near = sum(v for k, v in book.items() if lo < k <= hi)
    far = sum(v for k, v in book.items() if far_lo < k <= far_hi)
    far_scaled = far * (hi - lo) / (far_hi - far_lo)   # normalise to the same width
    ratio = near / far_scaled if far_scaled > 0 else (math.inf if near > 0 else 0.0)
    return near, ratio


def cluster_signal(longs, shorts, px, oi_total, c=CFG):
    p, fp = c["cluster_pct"], c["cluster_far_pct"]
    up_n, up_r = _zone(shorts, px, px * (1 + p), px * (1 + p), px * (1 + fp))
    dn_n, dn_r = _zone(longs, px * (1 - p), px, px * (1 - fp), px * (1 - p))
    floor = c["cluster_min_oi_share"] * oi_total
    return dict(
        up_notional=up_n, up_ratio=up_r, down_notional=dn_n, down_ratio=dn_r,
        short_cluster_above=(up_n >= floor and up_r >= c["cluster_ratio_min"]),
        long_cluster_below=(dn_n >= floor and dn_r >= c["cluster_ratio_min"]),
    )
```

**Limitations of this estimate (be honest with yourself about these):**

- It only sees positions opened in the history window (500 x 5m bars is about 41h). Older positions are invisible.
- Leverage tier weights and the 50/50 long/short split are **assumptions**. Real distribution is unknown.
- It treats OI increases as new opens and decreases as pro-rata closes. Reality is messier.
- If you pay for a real heatmap (Coinglass liquidation heatmap API), swap it in for `build_heatmap` and keep the rest.

---

## 5. Combine + run loop

```python
def combine(s_oi, s_f, s_c, c=CFG):
    fb = c["flat_band"]
    long_side = [
        s_oi["spike"] and s_oi["price_ret"] >= -fb,   # OI up, price flat/rising
        s_f["crowded"] == "long",
        s_c["long_cluster_below"],
    ]
    short_side = [
        s_oi["spike"] and s_oi["price_ret"] <= fb,    # OI up, price flat/falling
        s_f["crowded"] == "short",
        s_c["short_cluster_above"],
    ]
    return {"DOWN_CASCADE_RISK": sum(long_side), "UP_SQUEEZE_RISK": sum(short_side)}


def evaluate():
    oi_val, bars, fhist, fcur = fetch_data()
    s_oi = oi_signal(oi_val, bars)
    s_f = funding_signal(fcur, fhist)
    longs, shorts = build_heatmap(oi_val, bars)
    s_c = cluster_signal(longs, shorts, bars[-1]["close"], oi_val[-1])
    return dict(ts=int(time.time()), price=bars[-1]["close"],
                oi_chg=s_oi["oi_chg"], oi_z=s_oi["z"], price_ret=s_oi["price_ret"],
                funding=s_f["rate"], funding_z=s_f["z"], crowded=s_f["crowded"],
                up_ratio=s_c["up_ratio"], down_ratio=s_c["down_ratio"],
                **combine(s_oi, s_f, s_c))


if __name__ == "__main__":
    path = "cascade_log.csv"
    while True:
        try:
            row = evaluate()
            new = not os.path.exists(path)
            with open(path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=row.keys())
                if new:
                    w.writeheader()
                w.writerow(row)
            print(row)
        except Exception as e:             # never let a data hiccup kill the loop
            print("error:", e)
        time.sleep(300)                    # signals use 5m bars; no point polling faster
```

---

## 6. Do this before anything touches real money

1. **Run the logger for weeks.** It writes every reading to `cascade_log.csv`. This is your own dataset, since exchange history is limited.
2. **Define "cascade" objectively** so you can score hits: e.g. BTC moves >= 1.5% in <= 15 min in the flagged direction, within the next 1-4 hours.
3. **Measure, per score level (2/3 and 3/3):** hit rate, false positive rate, average move. If 3/3 doesn't beat the base rate of such moves, the signal is noise. Drop it.
4. **Tune thresholds on one period, test on a later period.** Tuning and testing on the same data will fool you.
5. **Paper trade** for at least a few weeks after that.

---

## 7. How to use it in the Delta bot (safest use first)

1. **Risk filter:** on `DOWN_CASCADE_RISK >= 2`, skip new longs and cut leverage. On `UP_SQUEEZE_RISK >= 2`, skip new shorts.
2. **Stop placement:** with a cluster near price, widen stops or reduce size. Stops sitting inside the cluster zone are the first thing a cascade sweeps.
3. **Fade/follow entries (riskiest):** only act after **confirmation**, meaning price actually breaks into the cluster with a volume surge. Never enter just because the flag is on.
4. Keep a hard max-loss and a kill switch independent of this signal.

**Optional: read Delta's own BTC numbers** (to compare against Binance):

```python
def delta_btc():
    r = requests.get("https://api.india.delta.exchange/v2/tickers/BTCUSD", timeout=10).json()["result"]
    return float(r["mark_price"]), float(r["oi_value"])
```

> Verify the response shape in the live Delta docs. The docs list `mark_price` and `oi`/`oi_value` on the ticker, but funding rate was **not** in the REST ticker example I found. Delta exposes it via a websocket `funding_rate` channel. This is another reason to take funding from Binance.

---

## 8. Known weaknesses

- **Timing:** the flag can stay on for hours with no cascade. Crowded positioning is necessary, not sufficient.
- **Estimated heatmap:** the weakest link. Treat cluster output as a rough hint.
- **Regime dependence:** thresholds that work in a quiet market will misfire in a volatile one. Z-scores adapt partly; the absolute funding floor does not.
- **Cross-venue gap:** Binance signals, Delta execution. Cascades usually transmit across venues, but slippage and basis on Delta are your problem.
- **Not financial advice.** This is a research tool. Leveraged trading can lose more than you expect, fast.