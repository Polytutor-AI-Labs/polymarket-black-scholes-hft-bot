# Polymarket HFT Bot Using Black-Scholes

**English** | [中文](README.zh-CN.md)

**Real-time probabilistic trading infrastructure for Polymarket crypto Up/Down markets.**

![Polymarket HFT Bot dashboard](docs/dashboard.jpg)

Polymarket HFT Bot is a research-driven trading platform designed to identify short-term pricing inefficiencies in Polymarket crypto prediction markets.

The system combines:

* Polymarket CLOB order-book data
* Chainlink price feeds
* Multi-exchange crypto prices
* Short-term volatility
* Market resolution mechanics
* A 60-second TWAP probability model for 5m and 15m Up/Down markets
* A Black-Scholes probability model for optional long-dated strike markets
* Fee-aware expected value
* Position and portfolio risk management

The platform continuously estimates the fair probability of an **UP** or **DOWN** outcome and compares that value with the current executable price on Polymarket.

When the difference is large enough to justify fees, slippage, and model uncertainty, the system can generate a trading signal.

> This project is designed primarily for research, strategy development, paper trading, and market-microstructure analysis. Historical or simulated performance does not guarantee future profitability.

---

# Overview

Polymarket crypto Up/Down markets settle based on whether the underlying asset finishes above or below a reference price during a predefined resolution window.

For example:

```text
BTC Up or Down — 5 Minutes

Reference Price:
$110,125

Current Price:
$110,234

Time Remaining:
2m 34s
```

Instead of simply asking:

```text
"Is Bitcoin currently going up?"
```

the bot asks a more useful question:

```text
"What is the probability that the final settlement value
will finish above the reference price?"
```

The estimated probability is then compared against the price offered by Polymarket.

Example:

```text
Model P(UP):          68.2%
Safety Haircut:       -2.0%
Effective P(UP):      66.2%

Polymarket YES Ask:   $0.55
Estimated Fee:        $0.0173
Total Cost:           $0.5673

Estimated Edge:       +9.3%
```

If the expected edge exceeds the configured trading threshold, the strategy may generate a:

```text
BUY YES
```

signal.

---

# Supported Markets

The platform is designed primarily for short-duration cryptocurrency markets.

Typical supported assets include:

| Asset    | Symbol |
| -------- | ------ |
| Bitcoin  | BTC    |
| Ethereum | ETH    |
| Solana   | SOL    |
| XRP      | XRP    |
| Dogecoin | DOGE   |
| BNB      | BNB    |

Primary trading windows:

```text
5 Minute
15 Minute
```

Longer-duration markets can also be used for research and experimentation.

---

# How the Strategy Works

The strategy can be summarized as:

```text
External Crypto Prices
        ↓
Chainlink Price Feed
        ↓
Market Reference Price
        ↓
Short-Term Volatility
        ↓
Settlement Probability Model
        ↓
Polymarket Order Book
        ↓
Fee + Risk Adjustment
        ↓
Estimated Edge
        ↓
Trade / Ignore
```

---

# 1. Reference Price Detection

Each Up/Down market has a reference value used to determine the final result.

Example:

```text
BTC Reference Price
$110,125
```

Accurately identifying this value is critical.

Even a small reference-price error can generate a false trading opportunity.

For this reason, the strategy should prioritize the actual Chainlink reference instead of estimating the reference from a nearby exchange candle.

Conceptually:

```text
Market Start
    ↓
Capture Exact Chainlink Reference
    ↓
Store Reference
    ↓
Use Reference Throughout Market
```

---

# 2. Real-Time Crypto Price Aggregation

The trading engine can monitor multiple exchanges such as:

```text
Binance
Coinbase
Kraken
Bybit
OKX
Bitget
```

Rather than relying completely on a single venue, the system can calculate a composite market price.

For example:

```text
Binance   $110,240
Coinbase  $110,236
Kraken    $110,238
Bybit     $110,242
OKX       $110,235
Bitget    $110,241

Composite Price
≈ $110,239
```

Using multiple venues helps reduce the effect of temporary exchange-specific price anomalies.

---

# 3. Chainlink vs Exchange Price

Centralized exchange markets often react extremely quickly.

Chainlink price updates may follow a slightly different update process.

The system can therefore study the relationship:

```text
Exchange Price
      ↓
Potential Lead
      ↓
Chainlink Price
      ↓
Polymarket Resolution
```

The bot can estimate the current difference between the exchange composite and Chainlink.

Conceptually:

```text
Basis =
Chainlink Price / Exchange Composite - 1
```

This information can be used to estimate where Chainlink may move next.

---

# 4. Settlement-Aware Probability Model

The strategy does not simply predict the next BTC price.

It attempts to estimate:

```text
P(UP)
```

and:

```text
P(DOWN)
```

for the actual Polymarket settlement condition.

Example:

```text
P(UP)   = 68.2%
P(DOWN) = 31.8%
```

The model considers variables such as:

```text
Current Price
Reference Price
Time Remaining
Short-Term Volatility
Observed Settlement Window
Chainlink Price
External Exchange Price
```

That estimate is **not** a Black-Scholes price. The 5-minute and 15-minute markets resolve on a 60-second Chainlink TWAP, so the live model is `prob_up_twap` in `bot/updown.py` (sections 5 and 6). Black-Scholes is a second model, used only for long-dated strike markets, and it is off unless `ENABLE_LONG_MARKETS=true`. See [Black-Scholes model](#black-scholes-model).

---

# 5. Final TWAP Modeling

For markets whose resolution depends on an averaging period, the model should account for the portion of the settlement window that has already occurred.

Example:

```text
Settlement Window:
60 seconds

Observed:
12 seconds

Remaining:
48 seconds
```

The final outcome therefore depends on:

```text
Observed TWAP
+
Expected Future Price Path
```

rather than only the latest spot price.

This becomes especially important near expiration.

---

# 6. Short-Term Volatility

The probability model requires an estimate of uncertainty.

The engine can calculate short-term realized volatility using recent crypto returns.

Conceptually:

```text
Recent 1-Minute Prices
        ↓
Log Returns
        ↓
Standard Deviation
        ↓
Short-Term Volatility Estimate
```

Example dashboard value:

```text
1m Volatility:
0.42%
```

Higher volatility generally increases uncertainty around the final outcome.

---

# Black-Scholes model

The bot has two probability models. They answer different settlement questions, and only one of them is Black-Scholes.

| | 5m and 15m Up/Down (default) | Long-dated strike markets (optional) |
|---|---|---|
| Code | `prob_up_twap` in `bot/updown.py` | `_black_scholes_probability` in `bot/analyzer.py` |
| Question | Will the Chainlink TWAP of the **last 60 seconds** finish at or above the window's opening price? | Will the spot finish above (or below) a dollar strike by an expiry that is days or months away? |
| Model | Normal model of that TWAP, using volatility per square-root-second | Black-Scholes, zero drift: `P(S_T > K) = N(d2)` |
| On by default | Yes | No. Set `ENABLE_LONG_MARKETS=true` |

## What the Up/Down bot does not use

The markets this bot trades by default do not settle on a terminal price versus a distant strike. A classic Black-Scholes `d2` would answer the wrong question. The live path uses the TWAP model in sections 5 and 6, then subtracts the taker fee and a 2% haircut before it compares the result with the Polymarket ask.

## Where Black-Scholes is used

`MarketAnalyzer._analyze_market` runs only when long markets are enabled. `MarketScanner` keeps questions that name an asset and a dollar target, such as "Will Bitcoin be above $100,000 by [date]?". Spot comes from Binance for crypto and from Yahoo Finance for gold, silver, and oil. Time to expiry comes from the market end date.

The price is modeled as a geometric Brownian motion with **no drift**. With interest rate zero, the risk-neutral probability of finishing above the strike is the Black-Scholes term `N(d2)`:

```text
P(S_T > K) = N(d2)

d2 = (ln(S / K) - 0.5 · σ² · T) / (σ · √T)
```

| Symbol | Meaning in this bot |
|---|---|
| `S` | Current spot |
| `K` | Dollar target parsed from the question |
| `T` | Days to expiry / 365 |
| `σ` | Annualized realized volatility from recent daily log returns. If history is missing, a per-asset default is used (for example 65% for BTC, 15% for gold) |
| `N` | Standard normal CDF |

A market whose question is "below" the target uses `1 - P(S_T > K)`.

If the market has already expired (`T = 0`), the probability is `1` or `0` depending on whether spot is already past the strike. If `σ` or `T` is not positive, the function returns `0.5`.

## Barrier markets

Some questions pay if the price **touches** a level at any time ("hit", "reach", "dip"), not only if it finishes there. Those are not a plain Black-Scholes terminal probability. If the level has not been touched yet, the code applies the reflection principle with no drift and uses about `2 · N(d2)`, capped at `0.99`. If spot is already through the level, the probability is set to `0.99`.

## How that probability becomes a trade

The Black-Scholes probability is compared with both the YES price and the NO price on Polymarket. The side with the larger gap is kept only when all of these are true:

* Edge is at least `MIN_EDGE` and no larger than 35% (a larger gap is treated as a bad parse or a bad model, not as free money)
* Win probability is at least `MIN_WIN_PROBABILITY`
* Expected value per USDC is at least `MIN_EXPECTED_VALUE`
* Fractional Kelly size is positive

Expected value per USDC staked is `true_probability / market_price - 1`. Kelly uses net odds `b = 1/price - 1`, then multiplies by `KELLY_FRACTION`.

## What this path is not

`ENABLE_LONG_MARKETS` defaults to `false`. Turning it on trades markets that can lock capital for months, and that path has not been checked with the live Up/Down journal (`calibration.py`). It does not replace the 5-minute and 15-minute TWAP model, and it does not price the contract premium the way an options desk would. It only estimates the probability of finishing on one side of a strike, then compares that probability with the Polymarket price.

---

# 7. Polymarket Order Book

The bot reads executable prices from the Polymarket CLOB.

Example:

```text
YES

Price    Size
0.57     420
0.56     320
0.55     1,250
0.54     800


NO

Price    Size
0.52     500
0.51     950
0.50     1,200
0.49     800
```

The strategy should use executable bid/ask prices rather than relying only on midpoint prices.

This is important because a theoretical opportunity may disappear after:

```text
spread
fees
slippage
latency
limited liquidity
```

are considered.

---

# 8. Fair Value vs Market Price

Suppose the model calculates:

```text
P(UP) = 68.2%
```

A safety adjustment can be applied:

```text
Model Probability:
68.2%

Safety Haircut:
-2.0%

Effective Probability:
66.2%
```

Suppose Polymarket offers YES at:

```text
$0.55
```

After fees:

```text
Total Cost:
$0.5673
```

Estimated edge:

```text
66.2%
-
56.73%
=
+9.47%
```

If the configured minimum edge is:

```text
8%
```

the system can generate:

```text
BUY YES
```

---

# Trading Signal Engine

A trade should only be considered when all required conditions are satisfied.

Example:

```text
Exact reference available       ✓
Market is active                ✓
Enough time remains             ✓
Order-book liquidity available  ✓
Price within allowed range      ✓
Risk limits available           ✓
Estimated edge > threshold      ✓
```

Then:

```text
SIGNAL

BUY
YES / UP

Projected Edge
+9.3%
```

Otherwise:

```text
NO TRADE
```

---

# Position Sizing

The system can use fractional Kelly sizing.

The purpose is not simply to maximize individual trade size.

Instead, sizing takes into account:

```text
Estimated Probability
Entry Cost
Available Balance
Expected Edge
Portfolio Exposure
Maximum Position Size
```

A fractional Kelly configuration can reduce the risk created by imperfect model probabilities.

For example:

```text
Full Kelly:
12%

Configured Kelly Fraction:
25%

Actual Suggested Allocation:
3%
```

---

# Portfolio Risk Management

Crypto prediction markets are highly correlated.

Holding:

```text
BTC YES
ETH YES
SOL YES
BNB YES
```

does not represent four independent trades.

A broad crypto market move could affect all four simultaneously.

The platform therefore tracks:

```text
Total Exposure
Open Positions
Per-Market Exposure
Per-Asset Exposure
Maximum Position Size
Portfolio Exposure Limit
Pending Orders
```

Example:

```text
Balance:
$10,000

Current Exposure:
12.4%

Maximum Exposure:
25%
```

---

# Exit Logic

The platform can support several exit methods.

## Fair-Value Exit

Suppose:

```text
Model Fair Value:
$0.62

Polymarket Bid:
$0.70
```

If Polymarket is now offering significantly more than the model believes the position is worth, the bot can close the trade.

Conceptually:

```text
Market becomes expensive
relative to model
        ↓
SELL
```

---

## Profit Lock

Positions can optionally be closed after reaching a configured return.

Example:

```text
Entry:
$0.40

Current Net Value:
$0.53

Return:
+32.5%
```

If the configured profit-lock threshold is:

```text
30%
```

the position can be closed.

---

## Hold Until Resolution

The bot can also hold a position until settlement.

At resolution:

```text
Winning Share → $1
Losing Share  → $0
```

This removes the need for another market exit but introduces full settlement risk.

---

# Dashboard

The web dashboard is designed to give users one interface for monitoring strategy behavior. The layout is shown at the top of this page.

Main navigation:

```text
Dashboard
Markets
Trading
Positions
Analytics
Backtesting
Live Logs
Settings
```

---

# Dashboard Overview

The main dashboard displays important strategy statistics.

Example:

```text
Total P&L
+$324.50

Return
+3.25%

Win Rate
58.7%

Open Positions
3

Portfolio Exposure
12.4%

Active Markets
6 / 6
```

---

# Market Monitor

Users can switch between different market durations.

```text
5m
15m
4h
```

And monitor assets such as:

```text
BTC
ETH
SOL
XRP
DOGE
BNB
```

Each market card can display:

```text
Current Price
Price Change
Market Duration
Market Status
Number of Active Markets
```

---

# Real-Time Price Chart

The primary market chart can display multiple data sources simultaneously.

For example:

```text
BTC Exchange Price
Estimated Chainlink
Actual Chainlink
Reference Price
```

This allows users to visually inspect:

```text
exchange movement
Chainlink response
reference distance
settlement window
```

in real time.

---

# Model Probability Panel

The model section displays the current estimate:

```text
P(UP)
68.2%

P(DOWN)
31.8%
```

Additional information:

```text
Volatility
0.42%

Time Remaining
2m 34s

Observed TWAP
12 / 60 seconds
```

---

# Trading Panel

Before execution, the interface displays the complete reasoning behind the signal.

Example:

```text
Signal
BUY YES

Model Probability
68.2%

Safety Haircut
-2.0%

Effective Probability
66.2%

Polymarket Ask
$0.55

Estimated Fee
1.73%

Total Cost
$0.5673

Estimated Edge
+9.3%
```

The user can then choose between:

```text
Manual Execution
```

or:

```text
Automatic Execution
```

depending on configuration.

---

# Open Positions

Users can monitor all active positions.

Example:

| Market  | Side | Size | Entry |    P&L |
| ------- | ---: | ---: | ----: | -----: |
| ETH 5m  |  YES |  120 |  0.48 | +18.2% |
| SOL 5m  |   NO |   80 |  0.62 |  -7.1% |
| BTC 15m |  YES |  150 |  0.51 | +12.5% |

---

# Recent Trades

The dashboard also maintains execution history.

Example:

| Time     | Market | Side | Price | Size |    P&L |
| -------- | ------ | ---- | ----: | ---: | -----: |
| 12:03:12 | BTC 5m | BUY  |  0.55 |  100 |      — |
| 11:58:41 | ETH 5m | SELL |  0.66 |   80 | +30.2% |
| 11:45:22 | SOL 5m | BUY  |  0.62 |   80 |      — |

---

# Analytics

A serious trading system should evaluate the model continuously.

Useful metrics include:

```text
Brier Score
Calibration Error
Prediction Accuracy
Expected Value
Realized P&L
Win Rate
Average Winner
Average Loser
Slippage
Fill Rate
Signal Frequency
Edge at Entry
Edge After Latency
```

---

# Model Calibration

The platform should compare:

```text
Our Model
vs
Polymarket Market Probability
```

This is extremely important.

A model generating many signals does not automatically mean the model contains useful information.

For example:

```text
Model:
P(UP) = 70%

Market:
P(UP) = 56%
```

The important question is not:

```text
"Is there a 14% difference?"
```

The important question is:

```text
"Which probability is better calibrated
against actual future outcomes?"
```

The Analytics section should make that visible.

---

# Backtesting

The platform can support historical strategy analysis.

However, prediction-market backtesting requires extra care.

A backtest should ideally use synchronized:

```text
Polymarket Order-Book Updates
CEX Trades
CEX Order Books
Chainlink Updates
Reference Prices
Settlement Results
```

Minute-level Polymarket data should not be treated as equivalent to second-level or millisecond-level executable market prices.

Otherwise, stale-price bias can create unrealistic historical profits.

---

# Why Backtests Can Be Misleading

Consider:

```text
12:03:00
Polymarket YES = $0.42

12:03:35
BTC moves sharply upward
```

If a backtest incorrectly combines:

```text
12:03:35 BTC price

with

12:03:00 Polymarket price
```

it may calculate:

```text
Model Probability:
64%

Polymarket:
42%

Apparent Edge:
22%
```

But the real Polymarket price at 12:03:35 may already have moved to:

```text
$0.63
```

The actual edge may therefore be almost zero.

This is why realistic timestamp synchronization is essential for HFT-style research.

---

# Paper Trading

Paper Trading mode is recommended before any live execution.

Paper mode should simulate:

```text
Real Order Book
Real Spread
Real Fees
Execution Delay
Slippage
Liquidity Limits
Position Sizing
Exit Rules
```

rather than simply assuming every signal fills at the desired price.

Example dashboard mode:

```text
Bot Mode:
Paper Trading
```

---

# Live Trading

Live trading should only be enabled after:

```text
Strategy Validation
Execution Testing
API Validation
Risk Validation
Paper Trading
Model Calibration
```

are completed.

Recommended progression:

```text
Research
   ↓
Backtest
   ↓
Historical Order-Book Replay
   ↓
Paper Trading
   ↓
Small-Capital Live Testing
   ↓
Performance Review
   ↓
Gradual Scaling
```

---

# Live Logs

The Live Logs page provides visibility into bot behavior.

Example:

```text
[12:03:10] BTC reference captured: 110125.24

[12:03:11] BTC current Chainlink: 110234.18

[12:03:11] Model P(UP): 0.682

[12:03:11] Polymarket YES ask: 0.55

[12:03:11] Effective edge: +9.3%

[12:03:11] BUY signal generated

[12:03:12] Order submitted

[12:03:12] Position opened
```

Useful logs include:

```text
Market discovery
Price feed updates
Reference capture
Probability calculations
Trading signals
Order execution
Risk rejection
Position updates
Exit signals
Errors
Connection status
```

---

# System Architecture

A simplified architecture:

```text
             ┌──────────────────┐
             │ Binance          │
             │ Coinbase         │
             │ Kraken           │
             │ Bybit            │
             │ OKX              │
             │ Bitget           │
             └────────┬─────────┘
                      │
                      ▼
             ┌──────────────────┐
             │ Price Aggregator │
             └────────┬─────────┘
                      │
             ┌────────▼─────────┐
             │ Chainlink Feed   │
             └────────┬─────────┘
                      │
                      ▼
            ┌────────────────────┐
            │ Probability Engine │
            └─────────┬──────────┘
                      │
              ┌───────▼────────┐
              │ Polymarket CLOB│
              └───────┬────────┘
                      │
                      ▼
              ┌────────────────┐
              │ Signal Engine  │
              └───────┬────────┘
                      │
              ┌───────▼────────┐
              │ Risk Manager   │
              └───────┬────────┘
                      │
                      ▼
              ┌────────────────┐
              │ Order Executor │
              └───────┬────────┘
                      │
                      ▼
              ┌────────────────┐
              │ Position Mgr   │
              └───────┬────────┘
                      │
                      ▼
              ┌────────────────┐
              │ Web Dashboard  │
              └────────────────┘
```

---

# Suggested Project Structure

```text
polymarket-hft/
│
├── backend/
│   ├── feeds/
│   │   ├── chainlink/
│   │   ├── polymarket/
│   │   └── exchanges/
│   │
│   ├── strategy/
│   │   ├── probability/
│   │   ├── signals/
│   │   └── calibration/
│   │
│   ├── execution/
│   │   ├── orders/
│   │   ├── positions/
│   │   └── risk/
│   │
│   ├── analytics/
│   └── api/
│
├── frontend/
│   ├── dashboard/
│   ├── markets/
│   ├── trading/
│   ├── positions/
│   ├── analytics/
│   ├── backtesting/
│   ├── logs/
│   └── settings/
│
├── research/
├── tests/
├── docs/
└── README.md
```

---

# Recommended Configuration

Example configuration:

```env
# Trading

TRADING_MODE=paper

MIN_EDGE=0.08

MODEL_HAIRCUT=0.02

KELLY_FRACTION=0.25

MAX_PORTFOLIO_EXPOSURE=0.25

MAX_OPEN_POSITIONS=10


# Strategy

ENABLE_5M=true

ENABLE_15M=true

REQUIRE_EXACT_REFERENCE=true


# Assets

BTC_ENABLED=true

ETH_ENABLED=true

SOL_ENABLED=true

XRP_ENABLED=true

DOGE_ENABLED=true

BNB_ENABLED=true
```

Do not commit private keys, API secrets, wallet credentials, or sensitive configuration values to GitHub.

---

# Security

Trading infrastructure should follow strict credential management practices.

Never store:

```text
Private Keys
Seed Phrases
Exchange API Secrets
Wallet Authentication Secrets
```

directly in source code.

Use:

```text
Environment Variables
Secret Managers
Restricted API Permissions
Dedicated Trading Wallets
Withdrawal-Disabled API Keys
```

where possible.

---

# Current Strategy Status

This repository should not present the strategy as guaranteed profitable.

The underlying strategy is an active research system.

Historical simulations may show promising results, while live or realistic execution can produce significantly different outcomes because of:

```text
Market Efficiency
Latency
Slippage
Fees
Model Error
Stale Data
Changing Market Conditions
Liquidity
Correlation
```

One of the primary goals of the platform is therefore not only to execute trades, but also to continuously determine whether the strategy actually has measurable predictive edge.

---

# What We Are Researching

Future strategy research may include:

```text
CEX Order-Flow Imbalance

Order-Book Imbalance

Microprice

Trade Aggressor Flow

Cross-Exchange Lead/Lag

Chainlink Update Prediction

Polymarket Order-Book Dynamics

Short-Term Momentum

Liquidation Data

Perpetual Futures Basis

CVD

OFI

Volatility Regimes

Machine-Learning Probability Models
```

The objective is to determine whether external market information can consistently reach the trading engine before that information is fully incorporated into Polymarket prices.

---

# Roadmap

## Phase 1 — Infrastructure

* [x] Real-time market monitoring
* [x] Chainlink integration
* [x] Multi-exchange prices
* [x] Polymarket order book
* [x] Probability modeling
* [x] Paper-trading engine
* [x] Position management

## Phase 2 — Web Platform

* [ ] Professional trading dashboard
* [ ] Market explorer
* [ ] Position management UI
* [ ] Trade history
* [ ] Live strategy logs
* [ ] Model analytics
* [ ] Backtesting interface
* [ ] User settings

## Phase 3 — Research

* [ ] Order-flow model
* [ ] Order-book imbalance
* [ ] Cross-exchange latency analysis
* [ ] Chainlink lead/lag analysis
* [ ] Probability calibration
* [ ] Regime detection
* [ ] Strategy comparison

## Phase 4 — Advanced Trading

* [ ] Automated execution
* [ ] Multi-strategy support
* [ ] Advanced risk management
* [ ] Portfolio optimization
* [ ] Strategy performance attribution

---

# Who Is This Project For?

This project may be useful for:

```text
Prediction-market researchers
Quantitative traders
Polymarket developers
Crypto trading-bot developers
Market-microstructure researchers
Data scientists
Algorithmic traders
```

It can also serve as infrastructure for testing new Polymarket trading strategies.

---

# Important Principle

The central philosophy of this project is:

> A difference between a model and the market is not automatically an opportunity.

A real trading edge exists only when the model provides information that is:

```text
More Accurate
+
Available Earlier
+
Executable After Fees
+
Executable After Slippage
+
Repeatable Over Time
```

than the information already reflected in the market.

---

# Disclaimer

This software is provided for educational, research, and experimental purposes.

Prediction markets and cryptocurrency trading involve substantial financial risk.

Nothing in this repository should be considered:

```text
Financial Advice
Investment Advice
A Profit Guarantee
A Recommendation to Trade
```

Users are responsible for understanding applicable regulations, platform rules, market risks, wallet security, execution risks, and potential financial losses.

Always validate a strategy independently before using real capital.

---

# Contributions

Research, bug reports, strategy discussions, and improvements are welcome.

Potential contribution areas include:

```text
Market Data
Execution
Backtesting
Probability Models
UI / UX
Risk Management
Analytics
Performance Optimization
Testing
Documentation
```

---

# Final Goal

The long-term goal of Polymarket HFT Bot is not simply to create another automated trading script.

The goal is to build a professional research and trading environment where users can:

```text
Observe Markets
        ↓
Understand Signals
        ↓
Test Strategies
        ↓
Measure Real Edge
        ↓
Paper Trade
        ↓
Analyze Performance
        ↓
Improve Models
        ↓
Deploy Carefully
```

with transparent data, realistic execution assumptions, and measurable risk.
