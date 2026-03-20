#!/usr/bin/env python3
"""
Options Unusual Activity Scanner
- Auto-scans 100+ liquid names for unusual flow
- Star any contract to track it to expiration
- Watchlist shows P&L, current price, and final result
"""

from flask import Flask, Response, render_template_string, request, jsonify
import yfinance as yf
import pandas as pd
import json, os, time, random
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")

# How many expiration dates to scan per ticker.
# Most unusual activity is near-term — 6 covers 0DTE through ~2 months.
MAX_EXPIRATIONS = 6

# ── Ticker universe (~180 most liquid options names) ───────────────────────────
DEFAULT_TICKERS = [
    # Broad market ETFs
    "SPY","QQQ","IWM","DIA","MDY","VTI","EFA","EEM","HYG","LQD","TLT","TBT",
    "GLD","SLV","GDX","GDXJ","USO","UNG","UVXY","VXX","VIXY",
    "SQQQ","TQQQ","SPXU","SPXL","SOXL","SOXS","LABU","LABD","ARKK",
    # Sector ETFs
    "XLF","XLE","XLK","XLV","XLI","XLU","XLP","XLRE","XLB","XLY","XLC",
    "XBI","IBB","SMH","KRE","IAT","GDX","KWEB",
    # Mega cap tech
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","AVGO","ORCL",
    "NFLX","ADBE","INTC","AMD","QCOM","TXN","MU","AMAT","LRCX","KLAC",
    "MRVL","ARM","TSM","ASML","SNPS","CDNS","MPWR","ONTO","WOLF",
    # Cloud / software
    "CRM","NOW","SNOW","DDOG","NET","CRWD","ZS","PANW","FTNT","OKTA",
    "MDB","CFLT","GTLB","PATH","TEAM","HUBS","ZM","DOCN","ESTC",
    # Consumer internet / fintech
    "UBER","LYFT","ABNB","DASH","COIN","HOOD","SQ","PYPL","AFRM","UPST","SOFI",
    # Financials
    "JPM","BAC","GS","MS","C","WFC","V","MA","AXP","BLK","SCHW","CME","ICE",
    # Healthcare / biotech
    "JNJ","PFE","MRNA","LLY","ABBV","BMY","GILD","BIIB","REGN","VRTX",
    "UNH","CVS","HUM","CI","ISRG","MDT","ABT","TMO","DHR","A",
    # Consumer
    "WMT","COST","TGT","AMZN","NKE","SBUX","MCD","CMG","YUM","DPZ",
    "HD","LOW","TSCO","DECK","LULU","TPR",
    # Energy
    "XOM","CVX","OXY","SLB","HAL","MPC","PSX","VLO","DVN","COP","EOG","PXD",
    # Industrials / defense
    "BA","CAT","DE","GE","GEV","LMT","RTX","NOC","HII","TDG","UPS","FDX",
    # Travel / leisure
    "UAL","DAL","AAL","JETS","MAR","HLT","RCL","CCL","NCLH",
    # Real estate / rates
    "SPG","AMT","CCI","EQIX","PLD","O","VNO",
    # Crypto / speculative
    "MSTR","COIN","MARA","RIOT","HUT","CLSK","BTBT",
    # Meme / high-volatility
    "GME","AMC","BBBY","SPCE","WISH",
    # Comms / media
    "T","VZ","DIS","CMCSA","PARA","WBD","NFLX","SPOT","SNAP","PINS","RBLX",
    # Autos
    "F","GM","TSLA","RIVN","LCID","NIO","LI","XPEV",
    # Other large caps
    "BRK-B","AAPL","COST","PG","KO","PEP","PM","MO","MDLZ","CL",
]
DEFAULT_TICKERS = list(dict.fromkeys(DEFAULT_TICKERS))  # dedupe

MIN_VOLUME    = 200
MIN_PREMIUM   = 25_000
VOL_OI_THRESH = 0.5
OTM_PCT_MAX   = 0.35


# ── Watchlist persistence ──────────────────────────────────────────────────────
def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE) as f:
            return json.load(f)
    return []

def save_watchlist(wl):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(wl, f, indent=2)

def contract_id(row):
    return f"{row['ticker']}_{row['type']}_{row['strike']}_{row['expiration']}"


# ── Options fetching helpers ───────────────────────────────────────────────────
def dte(exp_str):
    try:
        return (datetime.strptime(exp_str, "%Y-%m-%d").date() - datetime.today().date()).days
    except Exception:
        return 0

def classify_moneyness(strike, spot, opt_type):
    if not spot: return "?"
    pct = (strike - spot) / spot
    if opt_type == "CALL":
        if pct < -0.02: return "ITM"
        if pct < 0.02:  return "ATM"
        return f"OTM +{pct:.1%}"
    else:
        if pct > 0.02:  return "ITM"
        if pct > -0.02: return "ATM"
        return f"OTM {pct:.1%}"

def buy_signal(vol, oi, last, iv, d_dte, moneyness, premium, price_chg_pct, bid, ask, delta, week52_pos):
    """
    Returns (score 0-100, reasons list).
    Each reason is [sentiment, short_label, detail].
    sentiment: '+' good, '-' bad, '~' neutral.
    This is a heuristic — not financial advice.
    """
    pts = 0
    reasons = []  # [sentiment, label, detail]

    vol_oi = vol / max(oi, 1)
    mk = moneyness.split()[0]

    # ── 1. Vol/OI — new money vs existing positions ───────────────────────
    if vol_oi >= 5:
        pts += 28; reasons.append(['+', f'Vol/OI {vol_oi:.1f}x', 'Massive new positioning — someone is very convinced'])
    elif vol_oi >= 3:
        pts += 22; reasons.append(['+', f'Vol/OI {vol_oi:.1f}x', 'Strong new aggressive positioning'])
    elif vol_oi >= 1.5:
        pts += 15; reasons.append(['+', f'Vol/OI {vol_oi:.1f}x', 'New money entering — directional bet'])
    elif vol_oi >= 0.75:
        pts += 8;  reasons.append(['~', f'Vol/OI {vol_oi:.1f}x', 'Some new activity but mostly existing positions'])
    else:
        reasons.append(['-', f'Vol/OI {vol_oi:.1f}x', 'Mostly existing positions — less directional conviction'])

    # ── 2. DTE — time decay risk vs opportunity ───────────────────────────
    if 14 <= d_dte <= 45:
        pts += 22; reasons.append(['+', f'{d_dte}DTE', 'Ideal window — enough time, manageable theta decay'])
    elif 7 <= d_dte < 14:
        pts += 12; reasons.append(['~', f'{d_dte}DTE', 'Short window — high risk/reward, theta accelerating'])
    elif 1 <= d_dte < 7:
        pts += 4;  reasons.append(['-', f'{d_dte}DTE', 'Very short — near-expiry lottery, extreme theta'])
    elif d_dte == 0:
        reasons.append(['-', '0DTE', 'Expires today — binary outcome, max theta risk'])
    elif 45 < d_dte <= 90:
        pts += 16; reasons.append(['~', f'{d_dte}DTE', 'Enough time to develop, moderate theta cost'])
    elif d_dte > 90:
        pts += 10; reasons.append(['~', f'{d_dte}DTE', 'LEAPS — low theta but high capital cost'])

    # ── 3. Moneyness — leverage vs probability tradeoff ───────────────────
    if mk == 'ATM':
        pts += 20; reasons.append(['+', 'ATM', 'At the money — maximum delta sensitivity and leverage'])
    elif mk == 'OTM':
        try:
            raw = moneyness.split()[-1].strip('+-%')
            pct = abs(float(raw)) / 100
            if pct <= 0.04:
                pts += 17; reasons.append(['+', f'OTM {moneyness.split()[-1]}', 'Near-OTM — high leverage, realistic move needed'])
            elif pct <= 0.10:
                pts += 10; reasons.append(['~', f'OTM {moneyness.split()[-1]}', 'Moderate OTM — needs a notable move to pay off'])
            elif pct <= 0.20:
                pts += 4;  reasons.append(['-', f'OTM {moneyness.split()[-1]}', 'Deep OTM — low probability, needs big move'])
            else:
                reasons.append(['-', f'OTM {moneyness.split()[-1]}', 'Very far OTM — low probability play'])
        except Exception:
            pts += 6; reasons.append(['~', 'OTM', 'Out of the money'])
    elif mk == 'ITM':
        pts += 9; reasons.append(['~', 'ITM', 'In the money — higher probability but less leverage'])

    # ── 4. Premium size — institutional conviction signal ─────────────────
    if premium >= 2_000_000:
        pts += 22; reasons.append(['+', f'${premium/1e6:.1f}M premium', 'Very heavy institutional flow — major conviction'])
    elif premium >= 750_000:
        pts += 17; reasons.append(['+', f'${premium/1e3:.0f}K premium', 'Large institutional-sized bet'])
    elif premium >= 200_000:
        pts += 11; reasons.append(['+', f'${premium/1e3:.0f}K premium', 'Significant premium — notable conviction'])
    elif premium >= 50_000:
        pts += 5;  reasons.append(['~', f'${premium/1e3:.0f}K premium', 'Moderate premium size'])
    else:
        reasons.append(['~', f'${premium/1e3:.0f}K premium', 'Smaller premium — lower institutional weight'])

    # ── 5. IV — are we overpaying for the option? ─────────────────────────
    if iv <= 0.30:
        pts += 12; reasons.append(['+', f'IV {iv*100:.0f}%', 'Low IV — option is relatively cheap to buy'])
    elif iv <= 0.55:
        pts += 8;  reasons.append(['+', f'IV {iv*100:.0f}%', 'Moderate IV — reasonable premium to pay'])
    elif iv <= 0.85:
        pts += 3;  reasons.append(['~', f'IV {iv*100:.0f}%', 'Elevated IV — paying above-average premium'])
    elif iv <= 1.30:
        pts -= 3;  reasons.append(['-', f'IV {iv*100:.0f}%', 'High IV — expensive, event already partially priced in'])
    else:
        pts -= 8;  reasons.append(['-', f'IV {iv*100:.0f}%', 'Very high IV — likely event-driven, premium is expensive'])

    # ── 6. Bid/ask spread — liquidity, ability to exit ───────────────────
    if bid > 0 and ask > 0:
        sp = (ask - bid) / ((bid + ask) / 2)
        if sp <= 0.04:
            pts += 7;  reasons.append(['+', f'Spread {sp*100:.1f}%', 'Very tight spread — highly liquid, easy to exit'])
        elif sp <= 0.12:
            pts += 4;  reasons.append(['~', f'Spread {sp*100:.1f}%', 'Reasonable spread — adequate liquidity'])
        elif sp <= 0.25:
            pts -= 2;  reasons.append(['-', f'Spread {sp*100:.1f}%', 'Wide spread — liquidity concern, harder to exit at fair price'])
        else:
            pts -= 6;  reasons.append(['-', f'Spread {sp*100:.1f}%', 'Very wide spread — illiquid, significant slippage risk'])

    # ── 7. Delta — market-implied probability of finishing ITM ────────────
    if delta and delta != 0:
        prob = abs(delta) * 100
        if prob >= 50:
            pts += 10; reasons.append(['+', f'Δ {delta:.2f}', f'~{prob:.0f}% market-implied chance of finishing ITM'])
        elif prob >= 35:
            pts += 6;  reasons.append(['~', f'Δ {delta:.2f}', f'~{prob:.0f}% market-implied chance of finishing ITM'])
        elif prob >= 20:
            pts += 2;  reasons.append(['~', f'Δ {delta:.2f}', f'~{prob:.0f}% market-implied chance — needs big move'])
        else:
            pts -= 3;  reasons.append(['-', f'Δ {delta:.2f}', f'~{prob:.0f}% chance ITM — low probability lottery'])

    # ── 8. Stock momentum — is it already moving the right way? ──────────
    if price_chg_pct:
        if abs(price_chg_pct) >= 3:
            pts += 7; reasons.append(['+', f'Stock {price_chg_pct:+.1f}%', 'Strong price momentum today — directional move in play'])
        elif abs(price_chg_pct) >= 1.5:
            pts += 4; reasons.append(['+', f'Stock {price_chg_pct:+.1f}%', 'Noticeable move today — momentum supporting direction'])
        elif abs(price_chg_pct) >= 0.5:
            pts += 2; reasons.append(['~', f'Stock {price_chg_pct:+.1f}%', 'Mild movement today'])

    # ── 9. 52-week position — context on where stock is ──────────────────
    if week52_pos is not None:
        if week52_pos >= 0.85:
            pts += 4; reasons.append(['~', f'52W pos {week52_pos*100:.0f}%', 'Near 52-week highs — momentum or resistance depending on direction'])
        elif week52_pos <= 0.15:
            pts += 4; reasons.append(['~', f'52W pos {week52_pos*100:.0f}%', 'Near 52-week lows — potential bounce or continued weakness'])
        else:
            reasons.append(['~', f'52W pos {week52_pos*100:.0f}%', 'Mid-range on 52-week scale'])

    return max(0, min(pts, 100)), reasons


def score_unusual(vol, oi, last, iv):
    score = 0.0
    oi = oi or 1
    vol_oi  = vol / oi
    premium = vol * last * 100
    if vol_oi >= 2.0:              score += 40
    elif vol_oi >= VOL_OI_THRESH:  score += 20
    if vol > 10_000:   score += 25
    elif vol > 2_000:  score += 15
    elif vol > 500:    score += 5
    if premium > 1_000_000:   score += 25
    elif premium > 100_000:   score += 15
    elif premium > MIN_PREMIUM: score += 5
    if iv > 1.5:   score += 10
    elif iv > 0.8: score += 5
    return min(score, 100)

def scan_ticker(ticker, retries=3):
    for attempt in range(retries):
        try:
            # tiny jitter — just enough to stagger parallel workers
            time.sleep(random.uniform(0.02, 0.12))

            stock = yf.Ticker(ticker)
            info  = stock.fast_info

            spot       = info.get("last_price") or info.get("regularMarketPrice")
            prev_close = info.get("previous_close") or info.get("regularMarketPreviousClose")
            if not spot:
                h = stock.history(period="2d")
                if not h.empty:
                    spot = float(h["Close"].iloc[-1])
                    prev_close = float(h["Close"].iloc[-2]) if len(h) > 1 else spot
            price_chg_pct = ((spot - prev_close) / prev_close * 100) if spot and prev_close and prev_close > 0 else 0

            # 52-week position (0=at low, 1=at high)
            try:
                week52_high = info.get("year_high") or 0
                week52_low  = info.get("year_low")  or 0
                week52_pos  = round((spot - week52_low) / (week52_high - week52_low), 2) if week52_high > week52_low else 0.5
            except Exception:
                week52_pos = 0.5

            expirations = stock.options
            if not expirations:
                return ticker, [], spot or 0

            # ── KEY SPEED WIN: only scan near-term expirations ──────────────
            # Unusual activity is almost always in the first few expirations.
            # Scanning all 30+ for SPY was the main bottleneck.
            expirations = expirations[:MAX_EXPIRATIONS]

            # Fetch expirations — each gets its own Ticker instance (thread-safe)
            all_rows = []
            def fetch_exp(exp):
                try:
                    c = yf.Ticker(ticker).option_chain(exp)
                    rows = []
                    for opt_type, df in [("CALL", c.calls), ("PUT", c.puts)]:
                        df = df.copy()
                        df["type"] = opt_type
                        df["expiration"] = exp
                        rows.append(df)
                    return rows
                except Exception:
                    return []

            with ThreadPoolExecutor(max_workers=min(len(expirations), 3)) as inner_pool:
                for result in inner_pool.map(fetch_exp, expirations):
                    all_rows.extend(result)

            if not all_rows:
                return ticker, [], spot or 0

            full = pd.concat(all_rows, ignore_index=True)
            for col in ["volume","openInterest","lastPrice","impliedVolatility","bid","ask"]:
                full[col] = pd.to_numeric(full.get(col, 0), errors="coerce").fillna(0)
            full["openInterest"] = full["openInterest"].replace(0, 1)

            # delta/gamma if present
            for col in ["delta","gamma","theta","vega"]:
                if col in full.columns:
                    full[col] = pd.to_numeric(full[col], errors="coerce").fillna(0)
                else:
                    full[col] = 0.0

            full = full[full["volume"] >= MIN_VOLUME]
            prem = full["volume"] * full["lastPrice"] * 100
            full = full[prem >= MIN_PREMIUM]

            if spot > 0:
                full = full[abs(full["strike"] - spot) / spot <= OTM_PCT_MAX]

            if full.empty:
                return ticker, [], spot or 0

            rows = []
            for _, r in full.iterrows():
                vol   = int(r["volume"])
                oi    = int(r["openInterest"])
                last  = float(r["lastPrice"])
                iv    = float(r["impliedVolatility"])
                bid   = float(r["bid"])
                ask   = float(r["ask"])
                delta = float(r.get("delta", 0))
                gamma = float(r.get("gamma", 0))
                theta = float(r.get("theta", 0))
                d     = dte(r["expiration"])
                mness = classify_moneyness(r["strike"], spot, r["type"])
                prem_val = int(vol * last * 100)
                s     = score_unusual(vol, oi, last, iv)
                if s < 20:
                    continue
                spread = round(ask - bid, 2) if ask > bid else None
                spread_pct = round((ask - bid) / ((bid + ask) / 2) * 100, 1) if bid > 0 and ask > 0 else None
                bs, breasons = buy_signal(vol, oi, last, iv, d, mness, prem_val, price_chg_pct, bid, ask, delta, week52_pos)

                rows.append({
                    "ticker":       ticker,
                    "score":        int(s),
                    "buy_signal":   int(bs),
                    "buy_reasons":  breasons,
                    "type":         r["type"],
                    "strike":       float(r["strike"]),
                    "spot":         round(spot, 2),
                    "price_chg":    round(price_chg_pct, 2),
                    "week52_pos":   week52_pos,
                    "moneyness":    mness,
                    "expiration":   r["expiration"],
                    "dte":          d,
                    "last":         round(last, 2),
                    "bid":          round(bid, 2),
                    "ask":          round(ask, 2),
                    "spread":       spread,
                    "spread_pct":   spread_pct,
                    "volume":       vol,
                    "oi":           oi,
                    "vol_oi":       round(vol / oi, 2),
                    "iv":           round(iv, 4),
                    "delta":        round(delta, 3),
                    "gamma":        round(gamma, 4),
                    "theta":        round(theta, 3),
                    "premium":      prem_val,
                })

            rows.sort(key=lambda x: x["score"], reverse=True)
            return ticker, rows[:25], spot or 0

        except Exception as e:
            err = str(e)
            if "Too Many Requests" in err or "Rate limit" in err or "429" in err:
                wait = (attempt + 1) * 1.5 + random.uniform(0.5, 1.5)
                time.sleep(wait)
                continue
            return ticker, [], 0

    return ticker, [], 0


def fetch_current_option_price(ticker, opt_type, strike, expiration):
    """Fetch the current last price for a specific contract."""
    try:
        stock = yf.Ticker(ticker)
        spot = stock.fast_info.get("last_price") or stock.fast_info.get("regularMarketPrice") or 0
        chain = stock.option_chain(expiration)
        df = chain.calls if opt_type == "CALL" else chain.puts
        df = df[df["strike"] == strike]
        if df.empty:
            return None, spot, None
        row = df.iloc[0]
        price = float(row.get("lastPrice", 0) or 0)
        iv    = float(row.get("impliedVolatility", 0) or 0)
        return price, spot, iv
    except Exception:
        return None, 0, None


def compute_result(entry):
    """Compute current status and P&L for a watchlist entry."""
    days_left = dte(entry["expiration"])
    expired   = days_left < 0
    today     = datetime.today().date()

    current_price, current_spot, current_iv = fetch_current_option_price(
        entry["ticker"], entry["type"], entry["strike"], entry["expiration"]
    )

    entry_price = entry.get("entry_price", 0) or 0
    pnl_pct = None
    pnl_dollar = None

    if current_price is not None and entry_price > 0:
        pnl_dollar = (current_price - entry_price) * 100  # per contract
        pnl_pct    = ((current_price - entry_price) / entry_price) * 100

    # Final result for expired contracts
    result = entry.get("result", "PENDING")
    if expired:
        strike = entry["strike"]
        # Use current spot or saved exit_spot — don't require option price (expired options have no quote)
        spot_for_result = current_spot if (current_spot and current_spot > 0) else entry.get("exit_spot", 0)
        if spot_for_result and spot_for_result > 0:
            if entry["type"] == "CALL":
                itm = spot_for_result >= strike
            else:
                itm = spot_for_result <= strike
            result = "WIN" if itm else "EXPIRED_WORTHLESS"

    return {
        "current_price":  current_price,
        "current_spot":   current_spot,
        "current_iv":     current_iv,
        "pnl_pct":        pnl_pct,
        "pnl_dollar":     pnl_dollar,
        "days_left":      days_left,
        "expired":        expired,
        "result":         result,
        "moneyness_now":  classify_moneyness(entry["strike"], current_spot, entry["type"]) if current_spot else "?",
    }


# ── Stock info endpoint ────────────────────────────────────────────────────────
@app.route("/stock-info/<ticker>")
def stock_info(ticker):
    try:
        t    = yf.Ticker(ticker.upper())
        info = t.info
        hist = t.history(period="1y")

        price   = info.get("regularMarketPrice") or info.get("currentPrice") or 0
        prev    = info.get("regularMarketPreviousClose") or info.get("previousClose") or price
        chg     = price - prev
        chg_pct = (chg / prev * 100) if prev else 0

        def fmtnum(n):
            if n is None: return "—"
            if n >= 1e12: return f"${n/1e12:.2f}T"
            if n >= 1e9:  return f"${n/1e9:.2f}B"
            if n >= 1e6:  return f"${n/1e6:.2f}M"
            return str(n)

        return jsonify({
            "ticker":      ticker.upper(),
            "name":        info.get("shortName") or info.get("longName") or ticker,
            "price":       round(price, 2),
            "change":      round(chg, 2),
            "change_pct":  round(chg_pct, 2),
            "market_cap":  fmtnum(info.get("marketCap")),
            "pe":          round(info.get("trailingPE") or 0, 2) or "—",
            "fwd_pe":      round(info.get("forwardPE") or 0, 2) or "—",
            "eps":         info.get("trailingEps") or "—",
            "week52_high": info.get("fiftyTwoWeekHigh") or "—",
            "week52_low":  info.get("fiftyTwoWeekLow") or "—",
            "avg_vol":     fmtnum(info.get("averageVolume")).replace("$","") if info.get("averageVolume") else "—",
            "beta":        round(info.get("beta") or 0, 2) or "—",
            "div_yield":   f"{round((info.get('dividendYield') or 0)*100, 2)}%" if info.get("dividendYield") else "—",
            "sector":      info.get("sector") or "—",
            "industry":    info.get("industry") or "—",
            "description": (info.get("longBusinessSummary") or "")[:300] + ("…" if len(info.get("longBusinessSummary") or "") > 300 else ""),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── SSE scan stream ────────────────────────────────────────────────────────────
@app.route("/stream-scan")
def stream_scan():
    custom = request.args.get("tickers", "").strip()
    tickers = [t.upper().strip() for t in custom.split(",") if t.strip()] if custom else DEFAULT_TICKERS

    starred_ids = {contract_id(e) for e in load_watchlist()}

    def generate():
        yield f"data: {json.dumps({'type':'start','total':len(tickers)})}\n\n"
        done = 0
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(scan_ticker, t): t for t in tickers}
            for future in as_completed(futures):
                ticker, rows, spot = future.result()
                done += 1
                # tag already-starred rows
                for r in rows:
                    r["starred"] = contract_id(r) in starred_ids
                payload = {"type":"ticker","ticker":ticker,"spot":spot,
                           "rows":rows,"done":done,"total":len(tickers),
                           "empty": len(rows) == 0}
                yield f"data: {json.dumps(payload)}\n\n"
        elapsed = round(time.time() - t0, 1)
        print(f"[scan] {len(tickers)} tickers in {elapsed}s")
        yield f"data: {json.dumps({'type':'done','elapsed':elapsed})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


# ── Watchlist API ──────────────────────────────────────────────────────────────
@app.route("/star", methods=["POST"])
def star():
    data  = request.get_json()
    row   = data["row"]
    cid   = contract_id(row)
    wl    = load_watchlist()
    ids   = [contract_id(e) for e in wl]

    if cid in ids:
        wl = [e for e in wl if contract_id(e) != cid]
        save_watchlist(wl)
        return jsonify({"action":"unstarred","id":cid})
    else:
        entry = {
            "id":           cid,
            "ticker":       row["ticker"],
            "type":         row["type"],
            "strike":       row["strike"],
            "expiration":   row["expiration"],
            "starred_at":   datetime.now().isoformat(),
            "entry_price":  row["last"],
            "entry_spot":   row["spot"],
            "entry_score":  row["score"],
            "entry_vol_oi": row["vol_oi"],
            "entry_volume": row["volume"],
            "entry_premium":row["premium"],
            "entry_iv":     row["iv"],
            "result":       "PENDING",
            "notes":        "",
        }
        wl.append(entry)
        save_watchlist(wl)
        return jsonify({"action":"starred","id":cid})


@app.route("/watchlist")
def watchlist():
    return jsonify(load_watchlist())


@app.route("/watchlist/refresh", methods=["POST"])
def watchlist_refresh():
    """Re-fetch current prices for all watchlist entries."""
    wl = load_watchlist()
    updated = []
    for entry in wl:
        live = compute_result(entry)
        entry.update(live)
        # persist result + exit spot for expired contracts
        if live["expired"] and live["result"] != "PENDING":
            entry["result"] = live["result"]
            if live.get("current_spot") and live["current_spot"] > 0 and not entry.get("exit_spot"):
                entry["exit_spot"] = live["current_spot"]
        updated.append(entry)
    save_watchlist(updated)
    return jsonify(updated)


@app.route("/watchlist/note", methods=["POST"])
def watchlist_note():
    data = request.get_json()
    wl   = load_watchlist()
    for entry in wl:
        if entry["id"] == data["id"]:
            entry["notes"] = data["notes"]
    save_watchlist(wl)
    return jsonify({"ok": True})


@app.route("/watchlist/remove", methods=["POST"])
def watchlist_remove():
    data = request.get_json()
    wl   = [e for e in load_watchlist() if e["id"] != data["id"]]
    save_watchlist(wl)
    return jsonify({"ok": True})


# ── HTML ───────────────────────────────────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Options Flow Scanner</title>
<style>
:root {
  --bg: #0a0e14; --surf: #0f1318; --surf2: #181d25; --surf3: #1e242e;
  --border: #252c38; --text: #cdd6e0; --muted: #5c6b7a; --muted2: #7a8fa3;
  --cyan: #4da8f7; --green: #37c561; --red: #f0534a; --yellow: #e0a020;
  --purple: #b07ef5; --orange: #f07840;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, 'Segoe UI', sans-serif; font-size: 13px; }

/* ── Header ── */
header {
  background: var(--surf);
  border-bottom: 1px solid var(--border);
  padding: 0 24px;
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.logo { font-size: 15px; font-weight: 700; color: var(--cyan); display: flex; align-items: center; gap: 8px; letter-spacing: .3px; }
.logo-dot { width: 8px; height: 8px; background: var(--green); border-radius: 50%; box-shadow: 0 0 6px var(--green); animation: pulse 2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
.header-meta { display: flex; align-items: center; gap: 18px; font-size: 11px; color: var(--muted2); }
.header-meta span { display: flex; align-items: center; gap: 4px; }

/* ── Nav ── */
.nav { display: flex; background: var(--surf); border-bottom: 1px solid var(--border); padding: 0 24px; }
.nav-tab { padding: 12px 16px; cursor: pointer; color: var(--muted2); font-size: 13px; border-bottom: 2px solid transparent; transition: all .15s; display: flex; align-items: center; gap: 6px; }
.nav-tab:hover { color: var(--text); }
.nav-tab.active { color: var(--cyan); border-bottom-color: var(--cyan); }
.page { display: none; } .page.active { display: block; }

/* ── Toolbar ── */
.toolbar { padding: 12px 24px; background: var(--surf); border-bottom: 1px solid var(--border); display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.btn { padding: 8px 18px; border-radius: 7px; font-size: 13px; font-weight: 600; cursor: pointer; border: none; transition: all .15s; display: inline-flex; align-items: center; gap: 6px; }
.btn-primary { background: var(--cyan); color: #050810; }
.btn-primary:hover { background: #6bbfff; }
.btn-primary:disabled { opacity: .4; cursor: not-allowed; }
.btn-danger { background: rgba(240,83,74,.15); color: var(--red); border: 1px solid rgba(240,83,74,.3); }
.btn-danger:hover { background: rgba(240,83,74,.25); }
.btn-ghost { background: var(--surf3); color: var(--text); border: 1px solid var(--border); }
.btn-ghost:hover { border-color: var(--muted2); }
.btn-sm { padding: 4px 10px; font-size: 11px; }
.ticker-input-wrap { display: flex; align-items: center; gap: 8px; margin-left: 8px; padding-left: 12px; border-left: 1px solid var(--border); }
.ticker-input-wrap label { font-size: 11px; color: var(--muted2); white-space: nowrap; }
input.ti { background: var(--surf2); border: 1px solid var(--border); color: var(--text); padding: 7px 12px; border-radius: 7px; font-size: 13px; width: 270px; outline: none; transition: border-color .15s; }
input.ti:focus { border-color: var(--cyan); }

/* ── Filter Panel ── */
.filter-panel { background: var(--surf); border-bottom: 1px solid var(--border); padding: 0 24px; }
.filter-row { display: flex; align-items: center; gap: 6px; padding: 8px 0; flex-wrap: wrap; border-bottom: 1px solid var(--border); }
.filter-row:last-child { border-bottom: none; }
.filter-section-label { font-size: 10px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; min-width: 64px; }
.fi { display: flex; align-items: center; gap: 5px; }
.fi label { font-size: 11px; color: var(--muted2); white-space: nowrap; }
.fi input, .fi select { background: var(--surf2); border: 1px solid var(--border); color: var(--text); padding: 5px 8px; border-radius: 5px; font-size: 12px; width: 80px; outline: none; transition: border-color .15s; }
.fi input:focus, .fi select:focus { border-color: var(--cyan); }
.filter-divider { width: 1px; height: 20px; background: var(--border); margin: 0 4px; }

/* Moneyness toggle buttons */
.money-toggles { display: flex; gap: 4px; }
.money-btn { padding: 5px 12px; border-radius: 5px; font-size: 11px; font-weight: 600; cursor: pointer; border: 1px solid var(--border); background: var(--surf2); color: var(--muted2); transition: all .15s; }
.money-btn:hover { border-color: var(--muted2); color: var(--text); }
.money-btn.active-itm { background: rgba(176,126,245,.15); border-color: rgba(176,126,245,.5); color: var(--purple); }
.money-btn.active-atm { background: rgba(77,168,247,.15); border-color: rgba(77,168,247,.5); color: var(--cyan); }
.money-btn.active-otm { background: rgba(92,107,122,.15); border-color: rgba(92,107,122,.4); color: var(--muted2); }

/* DTE quick chips */
.dte-chips { display: flex; gap: 4px; flex-wrap: wrap; }
.dte-chip { padding: 4px 10px; border-radius: 12px; font-size: 11px; cursor: pointer; border: 1px solid var(--border); background: var(--surf2); color: var(--muted2); transition: all .15s; white-space: nowrap; }
.dte-chip:hover { border-color: var(--cyan); color: var(--cyan); }
.dte-chip.active { background: rgba(77,168,247,.15); border-color: var(--cyan); color: var(--cyan); font-weight: 600; }

/* ── Progress + Status ── */
#pb-wrap { height: 2px; background: var(--border); display: none; }
#pb { height: 2px; background: linear-gradient(90deg, var(--cyan), var(--purple)); width: 0; transition: width .4s; }
#status-bar { padding: 8px 24px; font-size: 12px; background: var(--bg); border-bottom: 1px solid var(--border); min-height: 34px; display: flex; align-items: center; gap: 10px; }
.status-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }

/* ── Stats row ── */
#stats-row { display: none; gap: 0; border-bottom: 1px solid var(--border); }
.stat-card { padding: 12px 20px; border-right: 1px solid var(--border); flex: 1; min-width: 110px; }
.stat-card:last-child { border-right: none; }
.stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .8px; margin-bottom: 4px; }
.stat-value { font-size: 20px; font-weight: 700; line-height: 1; }
.stat-sub { font-size: 10px; color: var(--muted2); margin-top: 3px; }

/* ── Table ── */
#tw { padding: 0; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; white-space: nowrap; }
thead th { background: var(--surf); color: var(--muted2); font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .7px; padding: 10px 14px; border-bottom: 1px solid var(--border); cursor: pointer; user-select: none; text-align: right; position: sticky; top: 0; z-index: 1; }
thead th.left { text-align: left; }
thead th:hover { color: var(--text); }
tbody tr { border-bottom: 1px solid rgba(37,44,56,.6); transition: background .1s; }
tbody tr:hover { background: var(--surf); }
td { padding: 9px 14px; text-align: right; font-size: 12px; }
td.left { text-align: left; }

/* Score badges */
.badge { display: inline-flex; align-items: center; border-radius: 5px; padding: 2px 8px; font-weight: 700; font-size: 11px; gap: 3px; }
.s-hot  { background: rgba(240,83,74,.15);  color: var(--red);    border: 1px solid rgba(240,83,74,.3); }
.s-warm { background: rgba(224,160,32,.15); color: var(--yellow); border: 1px solid rgba(224,160,32,.3); }
.s-cool { background: rgba(55,197,97,.15);  color: var(--green);  border: 1px solid rgba(55,197,97,.3); }

/* Type badges */
.type-badge { display: inline-flex; align-items: center; gap: 4px; padding: 3px 9px; border-radius: 5px; font-size: 11px; font-weight: 700; }
.type-call { background: rgba(55,197,97,.12); color: var(--green); border: 1px solid rgba(55,197,97,.25); }
.type-put  { background: rgba(240,83,74,.12); color: var(--red);   border: 1px solid rgba(240,83,74,.25); }

/* Moneyness badges */
.m-badge { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.m-itm { background: rgba(176,126,245,.15); color: var(--purple); border: 1px solid rgba(176,126,245,.3); }
.m-atm { background: rgba(77,168,247,.15);  color: var(--cyan);   border: 1px solid rgba(77,168,247,.3); }
.m-otm { background: rgba(92,107,122,.1);   color: var(--muted2); border: 1px solid rgba(92,107,122,.2); }

.tkr { color: var(--cyan); font-weight: 700; font-size: 13px; cursor: pointer; }
.tkr:hover { text-decoration: underline; }
.spot-sub { font-size: 10px; color: var(--muted); }
.voi-h { color: var(--red); font-weight: 700; }
.voi-w { color: var(--yellow); }
.p-h { color: var(--red); font-weight: 700; }
.p-w { color: var(--yellow); }

.star-btn { background: none; border: none; cursor: pointer; font-size: 15px; padding: 2px 5px; line-height: 1; border-radius: 4px; transition: all .12s; color: var(--muted); }
.star-btn:hover { background: var(--surf3); color: var(--yellow); transform: scale(1.15); }
.star-btn.starred { color: var(--yellow); }

/* Buy signal — intentionally subtle */
.sig-strong { display:inline-flex;align-items:center;gap:3px;font-size:10px;font-weight:600;color:#5aaa6a;background:rgba(55,197,97,.08);border:1px solid rgba(55,197,97,.2);border-radius:4px;padding:2px 7px; }
.sig-watch  { display:inline-flex;align-items:center;gap:3px;font-size:10px;font-weight:500;color:#7a9ab0;background:transparent;border:1px solid rgba(122,154,176,.15);border-radius:4px;padding:2px 7px; }
.sig-none   { color:var(--border);font-size:11px;padding:0 6px; }

/* Reason tags */
.reasons-wrap { display:flex;flex-wrap:wrap;gap:3px;max-width:320px;white-space:normal; }
.rtag { display:inline-flex;align-items:center;gap:3px;font-size:10px;padding:2px 6px;border-radius:4px;cursor:default;line-height:1.3; }
.rtag-pos { background:rgba(55,197,97,.08); color:#5aaa6a; border:1px solid rgba(55,197,97,.18); }
.rtag-neg { background:rgba(240,83,74,.08); color:#d06060; border:1px solid rgba(240,83,74,.18); }
.rtag-neu { background:rgba(92,107,122,.07); color:var(--muted2); border:1px solid rgba(92,107,122,.15); }
.rtag .ri  { font-size:8px; }

/* ── Empty state ── */
#empty { text-align: center; padding: 70px 24px; color: var(--muted2); }
.empty-icon { font-size: 42px; margin-bottom: 14px; }
.empty-title { font-size: 16px; font-weight: 600; color: var(--text); margin-bottom: 8px; }
.empty-sub { font-size: 13px; line-height: 1.7; }

/* ── Watchlist ── */
.wl-toolbar { padding: 12px 24px; display: flex; gap: 8px; align-items: center; border-bottom: 1px solid var(--border); background: var(--surf); flex-wrap: wrap; }
.wl-wrap { padding: 0; overflow-x: auto; }
.result-win  { color: var(--green); font-weight: 700; }
.result-loss { color: var(--red); font-weight: 700; }
.result-pending { color: var(--yellow); }
.result-expired { color: var(--muted2); }
.pnl-pos { color: var(--green); font-weight: 700; }
.pnl-neg { color: var(--red); font-weight: 700; }
.note-input { background: var(--surf2); border: 1px solid var(--border); color: var(--text); padding: 4px 8px; border-radius: 5px; font-size: 11px; width: 100%; outline: none; }
.note-input:focus { border-color: var(--cyan); }

/* ── Watchlist Cards ── */
.wl-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; padding: 20px 24px; }
.wl-card { background: var(--surf); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; transition: border-color .15s; }
.wl-card:hover { border-color: var(--muted2); }
.wl-card.card-win  { border-left: 3px solid var(--green); }
.wl-card.card-loss { border-left: 3px solid var(--red); }
.wl-card.card-pending { border-left: 3px solid var(--yellow); }
.wl-card.card-expired { border-left: 3px solid var(--muted2); }
.card-header { padding: 12px 14px 10px; display: flex; align-items: flex-start; justify-content: space-between; border-bottom: 1px solid var(--border); }
.card-ticker { font-size: 18px; font-weight: 800; color: var(--cyan); cursor: pointer; }
.card-ticker:hover { text-decoration: underline; }
.card-meta { font-size: 11px; color: var(--muted2); margin-top: 2px; }
.card-outcome { text-align: right; }
.outcome-badge { display: inline-block; padding: 5px 10px; border-radius: 6px; font-size: 12px; font-weight: 700; letter-spacing: .3px; }
.outcome-win  { background: rgba(55,197,97,.15); color: var(--green); border: 1px solid rgba(55,197,97,.3); }
.outcome-loss { background: rgba(240,83,74,.15); color: var(--red); border: 1px solid rgba(240,83,74,.3); }
.outcome-pending { background: rgba(224,160,32,.12); color: var(--yellow); border: 1px solid rgba(224,160,32,.25); }
.outcome-expired { background: var(--surf2); color: var(--muted2); border: 1px solid var(--border); }
.card-body { padding: 12px 14px; }
.card-snapshot { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin-bottom: 10px; }
.snap-item { background: var(--surf2); border-radius: 6px; padding: 7px 9px; }
.snap-label { font-size: 10px; color: var(--muted2); text-transform: uppercase; letter-spacing: .4px; margin-bottom: 2px; }
.snap-val { font-size: 13px; font-weight: 600; color: var(--text); }
.card-pnl { display: flex; gap: 10px; align-items: center; padding: 8px 10px; background: var(--surf2); border-radius: 6px; margin-bottom: 10px; font-size: 13px; }
.card-footer { padding: 0 14px 12px; display: flex; gap: 8px; align-items: center; }
.wl-view-toggle { display: flex; gap: 4px; margin-left: auto; }
.view-btn { background: var(--surf2); border: 1px solid var(--border); color: var(--muted2); padding: 4px 10px; border-radius: 5px; font-size: 11px; cursor: pointer; }
.view-btn.active { background: var(--surf3); border-color: var(--cyan); color: var(--cyan); }

/* ── Sort indicators ── */
.sort-desc::after { content: " ↓"; color: var(--cyan); font-size: 10px; }
.sort-asc::after  { content: " ↑"; color: var(--cyan); font-size: 10px; }

/* ── Spinner ── */
.spinner { display: inline-block; width: 11px; height: 11px; border: 2px solid var(--border); border-top-color: var(--cyan); border-radius: 50%; animation: spin .7s linear infinite; vertical-align: middle; margin-right: 5px; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Modal ── */
.modal-overlay { display: none; position: fixed; inset: 0; z-index: 1000; background: rgba(0,0,0,.75); backdrop-filter: blur(6px); }
.modal-box { position: absolute; top: 4%; left: 50%; transform: translateX(-50%); width: min(980px,96vw); max-height: 92vh; overflow-y: auto; background: var(--surf); border: 1px solid var(--border); border-radius: 14px; box-shadow: 0 24px 80px rgba(0,0,0,.6); }
.modal-header { display: flex; align-items: center; justify-content: space-between; padding: 18px 22px; border-bottom: 1px solid var(--border); }
.modal-close { background: var(--surf2); border: 1px solid var(--border); color: var(--muted2); width: 28px; height: 28px; border-radius: 6px; cursor: pointer; font-size: 14px; display: flex; align-items: center; justify-content: center; transition: all .15s; }
.modal-close:hover { background: var(--surf3); color: var(--text); }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(155px,1fr)); gap: 10px; padding: 16px 22px; }
.stat-tile { background: var(--surf2); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; }
.tile-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .7px; margin-bottom: 5px; }
.tile-value { font-size: 14px; font-weight: 700; color: var(--text); }

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--muted); }
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-dot"></div>
    Options Flow Scanner
  </div>
  <div class="header-meta">
    <span>~15min delayed</span>
    <span id="clock"></span>
    <span id="contract-count" style="color:var(--cyan);font-weight:600">0 contracts flagged</span>
  </div>
</header>

<div class="nav">
  <div class="nav-tab active" onclick="showTab('scanner',this)">📡 Live Scanner</div>
  <div class="nav-tab" onclick="showTab('watchlist',this)">⭐ Watchlist <span id="wl-badge" style="background:var(--cyan);color:#050810;border-radius:10px;padding:1px 7px;font-size:10px;font-weight:700;display:none;margin-left:4px">0</span></div>
</div>

<!-- ═══════════ SCANNER ═══════════ -->
<div id="page-scanner" class="page active">

  <div class="toolbar">
    <button class="btn btn-primary" id="scan-btn" onclick="startScan()">▶&nbsp; Auto Scan Market</button>
    <button class="btn btn-danger"  id="stop-btn" onclick="stopScan()" style="display:none">■&nbsp; Stop</button>
    <button class="btn btn-ghost"   onclick="clearResults()">Clear</button>
    <div class="ticker-input-wrap">
      <label>Specific tickers:</label>
      <input class="ti" id="custom-tickers" placeholder="AAPL TSLA NVDA  (blank = full scan)">
    </div>
  </div>

  <!-- Filter Panel -->
  <div class="filter-panel">

    <!-- Row 1: Contract filters -->
    <div class="filter-row">
      <span class="filter-section-label">Contract</span>
      <div class="fi"><label>Call / Put</label>
        <select id="f-type" onchange="applyFilter()">
          <option value="ALL">All Types</option>
          <option value="CALL">Calls only</option>
          <option value="PUT">Puts only</option>
        </select>
      </div>
      <div class="filter-divider"></div>
      <span class="fi" style="gap:6px"><label>Moneyness</label></span>
      <div class="money-toggles">
        <button class="money-btn" id="m-all" onclick="setMoney('ALL')" style="background:rgba(77,168,247,.15);border-color:rgba(77,168,247,.5);color:var(--cyan)">All</button>
        <button class="money-btn" id="m-itm" onclick="setMoney('ITM')">ITM</button>
        <button class="money-btn" id="m-atm" onclick="setMoney('ATM')">ATM</button>
        <button class="money-btn" id="m-otm" onclick="setMoney('OTM')">OTM</button>
      </div>
      <div class="filter-divider"></div>
      <div class="fi"><label>Min Score</label><input id="f-score" type="number" value="45" style="width:60px" onchange="applyFilter()"></div>
      <div class="filter-divider"></div>
      <div class="fi" style="gap:8px">
        <label>Show</label>
        <button id="sig-all" class="dte-chip active" onclick="setSigFilter('all',this)">All contracts</button>
        <button id="sig-watch" class="dte-chip" onclick="setSigFilter('watch',this)" title="buy + watch signals">Signals only</button>
        <button id="sig-buy" class="dte-chip" onclick="setSigFilter('buy',this)" title="strongest signal only" style="color:var(--green)">● Buy signals</button>
      </div>
      <div class="fi"><label>Min Vol/OI</label><input id="f-voi" type="number" value="0.5" step="0.1" style="width:65px"></div>
      <div class="fi"><label>Min Volume</label><input id="f-vol" type="number" value="200" style="width:70px"></div>
      <div class="fi"><label>Min Premium</label><input id="f-prem" type="number" value="25000" style="width:90px"></div>
      <div class="fi"><label>Max OTM%</label><input id="f-otm" type="number" value="35" step="5" style="width:65px"></div>
    </div>

    <!-- Row 2: Expiry filters -->
    <div class="filter-row">
      <span class="filter-section-label">Expiry</span>
      <div class="fi"><label>Min DTE</label><input id="f-dte-min" type="number" value="0" style="width:55px" onchange="applyFilter()"></div>
      <div class="fi"><label>Max DTE</label><input id="f-dte-max" type="number" value="90" style="width:55px" onchange="applyFilter()"></div>
      <div class="filter-divider"></div>
      <div class="dte-chips">
        <span class="dte-chip" onclick="applyDteChip(this,0,1)">0DTE</span>
        <span class="dte-chip" onclick="applyDteChip(this,0,7)">This week</span>
        <span class="dte-chip" onclick="applyDteChip(this,0,30)">≤ 30d</span>
        <span class="dte-chip" onclick="applyDteChip(this,0,60)">≤ 60d</span>
        <span class="dte-chip" onclick="applyDteChip(this,30,90)">30–90d</span>
        <span class="dte-chip" onclick="applyDteChip(this,60,999)">LEAPS 60d+</span>
        <span class="dte-chip active" onclick="applyDteChip(this,0,90)">All ≤ 90d</span>
      </div>
    </div>

  </div><!-- /filter-panel -->

  <div id="pb-wrap"><div id="pb"></div></div>

  <div id="status-bar">
    <div class="status-dot" id="status-dot" style="background:var(--muted)"></div>
    <span id="status-text">Click <strong>Auto Scan Market</strong> to scan 100+ liquid names for unusual options flow.</span>
  </div>

  <!-- Stats row -->
  <div id="stats-row" style="display:none;flex-wrap:wrap;display:flex">
    <div class="stat-card">
      <div class="stat-label">Contracts Flagged</div>
      <div class="stat-value" id="s-ct" style="color:var(--cyan)">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Tickers Scanned</div>
      <div class="stat-value" id="s-tk" style="color:var(--text)">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total Call Flow</div>
      <div class="stat-value" id="s-cl" style="color:var(--green)">$0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total Put Flow</div>
      <div class="stat-value" id="s-pu" style="color:var(--red)">$0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Call / Put Ratio</div>
      <div class="stat-value" id="s-ra" style="color:var(--yellow)">—</div>
      <div class="stat-sub" id="s-bias"></div>
    </div>
  </div>

  <!-- Main table -->
  <div id="tw">
    <div id="empty">
      <div class="empty-icon">📡</div>
      <div class="empty-title">No results yet</div>
      <div class="empty-sub">Hit <strong>Auto Scan Market</strong> to detect unusual options flow across 100+ names.<br>Star any contract (⭐) to track its performance to expiration.</div>
    </div>
    <table id="main-table" style="display:none">
      <thead><tr>
        <th class="left" style="width:36px; padding-left:16px"></th>
        <th class="left" data-col="score">Score</th>
        <th class="left" data-col="buy_signal" title="Buy signal — subjective heuristic only, not financial advice">Signal</th>
        <th class="left" style="min-width:260px">Reasons <span style="font-weight:400;color:var(--muted);font-size:9px;text-transform:none;letter-spacing:0">(heuristic, not advice)</span></th>
        <th class="left" data-col="ticker">Ticker</th>
        <th class="left" data-col="type">Type</th>
        <th class="left" data-col="moneyness">Moneyness</th>
        <th class="left" data-col="strike">Strike</th>
        <th class="left" data-col="expiration">Expiry</th>
        <th data-col="dte">DTE</th>
        <th data-col="last">Last $</th>
        <th data-col="bid">Bid</th>
        <th data-col="ask">Ask</th>
        <th data-col="spread_pct">Spread%</th>
        <th data-col="volume">Volume</th>
        <th data-col="oi">Open Int</th>
        <th data-col="vol_oi">Vol / OI</th>
        <th data-col="iv">IV</th>
        <th data-col="delta">Delta</th>
        <th data-col="premium">Premium</th>
        <th data-col="price_chg">Stk Chg%</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>

</div><!-- /page-scanner -->

<!-- ═══════════ WATCHLIST ═══════════ -->
<div id="page-watchlist" class="page">
  <div class="wl-toolbar">
    <button class="btn btn-primary" onclick="refreshWatchlist()">↻ &nbsp;Refresh Prices</button>
    <span id="wl-status" style="color:var(--muted2);font-size:11px"></span>
    <span id="wl-stats" style="display:none;font-size:12px;display:flex;gap:10px;align-items:center"></span>
    <div class="wl-view-toggle">
      <button class="view-btn active" id="btn-cards" onclick="setWlView('cards')">⊞ Cards</button>
      <button class="view-btn" id="btn-table" onclick="setWlView('table')">≡ Table</button>
    </div>
  </div>
  <div class="wl-wrap">
    <div id="wl-empty" style="text-align:center;padding:70px;color:var(--muted2)">
      <div style="font-size:42px;margin-bottom:14px">⭐</div>
      <div style="font-size:16px;font-weight:600;color:var(--text);margin-bottom:8px">No tracked contracts</div>
      <div style="font-size:13px;line-height:1.7">Star contracts from the scanner to track their performance to expiration.</div>
    </div>
    <!-- Cards view -->
    <div id="wl-cards" style="display:none" class="wl-cards"></div>
    <!-- Table view -->
    <table id="wl-table" style="display:none;width:100%;border-collapse:collapse;white-space:nowrap">
      <thead><tr>
        <th class="left">Ticker</th>
        <th class="left">Type</th>
        <th class="left">Moneyness</th>
        <th class="left">Strike</th>
        <th class="left">Expiry</th>
        <th>DTE</th>
        <th>Entry $</th>
        <th>Entry Spot</th>
        <th>Cur $</th>
        <th>Cur Spot</th>
        <th>P&amp;L / contract</th>
        <th>P&amp;L %</th>
        <th>Score</th>
        <th>Vol/OI</th>
        <th>IV</th>
        <th>Result</th>
        <th class="left">Notes</th>
        <th></th>
      </tr></thead>
      <tbody id="wl-tbody"></tbody>
    </table>
  </div>
</div>

<!-- ═══════════════════ TICKER MODAL ═══════════════════ -->
<div id="ticker-modal" style="display:none;position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.7);backdrop-filter:blur(4px)" onclick="if(event.target===this)closeModal()">
  <div style="position:absolute;top:5%;left:50%;transform:translateX(-50%);width:min(960px,95vw);max-height:90vh;overflow-y:auto;background:var(--surf);border:1px solid var(--border);border-radius:12px">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--border)">
      <div>
        <span id="modal-name" style="font-size:18px;font-weight:700;color:var(--cyan)"></span>
        <span id="modal-price" style="font-size:16px;margin-left:12px"></span>
        <span id="modal-chg" style="font-size:13px;margin-left:8px"></span>
      </div>
      <button onclick="closeModal()" style="background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer;padding:4px 8px">✕</button>
    </div>

    <!-- Chart -->
    <div style="padding:16px 20px 0">
      <div id="tv-chart-container" style="height:400px;border-radius:8px;overflow:hidden"></div>
    </div>

    <!-- Stats grid -->
    <div id="modal-stats" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;padding:16px 20px">
      <div style="text-align:center;padding:20px;color:var(--muted)"><span class="spinner"></span>Loading stats…</div>
    </div>

    <!-- Description -->
    <div style="padding:0 20px 16px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.7px;margin-bottom:6px">About</div>
      <div id="modal-desc" style="color:var(--muted);font-size:12px;line-height:1.7"></div>
    </div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let allRows = [], starredIds = new Set();
let sortCol = 'score', sortDir = -1;
let evtSource = null;
let totalCall = 0, totalPut = 0;
let emptyCount = 0;
let moneyFilter = 'ALL';
let sigFilter = 'all';

// ── Ticker modal ───────────────────────────────────────────────────────────
function openTickerModal(ticker) {
  document.getElementById('ticker-modal').style.display = '';
  document.getElementById('modal-name').textContent = ticker;
  document.getElementById('modal-price').textContent = '';
  document.getElementById('modal-chg').textContent = '';
  document.getElementById('modal-desc').textContent = '';
  document.getElementById('modal-stats').innerHTML = '<div style="text-align:center;padding:20px;color:var(--muted)"><span class="spinner"></span>Loading stats…</div>';

  // TradingView chart
  const container = document.getElementById('tv-chart-container');
  container.innerHTML = '';
  const script = document.createElement('script');
  script.src = 'https://s3.tradingview.com/tv.js';
  script.onload = () => {
    new TradingView.widget({
      container_id: 'tv-chart-container',
      symbol: ticker,
      interval: 'D',
      timezone: 'America/New_York',
      theme: 'dark',
      style: '1',
      locale: 'en',
      toolbar_bg: '#161b22',
      enable_publishing: false,
      hide_side_toolbar: false,
      allow_symbol_change: false,
      width: '100%',
      height: 400,
    });
  };
  document.head.appendChild(script);

  // Fetch stats
  fetch(`/stock-info/${ticker}`)
    .then(r => r.json())
    .then(d => {
      if (d.error) { document.getElementById('modal-stats').innerHTML = `<div style="color:var(--red);padding:16px">${d.error}</div>`; return; }

      const chgColor = d.change >= 0 ? 'var(--green)' : 'var(--red)';
      const chgSign  = d.change >= 0 ? '+' : '';
      document.getElementById('modal-price').innerHTML = `<span style="font-weight:700">$${d.price}</span>`;
      document.getElementById('modal-chg').innerHTML   = `<span style="color:${chgColor}">${chgSign}$${d.change} (${chgSign}${d.change_pct}%)</span>`;
      document.getElementById('modal-name').textContent = `${d.ticker}  —  ${d.name}`;
      document.getElementById('modal-desc').textContent = d.description;

      const stats = [
        ['Market Cap',    d.market_cap],
        ['P/E (TTM)',     d.pe],
        ['Fwd P/E',       d.fwd_pe],
        ['EPS (TTM)',     d.eps !== '—' ? `$${d.eps}` : '—'],
        ['52W High',      d.week52_high !== '—' ? `$${d.week52_high}` : '—'],
        ['52W Low',       d.week52_low  !== '—' ? `$${d.week52_low}`  : '—'],
        ['Avg Volume',    d.avg_vol],
        ['Beta',          d.beta],
        ['Div Yield',     d.div_yield],
        ['Sector',        d.sector],
        ['Industry',      d.industry],
      ];

      document.getElementById('modal-stats').innerHTML = stats.map(([label, val]) => `
        <div style="background:var(--surf2);border:1px solid var(--border);border-radius:8px;padding:10px 14px">
          <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.7px;margin-bottom:4px">${label}</div>
          <div style="font-weight:700;color:var(--text);font-size:14px">${val}</div>
        </div>`).join('');
    })
    .catch(() => document.getElementById('modal-stats').innerHTML = '<div style="color:var(--red);padding:16px">Failed to load stats.</div>');
}

function closeModal() {
  document.getElementById('ticker-modal').style.display = 'none';
  document.getElementById('tv-chart-container').innerHTML = '';
}

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ── DTE chips ─────────────────────────────────────────────────────────────
function applyDteChip(el, mn, mx) {
  document.querySelectorAll('.dte-chip').forEach(c => c.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('f-dte-min').value = mn;
  document.getElementById('f-dte-max').value = mx === 999 ? '' : mx;
  applyFilter();
}

// ── Signal filter ─────────────────────────────────────────────────────────
function setSigFilter(val, el) {
  sigFilter = val;
  document.querySelectorAll('#sig-all,#sig-watch,#sig-buy').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  applyFilter();
}

// ── Moneyness filter ──────────────────────────────────────────────────────
function setMoney(val) {
  moneyFilter = val;
  document.querySelectorAll('.money-btn').forEach(b => {
    b.className = 'money-btn';
  });
  const btn = document.getElementById('m-' + val.toLowerCase());
  if (val === 'ALL') btn.style.cssText = 'background:rgba(77,168,247,.15);border-color:rgba(77,168,247,.5);color:var(--cyan)';
  else if (val === 'ITM') btn.classList.add('active-itm');
  else if (val === 'ATM') btn.classList.add('active-atm');
  else if (val === 'OTM') btn.classList.add('active-otm');
  applyFilter();
}

// ── Tabs ───────────────────────────────────────────────────────────────────
function showTab(name, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  el.classList.add('active');
  if (name === 'watchlist') loadWatchlist();
}

// ── Formatters ────────────────────────────────────────────────────────────
function fmt(n) {
  if (Math.abs(n)>=1e9) return (n>=0?'$':'-$')+(Math.abs(n)/1e9).toFixed(2)+'B';
  if (Math.abs(n)>=1e6) return (n>=0?'$':'-$')+(Math.abs(n)/1e6).toFixed(1)+'M';
  if (Math.abs(n)>=1e3) return (n>=0?'$':'-$')+(Math.abs(n)/1e3).toFixed(0)+'K';
  return (n>=0?'$':'-$')+Math.abs(n).toFixed(0);
}
function fmtV(n){return n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(0)+'K':n.toString()}
function scoreBadge(s){let c=s>=70?'s-hot':s>=45?'s-warm':'s-cool';return `<span class="badge ${c}">${s}</span>`}
function mc(m){const k=m.split(' ')[0];return k==='ITM'?'itm':k==='ATM'?'atm':'otm'}

// ── Scanner rendering ─────────────────────────────────────────────────────
function getVisible(){
  const t      = document.getElementById('f-type').value;
  const ms     = parseFloat(document.getElementById('f-score').value) || 0;
  const dmn    = parseInt(document.getElementById('f-dte-min').value) || 0;
  const dmxRaw = parseInt(document.getElementById('f-dte-max').value);
  const dteMax = isNaN(dmxRaw) ? 9999 : dmxRaw;
  return allRows.filter(r => {
    if (t !== 'ALL' && r.type !== t) return false;
    if (r.score < ms) return false;
    if (r.dte < dmn || r.dte > dteMax) return false;
    if (moneyFilter !== 'ALL') {
      const key = r.moneyness.split(' ')[0];
      if (key !== moneyFilter) return false;
    }
    if (sigFilter === 'watch' && (r.buy_signal||0) < 40) return false;
    if (sigFilter === 'buy'   && (r.buy_signal||0) < 65) return false;
    return true;
  });
}

function applyFilter(){renderTable();updateStats()}

function renderTable(){
  const rows=[...getVisible()];
  rows.sort((a,b)=>sortDir*(a[sortCol]>b[sortCol]?1:a[sortCol]<b[sortCol]?-1:0));
  if(!rows.length){
    document.getElementById('main-table').style.display='none';
    document.getElementById('empty').style.display='';
    return;
  }
  document.getElementById('empty').style.display='none';
  document.getElementById('main-table').style.display='';
  document.getElementById('stats-row').style.display='flex';
  document.getElementById('wl-badge').style.display=starredIds.size?'':'none';
  document.getElementById('wl-badge').textContent=starredIds.size;
  document.getElementById('contract-count').textContent=rows.length+' contracts flagged';

  document.getElementById('tbody').innerHTML=rows.map(r=>{
    const starred=starredIds.has(rowId(r));
    const typeTag=r.type==='CALL'
      ?'<span class="type-badge type-call">▲ CALL</span>'
      :'<span class="type-badge type-put">▼ PUT</span>';
    const mKey=r.moneyness.split(' ')[0];
    const mCls=mKey==='ITM'?'m-itm':mKey==='ATM'?'m-atm':'m-otm';
    const vc=r.vol_oi>=2?'voi-h':r.vol_oi>=.5?'voi-w':'';
    const pc=r.premium>=1e6?'p-h':r.premium>=1e5?'p-w':'';

    // subtle signal indicator
    let sigHtml;
    const bs=r.buy_signal||0;
    if(bs>=65)      sigHtml=`<span class="sig-strong" title="Multiple factors align — not financial advice">● buy</span>`;
    else if(bs>=40) sigHtml=`<span class="sig-watch"  title="Some factors align — watch list candidate">◎ watch</span>`;
    else            sigHtml=`<span class="sig-none">·</span>`;

    const deltaStr=r.delta?r.delta.toFixed(2):'—';
    const bidStr=r.bid?'$'+r.bid.toFixed(2):'—';
    const askStr=r.ask?'$'+r.ask.toFixed(2):'—';
    const spreadStr=r.spread_pct!=null?r.spread_pct.toFixed(1)+'%':'—';
    const chgStr=r.price_chg!=null
      ?`<span style="color:${r.price_chg>=0?'var(--green)':'var(--red)'}">${r.price_chg>=0?'+':''}${r.price_chg.toFixed(1)}%</span>`
      :'—';

    // Build reason tags
    const reasons = r.buy_reasons || [];
    const reasonsHtml = '<div class="reasons-wrap">' +
      reasons.map(([sent, label, detail]) => {
        const cls = sent==='+' ? 'rtag-pos' : sent==='-' ? 'rtag-neg' : 'rtag-neu';
        const icon = sent==='+' ? '▲' : sent==='-' ? '▼' : '●';
        return `<span class="rtag ${cls}" title="${detail.replace(/"/g,'&quot;')}"><span class="ri">${icon}</span>${label}</span>`;
      }).join('') +
    '</div>';

    return `<tr>
      <td class="left" style="padding-left:16px"><button class="star-btn${starred?' starred':''}" title="${starred?'Unstar':'Star to track'}" onclick="toggleStar(this,'${encodeURIComponent(JSON.stringify(r))}')">${starred?'⭐':'☆'}</button></td>
      <td class="left">${scoreBadge(r.score)}</td>
      <td class="left">${sigHtml}</td>
      <td class="left" style="white-space:normal">${reasonsHtml}</td>
      <td class="left"><span class="tkr" onclick="openTickerModal('${r.ticker}')" title="View chart & stats">${r.ticker}</span><br><span class="spot-sub">$${r.spot} ${chgStr}</span></td>
      <td class="left">${typeTag}</td>
      <td class="left"><span class="m-badge ${mCls}">${r.moneyness}</span></td>
      <td class="left">$${r.strike.toFixed(1)}</td>
      <td class="left">${r.expiration}</td>
      <td>${r.dte}d</td>
      <td>$${r.last.toFixed(2)}</td>
      <td>${bidStr}</td>
      <td>${askStr}</td>
      <td>${spreadStr}</td>
      <td>${fmtV(r.volume)}</td>
      <td>${fmtV(r.oi)}</td>
      <td class="${vc}">${r.vol_oi.toFixed(2)}x</td>
      <td>${(r.iv*100).toFixed(0)}%</td>
      <td>${deltaStr}</td>
      <td class="${pc}">${fmt(r.premium)}</td>
    </tr>`;
  }).join('');
}

function rowId(r){return `${r.ticker}_${r.type}_${r.strike}_${r.expiration}`}

async function toggleStar(btn, encoded){
  const row=JSON.parse(decodeURIComponent(encoded));
  const res=await fetch('/star',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({row})});
  const data=await res.json();
  if(data.action==='starred'){
    starredIds.add(data.id); btn.textContent='⭐';
    showToast('⭐ Contract starred — view in Watchlist tab');
  } else {
    starredIds.delete(data.id); btn.textContent='☆';
  }
  document.getElementById('wl-badge').style.display=starredIds.size?'':'none';
  document.getElementById('wl-badge').textContent=starredIds.size;
}

function updateStats(){
  const v=getVisible();
  document.getElementById('s-ct').textContent=v.length;
  document.getElementById('s-tk').textContent=document.getElementById('s-tk').textContent||'0';
  document.getElementById('s-cl').textContent=fmt(totalCall);
  document.getElementById('s-pu').textContent=fmt(totalPut);
  const ratio=totalPut>0?totalCall/totalPut:0;
  const el=document.getElementById('s-ra');
  el.textContent=ratio?ratio.toFixed(2)+'x':'—';
  el.style.color=ratio>1.2?'var(--green)':ratio<.8?'var(--red)':'var(--yellow)';
  const bias=document.getElementById('s-bias');
  if(bias) bias.textContent=ratio>1.2?'Bullish lean':ratio<.8?'Bearish lean':'Neutral';
}

function addRows(rows){
  for(const r of rows){
    allRows.push(r);
    if(r.type==='CALL') totalCall+=r.premium; else totalPut+=r.premium;
    if(r.starred) starredIds.add(rowId(r));
  }
}

// ── Scan control ──────────────────────────────────────────────────────────
function startScan(){
  clearResults();
  const custom=document.getElementById('custom-tickers').value.trim();
  const qs=custom?'?tickers='+custom.toUpperCase().split(/[\s,]+/).filter(Boolean).join(','):'';
  evtSource=new EventSource('/stream-scan'+qs);
  document.getElementById('scan-btn').disabled=true;
  document.getElementById('stop-btn').style.display='inline-flex';
  document.getElementById('pb-wrap').style.display='block';
  document.getElementById('stats-row').style.display='flex';

  evtSource.onmessage=(e)=>{
    const msg=JSON.parse(e.data);
    if(msg.type==='start') setStatus(`<span class="spinner"></span>Scanning ${msg.total} tickers for unusual flow…`,'loading');
    if(msg.type==='ticker'){
      document.getElementById('pb').style.width=(msg.done/msg.total*100)+'%';
      document.getElementById('s-tk').textContent=msg.done+'/'+msg.total;
      if(msg.empty) emptyCount++;
      const rateWarn = emptyCount > 10 && allRows.length === 0
        ? ' <span style="color:var(--red)">⚠ Yahoo may be rate limiting — results will be sparse</span>' : '';
      setStatus(`<span class="spinner"></span>${msg.done}/${msg.total} — last: <strong>${msg.ticker}</strong>  ·  ${allRows.length} contracts flagged${rateWarn}`,'loading');
      if(msg.rows&&msg.rows.length){addRows(msg.rows);renderTable();updateStats()}
    }
    if(msg.type==='done'){
      evtSource.close();evtSource=null;
      document.getElementById('scan-btn').disabled=false;
      document.getElementById('stop-btn').style.display='none';
      document.getElementById('pb-wrap').style.display='none';
      const elapsed = msg.elapsed ? ` in ${msg.elapsed}s` : '';
      if(allRows.length === 0){
        setStatus(`⚠ Scan complete but 0 contracts found. Yahoo Finance may be rate limiting — wait 1–2 minutes and try again.`,'err');
      } else {
        setStatus(`✓ Scan complete — ${allRows.length} unusual contracts found${elapsed}  |  ${new Date().toLocaleTimeString()}`,'done');
      }
      updateStats();
    }
  };
  evtSource.onerror=()=>{
    if(evtSource){evtSource.close();evtSource=null;}
    document.getElementById('scan-btn').disabled=false;
    document.getElementById('stop-btn').style.display='none';
    setStatus('Connection error — try again.','err');
  };
}

function stopScan(){
  if(evtSource){evtSource.close();evtSource=null;}
  document.getElementById('scan-btn').disabled=false;
  document.getElementById('stop-btn').style.display='none';
  document.getElementById('pb-wrap').style.display='none';
  setStatus('Scan stopped.','err');
}

function clearResults(){
  allRows=[];totalCall=0;totalPut=0;emptyCount=0;
  document.getElementById('tbody').innerHTML='';
  document.getElementById('main-table').style.display='none';
  document.getElementById('empty').style.display='';
  document.getElementById('stats-row').style.display='none';
  document.getElementById('s-tk').textContent='0';
  document.getElementById('pb').style.width='0';
}

function setStatus(msg,type){
  document.getElementById('status-text').innerHTML=msg;
  const dot=document.getElementById('status-dot');
  dot.style.background=type==='loading'?'var(--yellow)':type==='done'?'var(--green)':type==='err'?'var(--red)':'var(--muted)';
  if(type==='loading'){dot.style.animation='pulse 1s infinite';}
  else{dot.style.animation='none';}
}

// Sort
document.querySelectorAll('#main-table thead th[data-col]').forEach(th=>{
  th.addEventListener('click',()=>{
    const col=th.dataset.col;
    sortDir=sortCol===col?sortDir*-1:-1; sortCol=col;
    document.querySelectorAll('#main-table thead th').forEach(t=>t.classList.remove('sort-asc','sort-desc'));
    th.classList.add(sortDir===-1?'sort-desc':'sort-asc');
    renderTable();
  });
});

// ── Watchlist ─────────────────────────────────────────────────────────────
async function loadWatchlist(){
  const res=await fetch('/watchlist');
  const wl=await res.json();
  renderWatchlist(wl);
}

async function refreshWatchlist(){
  document.getElementById('wl-status').innerHTML='<span class="spinner"></span>Fetching current prices…';
  const res=await fetch('/watchlist/refresh',{method:'POST'});
  const wl=await res.json();
  renderWatchlist(wl);
  document.getElementById('wl-status').textContent='Updated '+new Date().toLocaleTimeString();
}

let wlView='cards';
let wlData=[];

function setWlView(v){
  wlView=v;
  document.getElementById('btn-cards').classList.toggle('active',v==='cards');
  document.getElementById('btn-table').classList.toggle('active',v==='table');
  document.getElementById('wl-cards').style.display=v==='cards'&&wlData.length?'':'none';
  document.getElementById('wl-table').style.display=v==='table'&&wlData.length?'':'none';
}

function renderWatchlist(wl){
  wlData=wl;
  document.getElementById('wl-badge').style.display=wl.length?'':'none';
  document.getElementById('wl-badge').textContent=wl.length;
  if(!wl.length){
    document.getElementById('wl-cards').style.display='none';
    document.getElementById('wl-table').style.display='none';
    document.getElementById('wl-empty').style.display='';
    document.getElementById('wl-stats').style.display='none';
    return;
  }
  document.getElementById('wl-empty').style.display='none';

  // Stats summary bar
  const wins=wl.filter(e=>e.result==='WIN').length;
  const losses=wl.filter(e=>e.result==='EXPIRED_WORTHLESS').length;
  const pending=wl.filter(e=>!e.result||e.result==='PENDING').length;
  const closed=wins+losses;
  const winRate=closed>0?Math.round(wins/closed*100):null;
  const statsEl=document.getElementById('wl-stats');
  statsEl.style.display='flex';
  statsEl.innerHTML=
    `<span style="color:var(--green);font-weight:700">${wins} WIN${wins!==1?'S':''}</span>`+
    `<span style="color:var(--muted2)">·</span>`+
    `<span style="color:var(--red);font-weight:700">${losses} LOSS${losses!==1?'ES':''}</span>`+
    `<span style="color:var(--muted2)">·</span>`+
    `<span style="color:var(--yellow)">${pending} PENDING</span>`+
    (winRate!==null?`<span style="color:var(--muted2)">·</span><span style="color:var(--cyan);font-weight:700">${winRate}% ITM rate</span>`:'');

  // ── Build card data ──────────────────────────────────────
  const cardHtml=wl.map(e=>{
    const daysLeft=e.days_left!=null?e.days_left:dte(e.expiration);
    const expired=daysLeft<0;
    const r=e.result||'PENDING';

    // Card color class
    let cardCls='card-pending';
    if(r==='WIN') cardCls='card-win';
    else if(r==='EXPIRED_WORTHLESS') cardCls='card-loss';
    else if(expired) cardCls='card-expired';

    // Outcome badge
    let badge='';
    if(r==='WIN'){
      const exitTxt=e.exit_spot?` · stock $${e.exit_spot.toFixed(2)}`:'';
      badge=`<span class="outcome-badge outcome-win">✓ IN THE MONEY${exitTxt}</span>`;
    } else if(r==='EXPIRED_WORTHLESS'){
      const exitTxt=e.exit_spot?` · stock $${e.exit_spot.toFixed(2)}`:'';
      badge=`<span class="outcome-badge outcome-loss">✗ EXPIRED OUT OF THE MONEY${exitTxt}</span>`;
    } else if(expired){
      badge=`<span class="outcome-badge outcome-expired">EXPIRED · hit Refresh</span>`;
    } else {
      badge=`<span class="outcome-badge outcome-pending">${daysLeft}d until expiry</span>`;
    }

    // P&L line
    let pnlLine='';
    if(e.pnl_dollar!=null){
      const dcls=e.pnl_dollar>=0?'pnl-pos':'pnl-neg';
      const pcls=e.pnl_pct!=null?(e.pnl_pct>=0?'pnl-pos':'pnl-neg'):'';
      const pctTxt=e.pnl_pct!=null?` <span class="${pcls}">(${e.pnl_pct>=0?'+':''}${e.pnl_pct.toFixed(1)}%)</span>`:'';
      pnlLine=`<div class="card-pnl">
        <span style="color:var(--muted2);font-size:11px">P&amp;L / contract</span>
        <span class="${dcls}" style="font-size:15px">${e.pnl_dollar>=0?'+':''}$${e.pnl_dollar.toFixed(0)}</span>${pctTxt}
        ${e.current_price!=null?`<span style="color:var(--muted2);font-size:11px;margin-left:auto">cur $${e.current_price.toFixed(2)}</span>`:''}
      </div>`;
    }

    // Current moneyness
    const mnow=e.moneyness_now||'';
    const mnowHtml=mnow?`<span class="${mc(mnow)}" style="font-size:11px">${mnow}</span> · `:'';

    const typeColor=e.type==='CALL'?'var(--green)':'var(--red)';
    const typeArrow=e.type==='CALL'?'▲':'▼';
    const starredDate=e.starred_at?e.starred_at.split('T')[0]:'';
    const curSpotTxt=e.current_spot?` · now $${e.current_spot.toFixed(2)}`:'';

    return `<div class="wl-card ${cardCls}">
      <div class="card-header">
        <div>
          <div style="display:flex;align-items:center;gap:8px">
            <span class="card-ticker" onclick="openTickerModal('${e.ticker}')">${e.ticker}</span>
            <span style="color:${typeColor};font-weight:700;font-size:13px">${typeArrow} ${e.type}</span>
            <span style="color:var(--text);font-size:13px">$${e.strike.toFixed(1)} strike</span>
          </div>
          <div class="card-meta">exp ${e.expiration} · added ${starredDate}${curSpotTxt}</div>
        </div>
        <div class="card-outcome">${badge}</div>
      </div>
      <div class="card-body">
        <div style="font-size:10px;color:var(--muted2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Entry Snapshot</div>
        <div class="card-snapshot">
          <div class="snap-item"><div class="snap-label">Option Price</div><div class="snap-val">$${(e.entry_price||0).toFixed(2)}</div></div>
          <div class="snap-item"><div class="snap-label">Stock Price</div><div class="snap-val">$${(e.entry_spot||0).toFixed(2)}</div></div>
          <div class="snap-item"><div class="snap-label">Score</div><div class="snap-val">${scoreBadge(e.entry_score||0)}</div></div>
          <div class="snap-item"><div class="snap-label">Vol / OI</div><div class="snap-val">${(e.entry_vol_oi||0).toFixed(1)}x</div></div>
          <div class="snap-item"><div class="snap-label">IV</div><div class="snap-val">${((e.entry_iv||0)*100).toFixed(0)}%</div></div>
          <div class="snap-item"><div class="snap-label">Premium</div><div class="snap-val">${e.entry_premium>=1e6?'$'+(e.entry_premium/1e6).toFixed(1)+'M':e.entry_premium>=1e3?'$'+(e.entry_premium/1e3).toFixed(0)+'K':'$'+(e.entry_premium||0).toFixed(0)}</div></div>
        </div>
        ${pnlLine}
        <div class="card-footer">
          <input class="note-input" value="${(e.notes||'').replace(/"/g,'&quot;')}" placeholder="add notes…" onblur="saveNote('${e.id}',this.value)">
          <button class="btn btn-ghost btn-sm" onclick="removeFromWL('${e.id}')" style="flex-shrink:0">✕ Remove</button>
        </div>
      </div>
    </div>`;
  }).join('');
  document.getElementById('wl-cards').innerHTML=cardHtml;

  // ── Build table rows ─────────────────────────────────────
  document.getElementById('wl-tbody').innerHTML=wl.map(e=>{
    const daysLeft=e.days_left!=null?e.days_left:dte(e.expiration);
    const expired=daysLeft<0;
    const typeTag=e.type==='CALL'?'<span class="call">▲ CALL</span>':'<span class="put">▼ PUT</span>';

    let pnlHtml='<span style="color:var(--muted)">—</span>';
    if(e.pnl_dollar!=null){
      const cls=e.pnl_dollar>=0?'pnl-pos':'pnl-neg';
      pnlHtml=`<span class="${cls}">${e.pnl_dollar>=0?'+':''}$${e.pnl_dollar.toFixed(0)}</span>`;
    }
    let pnlPctHtml='<span style="color:var(--muted)">—</span>';
    if(e.pnl_pct!=null){
      const cls=e.pnl_pct>=0?'pnl-pos':'pnl-neg';
      pnlPctHtml=`<span class="${cls}">${e.pnl_pct>=0?'+':''}${e.pnl_pct.toFixed(1)}%</span>`;
    }

    const r=e.result||'PENDING';
    let resultHtml;
    if(r==='WIN'){
      const exitInfo=e.exit_spot?` @ $${e.exit_spot.toFixed(2)}`:'';
      resultHtml=`<span class="result-win">✓ ITM${exitInfo}</span>`;
    } else if(r==='EXPIRED_WORTHLESS'){
      const exitInfo=e.exit_spot?` @ $${e.exit_spot.toFixed(2)}`:'';
      resultHtml=`<span class="result-loss">✗ OTM${exitInfo}</span>`;
    } else if(expired){
      resultHtml='<span class="result-expired">EXPIRED (refresh)</span>';
    } else {
      resultHtml=`<span class="result-pending">${daysLeft}d left</span>`;
    }

    const mnow=e.moneyness_now||'—';
    const curPrice=e.current_price!=null?'$'+e.current_price.toFixed(2):'<span style="color:var(--muted)">—</span>';
    const curSpot=e.current_spot?'$'+e.current_spot.toFixed(2):'<span style="color:var(--muted)">—</span>';
    const starredDate=e.starred_at?e.starred_at.split('T')[0]:'';

    return `<tr>
      <td class="left"><span class="tkr" style="cursor:pointer;text-decoration:underline dotted" onclick="openTickerModal('${e.ticker}')">${e.ticker}</span><br><span style="color:var(--muted);font-size:10px">${starredDate}</span></td>
      <td class="left">${typeTag}</td>
      <td class="left ${mc(mnow)}">${mnow}</td>
      <td class="left">$${e.strike.toFixed(1)}</td>
      <td class="left">${e.expiration}</td>
      <td>${expired?'<span style="color:var(--muted)">EXP</span>':daysLeft+'d'}</td>
      <td>$${(e.entry_price||0).toFixed(2)}</td>
      <td>$${(e.entry_spot||0).toFixed(2)}</td>
      <td>${curPrice}</td>
      <td>${curSpot}</td>
      <td>${pnlHtml}</td>
      <td>${pnlPctHtml}</td>
      <td>${scoreBadge(e.entry_score||0)}</td>
      <td>${(e.entry_vol_oi||0).toFixed(2)}x</td>
      <td>${((e.entry_iv||0)*100).toFixed(0)}%</td>
      <td>${resultHtml}</td>
      <td class="left"><input class="note-input" value="${(e.notes||'').replace(/"/g,'&quot;')}" placeholder="add notes…" onblur="saveNote('${e.id}',this.value)"></td>
      <td><button class="btn btn-ghost btn-sm" onclick="removeFromWL('${e.id}')">✕</button></td>
    </tr>`;
  }).join('');

  // Show correct view
  setWlView(wlView);
}

function dte(exp){
  try{
    const d=new Date(exp);const t=new Date();
    return Math.ceil((d-t)/(1000*60*60*24));
  }catch{return 0}
}

async function saveNote(id,notes){
  await fetch('/watchlist/note',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,notes})});
}

async function removeFromWL(id){
  await fetch('/watchlist/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  starredIds.delete(id.split('_').slice(0,4).join('_'));
  loadWatchlist();
  renderTable();
}

// Toast
function showToast(msg){
  const t=document.createElement('div');
  t.style.cssText='position:fixed;bottom:20px;right:20px;background:var(--surf2);border:1px solid var(--border);border-radius:8px;padding:10px 16px;font-size:12px;color:var(--text);z-index:9999;animation:fadein .3s';
  t.textContent=msg;document.body.appendChild(t);
  setTimeout(()=>t.remove(),3000);
}

// Clock
setInterval(()=>document.getElementById('clock').textContent=new Date().toLocaleTimeString(),1000);
document.getElementById('clock').textContent=new Date().toLocaleTimeString();

// Load starred IDs on boot
fetch('/watchlist').then(r=>r.json()).then(wl=>{
  wl.forEach(e=>starredIds.add(e.id));
  document.getElementById('wl-badge').style.display=wl.length?'':'none';
  document.getElementById('wl-badge').textContent=wl.length;
});
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    print("\n  Options Flow Scanner  →  http://localhost:8888\n")
    app.run(debug=False, host="127.0.0.1", port=8888, threaded=True)
