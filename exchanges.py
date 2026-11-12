"""DeltaExchange - real orders on Delta Exchange India.

Interface: candles(resolution, bars, now) position() balance() mark_price()
                  set_leverage(lev) place_entry(side, lots, stop, target, client_id) close_position()
"""
from __future__ import annotations

import time

import pandas as pd

from delta_client import RES_SECONDS, DeltaError


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

    def funding_history(self, bars: int, now: float) -> list:
        """Funding samples oldest -> newest from FUNDING:<symbol> 15m candles (CLOSED only).
        Only the current value's rank inside this history is used, so units don't matter."""
        secs = RES_SECONDS["15m"]
        end, rows, cur = int(now), {}, int(now) - bars * secs
        while cur < end:
            nxt = min(end, cur + 1500 * secs)
            j = self.public.request("GET", "/history/candles",
                                    params={"resolution": "15m", "symbol": f"FUNDING:{self.symbol}",
                                            "start": cur, "end": nxt})
            for c in j.get("result", []):
                rows[int(c["time"])] = c
            cur = nxt
        vals = [float(rows[t]["close"]) for t in sorted(rows)
                if rows[t].get("close") is not None and t + secs <= end]
        if not vals:
            raise DeltaError("no funding candles returned")
        return vals

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
