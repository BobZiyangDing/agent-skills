# agent-skills

A growing collection of **agent skills** for [Claude Code](https://claude.com/claude-code)
and [Codex](https://github.com/openai/codex). Each skill is a self-contained folder
(`SKILL.md` + optional scripts/reference) that installs globally so any project can
use it. One command installs everything, dependencies included.

---

## 🚀 Install (one block, copy-paste)

**Claude Code (global):**

```bash
curl -fsSL https://raw.githubusercontent.com/BobZiyangDing/agent-skills/main/install.sh | bash
```

**Codex (global):**

```bash
curl -fsSL https://raw.githubusercontent.com/BobZiyangDing/agent-skills/main/install.sh | bash -s -- --target codex
```

**Both:**

```bash
curl -fsSL https://raw.githubusercontent.com/BobZiyangDing/agent-skills/main/install.sh | bash -s -- --target all
```

That's it. The installer will:
1. Fetch this repo.
2. Ensure [`uv`](https://astral.sh/uv) is present (auto-installs it if missing) — this is how the Python-backed skills get their dependencies (`yfinance`, `requests`, …) **on demand at runtime**, so there's nothing else to `pip install`.
3. Copy every skill into `~/.claude/skills/` (Claude) and/or `~/.codex/skills/` + a `/<skill>` prompt in `~/.codex/prompts/` (Codex).

Restart the CLI afterward and the skills auto-load.

### Options

| Flag | Values | Default | Meaning |
|------|--------|---------|---------|
| `--target` | `claude` \| `codex` \| `all` | `claude` | Where to install |
| `--skill`  | `<name>` \| `all` | `all` | Install a single skill or all |
| `--ref`    | branch/tag/commit | `main` | Version to install |
| `--no-uv`  | — | off | Skip auto-installing uv (use pip fallback) |

Install just one skill:

```bash
curl -fsSL https://raw.githubusercontent.com/BobZiyangDing/agent-skills/main/install.sh \
  | bash -s -- --skill big-position-think-twice --target all
```

### Install from a local clone

```bash
git clone https://github.com/BobZiyangDing/agent-skills.git
cd agent-skills
./install.sh --target all
```

---

## 📦 Skills catalog

| Skill | What it does |
|-------|--------------|
| [`big-position-think-twice`](skills/big-position-think-twice/) | A pre-trade discipline gate. Before you put on a large / conviction position, it interrogates your intent and pain threshold, pulls a live macro-risk dashboard (VIX, VIXEQ/VIX dispersion, COR1M, MOVE, HYG/IEI, CCC−BB credit spread, SOFR−IORB funding, VIX term structure), computes best/worst-case PnL against your account, enumerates end-of-day "locked-out" timing scenarios, argues both sides, and outputs the crucial point of each side plus the best-execution strategy (timing, hedging, Kelly-capped-by-sleep-test sizing). **Read-only — never places orders.** |

_More skills land here over time — same install command picks them all up._

---

## 🧠 Requirements

- **Claude Code** and/or **Codex** CLI.
- **`uv`** — auto-installed by the installer. It transparently provides Python
  dependencies when a skill runs (`uv run --with yfinance --with requests …`), so you
  never manage a venv. If you prefer pip, each Python skill ships a `requirements.txt`
  and the skill falls back to it automatically.
- Some skills use MCP servers you already have connected (e.g. a broker MCP for the
  optional portfolio read). These are optional; skills degrade gracefully without them.

---

## ➕ Add a new skill

The repo is built to scale to many skills. To add one, drop a folder under `skills/`:

```
skills/<your-skill-name>/
├── SKILL.md            # required — YAML frontmatter (name, description) + instructions
├── scripts/            # optional — helper scripts the skill runs
├── reference/          # optional — reference docs the skill reads
└── requirements.txt    # optional — pip fallback for Python deps
```

Conventions that keep it portable:
- In `SKILL.md`, reference scripts via **`<SKILL_BASE_DIR>/scripts/...`** rather than a
  hardcoded `~/.claude/...` path. The installer rewrites this token for Codex, and
  Claude Code supplies the skill's base directory at launch. This is what lets the same
  skill work under both runtimes.
- Prefer `uv run --with <pkg>` for Python deps so there's zero install step, and mirror
  the packages in `requirements.txt` as a fallback.
- Keep runtime output (logs, caches) in a `logs/` subdir — it's git-ignored.

Commit, push, and the one-line installer distributes it.

---

## 🗑️ Uninstall

```bash
rm -rf ~/.claude/skills/<skill-name>
rm -rf ~/.codex/skills/<skill-name> ~/.codex/prompts/<skill-name>.md
```

---

## ⚠️ Disclaimer

The trading/finance skills here are **decision-support and risk-discipline tools, not
investment advice**. They are read-only and never execute orders. You are responsible
for your own trades and outcomes.

## License

MIT — see [LICENSE](LICENSE).
