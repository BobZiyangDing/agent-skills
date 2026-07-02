#!/usr/bin/env python3
"""
scenario_pnl.py — exhaustive scenario-tree PnL engine for big-position-think-twice.

Two modes:

DAILY mode (default): timeline steps are session closes to the horizon.

INTRADAY mode (--entry-time HH:MM, ET; options only): models the real
microstructure of a late-session entry — the exact situation the skill exists
for. Segments of the first ~24h are enumerated separately:

    seg1  entry -> 16:00   close auction   ACTABLE   (MOC imbalance / pension
                                                      rebalance flows at month
                                                      & quarter ends)
    seg2  16:00 -> 16:15   late close      ACTABLE only for SPY/QQQ/IWM/DIA/
                                           SPX/XSP/NDX/RUT-class options
    seg3  16:15 -> 9:30    OVERNIGHT       LOCKED — futures/Asia/Europe move
                                           (KR/JP pension flows at quarter
                                           turn), you cannot touch the option
    seg4  9:30 -> 10:30    morning window  ACTABLE  (the "buy the dip" window)
    seg5  10:30 -> 16:00   rest of day     ACTABLE
    then remaining sessions compressed into <=2 daily chunks to expiry.

Every path through (branches per segment) is enumerated and repriced with
Black-Scholes at every node. On top of the usual [A]-[E] outputs, intraday
mode adds:

    [F] ENTRY-TIMING COMPARISON — same budget deployed via four strategies:
        enter-now / enter-at-late-close(16:14) / wait-next-morning(10:30) /
        wait-morning-ONLY-if-overnight-dip; expected PnL per overnight
        scenario. This answers "should I buy the close or buy tomorrow's dip"
        with numbers instead of vibes.

Spot is fetched INSTANTANEOUSLY (fast_info live price, falling back to the
latest 1-minute bar, then daily close) and the quote timestamp is printed.
This tool feeds a fast, now-or-not decision — never silently use a stale close.

Usage (intraday, the 6/30 pension-rebalance case):
  uv run --with yfinance --with pandas-market-calendars python3 scenario_pnl.py \
    --ticker SPY --type call --strike 754 --expiry 2026-07-07 \
    --asof 2026-06-30 --entry-time 15:45 --iv 0.072 --entry-price 0.77 \
    --qty 133 --account 51234 --sleep-pct 10

Honest limitations: constant IV (no crush/spike), normal-discretized branch
probabilities, premium floored at $0.05 for entry-sizing (liquidity realism).
"""

import argparse
import datetime as dt
import itertools
import math
import sys
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
LATE_CLOSE_UNDERLYINGS = {"SPY", "QQQ", "IWM", "DIA", "XSP", "SPX", "^SPX",
                          "^XSP", "NDX", "^NDX", "RUT", "^RUT", "VIX", "^VIX"}
PREMIUM_FLOOR = 0.05  # $/share floor when sizing a delayed entry

# ---------------- black-scholes ----------------

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bs_value(S, K, T_years, r, iv, opt_type):
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

# ---------------- market data ----------------

def nyse_sessions(start: dt.date, end: dt.date):
    import pandas_market_calendars as pmc
    cal = pmc.get_calendar("NYSE")
    sched = cal.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    return [(idx.date(), row["market_close"].to_pydatetime())
            for idx, row in sched.iterrows()]

def live_spot(ticker):
    """Instantaneous price: fast_info -> latest 1m bar -> daily close.
    Returns (price, source, timestamp_str)."""
    import yfinance as yf
    tk = yf.Ticker(ticker)
    try:
        p = tk.fast_info.get("last_price") or tk.fast_info.get("lastPrice")
        if p and p > 0:
            return float(p), "fast_info(live)", dt.datetime.now(ET).strftime("%H:%M:%S ET")
    except Exception:
        pass
    try:
        h = tk.history(period="1d", interval="1m")
        if not h.empty:
            ts = h.index[-1].tz_convert(ET)
            return float(h["Close"].iloc[-1]), "1m-bar", ts.strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        pass
    h = tk.history(period="1d")
    return float(h["Close"].iloc[-1]), "daily-close(STALE — flag to user)", str(h.index[-1].date())

def grid_weights(levels):
    ws = []
    for i, lv in enumerate(levels):
        hi = math.inf if i == 0 else (levels[i - 1] + lv) / 2.0
        lo = -math.inf if i == len(levels) - 1 else (lv + levels[i + 1]) / 2.0
        hi_c = 1.0 if hi is math.inf else norm_cdf(hi)
        lo_c = 0.0 if lo is -math.inf else norm_cdf(lo)
        ws.append(hi_c - lo_c)
    s = sum(ws)
    return [w / s for w in ws]

G3 = [1.0, 0.0, -1.0]           # intraday fine segments
G5 = [2.0, 1.0, 0.0, -1.0, -2.0]  # overnight & daily chunks (fat-tail aware)

def month_end_flags(day: dt.date, sessions_all):
    """Is `day` the last session of its month / quarter? -> flow warnings."""
    later = [d for d, _ in sessions_all if d > day]
    nxt = later[0] if later else None
    me = nxt is not None and nxt.month != day.month
    qe = me and day.month in (3, 6, 9, 12)
    return me, qe

# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser(description="Exhaustive scenario-tree PnL")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--type", required=True, choices=["call", "put", "stock", "letf"])
    ap.add_argument("--strike", type=float)
    ap.add_argument("--expiry", help="YYYY-MM-DD (options)")
    ap.add_argument("--days", type=int, help="horizon in trading days (stock/letf)")
    ap.add_argument("--entry-price", type=float, required=True)
    ap.add_argument("--qty", type=float, required=True)
    ap.add_argument("--multiplier", type=float, default=None)
    ap.add_argument("--account", type=float)
    ap.add_argument("--sleep-pct", type=float)
    ap.add_argument("--iv", type=float)
    ap.add_argument("--spot", type=float)
    ap.add_argument("--leverage", type=float, default=2.0)
    ap.add_argument("--r", type=float, default=0.04)
    ap.add_argument("--max-steps", type=int, default=3)
    ap.add_argument("--grid", default="2,1,0")
    ap.add_argument("--asof", help="YYYY-MM-DD backtest: entry on this session")
    ap.add_argument("--entry-time", help="HH:MM ET -> INTRADAY mode (options only)")
    ap.add_argument("--overnight-var-share", type=float, default=0.35,
                    help="share of a day's variance realized 16:15->9:30")
    args = ap.parse_args()

    is_opt = args.type in ("call", "put")
    if is_opt and (not args.strike or not args.expiry):
        sys.exit("options need --strike and --expiry")
    if not is_opt and not args.days:
        sys.exit("stock/letf needs --days")
    if args.entry_time and not is_opt:
        sys.exit("--entry-time (intraday mode) currently supports options only")
    mult = args.multiplier if args.multiplier is not None else (100.0 if is_opt else 1.0)
    asof = dt.date.fromisoformat(args.asof) if args.asof else None

    # ---- spot & iv: INSTANTANEOUS by default ----
    spot, iv, spot_src, spot_ts = args.spot, args.iv, "user-supplied", "-"
    import yfinance as yf
    tk = yf.Ticker(args.ticker)
    if spot is None:
        if asof:
            h = tk.history(start=asof.isoformat(),
                           end=(asof + dt.timedelta(days=4)).isoformat())
            if h.empty or h.index[0].date() != asof:
                sys.exit(f"no session close for --asof {asof}")
            spot, spot_src, spot_ts = float(h["Close"].iloc[0]), "backtest-close", str(asof)
        else:
            spot, spot_src, spot_ts = live_spot(args.ticker)
    if is_opt and iv is None:
        if asof:
            print("  [warn] backtest: historical IV unavailable, using CURRENT "
                  "chain IV as proxy", file=sys.stderr)
        try:
            ch = tk.option_chain(args.expiry)
            side = ch.calls if args.type == "call" else ch.puts
            side = side.assign(dist=(side["strike"] - args.strike).abs()).sort_values("dist")
            iv = float(side.iloc[0]["impliedVolatility"])
        except Exception as e:
            sys.exit(f"could not fetch IV (pass --iv): {e}")
    if iv is None:
        import statistics
        h = tk.history(period="3mo")["Close"]
        rets = [math.log(b / a) for a, b in zip(h[:-1], h[1:])]
        iv = statistics.stdev(rets) * math.sqrt(252)

    today = asof if asof else dt.date.today()
    horizon = dt.date.fromisoformat(args.expiry) if is_opt else None
    end_for_cal = horizon if is_opt else today + dt.timedelta(days=args.days * 2 + 15)
    sessions_all = nyse_sessions(today, end_for_cal)
    sigma_day = spot * iv / math.sqrt(252.0)
    intraday = bool(args.entry_time)

    # =====================================================================
    # SEGMENTS: list of dicts {label, sigma, levels, weights, actable, note,
    #                          t_end (datetime ET), checkpoint_label}
    # =====================================================================
    segs = []
    ovn = args.overnight_var_share

    if intraday:
        eh, em = map(int, args.entry_time.split(":"))
        d1 = today
        if d1 not in [d for d, _ in sessions_all]:
            sys.exit(f"{d1} is not a trading session")
        entry_dt = dt.datetime(d1.year, d1.month, d1.day, eh, em, tzinfo=ET)
        rest = [d for d, _ in sessions_all if d > d1]
        if not rest:
            sys.exit("intraday mode needs at least one session after entry day")
        d2 = rest[0]
        me, qe = month_end_flags(d1, sessions_all)
        flow1 = (" ** QUARTER-END: pension/index rebalance flows in the close auction **" if qe
                 else " ** MONTH-END: pension rebalance flows in the close auction **" if me
                 else "")
        flow2 = (" ** quarter turn: Asia-session pension flows (KR NPS/JP GPIF) can hit "
                 "futures while you are LOCKED **" if (me or qe) else
                 " (futures/Asia/Europe move; you cannot touch the option)")
        late_ok = args.ticker.upper() in LATE_CLOSE_UNDERLYINGS

        mins_to_close = max(1.0, (dt.datetime(d1.year, d1.month, d1.day, 16, 0, tzinfo=ET)
                                  - entry_dt).total_seconds() / 60.0)
        def d_(day, h, m): return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=ET)

        segs.append(dict(label=f"{args.entry_time}->16:00 close auction",
                         sigma=sigma_day * math.sqrt((mins_to_close/390.0)*(1-ovn)),
                         levels=G3, actable=True, t_end=d_(d1,16,0),
                         note="ACTABLE" + flow1))
        segs.append(dict(label="16:00->16:15 late-close window",
                         sigma=sigma_day * math.sqrt((15/390.0)*(1-ovn)),
                         levels=G3, actable=late_ok, t_end=d_(d1,16,15),
                         note=("ACTABLE (index-ETF/index option late close)" if late_ok
                               else "LOCKED (single-name options stop at 16:00)")))
        segs.append(dict(label=f"16:15->{d2} 9:30 OVERNIGHT",
                         sigma=sigma_day * math.sqrt(ovn),
                         levels=G5, actable=False, t_end=d_(d2,9,30),
                         note="LOCKED" + flow2))
        segs.append(dict(label=f"{d2} 9:30->10:30 morning (dip-buy window)",
                         sigma=sigma_day * math.sqrt((60/390.0)*(1-ovn)),
                         levels=G3, actable=True, t_end=d_(d2,10,30),
                         note="ACTABLE — gap is realized, you can re-decide"))
        segs.append(dict(label=f"{d2} 10:30->16:00 rest of day",
                         sigma=sigma_day * math.sqrt((330/390.0)*(1-ovn)),
                         levels=G3, actable=True, t_end=d_(d2,16,0),
                         note="ACTABLE"))
        # compress remaining sessions to <=2 chunks
        remaining = [s for s in sessions_all if s[0] > d2]
        if remaining:
            k = min(2, len(remaining))
            base, extra = divmod(len(remaining), k)
            i = 0
            for j in range(k):
                size = base + (1 if j < extra else 0)
                chunk = remaining[i:i+size]; i += size
                d_end = chunk[-1][0]
                segs.append(dict(
                    label=f"{chunk[0][0]}..{d_end}" if len(chunk) > 1 else f"{d_end}",
                    sigma=sigma_day * math.sqrt(len(chunk)),
                    levels=G5, actable=True, t_end=d_(d_end,16,0),
                    note=f"ACTABLE ({len(chunk)} session{'s' if len(chunk)>1 else ''}, daily granularity)"))
    else:
        # DAILY mode (unchanged behavior)
        if asof:
            sess = [s for s in sessions_all if s[0] > asof]
        else:
            now_utc = dt.datetime.now(dt.timezone.utc)
            sess = [s for s in sessions_all if s[1] > now_utc]
        if not is_opt:
            sess = sess[: args.days]
        if not sess:
            sys.exit("no remaining sessions before horizon")
        k = min(args.max_steps, len(sess))
        base, extra = divmod(len(sess), k)
        pos_levels = sorted({float(x) for x in args.grid.split(",")}, reverse=True)
        lv = sorted({l for l in pos_levels} | {-l for l in pos_levels}, reverse=True)
        i = 0
        prev = today
        for j in range(k):
            size = base + (1 if j < extra else 0)
            chunk = sess[i:i+size]; i += size
            gap = (chunk[0][0] - prev).days
            d_end = chunk[-1][0]
            segs.append(dict(
                label=f"{chunk[0][0]}..{d_end}" if len(chunk) > 1 else f"{d_end}",
                sigma=sigma_day * math.sqrt(len(chunk)),
                levels=lv, actable=True,
                t_end=dt.datetime(d_end.year, d_end.month, d_end.day, 16, 0, tzinfo=ET),
                note=(f"includes {gap-1} closed calendar day(s) before it" if gap > 1 else "")))
            prev = d_end

    for s in segs:
        s["weights"] = grid_weights(s["levels"])

    expiry_dt = (dt.datetime(horizon.year, horizon.month, horizon.day, 16, 0, tzinfo=ET)
                 if is_opt else segs[-1]["t_end"])

    def unit_value(S, t_node):
        if is_opt:
            T = max(0.0, (expiry_dt - t_node).total_seconds()) / (365.0 * 86400.0)
            return bs_value(S, args.strike, T, args.r, iv, args.type)
        return S

    cost = args.qty * mult * args.entry_price
    budget = cost

    # ---------------- header ----------------
    W = 78
    label = (f"{args.ticker} {args.expiry} {args.strike:g}{'C' if args.type=='call' else 'P'}"
             if is_opt else f"{args.ticker} {args.type}")
    mode = ("INTRADAY" + (f" BACKTEST asof {asof} {args.entry_time} ET" if asof
                          else f" live {args.entry_time} ET")) if intraday else \
           (f"DAILY BACKTEST asof {asof}" if asof else "DAILY live")
    print("=" * W)
    print(f"SCENARIO TREE [{mode}] — {label}")
    print(f"spot {spot:.2f}  [{spot_src} @ {spot_ts}]   IV {iv*100:.1f}%   "
          f"sigma/day ${sigma_day:.2f} ({100*sigma_day/spot:.2f}%)")
    print(f"position: {args.qty:g} x {mult:g} @ {args.entry_price:g} = ${cost:,.0f}"
          + (f"  |  account ${args.account:,.0f}" if args.account else ""))
    print("=" * W)

    print("\n[A] TIMELINE — segments, actable vs LOCKED, flow warnings")
    for j, s in enumerate(segs, 1):
        print(f"  seg{j}: {s['label']:<38} sigma ${s['sigma']:.2f} "
              f"({100*s['sigma']/spot:.2f}%)  [{len(s['levels'])}-branch]")
        print(f"        {s['note']}")
    if is_opt:
        print(f"  expiry: {horizon} 16:00 ET")

    print("\n[B] MOVE GRID per segment (sigma-multiples, normal-discretized weights)")
    shown = set()
    for s in segs:
        key = tuple(s["levels"])
        if key in shown:
            continue
        shown.add(key)
        print(f"  {len(s['levels'])}-branch: " +
              "  ".join(f"{l:+.0f}s:{w*100:.0f}%" for l, w in zip(s["levels"], s["weights"])))

    # ---------------- enumerate ----------------
    idx_ranges = [range(len(s["levels"])) for s in segs]
    paths = []
    for combo in itertools.product(*idx_ranges):
        S = spot; prob = 1.0; node_S = []; node_v = []
        F = args.entry_price
        for j, gi in enumerate(combo):
            s = segs[j]
            S_prev, S = S, max(0.01, S + s["levels"][gi] * s["sigma"])
            prob *= s["weights"][gi]
            node_S.append(S)
            if args.type == "letf":
                daily = (S / S_prev) - 1.0
                F *= (1.0 + args.leverage * daily)
                node_v.append(F)
            else:
                node_v.append(unit_value(S, s["t_end"]))
        pnl = (node_v[-1] - args.entry_price) * args.qty * mult
        paths.append(dict(seq=combo, S_end=S, prob=prob, vals=node_v, pnl=pnl))

    total = len(paths)
    paths_sorted = sorted(paths, key=lambda p: p["pnl"], reverse=True)

    def seq_str(combo):
        return "|".join(f"{segs[j]['levels'][g]:+.0f}" for j, g in enumerate(combo))

    print(f"\n[C] SCENARIO TABLE — {total} exhaustive paths, sorted by PnL "
          f"(path = sigma-moves per segment, in [A] order)")
    hdr = f"  {'#':>5} {'path':<{3*len(segs)+2}} {'S_end':>8} {'PnL$':>11}"
    if args.account:
        hdr += f" {'acct%':>7}"
    hdr += f" {'prob':>7}"
    print(hdr)
    def prow(i, p):
        line = (f"  {i:>5} {seq_str(p['seq']):<{3*len(segs)+2}} {p['S_end']:>8.2f} "
                f"{p['pnl']:>+11,.0f}")
        if args.account:
            line += f" {100*p['pnl']/args.account:>+6.1f}%"
        line += f" {p['prob']*100:>6.2f}%"
        if args.sleep_pct and args.account and p["pnl"] <= -args.sleep_pct/100*args.account:
            line += "  <BREACH"
        return line
    if total <= 40:
        for i, p in enumerate(paths_sorted, 1):
            print(prow(i, p))
    else:
        for i, p in enumerate(paths_sorted[:8], 1):
            print(prow(i, p))
        print(f"  ..... {total-16} middle paths omitted (all included in every stat below) .....")
        for i, p in enumerate(paths_sorted[-8:], total-7):
            print(prow(i, p))

    print(f"\n[D] EXIT MATRIX — position PnL if you exit AT each checkpoint")
    print(f"  {'checkpoint':<44} {'min$':>10} {'median$':>10} {'max$':>10}")
    for j, s in enumerate(segs):
        vals = sorted((p["vals"][j] - args.entry_price) * args.qty * mult for p in paths)
        tag = "" if s["actable"] or j+1 == len(segs) else "  [end of LOCKED window]"
        print(f"  seg{j+1} end: {s['label']:<36} {vals[0]:>+10,.0f} "
              f"{vals[len(vals)//2]:>+10,.0f} {vals[-1]:>+10,.0f}{tag}")

    exp_pnl = sum(p["pnl"] * p["prob"] for p in paths)
    p_loss = sum(p["prob"] for p in paths if p["pnl"] < 0)
    best, worst = paths_sorted[0], paths_sorted[-1]
    print(f"\n[E] SUMMARY (derived from the {total}-path enumeration)")
    print(f"  MAX PnL:  {best['pnl']:>+12,.0f}  path {seq_str(best['seq'])}  (prob {best['prob']*100:.3f}%)")
    print(f"  MIN PnL:  {worst['pnl']:>+12,.0f}  path {seq_str(worst['seq'])}  (prob {worst['prob']*100:.3f}%)")
    print(f"  EXPECTED: {exp_pnl:>+12,.0f}   P(loss): {p_loss*100:.1f}%")
    if args.account and args.sleep_pct:
        pb = sum(p["prob"] for p in paths if p["pnl"] <= -args.sleep_pct/100*args.account)
        print(f"  P(sleep-line -{args.sleep_pct:g}% breach): {pb*100:.1f}%  -> sleep test "
              f"{'FAIL' if pb > 0.25 else 'MARGINAL' if pb > 0 else 'PASS'}")

    # ---------------- [F] entry-timing comparison (intraday only) --------
    if intraday and is_opt:
        ovn_idx = next(i for i, s in enumerate(segs) if not s["actable"] or "OVERNIGHT" in s["label"])
        late_idx = 1   # end of late-close window
        morn_idx = ovn_idx + 1  # end of morning window
        late_ok = segs[late_idx]["actable"]

        def strat_pnl(p, entry_idx, cond_dip=None):
            """Deploy `budget` at node entry_idx of path p (premium floored);
            cond_dip: only enter if overnight move <= cond_dip sigma, else 0."""
            if cond_dip is not None:
                ovn_move = segs[ovn_idx]["levels"][p["seq"][ovn_idx]]
                if ovn_move > cond_dip:
                    return 0.0
            v_in = max(PREMIUM_FLOOR, p["vals"][entry_idx])
            return budget * (p["vals"][-1] - v_in) / v_in

        strategies = [("enter NOW (this close)", lambda p: budget*(p["vals"][-1]-args.entry_price)/args.entry_price)]
        if late_ok:
            strategies.append(("enter 16:14 late-close", lambda p: strat_pnl(p, late_idx)))
        strategies.append(("wait -> buy 10:30 tomorrow (always)", lambda p: strat_pnl(p, morn_idx)))
        strategies.append(("wait -> buy ONLY if overnight <= -1s dip", lambda p: strat_pnl(p, morn_idx, cond_dip=-1.0)))

        ovn_levels = segs[ovn_idx]["levels"]
        print(f"\n[F] ENTRY-TIMING COMPARISON — same ${budget:,.0f} budget, strategy x overnight scenario")
        print(f"    (expected PnL$ conditional on the overnight move; strike held constant;")
        print(f"     delayed entries floored at ${PREMIUM_FLOOR:.2f} premium — sizing realism)")
        colw = 11
        print(f"  {'strategy':<38}" + "".join(f"{f'ovn {l:+.0f}s':>{colw}}" for l in ovn_levels)
              + f"{'E[PnL]':>{colw}}{'P(loss)':>9}")
        for name, fn in strategies:
            row = f"  {name:<38}"
            for l in ovn_levels:
                cond = [(fn(p), p["prob"]) for p in paths
                        if segs[ovn_idx]["levels"][p["seq"][ovn_idx]] == l]
                tot = sum(w for _, w in cond)
                e = sum(v*w for v, w in cond) / tot if tot else 0.0
                row += f"{e:>+{colw},.0f}"
            allv = [(fn(p), p["prob"]) for p in paths]
            e_all = sum(v*w for v, w in allv)
            pl = sum(w for v, w in allv if v < 0)
            row += f"{e_all:>+{colw},.0f}{pl*100:>8.1f}%"
            print(row)
        print(f"    overnight scenario probabilities: " +
              "  ".join(f"{l:+.0f}s:{w*100:.0f}%" for l, w in
                        zip(ovn_levels, segs[ovn_idx]['weights'])))

    print("\n  caveats: constant IV (no crush/spike), normal-discretized branch probs,")
    print("  additive sigma steps, delayed-entry premium floored at "
          f"${PREMIUM_FLOOR:.2f}. Overnight var share = {ovn:.0%} of a day.")

if __name__ == "__main__":
    main()
