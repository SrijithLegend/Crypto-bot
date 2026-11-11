"""Two interchangeable back-ends with the same methods:
   DeltaExchange  - real orders (testnet or live)
   PaperExchange  - virtual account that trades against REAL live prices (no keys needed)

Common interface: candles(resolution, bars, now) position() balance() mark_price()
                  set_leverage(lev) place_entry(side, lots, stop, target, client_id) close_position()
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd

from delta_client import RES_SECONDS, DeltaError
from risk import SLIP, TAKER


def _decimals(tick: float) -> int:
    s = f"{tick:.8f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


class DeltaExchange:
    def __init__(self, client, public, symbol="BTCUSD", asset="USD",
                 trigger_method="last_traded_price", sleep=time.sleep):
        self.c, self.public, self.symbol, self.asset = client, public, symbol, asset
        self.trigger, self.sleep = trigger_method, sleep
        p = public.get_product(symbol)
        self.product_id = int(p["id"])
        self.contract_value = float(p.get("contract_value") or 0.001)
        self.tick = float(p.get("tick_size") or 0.5)

    def _px(self, x: float) -> str:
        v = round(x / self.tick) * self.tick
        return f"{v:.{_decimals(self.tick)}f}"

    def candles(self, resolution: str, bars: int, now: float) -> pd.DataFrame:
        secs = RES_SECONDS[resolution]
        return self.public.get_candles(self.symbol, resolution, int(now) - bars * secs, int(now))

    def balance(self) -> float:
        j = self.c.get_balances()
        for r in j.get("result", []):
            if r.get("asset_symbol") == self.asset:
                return float(r["balance"])
        raise DeltaError(f"asset {self.asset!r} not in wallet; have "
                         f"{[r.get('asset_symbol') for r in j.get('result', [])]}")

    def position(self) -> tuple[int, float]:
        r = self.c.get_position(self.product_id)
        if not r:
            return 0, 0.0
        size = int(float(r.get("size") or 0))          # signed: + long, - short
        return size, float(r.get("entry_price") or 0.0)

    def mark_price(self) -> float:
        return float(self.public.get_ticker(self.symbol)["mark_price"])

    def set_leverage(self, lev: int):
        self.c.set_leverage(self.product_id, lev)

    def place_entry(self, side: int, lots: int, stop: float, target: float, client_id: str) -> dict:
        body = {"product_id": self.product_id, "size": int(lots),
                "side": "buy" if side == 1 else "sell", "order_type": "market_order",
                "client_order_id": client_id[:32],
                "bracket_stop_loss_price": self._px(stop),
                "bracket_take_profit_price": self._px(target),
                "bracket_stop_trigger_method": self.trigger}
        return self.c.place_order(body)

    def close_position(self):
        size, _ = self.position()
        if size == 0:
            return
        self.c.place_order({"product_id": self.product_id, "size": abs(size),
                            "side": "sell" if size > 0 else "buy", "order_type": "market_order",
                            "reduce_only": True})
        try:
            self.c.cancel_all(self.product_id)         # drop any leftover bracket legs
        except DeltaError:
            pass


# --------------------------------------------------------------------- paper
def walk_exit(side, entry, stop, target, lev, o, h, l):
    """First exit on 1m candles (arrays). Stop-first when stop & target share a minute.
    Returns (index, price, reason) or None if still open."""
    from risk import LIQ_MAINT
    liq = entry * (1 - side * max(1.0 / lev - LIQ_MAINT, 0.0005))
    n = len(o)
    if n == 0:
        return None
    if side == 1:
        adverse, kind = max(stop, liq), ("liq" if liq >= stop else "stop")
        ha, ht = l <= adverse, h >= target
    else:
        adverse, kind = min(stop, liq), ("liq" if liq <= stop else "stop")
        ha, ht = h >= adverse, l <= target
    ia = int(np.argmax(ha)) if ha.any() else n
    it = int(np.argmax(ht)) if ht.any() else n
    if ia == n and it == n:
        return None
    if ia <= it:
        if kind == "liq":
            return ia, float(liq), "liq"
        px = min(adverse, o[ia]) if side == 1 else max(adverse, o[ia])
        return ia, float(px), "stop"
    return it, float(target), "target"


class PaperExchange:
    """Virtual account. Fills at live mark + 1 tick slippage; exits resolved from real 1m candles
    (stop first if both touched). Exits are charged TAKER fees (brackets on Delta trigger market orders)."""

    def __init__(self, public, state_path="paper_state.json", start_usd=1100 / 85, leverage=10,
                 symbol="BTCUSD", contract_value=0.001, time_fn=time.time):
        self.public, self.path, self.symbol = public, state_path, symbol
        self.lev, self.cv, self.now = leverage, contract_value, time_fn
        if os.path.exists(state_path):
            with open(state_path) as f:
                self.st = json.load(f)
        else:
            self.st = {"balance": float(start_usd), "pos": None}
            self._save()

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.st, f)
        os.replace(tmp, self.path)

    def candles(self, resolution, bars, now):
        secs = RES_SECONDS[resolution]
        return self.public.get_candles(self.symbol, resolution, int(now) - bars * secs, int(now))

    def mark_price(self):
        return float(self.public.get_ticker(self.symbol)["mark_price"])

    def set_leverage(self, lev):
        self.lev = lev

    def balance(self):
        self.tick()
        return self.st["balance"]

    def position(self):
        self.tick()
        p = self.st["pos"]
        return (0, 0.0) if not p else (p["side"] * p["lots"], p["entry"])

    def place_entry(self, side, lots, stop, target, client_id):
        entry = self.mark_price() + side * SLIP
        now = self.now()
        self.st["pos"] = {"side": side, "lots": int(lots), "entry": entry, "stop": stop, "target": target,
                          "lev": self.lev, "opened": now, "checked": int(now // 60 * 60) + 60, "id": client_id}
        self._save()
        return {"average_fill_price": str(entry), "client_order_id": client_id}

    def _settle(self, px, reason):
        p = self.st["pos"]
        notional = p["lots"] * self.cv * p["entry"]
        margin = notional / p["lev"]
        fee_in = notional * TAKER
        if reason == "liq":
            pnl = -margin - fee_in
        else:
            exit_px = px - p["side"] * SLIP if reason in ("stop", "manual") else px
            pnl = (p["side"] * (exit_px - p["entry"]) * p["lots"] * self.cv
                   - fee_in - p["lots"] * self.cv * exit_px * TAKER)
        self.st["balance"] = max(0.0, self.st["balance"] + pnl)
        self.st["pos"] = None
        self._save()

    def tick(self):
        p = self.st["pos"]
        if not p:
            return
        now = int(self.now())
        start = int(p["checked"])
        if now - start < 60:
            return
        df = self.public.get_candles(self.symbol, "1m", start, now)
        if len(df):
            r = walk_exit(p["side"], p["entry"], p["stop"], p["target"], p["lev"],
                          df["open"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy())
            if r:
                self._settle(r[1], r[2])
                return
            p["checked"] = int(df["time"].iloc[-1].timestamp()) + 60
            self._save()

    def close_position(self):
        if self.st["pos"]:
            self._settle(self.mark_price(), "manual")
