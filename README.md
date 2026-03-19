# unusual-options-activity

A real-time options flow scanner that automatically detects unusual activity across 180+ liquid tickers and surfaces high-conviction contract setups.

## Features

- **Auto-scan 180+ tickers** — scans the most liquid names on the market without manual input
- **Live streaming results** — contracts appear as each ticker finishes scanning
- **Buy signal + reasons** — multi-factor heuristic flags contracts with aligned signals and explains each one
- **Moneyness filter** — quickly filter ITM / ATM / OTM contracts
- **DTE filter** — filter by expiration range with quick chips (0DTE, this week, ≤30d, LEAPS, etc.)
- **Star & track** — star any contract to track its P&L to expiration in the Watchlist tab
- **Ticker modal** — click any ticker for a TradingView chart + key stats (P/E, beta, 52W range, etc.)

## Data pulled per contract

| Field | Description |
|---|---|
| Score | Unusualness score (Vol/OI, volume, premium, IV) |
| Signal | Buy / Watch heuristic based on 9 factors |
| Reasons | Tagged explanation of each signal factor |
| Bid / Ask / Spread% | Liquidity check |
| Delta | Market-implied probability of finishing ITM |
| Vol/OI | Volume ÷ Open Interest — new money vs existing positions |
| Premium | Total dollar flow (Volume × Price × 100) |
| IV | Implied Volatility |
| Stock Chg% | Underlying price move today |

## Setup

```bash
pip install yfinance pandas flask rich numpy
python app.py
```

Open **http://localhost:8888**

## Run

```bash
python app.py
```

Then hit **Auto Scan Market** in the browser.

## Disclaimer

This tool is for informational and educational purposes only. Nothing here constitutes financial advice. Options trading involves substantial risk of loss.
