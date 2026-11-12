"""Stage 5 - Funding vote (contrarian). Compares the current funding value with its own recent history
(percentile rank, so no unit assumptions). Positive funding = longs pay shorts = longs crowded.
  rank >= hi -> -1 (vote SHORT)   rank <= lo -> +1 (vote LONG)   else / no data -> 0 (no vote, fail-open)
Series is oldest -> newest; the LAST item is 'now' (must be the last CLOSED sample: no look-ahead)."""
from __future__ import annotations

import numpy as np


def funding_vote(series, hi=0.90, lo=0.10, min_history=500, lookback=None) -> tuple[int, float]:
    s = np.asarray(series, dtype=float)
    s = s[np.isfinite(s)]
    if len(s) < 2:
        return 0, float("nan")
    now, hist = s[-1], s[:-1]
    if lookback:
        hist = hist[-lookback:]
    if len(hist) < min_history or hist.max() == hist.min():
        return 0, float("nan")
    rank = float((hist < now).mean() + 0.5 * (hist == now).mean())   # ties land mid-pack
    return (-1 if rank >= hi else 1 if rank <= lo else 0), rank
