# Indicator interpretation guide

This is the reasoning reference for `/big-position-think-twice`. The dashboard
script prints raw values + rough flags; use this file to turn them into a
coherent **regime read** and to run the PnL / timing / sizing math consistently.

> These thresholds are heuristics, not laws. Levels drift over regimes — always
> weight the **direction and speed of change** over the absolute level, and lean
> on 52-week percentile when a level is ambiguous.

---

## 1. The required quant indicators (spec §2.1)

| Indicator | Source | Calm | Watch | Stress | What it means |
|---|---|---|---|---|---|
| **VIX** | `^VIX` | <15 | 15–20 | >20, spike >30 | Index-level 30d implied vol. The headline "fear gauge." |
| **VVIX** | `^VVIX` | <90 | 90–110 | >110 | Vol-of-vol. High = the vol surface itself is unstable; tail hedging is getting bid. |
| **VIX9D/VIX** | computed | <1 (contango) | ~1 | >1 backwardation | Near-term panic vs 30d. >1 = acute, immediate stress. |
| **VIX/VIX3M** | computed | <1 (contango) | ~1 | >1 backwardation | Curve inversion = market pricing near-term shock. |
| **MOVE** | `^MOVE` | <80 | 80–120 | >120 | Bond-market VIX. Rate vol leads equity vol; a MOVE spike is an early warning. |
| **VIXEQ** | `^VIXEQ` | — | — | — | Cboe S&P 500 **Constituent** Volatility Index — cap-weighted single-stock IV. Structurally higher than VIX. |
| **VIXEQ/VIX** | computed | — | rising | high & rising | **Frank's #5.** High = single stocks are wild but the index is pinned (low correlation / heavy dispersion). Calm surface, unstable interior. Danger is when correlation snaps back up and single-stock vol transmits into index vol → fast index down-move. |
| **COR1M** | `^COR1M` | low | — | **rising fast** | Cboe 1-month implied correlation. The other side of the dispersion coin. LOW corr + HIGH VIXEQ/VIX = fragile calm. A sharp **rise** in COR1M is the trigger signal. |
| **COR3M** | `^COR3M` | — | — | — | 3-month implied correlation; term context for COR1M. |
| **CCC−BB OAS spread** | FRED `BAMLH0A3HYC` − `BAMLH0A1HYBB` | stable/tight | — | **widening** | Junkiest credit vs mid-junk. Low-quality credit cracks *before* equities. Widening = risk-off leading indicator. |
| **HYG/IEI** | `HYG`÷`IEI` | rising/flat | — | **falling** | High-yield vs safe Treasuries = credit risk appetite. Falling = money leaving junk for safety. |
| **SOFR−IORB** | FRED `SOFR` − `IORB` | <0 to +5bps | +5–10bps | >10bps | Funding plumbing. SOFR pushing above IORB = repo/funding pressure (dollar scarcity). |

### Combining into a regime read
- **Green (add-risk OK):** VIX calm + contango term structure + MOVE calm + HYG/IEI flat-to-up + CCC−BB stable + SOFR−IORB benign.
- **Yellow (fragile calm — the dangerous one):** VIX low BUT VIXEQ/VIX high + COR1M low/creeping up + CCC−BB quietly widening. Surface says calm, plumbing says stressed. Big positions here get blindsided.
- **Red (de-risk):** VIX >20 & rising, term structure backwardated, MOVE >120, HYG/IEI falling hard, CCC−BB widening fast, SOFR−IORB >10bps. Do not size up.

The single most valuable pattern for this skill: **low VIX + high VIXEQ/VIX + rising COR1M + widening CCC−BB.** That is "everyone thinks it's calm right before it isn't." Flag it loudly.

## 2. Required non-quant indicators (spec §2.2) — via WebSearch
- **Index rebalancing:** S&P & Nasdaq quarterly rebalances (3rd Friday Mar/Jun/Sep/Dec); month/quarter-end index reconstitution flows. Big MOC imbalances.
- **Pension rebalancing:** month-end and especially quarter-end, pensions rebalance stock↔bond to targets — estimated multi-$bn flows, direction depends on the quarter's equity move.
- **Pending Fed events:** next FOMC date + whether it's a "live" meeting, Fed speakers, minutes, and the big data prints (CPI, NFP, PCE) before your intended hold horizon. A position held over an unhedged Fed/CPI print is a different risk than an intraday scalp.

## 3. Worth-considering objective (spec §2.3)
- Concerning index/sector/single-stock **past 5-day move** (script prints it), plus 20d.
- **EMA / SMA** alignment (script prints SMA20/50, EMA20/50). Price vs SMA20 = short-term trend; SMA20 vs SMA50 = regime.

---

## 4. PnL math (spec §3.1 & §5.2) — scenario-tree enumeration

**The rule: max/min PnL are *derived* from an exhaustively enumerated scenario set,
never assumed.** `scripts/scenario_pnl.py` is the engine; its model:

- **Time axis:** every remaining NYSE session between now and the horizon/expiry
  (holiday & half-day aware), compressed into ≤ `--max-steps` steps. Checkpoints are
  session closes — the only moments you can act. Every step *contains* the locked
  overnight/weekend gap that precedes it, which is exactly the lockout structure of §5.
- **Move axis:** per step, sigma-multiples {+2σ, +1σ, 0, −1σ, −2σ} where
  `σ_step = spot × IV × sqrt(sessions_in_step / 252)`; each branch gets its
  normal-discretized probability (≈7/24/38/24/7%).
- **Paths:** the full cartesian product (branches^steps). Each path is repriced at
  every node — Black-Scholes for options (constant IV), daily-reset compounding for
  LETFs (decay emerges naturally), linear for stock.
- **Outputs:** [A] timeline+locked gaps, [B] move grid, [C] full path table with
  PnL$/accountΔ%/probability per path and sleep-line breaches flagged, [D] exit matrix
  per checkpoint, [E] MAX/MIN/EXPECTED PnL + P(loss) + P(sleep-breach), each tied to
  its exact path.

How to read it with the user:
- The extremes ([E] MAX/MIN) come with their **path and probability** — a +157% path at
  0.4% probability is a lottery ticket, not a scenario to size for.
- The **middle paths** are where the surprises live (e.g. "+1σ then flat" still losing
  ~90% on a short-dated OTM option). Call these out explicitly.
- **Sleep test:** judge on **P(breach)** and MAX damage together, not just the worst
  path. Material breach probability (say >20–30%) = position too big *for this person*,
  independent of thesis.
- **ATR cross-check:** the dashboard's ATR expected move should roughly bracket the
  engine's ±1σ path endpoints; a large mismatch means the IV input is stale — say so.
- **Context the loss:** express worst-case $ as "= X days/weeks of your recent portfolio
  PnL" so it's felt, not abstract.
- Honest caveats every time: constant IV (no crush/spike), discretized probabilities,
  close-to-close steps.

## 5. Timing — intraday segments, lockout windows, and WHO moves the price while you sleep (spec §3.2)

Only enumerate if the entry is a **late-session** operation. The engine's intraday
mode (`--entry-time`) computes all of this; this section is the mental model.

### The segment map for a late-session index-option entry (ET)

| Segment | Window | Can act? | What moves the price here |
|---|---|---|---|
| close auction | entry→16:00 | ✅ | **MOC imbalances; month/quarter-end pension & index rebalance flows** (mechanical dumps/pumps into the bell) |
| late close | 16:00→16:15 | ✅ *only* SPY/QQQ/IWM/DIA/SPX/XSP/NDX/RUT-class options | thin, but it's your last exit ("SF 1:15pm" = 4:15pm ET) |
| **overnight** | 16:15→9:30 | ❌ **LOCKED** | ES/NQ futures 24h; **Asia session (KR NPS, JP GPIF quarter-turn rebalancing, China headlines)**; Europe open. A flow-driven dump can hit while you cannot touch the option |
| morning window | 9:30→10:30 | ✅ | gap realized; **this is the dip-buy window** |
| rest of day | 10:30→16:00 | ✅ | normal tape |

Single-name options lose the late-close row (locked from 16:00). Daily closes look
flat while hiding a huge overnight dip — **the daily candle lies; the segment map doesn't.**

### Flow calendar (mechanical, schedulable — check every run)
- **Month-end / quarter-end (±3 days):** US pensions rebalance into the close auction;
  Asia pensions (KR NPS, JP GPIF) rebalance in THEIR session = your locked overnight.
  Quarter-end (Mar/Jun/Sep/Dec) is the big one. 6/30 尾盘砸 + 半夜韩国养老金接着砸
  就是这个形态。
- **Index rebalances:** S&P/Nasdaq/Russell quarterly, 3rd Friday of Mar/Jun/Sep/Dec
  (Russell reconstitution late June) — massive MOC prints.
- Flow-driven moves are **mechanical, not informational** → elevated mean-reversion
  odds next morning. This is why "wait for the morning dip, then size up (re-strike
  ATM at the new spot)" beats "buy the close" around these dates — the engine's [F]
  table shows the cheaper-entry effect; the mean-reversion effect comes ON TOP of it
  (the engine is drift-free — say so).

**B. Leveraged single-stock ETFs (daily-reset, ETH-traded):**
7:55–8pm actable → 8pm–7am **LOCKED** (no ETH liquidity) → 7:00am reopen; plus
daily-reset decay compounds against you on reversals.

**C. Regular stock + regular ETF (24h venues):** no lockout enumeration; the risk is
overnight slippage/thin liquidity, not being locked out.

The deliverables: (1) "if I enter now, a −1σ/−2σ overnight gap costs $__ / account __%
while I'm locked", (2) the [F] strategy comparison — enter now vs late-close vs
wait-for-dip, (3) whether tonight is a flow night.

## 6. Debate (spec §4) & conclusion (spec §5)
Run an honest two-sided debate:
- **Pro-entry:** the strongest *reason to enter now* (thesis, setup, asymmetry, catalyst timing).
- **Anti-entry:** the strongest *reason to wait/pass* (regime is Yellow/Red, size fails sleep test, stuck-risk unhedgeable, chasing/FOMO).

Then present, **without declaring a winner**, the single most crucial point of each side. Let the user decide.

## 7. Best-execution strategy (spec §5.3) — only if user still wants in

**Timing** — options on the table:
- Enter anytime intraday / on a pullback to SMA20.
- Wait for last 5 min (3:55pm) — lets the day's tape resolve first.
- Wait for last 15s — minimize intraday whipsaw, accept close print.
- Late-Close variants (4:15pm −5min / −15s) if available.
- ETH variants (8pm −5min / −15s) for LETFs.
- Wait for next-day open — avoid the overnight gap entirely.

**Hedging** — options:
- Enter a small opposite instrument into the close (call→put, long→short) to neutralize the locked-out overnight window.
- Wait for next-day open instead of holding over an unhedged event.
- Structure as a spread / butterfly to cap the worst case (define max loss up front).

**Sizing — Kelly, capped by the sleep test:**
- Kelly fraction: `f* = edge/odds = (p·b − (1−p)) / b`, where `p` = your estimated win prob, `b` = win/loss payoff ratio. Use **fractional Kelly (¼–½)** — full Kelly is too violent for real accounts.
- **Hard cap:** whatever Kelly says, cap the position so that `worst-case account Δ%` ≤ the user's stated sleep-loss %. The sleep test always overrides Kelly. Report both the Kelly size and the sleep-capped size, and recommend the smaller.
