#!/usr/bin/env bash
# ============================================================================
# auto_resume.sh — the ACTUAL "auto-continue" mechanism for this build.
#
# The model (Claude Code) cannot wake itself after a usage/rate limit with no
# external trigger. This wrapper IS that trigger: it re-invokes Claude Code in
# headless continue mode and, when it detects a usage/rate-limit signal, sleeps
# and retries with backoff until the BUILD_COMPLETE marker exists.
#
# SECURITY: this runs Claude Code with permissions bypassed so it can proceed
# unattended. Only run it in a directory you trust, on a machine you control.
# Review the prompt and flags before use.
#
# Usage:   bash scripts/auto_resume.sh
# ============================================================================
set -uo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
MARKER="$ROOT/BUILD_COMPLETE"
PROMPT="Resume the build from BUILD_STATE.json and keep going until BUILD_COMPLETE exists. Read BUILD_STATE.json first; continue from next_action; commit after each step."

SLEEP_SHORT=300      # 5 min  — between normal iterations
SLEEP_LIMIT=600      # 10 min — after a usage/rate-limit signal
MAX_ITERS="${MAX_ITERS:-200}"

i=0
while [[ ! -f "$MARKER" ]]; do
  i=$((i+1))
  if (( i > MAX_ITERS )); then
    echo "[auto_resume] reached MAX_ITERS=$MAX_ITERS without completion; stopping."
    exit 1
  fi
  echo "[auto_resume] iteration $i — invoking Claude Code (headless continue)..."

  # Capture output to scan for limit signals. --dangerously-skip-permissions lets
  # it run unattended; remove it if you prefer to approve actions interactively.
  OUT="$(claude --continue --dangerously-skip-permissions -p "$PROMPT" 2>&1)" || true
  echo "$OUT"

  if [[ -f "$MARKER" ]]; then
    echo "[auto_resume] BUILD_COMPLETE detected. Done."
    break
  fi

  if echo "$OUT" | grep -Eiq "usage limit|rate limit|too many requests|quota|429|resets at|limit reached"; then
    echo "[auto_resume] limit signal detected; backing off ${SLEEP_LIMIT}s."
    sleep "$SLEEP_LIMIT"
  else
    echo "[auto_resume] iteration finished; pausing ${SLEEP_SHORT}s before continuing."
    sleep "$SLEEP_SHORT"
  fi
done

echo "[auto_resume] Build complete — marker present at $MARKER."
