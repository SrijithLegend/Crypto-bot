# Crypto Bot: a 5-stage BTCUSD trading bot

A bot that trades Bitcoin (BTCUSD) on **Delta Exchange India**. Every 15 minutes it looks at the latest candle, asks five simple "stages" what they think, and by default trades only on the main scanner's signal (the one setup with a measured edge); funding can veto it, or you can require several advisors to agree.

> ⚠️ **It uses real money. There is no practice mode. You can lose everything you put in.** Read the [Disclaimer](#disclaimer) before running it.

![How it works](docs/pipeline.png)

## How it works (in plain words)

Think of five advisors. Each one says **long** (price will go up), **short** (price will go down), or **no opinion**.

| Advisor | Says "long" when... |
|---|---|
| 1. Trend | price keeps making higher highs and higher lows |
| 2. Break of structure | price just closed above an important level |
| 3. Moving averages | the 20/50/200 EMA lines are stacked upward |
| 4. Continuation / reversal | the main scanner finds a full setup |
| 5. Funding | too many traders are short (a crowd that is often wrong) |

The bot has three **entry modes** (`entry_mode` in the config):

| Mode | Trades when... |
|---|---|
| `scanner` (default) | advisor 4 (the scanner) fires. The tested baseline. |
| `gated` | advisor 4 fires and funding does **not** vote the opposite way |
| `vote` | at least `min_votes` (3+) advisors agree, and advisor 4 is one of them |

![Vote examples](docs/votes.png)

Every trade gets a **stop-loss** (exit if it goes wrong) and a **take-profit** (exit when it works), placed on the exchange. Only one trade is open at a time.

## Before you run it

- **Nothing here is proven to make money.** Backtest, Jan-Oct 2026, 15m, no leverage, 2R target:
  - scanner only: 47 trades, 40% wins, +0.50% per trade, but the 95% range is -0.16% to +1.22% (not proven).
  - the old "any 2 of 5 agree" rule: 128 trades, +-0.12% per trade (**it lost money**), so it is no longer allowed (`min_votes` must be 3 or more).
  - these figures come from one 10-month sample and were measured outside this repo; the backtest tooling is not in the repo yet, so they are not reproducible from here.
- A +20%-of-equity take-profit at 20x replayed as a loser, so `target_equity_pct` now defaults to 0 (use the scanner's 2R target) and the bot warns if you set it too small. Defaults are 10x leverage, 8% risk per trade.
- It trades rarely (a handful of times a month).
- With a very small balance, one trade may need more money than you have, so the bot skips everything.
- Your PC must stay on. If it sleeps, only the exchange's stop protects you.

## Quick start

1. Install Python, then in this folder:
   ```
   pip install -r requirements.txt
   python -m pytest -q
   ```
2. On Delta, create an API key with **Read + Trade** only (withdrawals OFF).
3. Set these as environment variables (never put them in a file):
   - `DELTA_API_KEY`
   - `DELTA_API_SECRET`
   - `DELTA_LIVE_CONFIRM=I_ACCEPT_THE_RISK`
4. Check it works: `python bot.py selftest`
5. Delete the file named `STOP` (it blocks trading), then: `python bot.py run`

| Command | What it does |
|---|---|
| `python bot.py run` | start trading |
| `python bot.py status` | show what the bot remembers |
| `python bot.py flatten` | close the open trade now |
| `python bot.py reset-halt` | restart after the bot paused itself |

**Emergency stop:** create a file named `STOP` next to `bot.py`. No new trades will open.

## Settings

Copy `config.example.json` to `config.json` and edit (`config.json` and `STOP` are git-ignored, so your live settings never get committed). The important ones:

| Setting | Meaning |
|---|---|
| `leverage` | borrowing multiplier (max 20) |
| `risk_per_trade_pct` | most you can lose on one trade, % of your balance |
| `margin_use_pct` | most of your balance used per trade |
| `target_equity_pct` | take profit when a trade earns this % (0 = use the scanner's own 2R target, recommended) |
| `entry_mode` | `scanner`, `gated` or `vote` (see above) |
| `min_votes` | `vote` mode only: advisors that must agree (3-5) |

## Built-in safety

![Safety checks](docs/risk_gates.png)

The bot also pauses itself after 4 losses in a row, after a -15% day, or after 5 API errors in a row. It never touches a trade it did not open.

## Not yet checked against the real exchange

These come from Delta's documentation, not a live test. `selftest` checks most of them: signing, balance, position format. The **funding data** (`FUNDING:BTCUSD`) is the least certain; if it fails, that advisor just stays silent.

## Disclaimer

This software is provided "as is", without warranty of any kind. It is for educational and research purposes and is **not financial, investment, legal or tax advice**. Trading leveraged crypto derivatives carries a high risk of total loss, and past or backtested results do not predict future results. The author and contributors accept **no liability** for any loss, damage, missed trade, bug, exchange or API failure, or regulatory consequence arising from its use. You run it entirely at your own risk and are solely responsible for your API keys, your funds, and for complying with the laws, tax rules and exchange terms that apply to you, including whether trading crypto derivatives is permitted where you live. This project is not affiliated with or endorsed by Delta Exchange.

## Files

`bot.py` main loop · `scanner.py` stages 1-4 and the entry modes · `funding.py` stage 5 · `risk.py` safety limits · `exchanges.py` / `delta_client.py` talk to Delta · `docs/make_images.py` redraws the pictures

## Cascade-risk flag (`cascade.py`)

Logs a liquidation-cascade risk score (0-3 per side) from Binance public data. **A risk flag, not an entry signal** - see `claude.md` for the spec and caveats.

    python cascade.py       # appends a reading to cascade_log.csv every 5 min

Run it for weeks, then backtest the log before using it as a filter in the bot (`claude.md` section 6). It is not wired into `bot.py`.
