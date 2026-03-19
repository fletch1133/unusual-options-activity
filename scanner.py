#!/usr/bin/env python3
"""
Options Unusual Activity Scanner
Fetches real-time options chains and flags unusual activity.
Data is ~15min delayed (free tier standard).
"""

import yfinance as yf
import pandas as pd
import numpy as np
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich import box
from datetime import datetime, timedelta
import sys
import argparse

console = Console(width=160)

# --- Thresholds (tune these to your taste) ---
VOL_OI_THRESHOLD   = 0.5   # volume/OI ratio above this is notable
VOL_OI_HIGH        = 2.0   # very unusual
MIN_VOLUME         = 100   # ignore low-volume noise
MIN_PREMIUM        = 5_000 # minimum total premium ($) to care about
OTM_PCT_MAX        = 0.30  # max % OTM to consider (30% OTM)


def get_options_chain(ticker: str) -> tuple[pd.DataFrame, float]:
    """Fetch full options chain for all available expirations."""
    try:
        stock = yf.Ticker(ticker)
        spot = stock.fast_info.get("last_price") or stock.fast_info.get("regularMarketPrice")
        if not spot:
            hist = stock.history(period="1d")
            spot = float(hist["Close"].iloc[-1]) if not hist.empty else None

        expirations = stock.options
        if not expirations:
            console.print(f"[red]No options data found for {ticker}[/red]")
            return pd.DataFrame(), spot or 0

        all_rows = []
        for exp in expirations:
            try:
                chain = stock.option_chain(exp)
                for opt_type, df in [("CALL", chain.calls), ("PUT", chain.puts)]:
                    df = df.copy()
                    df["type"]       = opt_type
                    df["expiration"] = exp
                    all_rows.append(df)
            except Exception:
                continue

        if not all_rows:
            return pd.DataFrame(), spot or 0

        full = pd.concat(all_rows, ignore_index=True)
        return full, spot or 0

    except Exception as e:
        console.print(f"[red]Error fetching {ticker}: {e}[/red]")
        return pd.DataFrame(), 0


def score_unusual(row: pd.Series, spot: float) -> float:
    """Return a 0–100 unusualness score for an options contract."""
    score = 0.0

    vol   = row.get("volume", 0) or 0
    oi    = row.get("openInterest", 0) or 1
    last  = row.get("lastPrice", 0) or 0
    iv    = row.get("impliedVolatility", 0) or 0
    strike = row.get("strike", 0)

    vol_oi = vol / oi
    premium = vol * last * 100  # total dollars

    # Volume vs OI
    if vol_oi >= VOL_OI_HIGH:
        score += 40
    elif vol_oi >= VOL_OI_THRESHOLD:
        score += 20

    # Raw volume
    if vol > 10_000:
        score += 25
    elif vol > 2_000:
        score += 15
    elif vol > 500:
        score += 5

    # Total premium size
    if premium > 1_000_000:
        score += 25
    elif premium > 100_000:
        score += 15
    elif premium > MIN_PREMIUM:
        score += 5

    # High IV (potential event play)
    if iv > 1.5:
        score += 10
    elif iv > 0.8:
        score += 5

    return min(score, 100)


def classify_moneyness(strike: float, spot: float, opt_type: str) -> str:
    if spot == 0:
        return "?"
    pct = (strike - spot) / spot
    if opt_type == "CALL":
        if pct < -0.02:  return "ITM"
        if pct < 0.02:   return "ATM"
        return f"OTM +{pct:.1%}"
    else:
        if pct > 0.02:   return "ITM"
        if pct > -0.02:  return "ATM"
        return f"OTM {pct:.1%}"


def dte(exp_str: str) -> int:
    try:
        return (datetime.strptime(exp_str, "%Y-%m-%d").date() - datetime.today().date()).days
    except Exception:
        return 0


def filter_and_score(df: pd.DataFrame, spot: float) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    df["volume"]        = pd.to_numeric(df.get("volume", 0), errors="coerce").fillna(0)
    df["openInterest"]  = pd.to_numeric(df.get("openInterest", 1), errors="coerce").fillna(1).replace(0, 1)
    df["lastPrice"]     = pd.to_numeric(df.get("lastPrice", 0), errors="coerce").fillna(0)
    df["impliedVolatility"] = pd.to_numeric(df.get("impliedVolatility", 0), errors="coerce").fillna(0)

    # Basic filters
    df = df[df["volume"] >= MIN_VOLUME]
    total_premium = df["volume"] * df["lastPrice"] * 100
    df = df[total_premium >= MIN_PREMIUM]

    if df.empty:
        return df

    # OTM filter
    if spot > 0:
        otm_mask = abs(df["strike"] - spot) / spot <= OTM_PCT_MAX
        df = df[otm_mask]

    df["score"]      = df.apply(lambda r: score_unusual(r, spot), axis=1)
    df["vol_oi"]     = (df["volume"] / df["openInterest"]).round(2)
    df["premium_$"]  = (df["volume"] * df["lastPrice"] * 100).astype(int)
    df["moneyness"]  = df.apply(lambda r: classify_moneyness(r["strike"], spot, r["type"]), axis=1)
    df["DTE"]        = df["expiration"].apply(dte)

    df = df.sort_values("score", ascending=False)
    return df


def score_color(score: float) -> str:
    if score >= 70: return "bold red"
    if score >= 45: return "bold yellow"
    if score >= 20: return "green"
    return "white"


def build_table(df: pd.DataFrame, ticker: str, spot: float) -> Table:
    title = f"[bold cyan]{ticker}[/bold cyan]  spot=[bold white]${spot:.2f}[/bold white]  — Unusual Options Activity"
    table = Table(title=title, box=box.ROUNDED, show_lines=False, header_style="bold magenta")

    table.add_column("Score",   justify="center", min_width=6,  no_wrap=True)
    table.add_column("Type",    justify="center", min_width=5,  no_wrap=True)
    table.add_column("Strike",  justify="right",  min_width=8,  no_wrap=True)
    table.add_column("Money",   justify="left",   min_width=10, no_wrap=True)
    table.add_column("Exp",     justify="center", min_width=12, no_wrap=True)
    table.add_column("DTE",     justify="center", min_width=4,  no_wrap=True)
    table.add_column("Last $",  justify="right",  min_width=8,  no_wrap=True)
    table.add_column("Volume",  justify="right",  min_width=8,  no_wrap=True)
    table.add_column("OI",      justify="right",  min_width=8,  no_wrap=True)
    table.add_column("Vol/OI",  justify="right",  min_width=7,  no_wrap=True)
    table.add_column("IV",      justify="right",  min_width=6,  no_wrap=True)
    table.add_column("Premium", justify="right",  min_width=13, no_wrap=True)

    for _, row in df.head(30).iterrows():
        score = row["score"]
        c     = score_color(score)
        ptype = "[bold green]CALL[/bold green]" if row["type"] == "CALL" else "[bold red]PUT[/bold red]"
        premium_str = f"${row['premium_$']:,}"

        table.add_row(
            f"[{c}]{score:.0f}[/{c}]",
            ptype,
            f"${row['strike']:.1f}",
            row["moneyness"],
            row["expiration"],
            str(row["DTE"]),
            f"${row['lastPrice']:.2f}",
            f"{int(row['volume']):,}",
            f"{int(row['openInterest']):,}",
            f"{row['vol_oi']:.2f}x",
            f"{row['impliedVolatility']:.0%}",
            f"[{c}]{premium_str}[/{c}]",
        )

    return table


def summary_panel(df: pd.DataFrame, ticker: str) -> Panel:
    if df.empty:
        return Panel(f"[yellow]No unusual activity found for {ticker}[/yellow]")

    calls  = df[df["type"] == "CALL"]
    puts   = df[df["type"] == "PUT"]
    c_prem = calls["premium_$"].sum()
    p_prem = puts["premium_$"].sum()
    ratio  = c_prem / p_prem if p_prem > 0 else float("inf")
    bias   = "[bold green]BULLISH[/bold green]" if ratio > 1.2 else ("[bold red]BEARISH[/bold red]" if ratio < 0.8 else "[yellow]NEUTRAL[/yellow]")

    top = df.iloc[0]
    top_desc = (
        f"[bold]{top['type']} ${top['strike']:.0f} {top['expiration']}[/bold]  "
        f"score={top['score']:.0f}  vol={int(top['volume']):,}  premium=${top['premium_$']:,}"
    )

    text = (
        f"Total call premium : [bold green]${c_prem:>12,.0f}[/bold green]\n"
        f"Total put premium  : [bold red]${p_prem:>12,.0f}[/bold red]\n"
        f"Call/Put ratio     : {ratio:.2f}x  →  {bias}\n"
        f"Top contract       : {top_desc}"
    )
    return Panel(text, title=f"[bold]{ticker} Summary[/bold]", border_style="cyan")


def scan(tickers: list[str], min_volume=None, min_premium=None,
         vol_oi_threshold=None, otm_max=None):
    global MIN_VOLUME, MIN_PREMIUM, VOL_OI_THRESHOLD, OTM_PCT_MAX
    if min_volume       is not None: MIN_VOLUME       = min_volume
    if min_premium      is not None: MIN_PREMIUM      = min_premium
    if vol_oi_threshold is not None: VOL_OI_THRESHOLD = vol_oi_threshold
    if otm_max          is not None: OTM_PCT_MAX      = otm_max

    console.rule("[bold cyan]Options Unusual Activity Scanner[/bold cyan]")
    console.print(f"[dim]Scanning {len(tickers)} ticker(s) — data ~15min delayed  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]\n")

    for ticker in tickers:
        ticker = ticker.upper().strip()
        with console.status(f"[cyan]Fetching options chain for {ticker}...[/cyan]"):
            df, spot = get_options_chain(ticker)

        if df.empty:
            console.print(f"[yellow]Skipping {ticker} — no data.[/yellow]\n")
            continue

        scored = filter_and_score(df, spot)

        if scored.empty:
            console.print(f"[yellow]{ticker}: no contracts passed the activity filters.[/yellow]\n")
            continue

        console.print(build_table(scored, ticker, spot))
        console.print(summary_panel(scored, ticker))
        console.print()


def main():
    parser = argparse.ArgumentParser(
        description="Scan options chains for unusual activity.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scanner.py AAPL TSLA NVDA
  python3 scanner.py SPY QQQ --min-volume 500 --min-premium 50000
  python3 scanner.py AAPL --vol-oi 1.5
        """
    )
    parser.add_argument("tickers", nargs="+", help="Ticker symbols to scan")
    parser.add_argument("--min-volume",  type=int,   default=MIN_VOLUME,      help=f"Min contract volume (default {MIN_VOLUME})")
    parser.add_argument("--min-premium", type=int,   default=MIN_PREMIUM,     help=f"Min total premium $ (default {MIN_PREMIUM})")
    parser.add_argument("--vol-oi",      type=float, default=VOL_OI_THRESHOLD,help=f"Vol/OI threshold (default {VOL_OI_THRESHOLD})")
    parser.add_argument("--otm-max",     type=float, default=OTM_PCT_MAX,     help=f"Max OTM %% (default {OTM_PCT_MAX})")

    args = parser.parse_args()

    scan(
        args.tickers,
        min_volume=args.min_volume,
        min_premium=args.min_premium,
        vol_oi_threshold=args.vol_oi,
        otm_max=args.otm_max,
    )


if __name__ == "__main__":
    main()
