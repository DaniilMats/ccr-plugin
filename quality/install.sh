#!/usr/bin/env bash
# CCR plugin dependency check.
#
# Usage:
#   install.sh                # interactive check, prints missing deps and install hints
#   install.sh --check-only   # quiet mode for SessionStart hook; exits 0 unless critical binary missing
#
# Required runtime deps:
#   - python3          (core)
#   - glab             (GitLab MR mode)
#   - gemini           (Pass 1 reviewers)
#   - codex            (Pass 2 reviewers + default verifier)
#   - claude           (Pass 3 reviewers — Opus 4.7 max effort; usually present since this is a Claude Code plugin)
#
# The hook NEVER fails Claude Code startup — it only warns. Install hints go to stderr.

set -u

QUIET=0
if [[ "${1:-}" == "--check-only" ]]; then
  QUIET=1
fi

MISSING=()
HINTS=()

check() {
  local bin="$1"
  local hint="$2"
  if ! command -v "$bin" >/dev/null 2>&1; then
    MISSING+=("$bin")
    HINTS+=("$hint")
  fi
}

check python3 "brew install python@3.12"
check glab    "brew install glab && glab auth login"
check gemini  "npm install -g @google/gemini-cli && gemini auth"
check codex   "npm install -g @openai/codex && codex login"
check claude  "https://claude.com/claude-code — required for Pass 3 Opus 4.7 reviewers"

if [[ ${#MISSING[@]} -eq 0 ]]; then
  if [[ $QUIET -eq 0 ]]; then
    echo "[ccr] All required CLIs found: python3, glab, gemini, codex, claude"
  fi
  exit 0
fi

{
  echo "[ccr] Missing CLI dependencies: ${MISSING[*]}"
  echo "[ccr] Install hints:"
  for i in "${!MISSING[@]}"; do
    echo "  - ${MISSING[$i]}: ${HINTS[$i]}"
  done
  echo "[ccr] CCR will partially work without these, but some reviewer passes or MR mode may be skipped."
} >&2

exit 0
