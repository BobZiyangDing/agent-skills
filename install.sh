#!/usr/bin/env bash
#
# agent-skills installer
# Installs one or all skills into Claude Code (~/.claude/skills) and/or
# Codex (~/.codex/skills + ~/.codex/prompts) globally, and ensures the
# `uv` runtime so Python-backed skills have their dependencies on demand.
#
# Usage (from anywhere):
#   curl -fsSL https://raw.githubusercontent.com/BobZiyangDing/agent-skills/main/install.sh | bash
#
# Options (pass after `bash -s --` when piping):
#   --target claude|codex|all   Where to install (default: claude)
#   --skill  <name>|all         Which skill (default: all)
#   --ref    <git-ref>          Branch/tag/commit to install (default: main)
#   --repo   <git-url>          Override source repo
#   --no-uv                     Do not auto-install uv
#   -h, --help                  Show this help
#
set -euo pipefail

REPO_URL="${AGENT_SKILLS_REPO:-https://github.com/BobZiyangDing/agent-skills.git}"
REF="main"
TARGET="claude"
SKILL="all"
INSTALL_UV=1

CLAUDE_DIR="$HOME/.claude/skills"
CODEX_SKILLS_DIR="$HOME/.codex/skills"
CODEX_PROMPTS_DIR="$HOME/.codex/prompts"

c_bold=$'\033[1m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_red=$'\033[31m'; c_rst=$'\033[0m'
info() { printf "%s==>%s %s\n" "$c_grn" "$c_rst" "$*"; }
warn() { printf "%s!! %s%s\n" "$c_ylw" "$*" "$c_rst"; }
die()  { printf "%s✗  %s%s\n" "$c_red" "$*" "$c_rst" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2 ;;
    --skill)  SKILL="${2:-}"; shift 2 ;;
    --ref)    REF="${2:-}"; shift 2 ;;
    --repo)   REPO_URL="${2:-}"; shift 2 ;;
    --no-uv)  INSTALL_UV=0; shift ;;
    -h|--help)
      sed -n '2,20p' "$0" 2>/dev/null || echo "see header"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

case "$TARGET" in claude|codex|all) ;; *) die "--target must be claude|codex|all" ;; esac

# ---------------------------------------------------------------------------
# 1. Locate the repo: use a local checkout if we're inside one, else clone.
# ---------------------------------------------------------------------------
SELF="${BASH_SOURCE[0]:-}"
SRC=""
CLEANUP=""
if [ -n "$SELF" ] && [ -f "$SELF" ] && [ -d "$(cd "$(dirname "$SELF")" && pwd)/skills" ]; then
  SRC="$(cd "$(dirname "$SELF")" && pwd)"
  info "Using local checkout: $SRC"
else
  command -v git >/dev/null 2>&1 || die "git is required to fetch the repo"
  TMP="$(mktemp -d)"; CLEANUP="$TMP"
  info "Cloning $REPO_URL @ $REF"
  git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$TMP" 2>/dev/null \
    || git clone --quiet --depth 1 "$REPO_URL" "$TMP" \
    || die "clone failed"
  SRC="$TMP"
fi
trap '[ -n "$CLEANUP" ] && rm -rf "$CLEANUP"' EXIT

[ -d "$SRC/skills" ] || die "no skills/ directory in source"

# ---------------------------------------------------------------------------
# 2. Ensure the uv runtime (provides Python deps on demand at skill runtime).
# ---------------------------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
  info "uv present: $(uv --version 2>/dev/null)"
elif [ "$INSTALL_UV" = "1" ]; then
  warn "uv not found — installing (https://astral.sh/uv)"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || warn "uv install failed; skills can still use pip fallback"
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 && info "uv installed: $(uv --version 2>/dev/null)" || warn "uv still unavailable"
else
  warn "uv not found and --no-uv set; Python skills will use the pip fallback in requirements.txt"
fi

# ---------------------------------------------------------------------------
# 3. Resolve which skills to install.
# ---------------------------------------------------------------------------
skills=()
if [ "$SKILL" = "all" ]; then
  for d in "$SRC"/skills/*/; do [ -f "$d/SKILL.md" ] && skills+=("$(basename "$d")"); done
else
  [ -f "$SRC/skills/$SKILL/SKILL.md" ] || die "skill not found: $SKILL"
  skills=("$SKILL")
fi
[ "${#skills[@]}" -gt 0 ] || die "no installable skills found"

# Strip YAML frontmatter and rewrite <SKILL_BASE_DIR> -> absolute path (for Codex prompts).
render_codex_prompt() {  # $1=SKILL.md  $2=abs base dir
  awk 'BEGIN{fm=0} NR==1&&$0=="---"{fm=1;next} fm==1&&$0=="---"{fm=0;next} fm==0{print}' "$1" \
    | sed "s#<SKILL_BASE_DIR>#$2#g"
}

install_one() { # $1=name $2=dest_skills_dir
  local name="$1" base="$2/$1"
  rm -rf "$base"
  mkdir -p "$2"
  cp -R "$SRC/skills/$name" "$base"
  rm -rf "$base/logs"; mkdir -p "$base/logs"   # fresh, unshipped logs dir
  echo "$base"
}

# ---------------------------------------------------------------------------
# 4. Install per target.
# ---------------------------------------------------------------------------
did_claude=0; did_codex=0
for name in "${skills[@]}"; do
  if [ "$TARGET" = "claude" ] || [ "$TARGET" = "all" ]; then
    base="$(install_one "$name" "$CLAUDE_DIR")"
    info "Claude ← ${c_bold}$name${c_rst} → $base"
    did_claude=1
  fi
  if [ "$TARGET" = "codex" ] || [ "$TARGET" = "all" ]; then
    base="$(install_one "$name" "$CODEX_SKILLS_DIR")"
    mkdir -p "$CODEX_PROMPTS_DIR"
    render_codex_prompt "$base/SKILL.md" "$base" > "$CODEX_PROMPTS_DIR/$name.md"
    info "Codex  ← ${c_bold}$name${c_rst} → $base  (+ /$name prompt)"
    did_codex=1
  fi
done

# ---------------------------------------------------------------------------
# 5. Done.
# ---------------------------------------------------------------------------
echo
info "${c_bold}Install complete.${c_rst}"
[ "$did_claude" = 1 ] && echo "  • Claude Code: restart the CLI, then the skill(s) auto-load. Invoke by describing the task or /<skill-name>."
[ "$did_codex" = 1 ]  && echo "  • Codex: run \`codex\`, then type /<skill-name> to invoke the installed prompt."
echo "  • Runtime: skills self-fetch Python deps via uv (or pip -r requirements.txt)."
