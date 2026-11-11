"""Hard risk rules. Pure functions, no network - everything here is unit-tested."""
from __future__ import annotations

import math
from dataclasses import dataclass

MAX_LEVERAGE = 20          # hard ceiling in code. Backtest: >=50x is liquidated on most trades.
TAKER = 0.00045 * 1.18     # 0.0531 % per side incl. 18 % GST (from your own order CSV)
LIQ_MAINT = 0.0025         # liq distance ~= 1/lev - 0.25 %  (Delta's own 200x warning figure)
SLIP = 0.5                 # USD


@dataclass(frozen=True)
class Risk:
    leverage: int = 10
    risk_per_trade_pct: float = 8.0      # max loss at the stop, % of equity (1 lot is ~6-7 % at Rs1,100)
    margin_use_pct: float = 75.0         # max % of equity used as margin (1 lot @10x ~ 62 % at Rs1,100)
    target_equity_pct: float = 20.0      # take profit when the trade nets this % of equity (0 = use scanner's 2R target)
    daily_loss_limit_pct: float = 15.0   # stop opening trades for the day below this
    max_consecutive_losses: int = 4      # then halt until you run `reset-halt`
    min_liq_buffer: float = 2.0          # liquidation distance must be >= this x stop distance
    max_stop_pct: float = 3.0            # skip setups whose stop is wider than this
    max_entry_drift_pct: float = 0.3     # skip if price moved this far from the signal price
    emergency_stop_slip_pct: float = 0.2 # software backup: flatten if price is this far past the stop

    def validate(self):
        if not (1 <= self.leverage <= MAX_LEVERAGE):
            raise ValueError(f"leverage {self.leverage} outside 1..{MAX_LEVERAGE}. "
                             f"MAX_LEVERAGE in risk.py is deliberately low; the backtest liquidated "
                             f"94% of trades at 200x.")
        for name in ("risk_per_trade_pct", "margin_use_pct", "daily_loss_limit_pct", "max_stop_pct"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        if self.margin_use_pct > 100:
            raise ValueError("margin_use_pct must be <= 100")


def target_price(equity: float, side: int, entry: float, lots: int, pct: float, contract_value: float = 0.001) -> float:
    """Price at which the trade nets `pct` % of equity after round-trip fees and slippage."""
    dist = equity * pct / 100 / (lots * contract_value) + entry * 2 * TAKER + 2 * SLIP
    return entry + side * dist


@dataclass(frozen=True)
class Plan:
    lots: int
    margin_usd: float
    risk_usd: float
    risk_pct_equity: float
    stop_pct: float
    liq_pct: float


def plan_trade(equity: float, side: int, entry: float, stop: float, target: float, risk: Risk,
               contract_value: float = 0.001):
    """Returns (Plan | None, reason). side: +1 long, -1 short."""
    if equity <= 0 or entry <= 0:
        return None, "no equity"
    if side == 1 and not (stop < entry < target):
        return None, "bad geometry (long)"
    if side == -1 and not (target < entry < stop):
        return None, "bad geometry (short)"
    stop_dist = abs(entry - stop)
    stop_pct = stop_dist / entry * 100
    if stop_pct > risk.max_stop_pct:
        return None, f"stop {stop_pct:.2f}% wider than max {risk.max_stop_pct}%"
    liq_pct = max(1.0 / risk.leverage - LIQ_MAINT, 0.0) * 100
    if liq_pct < risk.min_liq_buffer * stop_pct:
        return None, (f"liquidation {liq_pct:.2f}% away is closer than {risk.min_liq_buffer}x the stop "
                      f"({stop_pct:.2f}%) at {risk.leverage}x")
    risk_per_lot = contract_value * (stop_dist + entry * 2 * TAKER + 2 * SLIP)
    lots_risk = math.floor(equity * risk.risk_per_trade_pct / 100 / risk_per_lot)
    lots_margin = math.floor(equity * risk.margin_use_pct / 100 * risk.leverage / (contract_value * entry))
    lots = min(lots_risk, lots_margin)
    if lots < 1:
        one_lot_risk = risk_per_lot / equity * 100
        one_lot_margin = contract_value * entry / risk.leverage / equity * 100
        return None, (f"1 lot needs risk {one_lot_risk:.1f}% (limit {risk.risk_per_trade_pct}%) and margin "
                      f"{one_lot_margin:.0f}% of equity (limit {risk.margin_use_pct:.0f}%)")
    margin = lots * contract_value * entry / risk.leverage
    risk_usd = lots * risk_per_lot
    return Plan(lots, margin, risk_usd, risk_usd / equity * 100, stop_pct, liq_pct), "ok"
