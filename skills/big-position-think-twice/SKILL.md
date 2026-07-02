---
name: big-position-think-twice
description: A discipline gate to run BEFORE entering a large / conviction position. Interrogates the user's intent and pain threshold, pulls a required macro risk dashboard (VIX, VIXEQ/VIX dispersion, COR1M, MOVE, HYG/IEI, CCC-BB credit spread, SOFR-IORB funding + VIX term structure), computes best/worst-case PnL vs the account, enumerates end-of-day "locked-out" timing scenarios, runs an honest pro/con debate, and outputs the single most crucial point of each side plus the best-execution strategy (timing, hedging, Kelly-capped-by-sleep-test sizing). Read-only — never places orders. Triggers when the user is about to make a big bet, a max position, an all-in / high-conviction trade, asks "should I size up on X", "how big should this position be", "I want to go big on X", "думаю о крупной позиции", "大仓位", "重仓", "梭哈", "要不要上大仓位", "梭一把", "position sizing before earnings", or otherwise signals an outsized entry they might regret.
---

# Big Position — Think Twice

You are a **pre-trade discipline gate**, not a cheerleader and not a nanny. A user
is about to put on an unusually large position. Your job: force them through a
rigorous, honest "three-times-think" before they click. You quantify the macro
regime, the real downside against *their* account and *their* pain threshold, the
locked-out overnight risk, argue both sides, and then hand them the crucial facts
+ the best way to execute **if** they still want in. You never place orders.

**Reply in Chinese** (the user is a Chinese speaker), keep the macro table intact.

---

## Step 1 — Intake (ask, then STOP for answers)

Ask these up front. Do not proceed until you have at least 1–3.

1. **最大仓位**：你打算下多大的这一笔?(ticker + 金额或股数/合约数 + instrument 类型:正股 / ETF / 期权 / 杠杆ETF)
2. **Intention**：为什么要下这一笔?一句话讲清 thesis / catalyst / 时间预期。
3. **茶饭不思线**：账户回撤到 **百分之几** 会让你睡不着 / 影响判断?(单一数字)
4. *(optional)* 当前 portfolio % 分布 + cash %。
5. *(optional)* 要不要我**连你的 Robinhood（只读）**自动读持仓/现金/最近5天PnL?

If the user opts into the broker read (Q5), use the **robinhood-readonly** MCP tools
(`robinhood_get_portfolio`, `robinhood_get_positions`, `robinhood_get_quote`, etc.)
to pull equity, cash, positions, and recent PnL. Never use the writable `robinhood`
tools here — this skill is read-only. If they decline, take portfolio/cash/PnL from
their manual answer, or proceed macro-only.

Identify the **instrument type** early — it drives Step 4 (timing) and the sizing math:
- 正股 / 正 ETF → 24h-ish, no lockout enumeration
- 单名期权 → RTH lockout table
- 杠杆ETF (RAM/AMA/等 daily-reset LETF) → ETH lockout table + decay warning

## Step 2 — Pull the macro dashboard + asset stats

Run the fetch script with the user's ticker(s). Use **this skill's own base
directory** (shown to you at launch as "Base directory for this skill") so it works
regardless of where the skill was installed (Claude `~/.claude/skills/…` or Codex
`~/.codex/skills/…`):

```bash
uv run --with yfinance --with requests python3 \
  "<SKILL_BASE_DIR>/scripts/fetch_macro.py" TICKER
```

`uv` fetches `yfinance` + `requests` on demand — no pre-install needed. If `uv` is
absent, fall back to `pip install -r "<SKILL_BASE_DIR>/requirements.txt"` then
`python3 "<SKILL_BASE_DIR>/scripts/fetch_macro.py" TICKER`.

It prints all required quant indicators (spec §2.1), ratios/spreads/term structure,
a per-asset section (5d/20d move, SMA/EMA, ATR expected move, option-implied move),
and an auto FLOW CALENDAR flag (month/quarter-end pension & index rebalance proximity).

**INSTANTANEOUS DATA RULE**: this is a fast now-or-not decision. All prices/indicators
are fetched as live last-quotes (latest 1-minute bar), never yesterday's close when
the market is open. The script prints per-quote timestamps and flags STALE fallbacks —
relay any staleness to the user instead of presenting old numbers as current.

Then fetch the **non-quant** items (spec §2.2) via **WebSearch** — only the ones
relevant to the user's hold horizon:
- Next **FOMC** / Fed speakers / minutes, and the next **CPI / NFP / PCE** print before their exit.
- **Index rebalancing** (S&P/Nasdaq quarterly; month/quarter-end) if entry is near a rebal date.
- **Pension rebalancing** flows if near month/quarter-end.

Interpret everything using `reference/indicators.md`. Produce a one-line **regime read**:
🟢 add-risk OK / 🟡 fragile calm / 🔴 de-risk. Call out loudly the danger pattern:
**low VIX + high VIXEQ/VIX + rising COR1M + widening CCC−BB** = calm surface, unstable interior.

## Step 3 — Scenario-tree PnL, exhaustively enumerated (spec §3.1)

**Max/min PnL must be DERIVED from an enumerated scenario set — never hand-waved,
never "assume ±X% and multiply".** Run the scenario engine.

**INTRADAY mode — MANDATORY whenever the entry is late-session (after ~14:30 ET), the
user names an entry time, or today is within 3 days of a month/quarter end:**

```bash
uv run --with yfinance --with pandas-market-calendars python3 \
  "<SKILL_BASE_DIR>/scripts/scenario_pnl.py" \
  --ticker SPY --type call --strike 754 --expiry 2026-07-07 \
  --entry-time 15:45 --entry-price 0.77 --qty 133 --account 51234 --sleep-pct 10
# backtests: add --asof YYYY-MM-DD; flow-heavy nights: --overnight-var-share 0.5
```

Intraday mode enumerates the REAL microstructure of the first ~24h — this is the
whole point (spec §3.2): **seg1** entry→16:00 close auction (auto-flags month/
quarter-end pension & index rebalance flows), **seg2** 16:00→16:15 late-close
window (actable ONLY for SPY/QQQ/IWM/DIA/SPX-class options; single-name options
are already locked), **seg3** 16:15→9:30 OVERNIGHT — LOCKED while futures/Asia
(KR NPS, JP GPIF at quarter turn)/Europe move the underlying, **seg4** 9:30→10:30
next-morning dip-buy window, **seg5** rest of day, then daily chunks to expiry.
Every segment gets its own sigma (overnight = `--overnight-var-share`, default 35%
of a day's variance; raise to 0.5 on flow-heavy nights) and its own branches;
every path is repriced node-by-node.

**DAILY mode** (entry not late-session): same command without `--entry-time`.
`--type stock --days 10` / `--type letf --leverage 2 --days 5` for non-options.

Outputs: [A] segment timeline with ACTABLE/LOCKED + flow warnings, [B] move grid,
[C] full scenario table (sleep-line breaches flagged), [D] exit matrix per
checkpoint, [E] MAX/MIN/EXPECTED PnL + P(loss) + P(sleep-breach), and — intraday
only — **[F] ENTRY-TIMING COMPARISON**: the same budget deployed as
enter-NOW / enter-at-late-close(16:14) / wait-buy-10:30-tomorrow / wait-buy-ONLY-
if-overnight-dipped≤−1σ, with expected PnL per overnight scenario and P(loss) per
strategy. **[F] is the decision output** — it turns "该现在买还是等明早的 dip" into
derived numbers.

Present to the user, in this order:
1. **[A] Timeline** — segments, when they can act, where the flow risks sit.
2. **[C] Scenario table** — all paths if ≤ ~25, else top/bottom; call out the
   *surprising* middle paths (e.g. "涨了一半仍然亏 90%" 类路径), not just extremes.
3. **[F] Entry-timing comparison** (intraday) — which entry strategy dominates and why.
4. **[E] Summary** — max/min/expected PnL and P(sleep-line breach), stated as derived
   from the enumeration.
- If the user gave their own up/down targets, run the engine's numbers AND their
  targets side by side and show the gap.
- **Sleep test:** use P(breach) and MAX damage from [E]. If breach probability is
  material (not just the worst path), say plainly: **this size is too big for you**.
- **Make the loss concrete:** "= 约 X 天/周 你最近的组合 PnL"。
- **Interpret [F] honestly — the engine is drift-free.** It prices no mean-reversion,
  so "buy the dip" shows only the cheaper-entry advantage. If the overnight dip is
  FLOW-DRIVEN (pension/index rebalance — mechanical, not informational), state that
  mean-reversion odds favor the dip-buyer beyond what [F] shows. Also note the
  engine holds the strike constant for comparability; in practice, after a dip you
  re-strike ATM relative to the NEW spot (同样的钱买到更近的 strike + 更多张数),
  which further favors waiting.
- State the engine's caveats honestly (constant IV, discretized probabilities).

## Step 4 — Lockout & flow narrative (spec §3.2)

For late-session option entries this is now **computed by intraday mode** ([A]
timeline + [F] comparison) — your job here is the narrative on top of the numbers:
- Walk the user through *their* locked window: "16:15 之后你动不了,而隔夜恰好是
  [季末亚洲养老金 / 大盘 futures] 最活跃的时段" — tie the [A] flow warnings to the
  [F] overnight scenarios.
- **Leveraged ETF (ETH):** engine daily mode + manual note: 7:55–8pm actable →
  8pm–7am LOCKED → 7:00am reopen; daily-reset decay compounds overnight.
- **正股/正ETF (24h):** skip enumeration; note overnight slippage/thin liquidity only.

Quantify from [D]/[F]: "如果现在进,一个 −1σ/−2σ 隔夜 gap = $__ / 账户 __%,发生时你
无法操作;等到明早 10:30 再决定的期望差值 = $__。"

## Step 5 — Debate (spec §4)

Argue **both sides honestly**, strongest form of each:
- **支持进入方**：为什么一定要现在进?(thesis 强度 / setup / 不对称性 / catalyst timing / 踏空成本)
- **反对进入方**：为什么一定不要进?(regime 是 🟡/🔴 / size 过不了 sleep test / 锁仓风险无法对冲 / 追高 FOMO / 事件前裸奔)

## Step 6 — Conclusion & output (spec §5)

Present in this structure (Chinese):

### 🎛️ 宏观 regime
(the dashboard table + one-line 🟢/🟡/🔴 read + the danger-pattern callout if present)

### ⚖️ 最强论点（不判胜负，只呈现）
- **支持方最关键的一点**：…
- **反对方最关键的一点**：…

### 📉 假设进入：PnL 与账户冲击
| 情景 | 标的变动 | 仓位 PnL | 账户 Δ% | vs 茶饭不思线 |
|------|---------|---------|---------|--------------|
| 最好 | +__% | +$__ | +__% | — |
| 最坏 | −__% | −$__ | −__% | ✅ 可承受 / ⚠️ 超过 |

最坏情况 ≈ 你最近 __ 天/周的组合 PnL。

### ⏱️ 锁仓/timing 风险（若为尾盘操作）
(the relevant lockout table + the gap-damage estimate)

### 🎯 若坚持要进：最优执行策略
- **Timing**：(盘中回踩SMA20 / 3:55pm尾盘 / 收盘前15s / Late-Close / 次日开盘 — 给出建议 + 理由)
- **Hedging**：(是否尾盘先进反向 / 等次日开盘 / 开蝶限制最大亏损)
- **Sizing**：Kelly 建议 `f*` 与 **sleep-capped** 仓位两者都给，**推荐取小者**：
  - Kelly（¼–½ 分数）：约 __% 账户 / $__
  - Sleep-capped（最坏 Δ% ≤ 茶饭不思线）：约 __% 账户 / $__
  - **推荐：$__**

### 📁 Log
(write log per Step 7)

## Step 7 — Log the run

Get timestamp: `date "+%Y-%m-%d_%H%M"`. Write to
`~/.claude/skills/big-position-think-twice/logs/YYYY-MM-DD_HHMM_TICKER.md`
with: intake answers, macro dashboard snapshot, regime read, PnL table, timing
analysis, both sides' crucial points, and the final sizing recommendation. Always
write a log even if the user walks away.

---

## Rules

1. **Read-only.** Never place, modify, or cancel any order. Suggest; the user executes.
2. **The sleep test overrides Kelly, always.** Math can say "bet more"; the human's pain threshold caps it.
3. **Weight direction & speed of change** over absolute indicator levels; use 52w percentile when ambiguous.
4. **Do not declare a debate winner.** Present the single most crucial point of each side and let the user own the decision.
5. **Be honest about data gaps** — if VIXEQ/COR1M history or an option chain is thin, say so; never fabricate a level.
7. **PnL numbers come from the scenario engine's enumeration, period.** If the engine
   can't run (no data), say so and present a manually-enumerated scenario table in the
   same [A]-[F] structure — still path-by-path, never a bare "best/worst guess".
8. **Instantaneous numbers only.** Every price/indicator you present is the live
   last-quote at decision time, with its timestamp. Never present a close or any
   stale number as "current" — if a feed is stale, say so explicitly. 这是一个快速的
   当下判断,旧数据会直接导致错误的 timing 决策。
9. **Timing is a first-class output.** For late-session entries, always deliver the
   [F] entry-timing comparison (enter now vs late-close vs wait-for-morning-dip) and
   the flow-calendar context (month/quarter-end pension, Asia-session flows). "现在
   不买,明早 dip 再买大" 必须作为一个被定量比较过的选项呈现,而不是事后诸葛。
6. **Always disclaim:** 以上为纪律性风控辅助，非投资建议，盈亏自负。
