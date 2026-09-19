#!/bin/bash
# NEU Helper uninstaller for macOS. ASCII-only, same reason as install.sh.
#
# Usage:
#   ./uninstall.sh
#   ./uninstall.sh --keep-token
#
# Removes the login item, the MCP registration, and (unless --keep-token) the
# stored Canvas credentials. Leaves the project files alone -- delete the folder
# yourself if you want those gone too.
set -u

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEEP_TOKEN=0
[ "${1:-}" = "--keep-token" ] && KEEP_TOKEN=1

C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_OFF=$'\033[0m'
ok()   { echo "    ${C_GREEN}OK  ${C_OFF} $1"; }
warn() { echo "    ${C_YELLOW}WARN${C_OFF} $1"; }

echo
echo "=== NEU Helper uninstaller (macOS) ==="

# 1. login item
PLIST="$HOME/Library/LaunchAgents/com.neuhelper.gate.plist"
gone=0
if [ -f "$PLIST" ]; then
  launchctl bootout "gui/$(id -u)/com.neuhelper.gate" >/dev/null 2>&1 \
    || launchctl unload -w "$PLIST" >/dev/null 2>&1 || true
  rm -f "$PLIST"
  ok "removed the login item"
  gone=1
fi
for n in "NEU Helper.command" "Canvas Helper.command"; do
  # Both names: anything installed before the rename is still on disk
  if [ -e "$HOME/Desktop/$n" ] || [ -L "$HOME/Desktop/$n" ]; then
    rm -f "$HOME/Desktop/$n"; ok "removed desktop alias ($n)"; gone=1
  fi
done
[ "$gone" = "0" ] && warn "no login item or aliases found"

# 2. MCP registration
CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
for cand in "$HOME/.local/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
  [ -n "$CLAUDE_BIN" ] && break
  [ -x "$cand" ] && CLAUDE_BIN="$cand"
done
if [ -n "$CLAUDE_BIN" ]; then
  if "$CLAUDE_BIN" mcp remove canvas -s user >/dev/null 2>&1; then
    ok "unregistered the canvas MCP server"
  else
    warn "canvas MCP server was not registered"
  fi
else
  warn "claude CLI not found; skipped MCP cleanup"
fi

# 3. credentials
if [ "$KEEP_TOKEN" = "1" ]; then
  warn "kept the stored token (--keep-token)"
else
  CFG="$HOME/.canvas-helper/config.json"
  [ -f "$CFG" ] && rm -f "$CFG" && ok "deleted $CFG"
  rm -f "$HOME/.canvas-helper/NEUHelper.lock"
  echo
  echo "  ${C_YELLOW}Reminder: this does NOT revoke the token on Canvas.${C_OFF}"
  echo "  ${C_YELLOW}Delete it at: Account -> Settings -> Approved Integrations${C_OFF}"
  # Mail credentials are left in place on purpose, matching uninstall.ps1 --
  # a reinstall keeps the mailbox set up. Said out loud because "I uninstalled
  # it" should not quietly leave an app password on disk without you knowing.
  if [ -f "$HOME/.canvas-helper/mail.json" ]; then
    echo
    warn "mail credentials are still at ~/.canvas-helper/mail.json"
    warn "delete that file too if you want them gone"
  fi
fi

# 4. local cache
if [ -d "$PROJ/data" ]; then
  rm -rf "$PROJ"/data/* 2>/dev/null || true
  ok "cleared data/ cache"
fi

echo
echo "=== Done. Project files left in place. ==="
echo
