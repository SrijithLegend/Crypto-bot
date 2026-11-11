"""4-stage strategy bot. LIVE ONLY: real money on Delta Exchange India.
Needs DELTA_API_KEY, DELTA_API_SECRET and DELTA_LIVE_CONFIRM=I_ACCEPT_THE_RISK in the environment.

  python bot.py run                 # trade live
  python bot.py selftest            # check keys/signing/balance/position parsing
  python bot.py status | flatten | reset-halt

Kill switch: create a file named STOP next to bot.py -> no new trades (open position keeps its bracket).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import scanner as S
from delta_client import PROD, DeltaClient, DeltaError, UncertainError
from exchanges import DeltaExchange
from risk import Risk, plan_trade, target_price

IST = timezone(timedelta(hours=5, minutes=30))
TF_MIN = 15
log = logging.getLogger("bot")


@dataclass
class Config:
    symbol: str = "BTCUSD"
    margin_asset: str = "USD"
    history_bars: int = 1500            # 15m candles fetched each time (~15 days; indicators need 600)
    settle_delay: int = 4               # seconds after a candle closes before reading it
    stale_give_up: int = 120            # stop retrying a bar this many seconds after it closed
    poll_seconds: int = 10
    risk: Risk = field(default_factory=Risk)

    @staticmethod
    def load(path="config.json") -> "Config":
        c = Config()
        if os.path.exists(path):
            raw = json.load(open(path))
            risk = Risk(**{**asdict(Risk()), **raw.pop("risk", {})})
            c = Config(**{**asdict(Config()), **{k: v for k, v in raw.items() if k != "risk"}, "risk": risk})
        c.risk.validate()
        return c


class State:
    DEFAULT = {"last_bar": 0, "day": "", "day_start_equity": None,
               "consecutive_losses": 0, "halted": "", "in_trade": None}

    def __init__(self, path="state.json"):
        self.path = path
        self.d = dict(self.DEFAULT)
        if os.path.exists(path):
            self.d.update(json.load(open(path)))

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f, indent=1)
        os.replace(tmp, self.path)

    def __getitem__(self, k):
        return self.d[k]

    def __setitem__(self, k, v):
        self.d[k] = v
        self.save()


class Bot:
    def __init__(self, ex, cfg: Config, mode: str, state: State | None = None, stop_file="STOP",
                 journal="trades.csv", sleep=time.sleep):
        self.ex, self.cfg, self.mode, self.st = ex, cfg, mode, state or State()
        self.stop_file, self.journal, self.sleep = stop_file, journal, sleep
        cfg.risk.validate()

    # ------------------------------------------------------------ helpers
    def _day(self, now):
        return datetime.fromtimestamp(now, IST).date().isoformat()

    def _roll_day(self, now):
        d = self._day(now)
        if self.st["day"] != d:
            self.st.d.update({"day": d, "day_start_equity": None})
            self.st.save()

    def _journal(self, row: dict):
        new = not os.path.exists(self.journal)
        with open(self.journal, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)

    def halted(self):
        return self.st["halted"]

    # ------------------------------------------------------------- trade lifecycle
    def _finalize_trade(self, now):
        t = self.st["in_trade"]
        bal = self.ex.balance()
        pnl = bal - t["equity_before"]
        won = pnl > 0
        if abs(pnl) > 1e-9:                      # a zero-pnl "trade" (never filled) is not a loss
            self.st.d["consecutive_losses"] = 0 if won else self.st["consecutive_losses"] + 1
        self.st.d["in_trade"] = None
        if self.st["consecutive_losses"] >= self.cfg.risk.max_consecutive_losses:
            self.st.d["halted"] = (f"{self.st['consecutive_losses']} losses in a row - "
                                   f"review, then `python bot.py reset-halt`")
        self.st.save()
        self._journal({"mode": self.mode, "opened_utc": datetime.fromtimestamp(t["opened"], timezone.utc).isoformat(),
                       "closed_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                       "side": "LONG" if t["side"] == 1 else "SHORT", "kind": t["kind"], "lots": t["lots"],
                       "entry": t["entry"], "stop": t["stop"], "target": t["target"],
                       "equity_before": round(t["equity_before"], 4), "equity_after": round(bal, 4),
                       "pnl_usd": round(pnl, 4), "pnl_pct": round(pnl / t["equity_before"] * 100, 2)})
        log.info("TRADE CLOSED pnl=%+.4f USD (%+.1f%%) equity=%.4f", pnl, pnl / t["equity_before"] * 100, bal)

    def _monitor(self, size, now):
        t = self.st["in_trade"]
        if not t:
            log.warning("position of %s lots exists that this bot did not open - not touching it", size)
            return "foreign_position"
        mark = self.ex.mark_price()
        slip = self.cfg.risk.emergency_stop_slip_pct / 100
        breached = (t["side"] == 1 and mark <= t["stop"] * (1 - slip)) or \
                   (t["side"] == -1 and mark >= t["stop"] * (1 + slip))
        if breached:
            log.error("EMERGENCY: mark %.1f is beyond stop %.1f + buffer - bracket did not fire; flattening",
                      mark, t["stop"])
            self.ex.close_position()
            return "emergency_close"
        return "in_position"

    def _try_enter(self, sig, now):
        r = self.cfg.risk
        side = 1 if sig["side"] == "LONG" else -1
        mark = self.ex.mark_price()
        drift = abs(mark - sig["entry"]) / sig["entry"] * 100
        if drift > r.max_entry_drift_pct:
            log.info("skip: price drifted %.2f%% from signal", drift)
            return "skip_drift"
        equity = self.ex.balance()
        if self.st["day_start_equity"] is None:
            self.st["day_start_equity"] = equity
        dd = (equity / self.st["day_start_equity"] - 1) * 100
        if dd <= -r.daily_loss_limit_pct:
            log.warning("daily loss limit hit (%.1f%%) - no more trades today", dd)
            return "daily_loss_limit"
        plan, why = plan_trade(equity, side, mark, sig["stop"], sig["target"], r,
                               getattr(self.ex, "contract_value", 0.001))
        if plan is None:
            log.info("skip %s signal: %s", sig["side"], why)
            return "skip_risk"
        if r.target_equity_pct > 0:
            sig["target"] = target_price(equity, side, mark, plan.lots, r.target_equity_pct,
                                         getattr(self.ex, "contract_value", 0.001))
        cid = f"s{int(sig['bar_open'])}{'L' if side == 1 else 'S'}"
        self.st.d["in_trade"] = {"side": side, "lots": plan.lots, "entry": mark, "stop": sig["stop"],
                                 "target": sig["target"], "kind": sig["kind"], "opened": now,
                                 "equity_before": equity, "cid": cid}
        self.st.save()                                   # intent is saved BEFORE the order goes out
        log.info("ENTER %s %d lots @~%.1f stop %.1f target %.1f risk %.1f%% equity (%s)",
                 sig["side"], plan.lots, mark, sig["stop"], sig["target"], plan.risk_pct_equity, sig["kind"])
        try:
            self.ex.set_leverage(r.leverage)
            res = self.ex.place_entry(side, plan.lots, sig["stop"], sig["target"], cid)
        except UncertainError as e:
            self.st["halted"] = f"order outcome unknown ({e}) - check the exchange, then reset-halt"
            log.critical(self.st["halted"])
            return "uncertain"
        except DeltaError as e:
            self.st.d["in_trade"] = None
            self.st.save()
            log.error("order rejected: %s", e)
            return "rejected"
        fill = res.get("average_fill_price")
        if fill:
            self.st.d["in_trade"]["entry"] = float(fill)
            self.st.save()
        return "entered"

    # --------------------------------------------------------------- main step
    def step(self, now: float) -> str:
        self._roll_day(now)
        size, _ = self.ex.position()
        if size != 0:
            return self._monitor(size, now)
        t = self.st["in_trade"]
        if t:
            if now - t["opened"] < 30:
                return "pending_fill"
            self._finalize_trade(now)
        if self.halted():
            return "halted"
        if os.path.exists(self.stop_file):
            return "stop_file"
        bar_s = TF_MIN * 60
        closed_open = int(now) // bar_s * bar_s - bar_s            # open time of the last CLOSED candle
        closes_at = closed_open + bar_s
        if now < closes_at + self.cfg.settle_delay:
            return "waiting"
        if closed_open <= self.st["last_bar"]:
            return "already_processed"
        df = self.ex.candles(f"{TF_MIN}m", self.cfg.history_bars, now)
        if len(df) < 700 or int(df["time"].iloc[-1].timestamp()) != closed_open:
            if now > closes_at + self.cfg.stale_give_up:
                log.warning("data still stale for bar %s - skipping it", closed_open)
                self.st["last_bar"] = closed_open
                return "stale_skipped"
            return "stale_retry"
        self.st["last_bar"] = closed_open
        res = S.scan(df.reset_index(drop=True), S.Config())
        sig = res.signals[res.signals["bar"] == len(df) - 1]
        if sig.empty:
            return "no_signal"
        s = sig.iloc[-1].to_dict()
        s["bar_open"] = closed_open
        log.info("SIGNAL %s %s score=%s entry=%.1f stop=%.1f target=%.1f", s["side"], s["kind"], s["score"],
                 s["entry"], s["stop"], s["target"])
        return self._try_enter(s, now)

    def run(self):
        log.info("bot started | mode=%s | leverage=%dx | risk/trade=%.1f%% | STOP file=%s",
                 self.mode, self.cfg.risk.leverage, self.cfg.risk.risk_per_trade_pct, self.stop_file)
        fails = 0
        while True:
            try:
                r = self.step(time.time())
                fails = 0
                if r not in ("waiting", "already_processed", "in_position", "stop_file", "halted"):
                    log.info("step -> %s", r)
            except KeyboardInterrupt:
                log.info("stopped by user. Any open position keeps its stop/target on the exchange.")
                return
            except (DeltaError, UncertainError) as e:
                fails += 1
                log.error("API problem (%d in a row): %s", fails, e)
                if fails >= 5 and not self.halted():
                    self.st["halted"] = "5 API failures in a row - no new trades until reset-halt"
            except Exception:
                log.exception("unexpected error")
                fails += 1
                if fails >= 5 and not self.halted():
                    self.st["halted"] = "5 unexpected errors in a row - no new trades until reset-halt"
            self.sleep(self.cfg.poll_seconds)


# ------------------------------------------------------------------ CLI
def build_exchange(mode: str, cfg: Config):
    if os.environ.get("DELTA_LIVE_CONFIRM") != "I_ACCEPT_THE_RISK":
        sys.exit("Live mode refused: set DELTA_LIVE_CONFIRM=I_ACCEPT_THE_RISK (and read the README first).")
    k, s = os.environ.get("DELTA_API_KEY"), os.environ.get("DELTA_API_SECRET")
    if not (k and s):
        sys.exit("API key/secret not found in environment variables (see README). Never put them in files.")
    return DeltaExchange(DeltaClient(PROD, k, s), DeltaClient(PROD), cfg.symbol, cfg.margin_asset)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "selftest", "status", "flatten", "reset-halt"])
    ap.add_argument("--config", default="config.json")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("bot.log")])
    cfg = Config.load(a.config)
    st = State()
    if a.cmd == "status":
        print(json.dumps(st.d, indent=1))
        return
    if a.cmd == "reset-halt":
        st.d.update({"halted": "", "consecutive_losses": 0})
        st.save()
        print("halt cleared")
        return
    ex = build_exchange("live", cfg)
    if a.cmd == "selftest":
        print("balance:", ex.balance(), cfg.margin_asset)
        print("position (signed lots, entry):", ex.position())
        print("mark price:", ex.mark_price())
        print("last 3 candles:\n", ex.candles("15m", 3, time.time()))
        print("OK - signing, balance, position and price calls all worked.")
        return
    if a.cmd == "flatten":
        ex.close_position()
        print("close order sent")
        return
    Bot(ex, cfg, "live", st).run()


if __name__ == "__main__":
    main()
