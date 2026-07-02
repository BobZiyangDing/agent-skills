#!/usr/bin/env python3
"""
big-position-think-twice :: macro + asset dashboard fetcher

Fetches every REQUIRED quant indicator in the skill spec plus per-asset stats,
computes ratios/spreads/term-structure, and prints a human-readable dashboard.

Run:
  uv run --with yfinance --with requests python3 fetch_macro.py [TICKER ...]

If TICKERs are given, an asset section is printed for each (5d move, SMA/EMA,
ATR-based expected move, and ATM implied-vol expected move if optionable).

All sections degrade gracefully to N/A on failure — never hard-crash.
"""

import sys
import io
import csv
import math
import urllib.request
import datetime as dt

# ---------- helpers ----------

def _pct(x):
    return f"{x:+.2f}%" if x is not None else "N/A"

def _f(x, d=2):
    return f"{x:.{d}f}" if x is not None else "N/A"

def fred_csv(series_id, days=400):
    """Public keyless FRED CSV download. Returns list[(date, float)] ascending."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8")
        rows = list(csv.reader(io.StringIO(raw)))
        out = []
        for r in rows[1:]:
            if len(r) < 2:
                continue
            d, v = r[0], r[1]
            if v in (".", "", "NA"):
                continue
            try:
                out.append((d, float(v)))
            except ValueError:
                continue
        return out[-days:]
    except Exception as e:
        print(f"    [FRED {series_id} fetch failed: {e}]", file=sys.stderr)
        return []

def latest_and_change(series, lookback=5):
    """series: list[(date, val)] ascending. Returns (latest_val, latest_date, chg_abs)."""
    if not series:
        return None, None, None
    latest_v = series[-1][1]
    latest_d = series[-1][0]
    prior = series[-1 - lookback][1] if len(series) > lookback else series[0][1]
    return latest_v, latest_d, latest_v - prior


# ---------- yfinance section ----------

try:
    import yfinance as yf
    HAVE_YF = True
except Exception:
    HAVE_YF = False


def yf_hist(ticker, period="3mo"):
    try:
        h = yf.Ticker(ticker).history(period=period, auto_adjust=False)
        if h is None or h.empty:
            return None
        return h
    except Exception as e:
        print(f"    [yf {ticker} failed: {e}]", file=sys.stderr)
        return None

def last_close(h):
    return float(h["Close"].iloc[-1]) if h is not None and not h.empty else None

def pct_change_ndays(h, n=5):
    if h is None or len(h) < n + 1:
        return None
    a = float(h["Close"].iloc[-1]); b = float(h["Close"].iloc[-1 - n])
    return (a / b - 1.0) * 100.0 if b else None

def sma(h, n):
    if h is None or len(h) < n:
        return None
    return float(h["Close"].iloc[-n:].mean())

def ema(h, n):
    if h is None or len(h) < n:
        return None
    return float(h["Close"].ewm(span=n, adjust=False).mean().iloc[-1])

def atr(h, n=14):
    if h is None or len(h) < n + 1:
        return None
    highs = h["High"].values; lows = h["Low"].values; closes = h["Close"].values
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) < n:
        return None
    return sum(trs[-n:]) / n


# ---------- MACRO DASHBOARD ----------

def macro_dashboard():
    print("=" * 66)
    print("MACRO RISK DASHBOARD  —  " + dt.datetime.now().strftime("%Y-%m-%d %H:%M local"))
    print("=" * 66)

    if not HAVE_YF:
        print("!! yfinance not available; rerun with: uv run --with yfinance --with requests")
        return

    # Pull the yahoo-based series
    tk = {
        "VIX":    yf_hist("^VIX"),
        "VIX9D":  yf_hist("^VIX9D"),
        "VIX3M":  yf_hist("^VIX3M"),
        "VVIX":   yf_hist("^VVIX"),
        "VIXEQ":  yf_hist("^VIXEQ"),
        "MOVE":   yf_hist("^MOVE"),
        "COR1M":  yf_hist("^COR1M"),
        "COR3M":  yf_hist("^COR3M"),
        "HYG":    yf_hist("HYG"),
        "IEI":    yf_hist("IEI"),
    }
    v = {k: last_close(h) for k, h in tk.items()}
    c5 = {k: pct_change_ndays(h, 5) for k, h in tk.items()}

    def line(label, val, chg5, note):
        print(f"  {label:<26} {_f(val):>10}   5d {_pct(chg5):>8}   {note}")

    print("\n-- 1. Volatility (equity) --")
    line("VIX (index fear)", v["VIX"], c5["VIX"],
         flag_vix(v["VIX"]))
    line("VVIX (vol-of-vol)", v["VVIX"], c5["VVIX"],
         ">100 = fragile vol" if v["VVIX"] else "")
    # Term structure
    ts_9d = (v["VIX9D"] / v["VIX"]) if (v["VIX9D"] and v["VIX"]) else None
    ts_3m = (v["VIX"] / v["VIX3M"]) if (v["VIX"] and v["VIX3M"]) else None
    line("VIX9D/VIX (near term)", ts_9d, None,
         "BACKWARDATION >1 = acute stress" if (ts_9d and ts_9d > 1) else "contango (calm)")
    line("VIX/VIX3M (curve)", ts_3m, None,
         "BACKWARDATION >1 = stress" if (ts_3m and ts_3m > 1) else "contango (calm)")

    print("\n-- 2. Dispersion / correlation (Frank's #5) --")
    veq_vix = (v["VIXEQ"] / v["VIX"]) if (v["VIXEQ"] and v["VIX"]) else None
    line("VIXEQ (single-stock vol)", v["VIXEQ"], c5["VIXEQ"], "")
    line("VIXEQ/VIX ratio", veq_vix, None,
         "HIGH = stocks wild, index calm (low corr) -> watch for corr snap-up")
    line("COR1M (implied corr 1m)", v["COR1M"], c5["COR1M"],
         "RISING corr = dispersion collapsing -> index down-risk")
    line("COR3M (implied corr 3m)", v["COR3M"], c5["COR3M"], "")

    print("\n-- 3. Rates / bond vol --")
    line("MOVE (bond VIX)", v["MOVE"], c5["MOVE"], flag_move(v["MOVE"]))

    print("\n-- 4. Credit / risk appetite --")
    hyg_iei = (v["HYG"] / v["IEI"]) if (v["HYG"] and v["IEI"]) else None
    # 5d change of the RATIO
    ratio_5d = None
    if tk["HYG"] is not None and tk["IEI"] is not None and len(tk["HYG"]) > 6 and len(tk["IEI"]) > 6:
        try:
            now = float(tk["HYG"]["Close"].iloc[-1]) / float(tk["IEI"]["Close"].iloc[-1])
            was = float(tk["HYG"]["Close"].iloc[-6]) / float(tk["IEI"]["Close"].iloc[-6])
            ratio_5d = (now / was - 1.0) * 100.0
        except Exception:
            pass
    line("HYG/IEI (risk appetite)", hyg_iei, ratio_5d,
         "FALLING = credit risk-off (junk underperforming safe)")

    # FRED credit spreads
    ccc = fred_csv("BAMLH0A3HYC")   # CCC & lower OAS (%)
    bb  = fred_csv("BAMLH0A1HYBB")  # BB OAS (%)
    ccc_v, ccc_d, ccc_ch = latest_and_change(ccc, 5)
    bb_v, bb_d, bb_ch = latest_and_change(bb, 5)
    ccc_bb = (ccc_v - bb_v) if (ccc_v is not None and bb_v is not None) else None
    print(f"  {'CCC OAS %':<26} {_f(ccc_v):>10}   5d {(_f(ccc_ch,2)+'pp') if ccc_ch is not None else 'N/A':>8}   ({ccc_d or 'N/A'})")
    print(f"  {'BB OAS %':<26} {_f(bb_v):>10}   5d {(_f(bb_ch,2)+'pp') if bb_ch is not None else 'N/A':>8}   ({bb_d or 'N/A'})")
    print(f"  {'CCC-BB spread (pp)':<26} {_f(ccc_bb):>10}   {'':>12}   WIDENING = low-quality credit stress leading")

    print("\n-- 5. Funding / plumbing --")
    sofr = fred_csv("SOFR")
    iorb = fred_csv("IORB")
    sofr_v, sofr_d, _ = latest_and_change(sofr, 5)
    iorb_v, iorb_d, _ = latest_and_change(iorb, 5)
    spread_bps = ((sofr_v - iorb_v) * 100.0) if (sofr_v is not None and iorb_v is not None) else None
    print(f"  {'SOFR %':<26} {_f(sofr_v):>10}   {'':>12}   ({sofr_d or 'N/A'})")
    print(f"  {'IORB %':<26} {_f(iorb_v):>10}   {'':>12}   ({iorb_d or 'N/A'})")
    print(f"  {'SOFR-IORB (bps)':<26} {_f(spread_bps,1):>10}   {'':>12}   {flag_sofr(spread_bps)}")

    print("\n" + "-" * 66)
    print("NON-QUANT (fetch separately via WebSearch — not in this script):")
    print("  * Index rebalancing dates (S&P/Nasdaq quarterly; watch month/quarter-end)")
    print("  * Pension fund rebalancing (month/quarter-end flows)")
    print("  * Pending Fed events (next FOMC, speeches, minutes, data prints)")
    print("-" * 66)


def flag_vix(x):
    if x is None: return ""
    if x < 15: return "CALM"
    if x < 20: return "normal"
    if x < 30: return "ELEVATED"
    return "STRESS"

def flag_move(x):
    if x is None: return ""
    if x < 80: return "calm"
    if x < 100: return "normal"
    if x < 120: return "ELEVATED"
    return "STRESS"

def flag_sofr(bps):
    if bps is None: return ""
    if bps < 0: return "SOFR below IORB (ample liquidity)"
    if bps < 5: return "normal"
    if bps < 10: return "firming (watch repo)"
    return "FUNDING PRESSURE (repo stress)"


# ---------- ASSET SECTION ----------

def asset_section(ticker):
    print("\n" + "=" * 66)
    print(f"ASSET  —  {ticker.upper()}")
    print("=" * 66)
    if not HAVE_YF:
        print("  yfinance unavailable")
        return
    h = yf_hist(ticker, period="6mo")
    if h is None:
        print("  no data")
        return
    px = last_close(h)
    print(f"  Last close:        {_f(px)}")
    print(f"  5-day move:        {_pct(pct_change_ndays(h,5))}")
    print(f"  20-day move:       {_pct(pct_change_ndays(h,20))}")
    print(f"  SMA20 / EMA20:     {_f(sma(h,20))} / {_f(ema(h,20))}   (px {'ABOVE' if (px and sma(h,20) and px>sma(h,20)) else 'below'} SMA20)")
    print(f"  SMA50 / EMA50:     {_f(sma(h,50))} / {_f(ema(h,50))}")
    a = atr(h, 14)
    print(f"  ATR(14):           {_f(a)}  ({_pct(100*a/px if (a and px) else None)} of price / day)")

    # ATR-based expected move over N trading days: ATR * sqrt(N)
    if a and px:
        for nd, lbl in [(1, "1d"), (5, "1wk"), (21, "1mo")]:
            em = a * math.sqrt(nd)
            print(f"    ATR expected move {lbl:>4}:  +/- {_f(em)}  ( +/-{_f(100*em/px)}% )")

    # Option-implied move from nearest expiry ATM IV
    try:
        t = yf.Ticker(ticker)
        exps = t.options
        if exps:
            exp = exps[0]
            chain = t.option_chain(exp)
            calls = chain.calls
            # nearest-ATM call IV
            calls = calls.assign(dist=(calls["strike"] - px).abs())
            row = calls.sort_values("dist").iloc[0]
            iv = float(row["impliedVolatility"])
            d2e = (dt.datetime.strptime(exp, "%Y-%m-%d").date() - dt.date.today()).days
            d2e = max(d2e, 1)
            move = px * iv * math.sqrt(d2e / 365.0)
            print(f"  Option-implied (exp {exp}, {d2e}d):")
            print(f"    ATM IV:            {_pct(iv*100)}")
            print(f"    implied move:      +/- {_f(move)}  ( +/-{_f(100*move/px)}% )  -> ~[{_f(px-move)}, {_f(px+move)}]")
    except Exception as e:
        print(f"  (option-implied move unavailable: {e})")


# ---------- main ----------

if __name__ == "__main__":
    macro_dashboard()
    for tkr in sys.argv[1:]:
        asset_section(tkr)
    print("\n[done]")
