#!/bin/bash
# NEU Helper -- first-run setup for the packaged macOS build (no Python needed).
#
#     ./setup_mac.sh --token "14523~...."
#
# Re-running is safe; it reuses the stored token if you omit --token.
#
# The packaged counterpart of install.sh. Two differences, both because there
# is no Python interpreter in this build:
#
#   * the MCP server is registered as the bundled canvas-mcp binary, not
#     `python3 canvas_mcp.py`
#   * no once-per-day login gate (that gate is a shell script that launches a
#     Python script). The login item just opens the app at every login --
#     harmless, because only one instance can run and the daily briefing is
#     separately capped at one per calendar day.
#
# ASCII-only, same reason as install.sh.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../NEU Helper.app/Contents/MacOS
APP="$(cd "$HERE/../.." && pwd)"                       # .../NEU Helper.app
EXE="$HERE/NEU Helper"
MCP="$HERE/canvas-mcp"
CFG_DIR="$HOME/.canvas-helper"
CFG_FILE="$CFG_DIR/config.json"
TOKEN=""
BASE_URL="https://northeastern.instructure.com"

while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="${2:-}"; shift 2 ;;
    --base-url) BASE_URL="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1"; exit 2 ;;
  esac
done

C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_C=$'\033[36m'; C_O=$'\033[0m'
ok()   { echo "    ${C_G}OK  ${C_O} $1"; }
warn() { echo "    ${C_Y}WARN${C_O} $1"; }
fail() { echo "    ${C_R}FAIL${C_O} $1"; }
step() { echo; echo "${C_C}[$1] $2${C_O}"; }

echo
echo "=== NEU Helper setup (packaged build) ==="
echo "    app: $APP"

[ -x "$EXE" ] || { fail "the app bundle looks incomplete (no executable)"; exit 1; }

# ---------------------------------------------------------------- 1. token
step 1 "Storing the Canvas token"
if [ -z "$TOKEN" ] && [ -f "$CFG_FILE" ]; then
  # sed, not python: this build deliberately has no interpreter to call.
  TOKEN="$(sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CFG_FILE" | head -1)"
  stored="$(sed -n 's/.*"base_url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CFG_FILE" | head -1)"
  [ -n "$stored" ] && BASE_URL="$stored"
  [ -n "$TOKEN" ] && ok "reusing the token already in $CFG_FILE"
fi
if [ -z "$TOKEN" ]; then
  fail 'no token yet. Re-run with:  --token "14523~...."'
  echo "         Get one at: Canvas -> Account -> Settings -> New Access Token"
  exit 1
fi

mkdir -p "$CFG_DIR"
chmod 700 "$CFG_DIR"
# umask 077 so the file is 600 from the moment it exists, not after the fact
( umask 077
  printf '{\n  "token": "%s",\n  "base_url": "%s"\n}\n' "$TOKEN" "$BASE_URL" > "$CFG_FILE" )
chmod 600 "$CFG_FILE"
ok "token -> $CFG_FILE (mode $(stat -f '%Lp' "$CFG_FILE"))"

# ---------------------------------------------------------------- 2. MCP
step 2 "Registering the canvas MCP server with Claude Code"
CLAUDE="$(command -v claude 2>/dev/null || true)"
for c in "$HOME/.local/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
  [ -n "$CLAUDE" ] && break
  [ -x "$c" ] && CLAUDE="$c"
done
if [ -z "$CLAUDE" ]; then
  warn "claude CLI not found. The dashboard and mailbox still work, but the"
  warn "daily briefing and the chat need it: https://claude.com/claude-code"
else
  "$CLAUDE" mcp remove canvas -s user >/dev/null 2>&1 || true
  if "$CLAUDE" mcp add canvas -s user -- "$MCP" >/dev/null 2>&1; then
    ok "MCP server 'canvas' -> the bundled canvas-mcp"
  else
    warn "claude mcp add failed; register it by hand if you want MCP"
  fi
fi

# ---------------------------------------------------------------- 3. config
step 3 "Preparing the assistant's config"
mkdir -p "$HERE/.claude"
cat > "$HERE/.claude/settings.local.json" <<'JSON'
{
  "permissions": {
    "allow": [
      "mcp__canvas",
      "Read(./data/**)",
      "Skill(course-files)",
      "Skill(course-sync)",
      "Bash(python .claude/skills/course-files/scripts/coursedoc.py:*)",
      "Bash(python3 .claude/skills/course-files/scripts/coursedoc.py:*)",
      "Bash(python .claude/skills/course-sync/scripts/sync_now.py:*)",
      "Bash(python3 .claude/skills/course-sync/scripts/sync_now.py:*)",
      "PowerShell(python .claude/skills/course-files/scripts/coursedoc.py:*)",
      "PowerShell(python .claude/skills/course-sync/scripts/sync_now.py:*)"
    ]
  }
}
JSON
ok "wrote .claude/settings.local.json"

# CLAUDE.md is the assistant's behaviour definition: course table, briefing
# rules, boundaries. It must sit next to the executable, because the app runs
# `claude -p` with that folder as the working directory.
if [ -f "$HERE/CLAUDE.example.md" ] && [ ! -f "$HERE/CLAUDE.md" ]; then
  cp "$HERE/CLAUDE.example.md" "$HERE/CLAUDE.md"
  ok "created CLAUDE.md -- open it and put your own courses in the table"
  echo "         open -e \"$HERE/CLAUDE.md\""
elif [ -f "$HERE/CLAUDE.md" ]; then
  ok "CLAUDE.md already there (left alone)"
fi

# ---------------------------------------------------------------- 4. login item
step 4 "Installing the login item"
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/com.neuhelper.app.plist"
mkdir -p "$AGENT_DIR"
# `open -a` rather than running the binary directly: that is what gives the
# app its Dock identity and its .app context. RunAtLoad only -- no KeepAlive,
# or launchd would relaunch it every time you quit.
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.neuhelper.app</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>-a</string>
    <string>$APP</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
PLISTEOF
launchctl bootout "gui/$(id -u)/com.neuhelper.app" >/dev/null 2>&1 || true
if launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 \
   || launchctl load -w "$PLIST" >/dev/null 2>&1; then
  ok "opens at login -> $PLIST"
else
  warn "could not register the login item; add it by hand in"
  warn "System Settings -> General -> Login Items"
fi

# ---------------------------------------------------------------- 5. verify
step 5 "Verifying"
# Talk to the MCP server the way Claude Code does: one initialize request on
# stdin, expect JSON-RPC back. It exits by itself when stdin closes.
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"setup","version":"1"}}}'
if printf '%s\n' "$INIT" | "$MCP" 2>/dev/null | grep -q '"serverInfo"'; then
  ok "canvas-mcp answers the MCP handshake"
else
  warn "canvas-mcp did not answer as expected"
fi
WHO='{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"canvas_whoami","arguments":{}}}'
if printf '%s\n%s\n' "$INIT" "$WHO" | "$MCP" 2>/dev/null | grep -q '"result"'; then
  ok "Canvas token works (canvas_whoami came back)"
else
  warn "Canvas check failed -- is the token still valid?"
fi

echo
echo "=== Done ==="
echo "    Start it:  open \"$APP\""
echo "    Mailbox:   set it up inside the app, Settings -> mailbox"
echo "    Your token lives in $CFG_DIR, outside the app bundle."
echo
