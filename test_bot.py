import hashlib, hmac, json, os, sys
import numpy as np, pandas as pd, pytest
import scanner as S
from test_scanner import synth
import bot as BOT
from bot import Bot, Config, State
from delta_client import DeltaClient, DeltaError, UncertainError
from exchanges import DeltaExchange, PaperExchange, walk_exit
from risk import MAX_LEVERAGE, Risk, plan_trade, TAKER

BAR = 900
NOW0 = 1_800_000_000 // BAR * BAR + 5 * BAR          # a candle boundary


# ------------------------------------------------------------ fake exchange
class FakeEx:
    contract_value = 0.001
    def __init__(self, df, balance=12.94, mark=None):
        self.df, self.bal, self.pos, self.orders, self.mark = df, balance, 0, [], mark
        self.closed = 0; self.raise_on_order = None
    def candles(self, res, bars, now): return self.df
    def balance(self): return self.bal
    def position(self): return self.pos, 0.0
    def mark_price(self): return self.mark
    def set_leverage(self, lev): self.lev = lev
    def place_entry(self, side, lots, stop, target, cid):
        if self.raise_on_order: raise self.raise_on_order
        self.orders.append(dict(side=side, lots=lots, stop=stop, target=target, cid=cid))
        self.pos = side * lots
        return {"average_fill_price": str(self.mark)}
    def close_position(self): self.closed += 1; self.pos = 0


def signal_df(max_risk_pct=2.5, seed=0):
    """A candle history whose LAST closed bar fires a signal; times aligned so last open = NOW0-BAR."""
    for sd in range(seed, seed + 30):
        df = synth(8000, sd)
        res = S.scan(df, S.Config())
        for s in res.signals.itertuples():
            if s.bar > 800 and s.risk_pct < max_risk_pct:
                d = df.iloc[: s.bar + 1].copy().reset_index(drop=True)
                d["time"] = pd.to_datetime(NOW0 - BAR - (len(d) - 1 - np.arange(len(d))) * BAR, unit="s", utc=True)
                return d, s
    raise RuntimeError("no suitable signal")


def quiet_df():
    d = synth(1500, 3).iloc[:1200].copy().reset_index(drop=True)
    res = S.scan(d, S.Config())
    while len(res.signals) and res.signals.bar.iloc[-1] == len(d) - 1:
        d = d.iloc[:-1]; res = S.scan(d, S.Config())
    d["time"] = pd.to_datetime(NOW0 - BAR - (len(d) - 1 - np.arange(len(d))) * BAR, unit="s", utc=True)
    return d


@pytest.fixture
def mkbot(tmp_path):
    def make(df, risk=None, balance=12.94, mark=None):
        cfg = Config(risk=risk or Risk(max_stop_pct=50, risk_per_trade_pct=50, margin_use_pct=100))
        ex = FakeEx(df, balance, mark if mark is not None else float(df["close"].iloc[-1]))
        b = Bot(ex, cfg, "paper", State(str(tmp_path / "s.json")), stop_file=str(tmp_path / "STOP"),
                journal=str(tmp_path / "t.csv"), sleep=lambda s: None)
        return b, ex
    return make


# ------------------------------------------------------------------ signing / HTTP
class Resp:
    def __init__(self, code=200, j=None, headers=None): self.status_code, self._j, self.headers = code, j or {"success": True, "result": {}}, headers or {}
    def json(self): return self._j

class Sess:
    def __init__(self, resps): self.resps, self.calls = list(resps), []
    def request(self, method, url, data=None, headers=None, timeout=None):
        self.calls.append((method, url, data, headers))
        r = self.resps.pop(0)
        if isinstance(r, Exception): raise r
        return r

def test_signature_string_and_headers():
    s = Sess([Resp()])
    c = DeltaClient("https://x.test", "KEY", "SECRET", session=s, time_fn=lambda: 1_700_000_000)
    c.request("GET", "/positions", params={"product_id": 27}, auth=True)
    m, url, data, h = s.calls[0]
    expect = hmac.new(b"SECRET", b"GET1700000000/v2/positions?product_id=27", hashlib.sha256).hexdigest()
    assert url == "https://x.test/v2/positions?product_id=27"
    assert h["signature"] == expect and h["api-key"] == "KEY" and h["timestamp"] == "1700000000"
    assert h["User-Agent"]                                  # Delta rejects requests without one

def test_post_body_signed_exactly_as_sent():
    s = Sess([Resp()])
    c = DeltaClient("https://x.test", "K", "S", session=s, time_fn=lambda: 1_700_000_000)
    c.request("POST", "/orders", body={"a": 1, "b": "x"}, auth=True)
    _, _, data, h = s.calls[0]
    assert data == '{"a":1,"b":"x"}'
    assert h["signature"] == hmac.new(b"S", ("POST1700000000/v2/orders" + data).encode(), hashlib.sha256).hexdigest()

def test_order_never_retried_on_timeout():
    import requests
    s = Sess([requests.Timeout("boom"), Resp()])
    c = DeltaClient("https://x.test", "K", "S", session=s, sleep_fn=lambda x: None)
    with pytest.raises(UncertainError): c.place_order({"size": 1})
    assert len(s.calls) == 1                                # a 2nd attempt could double the position

def test_get_retries_and_429_waits():
    s = Sess([Resp(429, headers={"X-RATE-LIMIT-RESET": "2000"}), Resp(500), Resp()])
    sl = []
    c = DeltaClient("https://x.test", session=s, sleep_fn=sl.append)
    c.request("GET", "/products/BTCUSD")
    assert len(s.calls) == 3 and sl[0] == 2.0

def test_error_response_raises():
    s = Sess([Resp(400, {"success": False, "error": {"code": "insufficient_margin"}})])
    with pytest.raises(DeltaError, match="insufficient_margin"):
        DeltaClient("https://x.test", session=s).request("GET", "/x")

def test_entry_order_body():
    class Pub:
        def get_product(self, s): return {"id": 27, "contract_value": "0.001", "tick_size": "0.5"}
    sent = []
    class C:
        def place_order(self, body): sent.append(body); return {"average_fill_price": "80000"}
    ex = DeltaExchange(C(), Pub())
    ex.place_entry(-1, 3, 80812.3, 79001.9, "s1799999100S")
    b = sent[0]
    assert b == {"product_id": 27, "size": 3, "side": "sell", "order_type": "market_order",
                 "client_order_id": "s1799999100S", "bracket_stop_loss_price": "80812.5",
                 "bracket_take_profit_price": "79002.0", "bracket_stop_trigger_method": "last_traded_price"}
    ex.place_entry(1, 1, 1, 2, "x" * 50)
    assert len(sent[1]["client_order_id"]) <= 32            # Delta limit

def test_position_parsing_none_and_signed():
    class Pub:
        def get_product(self, s): return {"id": 27}
    class C:
        r = None
        def get_position(self, pid): return self.r
    c = C(); ex = DeltaExchange(c, Pub())
    assert ex.position() == (0, 0.0)
    c.r = {"size": "-3", "entry_price": "80000.5"}
    assert ex.position() == (-3, 80000.5)


# ------------------------------------------------------------------ risk rules
def test_leverage_cap():
    with pytest.raises(ValueError): Risk(leverage=200).validate()
    with pytest.raises(ValueError): Risk(leverage=MAX_LEVERAGE + 1).validate()
    Risk(leverage=MAX_LEVERAGE).validate()

def test_one_lot_at_small_account():
    p, why = plan_trade(12.94, 1, 80000, 79200, 81600, Risk())
    assert p and p.lots == 1 and 5 < p.risk_pct_equity < 8, (p, why)

def test_skip_when_one_lot_too_risky():
    p, why = plan_trade(12.94, 1, 80000, 78400, 83200, Risk())     # 2 % stop -> ~13 % of equity
    assert p is None and "1 lot" in why

def test_skip_stop_wider_than_max():
    p, why = plan_trade(1000, 1, 80000, 77000, 90000, Risk(max_stop_pct=3))
    assert p is None and "wider" in why

def test_liquidation_buffer():
    p, why = plan_trade(1000, 1, 80000, 79200, 81600, Risk(leverage=20))   # liq 4.75 %, stop 1 %
    assert p is not None
    p, why = plan_trade(1000, 1, 80000, 76000, 90000, Risk(leverage=20, max_stop_pct=10))  # stop 5 % > liq/2
    assert p is None and "liquidation" in why

def test_bad_geometry():
    assert plan_trade(100, 1, 100, 101, 102, Risk())[0] is None
    assert plan_trade(100, -1, 100, 99, 98, Risk())[0] is None

def test_size_scales_with_equity_and_respects_risk():
    p, _ = plan_trade(1000, 1, 80000, 79200, 81600, Risk())
    assert p.lots >= 2 and p.risk_pct_equity <= 8.0 and p.margin_usd <= 750


# ------------------------------------------------------------------ bot behaviour
def test_waits_until_candle_closed(mkbot):
    df, _ = signal_df(); b, ex = mkbot(df)
    assert b.step(NOW0 + 1) == "waiting"
    assert ex.orders == []

def test_signal_places_exactly_one_bracketed_order_and_never_repeats(mkbot):
    df, s = signal_df(); b, ex = mkbot(df)
    assert b.step(NOW0 + 6) == "entered"
    o = ex.orders[0]
    assert o["side"] == (1 if s.side == "LONG" else -1) and o["lots"] >= 1
    assert abs(o["stop"] - s.stop) < 1e-9 and abs(o["target"] - s.target) < 1e-9
    ex.pos = 0; b.st.d["in_trade"]["opened"] -= 100            # pretend it already closed
    assert b.step(NOW0 + 20) in ("already_processed",)
    assert len(ex.orders) == 1                                  # same bar is never traded twice

def test_no_signal_no_order(mkbot):
    b, ex = mkbot(quiet_df())
    assert b.step(NOW0 + 6) == "no_signal" and ex.orders == []

def test_stale_data_retries_then_gives_up(mkbot):
    df, _ = signal_df(); df = df.iloc[:-1]; b, ex = mkbot(df)
    assert b.step(NOW0 + 6) == "stale_retry"
    assert b.step(NOW0 + 130) == "stale_skipped" and ex.orders == []
    assert b.step(NOW0 + 140) == "already_processed"

def test_price_drift_skips(mkbot):
    df, s = signal_df(); b, ex = mkbot(df, mark=float(df["close"].iloc[-1]) * 1.01)
    assert b.step(NOW0 + 6) == "skip_drift" and ex.orders == []

def test_risk_limit_skips(mkbot):
    df, _ = signal_df()
    b, ex = mkbot(df, risk=Risk(risk_per_trade_pct=0.01, max_stop_pct=50))
    assert b.step(NOW0 + 6) == "skip_risk" and ex.orders == []

def test_stop_file_blocks(mkbot, tmp_path):
    df, _ = signal_df(); b, ex = mkbot(df)
    (tmp_path / "STOP").write_text("x")
    assert b.step(NOW0 + 6) == "stop_file" and ex.orders == []

def test_daily_loss_limit_and_max_trades(mkbot):
    df, _ = signal_df(); b, ex = mkbot(df)
    b._roll_day(NOW0); b.st.d["day_start_equity"] = 20.0       # equity 12.94 = -35 % today
    assert b.step(NOW0 + 6) == "daily_loss_limit" and ex.orders == []
    b2, ex2 = mkbot(df); b2.st.d.update({"last_bar": 0, "day_start_equity": None}); b2._roll_day(NOW0); b2.st.d["trades_today"] = 3
    assert b2.step(NOW0 + 6) == "max_trades_day" and ex2.orders == []

def test_day_rolls_in_ist(mkbot):
    df, _ = signal_df(); b, ex = mkbot(df)
    b.st.d.update({"day": "1999-01-01", "trades_today": 3})
    b._roll_day(NOW0); assert b.st["trades_today"] == 0 and b.st["day"] != "1999-01-01"

def test_consecutive_losses_halt_and_reset(mkbot):
    df, _ = signal_df(); b, ex = mkbot(df, risk=Risk(max_stop_pct=50, risk_per_trade_pct=50, margin_use_pct=100, max_consecutive_losses=2))
    for i in range(2):
        b.st.d["in_trade"] = {"side": 1, "lots": 1, "entry": 1, "stop": 1, "target": 2, "kind": "x",
                              "opened": NOW0 - 1000, "equity_before": 12.94, "cid": "c"}
        ex.bal = 12.0 - i; ex.pos = 0
        b.step(NOW0 + 6)
    assert "losses in a row" in b.halted()
    assert b.step(NOW0 + BAR * 3 + 6) == "halted"
    assert os.path.exists(b.journal) and len(open(b.journal).read().strip().split("\n")) == 3

def test_zero_pnl_is_not_a_loss(mkbot):
    df, _ = signal_df(); b, ex = mkbot(df)
    b.st.d["in_trade"] = {"side": 1, "lots": 1, "entry": 1, "stop": 1, "target": 2, "kind": "x",
                          "opened": NOW0 - 1000, "equity_before": 12.94, "cid": "c"}
    b._finalize_trade(NOW0); assert b.st["consecutive_losses"] == 0

def test_uncertain_order_halts_and_keeps_intent(mkbot):
    df, _ = signal_df(); b, ex = mkbot(df); ex.raise_on_order = UncertainError("timeout")
    assert b.step(NOW0 + 6) == "uncertain"
    assert "unknown" in b.halted() and b.st["in_trade"] is not None

def test_rejected_order_clears_intent(mkbot):
    df, _ = signal_df(); b, ex = mkbot(df); ex.raise_on_order = DeltaError("insufficient_margin")
    assert b.step(NOW0 + 6) == "rejected"
    assert b.st["in_trade"] is None and b.st["trades_today"] == 0 and not b.halted()

def test_emergency_close_if_bracket_missing(mkbot):
    df, s = signal_df(); b, ex = mkbot(df)
    b.step(NOW0 + 6); t = b.st["in_trade"]
    ex.mark = t["stop"] * (0.99 if t["side"] == 1 else 1.01)    # blew through the stop
    assert b.step(NOW0 + 30) == "emergency_close" and ex.closed == 1
    ex.mark = t["entry"]; ex.pos = t["side"]                    # healthy position -> left alone
    assert b.step(NOW0 + 40) == "in_position" and ex.closed == 1

def test_foreign_position_untouched(mkbot):
    df, _ = signal_df(); b, ex = mkbot(df); ex.pos = 5
    assert b.step(NOW0 + 6) == "foreign_position" and ex.closed == 0 and ex.orders == []

def test_live_requires_confirmation(monkeypatch):
    monkeypatch.delenv("DELTA_LIVE_CONFIRM", raising=False)
    with pytest.raises(SystemExit): BOT.build_exchange("live", Config())

def test_config_rejects_200x(tmp_path):
    p = tmp_path / "c.json"; p.write_text(json.dumps({"risk": {"leverage": 200}}))
    with pytest.raises(ValueError): Config.load(str(p))


# ------------------------------------------------------------------ paper exchange
class PubFake:
    def __init__(self, c1m, mark): self.c1m, self.mark = c1m, mark
    def get_ticker(self, s): return {"mark_price": self.mark}
    def get_candles(self, s, res, start, end):
        d = self.c1m
        t = d["time"].map(lambda x: x.timestamp())
        return d[(t >= start) & (t < end)].reset_index(drop=True)

def c1m(start, rows):
    return pd.DataFrame({"time": pd.to_datetime([start + 60 * i for i in range(len(rows))], unit="s", utc=True),
                         "open": [r[0] for r in rows], "high": [r[1] for r in rows],
                         "low": [r[2] for r in rows], "close": [r[3] for r in rows], "volume": 1.0})

def paper(tmp_path, rows, mark=80000.0, now=None, lev=10, bal=100.0):
    t0 = 1_800_000_000 // 60 * 60
    pub = PubFake(c1m(t0 + 60, rows), mark)
    clock = {"t": t0 + 5}
    ex = PaperExchange(pub, str(tmp_path / "p.json"), start_usd=bal, leverage=lev, time_fn=lambda: clock["t"])
    return ex, clock, t0

def test_paper_target_pnl(tmp_path):
    rows = [(80000, 80100, 79950, 80050)] * 2 + [(80050, 80900, 80000, 80800)]
    ex, clk, t0 = paper(tmp_path, rows)
    ex.place_entry(1, 10, 79200.0, 80800.0, "c")
    entry = 80000.5; clk["t"] = t0 + 60 * 6
    assert ex.position() == (0, 0.0)
    notional = 10 * 0.001 * entry
    exp = 10 * 0.001 * (80800 - entry) - notional * TAKER - 10 * 0.001 * 80800 * TAKER
    assert abs(ex.balance() - (100 + exp)) < 1e-9

def test_paper_stop_first_when_both_touched(tmp_path):
    rows = [(80000, 81000, 79000, 80000)]
    ex, clk, t0 = paper(tmp_path, rows)
    ex.place_entry(1, 10, 79200.0, 80800.0, "c"); clk["t"] = t0 + 200
    assert ex.position() == (0, 0.0) and ex.balance() < 100

def test_paper_liquidation_at_20x_loses_margin(tmp_path):
    rows = [(80000, 80010, 75500, 75600)]      # -5.6 % : beyond the 4.75 % liquidation distance at 20x
    ex, clk, t0 = paper(tmp_path, rows, lev=20)
    ex.place_entry(1, 10, 74000.0, 90000.0, "c"); clk["t"] = t0 + 200
    ex.balance()
    margin = 10 * 0.001 * 80000.5 / 20
    assert abs(ex.st["balance"] - (100 - margin - 10 * 0.001 * 80000.5 * TAKER)) < 1e-9

def test_paper_state_survives_restart(tmp_path):
    rows = [(80000, 80100, 79950, 80050)] * 3
    ex, clk, t0 = paper(tmp_path, rows)
    ex.place_entry(-1, 5, 81000.0, 79000.0, "c")
    ex2 = PaperExchange(ex.public, str(tmp_path / "p.json"), time_fn=lambda: clk["t"])
    assert ex2.position()[0] == -5

def test_walk_exit_gap_through_stop_fills_at_open():
    o = np.array([100., 95.]); h = np.array([100.1, 95.5]); l = np.array([99.9, 94.])
    assert walk_exit(1, 100, 98, 102, 5, o, h, l) == (1, 95.0, "stop")
