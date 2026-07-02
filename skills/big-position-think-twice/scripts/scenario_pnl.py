#!/usr/bin/env python3
"""
scenario_pnl.py — exhaustive scenario-tree PnL engine for big-position-think-twice.

Philosophy: max/min PnL must be DERIVED from an enumerated scenario set, never
hand-waved. The engine:

  1. Builds the real timeline: every remaining NYSE session between now and the
     horizon/expiry (holiday & half-day aware via pandas-market-calendars),
     compressed into <= --max-steps steps. Marks locked (non-actable) windows.
  2. Builds the move grid per step from implied vol: sigma-multiples given by
     --grid (default +2/+1/0/-1/-2 sigma), each with a normal-discretized
     probability weight.
  3. Enumerates EVERY path through the tree (grid^steps), reprices the
     instrument at EVERY node (Black-Scholes for options, daily-reset
     compounding for leveraged ETFs, linear for stock), and prints:
        - the timeline with locked windows
        - the full scenario table (or extremes+quantiles if huge)
        - an exit matrix (PnL range if you exit at each actable checkpoint)
        - a summary: max / min / expected PnL, P(loss), P(sleep-line breach)

Usage examples:

  # option
  uv run --with yfinance --with pandas-market-calendars python3 scenario_pnl.py \
      --ticker SPY --type call --strike 748 --expiry 2026-07-07 \
      --entry-price 1.40 --qty 91 --account 51234 --sleep-pct 10

  # stock over 10 trading days
  ... --ticker NVDA --type stock --days 10 --entry-price 190 --qty 500 --account 51234

  # 2x leveraged ETF over 5 trading days
  ... --ticker AMDL --type letf --leverage 2 --days 5 --entry-price 12 --qty 4000

Notes / honest limitations (say these to the user):
  - Constant IV: option repricing holds IV fixed (no crush/spike modeling).
  - Additive sigma steps; probabilities are a normal discretization per step.
  - Steps are close-to-close: each step INCLUDES the overnight/weekend gap in
    front of it; you can only act at the checkpoints, which is exactly the
    lockout structure.
"""

import argparse
import datetime as dt
import itertools
import math
import statistics
import sys

# ---------------- black-scholes ----------------

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bs_value(S, K, T_years, r, iv, opt_type):
    """European BS value; at T<=0 returns intrinsic."""
    if S <= 0:
        S = 0.01
    if T_years <= 0 or iv <= 0:
        return max(0.0, S - K) if opt_type == "call" else max(0.0, K - S)
    sq = iv * math.sqrt(T_years)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T_years) / sq
    d2 = d1 - sq
    if opt_type == "call":
        return S * norm_cdf(d1) - K * math.exp(-r * T_years) * norm_cdf(d2)
    return K * math.exp(-r * T_years) * norm_cdf(-d2) - S * norm_cdf(-d1)

# ---------------- market calendar ----------------

def nyse_sessions(start: dt.date, end: dt.date):
    """List of (date, close_dt_utc) NYSE sessions in [start, end]."""
    import pandas_market_calendars as pmc
    cal = pmc.get_calendar("NYSE")
    sched = cal.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    out = []
    for idx, row in sched.iterrows():
        out.append((idx.date(), row["market_close"].to_pydatetime()))
    return out

# ---------------- probability grid ----------------

def grid_weights(levels):
    """levels: sorted desc sigma-multiples, e.g. [2,1,0,-1,-2].
    Weight of each = normal mass of the bin around it (boundaries midway
    between adjacent levels, tails to +/-inf)."""
    ws = []
    for i, lv in enumerate(levels):
        hi = math.inf if i == 0 else (levels[i - 1] + lv) / 2.0
        lo = -math.inf if i == len(levels) - 1 else (lv + levels[i + 1]) / 2.0
        hi_c = 1.0 if hi is math.inf else norm_cdf(hi)
        lo_c = 0.0 if lo is -math.inf else norm_cdf(lo)
        ws.append(hi_c - lo_c)
    s = sum(ws)
    return [w / s for w in ws]

# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser(description="Exhaustive scenario-tree PnL")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--type", required=True, choices=["call", "put", "stock", "letf"])
    ap.add_argument("--strike", type=float)
    ap.add_argument("--expiry", help="YYYY-MM-DD (options)")
    ap.add_argument("--days", type=int, help="horizon in trading days (stock/letf)")
    ap.add_argument("--entry-price", type=float, required=True,
                    help="premium per contract (options) or price per share")
    ap.add_argument("--qty", type=float, required=True,
                    help="contracts (options) or shares")
    ap.add_argument("--multiplier", type=float, default=None,
                    help="default 100 for options, 1 for stock/letf")
    ap.add_argument("--account", type=float, help="account equity for delta%%")
    ap.add_argument("--sleep-pct", type=float,
                    help="pain threshold as positive %% (e.g. 10 = -10%% line)")
    ap.add_argument("--iv", type=float, help="implied vol as decimal, e.g. 0.071; auto-fetched if omitted")
    ap.add_argument("--spot", type=float, help="spot; auto-fetched if omitted")
    ap.add_argument("--leverage", type=float, default=2.0, help="letf daily leverage")
    ap.add_argument("--r", type=float, default=0.04)
    ap.add_argument("--max-steps", type=int, default=3,
                    help="compress trading days into at most this many steps")
    ap.add_argument("--asof", help="YYYY-MM-DD: simulate entry at this session's close "
                    "(backtest mode; spot auto-fetched from history, IV should be "
                    "passed via --iv or current chain IV is used as a proxy)")
    ap.add_argument("--grid", default="2,1,0",
                    help="sigma multiples (mirrored), '2,1,0' -> +2,+1,0,-1,-2")
    args = ap.parse_args()

    is_opt = args.type in ("call", "put")
    if is_opt and (not args.strike or not args.expiry):
        sys.exit("options need --strike and --expiry")
    if not is_opt and not args.days:
        sys.exit("stock/letf needs --days")
    mult = args.multiplier if args.multiplier is not None else (100.0 if is_opt else 1.0)

    asof = dt.date.fromisoformat(args.asof) if args.asof else None

    # ---- spot & iv (auto-fetch if needed) ----
    spot, iv = args.spot, args.iv
    if spot is None or (is_opt and iv is None):
        import yfinance as yf
        tk = yf.Ticker(args.ticker)
        if spot is None:
            if asof:
                h = tk.history(start=asof.isoformat(),
                               end=(asof + dt.timedelta(days=4)).isoformat())
                if h.empty or h.index[0].date() != asof:
                    sys.exit(f"no session close found for --asof {asof}")
                spot = float(h["Close"].iloc[0])
            else:
                spot = float(tk.history(period="1d")["Close"].iloc[-1])
        if is_opt and iv is None and asof:
            print(f"  [warn] --asof backtest: historical IV unavailable, "
                  f"using CURRENT chain IV as proxy", file=sys.stderr)
        if is_opt and iv is None:
            try:
                ch = tk.option_chain(args.expiry)
                side = ch.calls if args.type == "call" else ch.puts
                side = side.assign(dist=(side["strike"] - args.strike).abs()).sort_values("dist")
                iv = float(side.iloc[0]["impliedVolatility"])
            except Exception as e:
                sys.exit(f"could not fetch IV for {args.ticker} {args.expiry} (pass --iv): {e}")
    if iv is None:  # stock/letf: use realized proxy from 3mo history
        import yfinance as yf
        h = yf.Ticker(args.ticker).history(period="3mo")["Close"]
        rets = [math.log(b / a) for a, b in zip(h[:-1], h[1:])]
        iv = statistics.stdev(rets) * math.sqrt(252)

    today = asof if asof else dt.date.today()
    horizon = dt.date.fromisoformat(args.expiry) if is_opt else None

    # ---- timeline: remaining NYSE sessions ----
    if is_opt:
        sessions = nyse_sessions(today, horizon)
    else:
        sessions = nyse_sessions(today, today + dt.timedelta(days=args.days * 2 + 15))
    if asof:
        # backtest: entry AT the asof close -> enumerate sessions strictly after
        sessions = [s for s in sessions if s[0] > asof]
    else:
        # live: drop today's session if its close already passed
        now_utc = dt.datetime.now(dt.timezone.utc)
        sessions = [s for s in sessions if s[1] > now_utc]
    if not is_opt:
        sessions = sessions[: args.days]
    if not sessions:
        sys.exit("no remaining sessions before horizon — nothing to enumerate")

    # ---- compress into steps ----
    n = len(sessions)
    k = min(args.max_steps, n)
    base, extra = divmod(n, k)
    steps, i = [], 0
    for j in range(k):
        size = base + (1 if j < extra else 0)
        chunk = sessions[i : i + size]
        i += size
        steps.append(chunk)  # each step = list of sessions; checkpoint = last close

    sigma_day = spot * iv / math.sqrt(252.0)

    # ---- grid ----
    pos_levels = sorted({float(x) for x in args.grid.split(",")}, reverse=True)
    levels = sorted({lv for lv in pos_levels} | {-lv for lv in pos_levels}, reverse=True)
    weights = grid_weights(levels)

    # ---- print timeline ----
    W = 78
    print("=" * W)
    label = (f"{args.ticker} {args.expiry} {args.strike:g}{'C' if args.type=='call' else 'P'}"
             if is_opt else f"{args.ticker} {args.type}"
             + (f" {args.leverage:g}x" if args.type == "letf" else ""))
    mode = f"  [BACKTEST as of {asof} close]" if asof else ""
    print(f"SCENARIO TREE — {label}  |  spot {spot:.2f}  IV {iv*100:.1f}%  "
          f"sigma/day ${sigma_day:.2f} ({100*sigma_day/spot:.2f}%){mode}")
    print(f"position: {args.qty:g} x {mult:g} @ {args.entry_price:g}  "
          f"= cost ${args.qty*mult*args.entry_price:,.0f}"
          + (f"  |  account ${args.account:,.0f}" if args.account else ""))
    print("=" * W)
    print("\n[A] TIMELINE  (checkpoints = session closes; between checkpoints you may")
    print("    be LOCKED overnight/weekend — each step already contains its gap)")
    prev_date = today
    for j, chunk in enumerate(steps, 1):
        d0, d1 = chunk[0][0], chunk[-1][0]
        ndays = len(chunk)
        gap = (chunk[0][0] - prev_date).days
        gap_note = f"  <- {gap-1} calendar day(s) closed before this step" if gap > 1 else ""
        rng = f"{d0}" if d0 == d1 else f"{d0} .. {d1}"
        sig = sigma_day * math.sqrt(ndays)
        print(f"  step {j}: {rng}  ({ndays} session{'s' if ndays>1 else ''}, "
              f"step sigma ${sig:.2f} / {100*sig/spot:.2f}%){gap_note}")
        prev_date = d1
    if is_opt:
        print(f"  expiry: {horizon}  (option settles at last checkpoint)")

    print(f"\n[B] MOVE GRID per step (sigma-multiples, normal-discretized weights)")
    print("  " + "   ".join(f"{lv:+.0f}s:{w*100:.0f}%" for lv, w in zip(levels, weights)))

    # ---- enumerate paths ----
    step_sigmas = [sigma_day * math.sqrt(len(c)) for c in steps]
    step_dates = [c[-1][0] for c in steps]
    cost = args.qty * mult * args.entry_price

    def unit_value(S, when: dt.date):
        if is_opt:
            T = max(0.0, (horizon - when).days) / 365.0
            return bs_value(S, args.strike, T, args.r, iv, args.type)
        return S  # stock handled at position level; letf handled path-wise

    paths = []
    for combo in itertools.product(range(len(levels)), repeat=len(steps)):
        S = spot
        F = args.entry_price  # letf price tracker
        node_vals, prob = [], 1.0
        for j, gi in enumerate(combo):
            move = levels[gi] * step_sigmas[j]
            S_prev, S = S, max(0.01, S + move)
            prob *= weights[gi]
            if args.type == "letf":
                # daily reset applied session-by-session within the step
                nd = len(steps[j])
                daily = (S / S_prev) ** (1.0 / nd) - 1.0
                F *= (1.0 + args.leverage * daily) ** nd
                node_vals.append(F * args.qty * mult)
            elif args.type == "stock":
                node_vals.append(S * args.qty * mult)
            else:
                node_vals.append(unit_value(S, step_dates[j]) * args.qty * mult)
        pnl = node_vals[-1] - cost
        paths.append({
            "seq": combo, "S_end": S, "prob": prob,
            "nodes": node_vals, "pnl": pnl,
        })

    # ---- scenario table ----
    def seq_str(combo):
        return " > ".join(f"{levels[g]:+.0f}s" for g in combo)

    paths_sorted = sorted(paths, key=lambda p: p["pnl"], reverse=True)
    total = len(paths)
    print(f"\n[C] SCENARIO TABLE — {total} exhaustive paths "
          f"({len(levels)} branches ^ {len(steps)} steps), sorted by PnL")
    hdr = f"  {'#':>3} {'path':<{7*len(steps)}} {'S_end':>8} {'value$':>10} {'PnL$':>11}"
    if args.account:
        hdr += f" {'acct%':>7}"
    hdr += f" {'prob':>6}"
    print(hdr)

    def row(i, p):
        line = (f"  {i:>3} {seq_str(p['seq']):<{7*len(steps)}} {p['S_end']:>8.2f} "
                f"{p['nodes'][-1]:>10,.0f} {p['pnl']:>+11,.0f}")
        if args.account:
            line += f" {100*p['pnl']/args.account:>+6.1f}%"
        line += f" {p['prob']*100:>5.1f}%"
        if args.sleep_pct and args.account and p["pnl"] <= -args.sleep_pct/100*args.account:
            line += "  <SLEEP-LINE BREACH"
        return line

    if total <= 40:
        for i, p in enumerate(paths_sorted, 1):
            print(row(i, p))
    else:
        for i, p in enumerate(paths_sorted[:10], 1):
            print(row(i, p))
        print(f"  ... {total-20} middle paths omitted (full set still in summary) ...")
        for i, p in enumerate(paths_sorted[-10:], total - 9):
            print(row(i, p))

    # ---- exit matrix ----
    print(f"\n[D] EXIT MATRIX — PnL if you exit AT each checkpoint (across all paths)")
    print(f"  {'checkpoint':<14} {'min PnL$':>11} {'median$':>11} {'max PnL$':>11}   (actable window)")
    for j, dte in enumerate(step_dates):
        vals = sorted(p["nodes"][j] - cost for p in paths)
        med = vals[len(vals)//2]
        print(f"  {str(dte):<14} {vals[0]:>+11,.0f} {med:>+11,.0f} {vals[-1]:>+11,.0f}   "
              f"(at {dte} session close)")

    # ---- summary ----
    exp_pnl = sum(p["pnl"] * p["prob"] for p in paths)
    p_loss = sum(p["prob"] for p in paths if p["pnl"] < 0)
    best, worst = paths_sorted[0], paths_sorted[-1]
    print(f"\n[E] SUMMARY (derived from the {total}-path enumeration — no hand-waving)")
    print(f"  MAX PnL:      {best['pnl']:>+12,.0f}  path {seq_str(best['seq'])}  (prob {best['prob']*100:.2f}%)")
    print(f"  MIN PnL:      {worst['pnl']:>+12,.0f}  path {seq_str(worst['seq'])}  (prob {worst['prob']*100:.2f}%)")
    print(f"  EXPECTED PnL: {exp_pnl:>+12,.0f}  (probability-weighted)")
    print(f"  P(loss):      {p_loss*100:>11.1f}%")
    if args.account:
        print(f"  MAX acct dmg: {100*worst['pnl']/args.account:>+11.1f}%")
        if args.sleep_pct:
            p_breach = sum(p["prob"] for p in paths
                           if p["pnl"] <= -args.sleep_pct/100*args.account)
            verdict = "FAIL" if p_breach > 0 else "PASS"
            print(f"  P(sleep-line -{args.sleep_pct:g}% breach): {p_breach*100:.1f}%  -> sleep test {verdict}")
    print("\n  caveats: constant IV (no crush/spike), normal-discretized step probs,")
    print("  steps are close-to-close so each includes its preceding locked gap.")

if __name__ == "__main__":
    main()
