# Crypto Bot: a 5-stage BTCUSD trading bot

A bot that trades Bitcoin (BTCUSD) on **Delta Exchange India**. Every 15 minutes it looks at the latest candle, asks five simple "stages" what they think, and only trades when **at least two agree**.

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

The bot trades only if **2 or more advisors agree** and fewer disagree.

![Vote examples](docs/votes.png)

Every trade gets a **stop-loss** (exit if it goes wrong) and a **take-profit** (exit when it works), placed on the exchange. Only one trade is open at a time.

## Before you run it

- It has **not been proven to make money**. The older 4-stage version made +0.46% per trade over 48 trades, which is too few to be sure. The "2 of 5" rule and the funding stage have **not been backtested**.
- The current `config.json` uses **20x leverage** and a **+20% take-profit**. When I replayed that on past data it **lost money**. Safer: `"target_equity_pct": 0` and 5-10x leverage.
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

Copy `config.example.json` to `config.json` and edit. The important ones:

| Setting | Meaning |
|---|---|
| `leverage` | borrowing multiplier (max 20) |
| `risk_per_trade_pct` | most you can lose on one trade, % of your balance |
| `margin_use_pct` | most of your balance used per trade |
| `target_equity_pct` | take profit when a trade earns this % (0 = use the scanner's own target) |
| `min_votes` | how many advisors must agree (default 2) |

## Built-in safety

![Safety checks](docs/risk_gates.png)

The bot also pauses itself after 4 losses in a row, after a -15% day, or after 5 API errors in a row. It never touches a trade it did not open.

## Not yet checked against the real exchange

These come from Delta's documentation, not a live test. `selftest` checks most of them: signing, balance, position format. The **funding data** (`FUNDING:BTCUSD`) is the least certain; if it fails, that advisor just stays silent.

## Disclaimer

This software is provided "as is", without warranty of any kind. It is for educational and research purposes and is **not financial, investment, legal or tax advice**. Trading leveraged crypto derivatives carries a high risk of total loss, and past or backtested results do not predict future results. The author and contributors accept **no liability** for any loss, damage, missed trade, bug, exchange or API failure, or regulatory consequence arising from its use. You run it entirely at your own risk and are solely responsible for your API keys, your funds, and for complying with the laws, tax rules and exchange terms that apply to you, including whether trading crypto derivatives is permitted where you live. This project is not affiliated with or endorsed by Delta Exchange.

## Files

`bot.py` main loop · `scanner.py` stages 1-4 and the voting · `funding.py` stage 5 · `risk.py` safety limits · `exchanges.py` / `delta_client.py` talk to Delta · `docs/make_images.py` redraws the pictures
