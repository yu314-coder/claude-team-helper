#!/bin/zsh
# Clones (APFS, no extra space) Claude's per-account data into a sandbox and runs the sync test there.
set -e
cd "${0:A:h}/.."
SRC="$HOME/Library/Application Support/Claude"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
sandbox() {  # a fresh clone in $T/claude, empty $T/helper
  rm -rf "$T/claude" "$T/helper"
  mkdir -p "$T/claude/local-agent-mode-sessions"
  cp -cR "$SRC/claude-code-sessions" "$T/claude/"
  for acct in "$SRC"/claude-code-sessions/*(/); do
    [[ -d "$SRC/local-agent-mode-sessions/${acct:t}" ]] && cp -cR "$SRC/local-agent-mode-sessions/${acct:t}" "$T/claude/local-agent-mode-sessions/"
  done
  cp -c "$SRC/claude_desktop_config.json" "$T/claude/"
}
export CTH_CLAUDE_SUPPORT="$T/claude" CTH_HELPER_SUPPORT="$T/helper"
echo "== Swift engine (macOS app)"
sandbox
cp tests/SyncEngineTest.swift "$T/main.swift"   # top-level code must live in main.swift
swiftc -O -swift-version 5 SyncEngine.swift "$T/main.swift" -o "$T/synctest"
"$T/synctest"
echo "== Python engine (Windows app)"
sandbox
python3 -I tests/test_sync_engine.py
