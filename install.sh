#!/bin/bash
# NEU Helper installer for macOS.
#
# The macOS counterpart of install.ps1. Same six steps, same order, so the two
# can be read side by side:
#
#   1. credentials   ->  ~/.canvas-helper/config.json  (chmod 600)
#   2. python        ->  interpreter + dependencies, path recorded for the gate
#   3. MCP           ->  register the canvas server with Claude Code
#   4. perms         ->  .claude/settings.local.json
#   5. startup       ->  LaunchAgent + a double-clickable .command
#   6. verify        ->  hit Canvas, boot the backend, check the token gate
#
# Usage:
#   ./install.sh --token "14523~...."     first time
#   ./install.sh                          re-run, reuses the stored token
#
# Deliberately ASCII-only in the messages: this may run in a terminal whose
# encoding we do not control, and a garbled installer log is worse than a
# plain one. (The same reasoning as start-canvas-helper.cmd -- see its header.)

set -u

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG_DIR="$HOME/.canvas-helper"
CFG_FILE="$CFG_DIR/config.json"
TOKEN=""
BASE_URL="https://northeastern.instructure.com"

while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="${2:-}"; shift 2 ;;
    --base-url) BASE_URL="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1"; exit 2 ;;
  esac
done

C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
C_RED=$'\033[31m'; C_OFF=$'\033[0m'
step() { echo; echo "${C_CYAN}[$1] $2${C_OFF}"; }
ok()   { echo "    ${C_GREEN}OK  ${C_OFF} $1"; }
warn() { echo "    ${C_YELLOW}WARN${C_OFF} $1"; }
fail() { echo "    ${C_RED}FAIL${C_OFF} $1"; }

echo
echo "=== NEU Helper installer (macOS) ==="
echo "    project: $PROJ"

# Find python3 up front, before step 1 needs it to read the stored token.
#
# Deliberately NOT hardcoding /usr/bin/python3: on a machine without the
# Command Line Tools that path is a stub which pops up a GUI installer prompt
# the moment you run it. Asking PATH and the Homebrew locations first means the
# common case never touches the stub.
PY_BIN=""
for cand in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if command -v "$cand" >/dev/null 2>&1; then PY_BIN="$(command -v "$cand")"; break; fi
done

# ---------------------------------------------------------------- 1. token
step 1 "Storing credentials"

if [ -z "$TOKEN" ]; then
  if [ -f "$CFG_FILE" ] && [ -n "$PY_BIN" ]; then
    # Read with python, not grep: the value contains ~ and / and the file is
    # real JSON, so a regex would be the fragile way to do this.
    TOKEN="$("$PY_BIN" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("token",""))' "$CFG_FILE" 2>/dev/null || true)"
    stored_url="$("$PY_BIN" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("base_url",""))' "$CFG_FILE" 2>/dev/null || true)"
    [ -n "$stored_url" ] && BASE_URL="$stored_url"
    [ -n "$TOKEN" ] && ok "reusing token already in $CFG_FILE"
  fi
fi
if [ -z "$TOKEN" ]; then
  fail 'no token found. Pass --token "14523~...." on the command line.'
  echo "         Canvas -> Account -> Settings -> New Access Token"
  exit 1
fi

if [ -z "$PY_BIN" ]; then
  fail "python3 not found. Install Python 3.9+ (brew install python) and re-run."
  exit 1
fi

mkdir -p "$CFG_DIR"
# 700 on the directory, 600 on the file. This is the macOS equivalent of the
# icacls SID grant in install.ps1: nobody but this user can read the token.
# The umask is set explicitly because the file is created by the redirect below
# and would otherwise inherit whatever the shell happens to have.
chmod 700 "$CFG_DIR"
( umask 077
  "$PY_BIN" - "$CFG_FILE" "$TOKEN" "$BASE_URL" <<'PY'
import json, sys
path, token, base = sys.argv[1:4]
with open(path, "w", encoding="utf-8") as f:
    json.dump({"token": token, "base_url": base}, f, indent=2)
PY
)
chmod 600 "$CFG_FILE"
ok "token -> $CFG_FILE (mode $(stat -f '%Lp' "$CFG_FILE"), dir mode $(stat -f '%Lp' "$CFG_DIR"))"

# No persistent environment variables here, on purpose. On Windows the
# installer sets user-level env vars as a convenience; macOS has no equivalent
# that also reaches GUI/LaunchAgent processes without editing shell rc files or
# installing another plist. config.json above is the primary source and is
# enough -- canvas_api.py reads the env vars only as an override.
ok "config.json is the credential source (no shell rc files touched)"

# ---------------------------------------------------------------- 2. python
step 2 "Locating Python"
# PY_BIN was already resolved above -- step 1 needed it to read the stored token
PY_VER="$("$PY_BIN" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')" || {
  fail "cannot run $PY_BIN"; exit 1; }
ok "python $PY_VER at $PY_BIN"

case "$PY_BIN" in
  /usr/bin/python3)
    warn "this is Apple's system python; pip installs may need --user"
    warn "a Homebrew python3 (brew install python) is the smoother option" ;;
esac

# requests = Canvas API, flask = the local HTTP backend, pywebview = the window.
# pyobjc-* = what the native side needs on macOS:
#   Cocoa   NSWindow / NSView / NSTimer   (native_cocoa, orb_cocoa, toast_cocoa)
#   Quartz  CGImage compositing           (the orb and the toast bitmaps)
#   WebKit  the WKWebView pywebview uses
PKGS="requests flask pywebview pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz pyobjc-framework-WebKit"
for pkg in $PKGS; do
  probe="$("$PY_BIN" -c "from importlib.metadata import version; print(version('$pkg'))" 2>/dev/null || true)"
  if [ -z "$probe" ]; then
    warn "$pkg is missing; installing it"
    if ! "$PY_BIN" -m pip install --quiet "$pkg" 2>/dev/null; then
      # Homebrew / system pythons refuse to touch the environment without this
      if ! "$PY_BIN" -m pip install --quiet --user --break-system-packages "$pkg"; then
        fail "pip install $pkg failed"; exit 1
      fi
    fi
    ok "$pkg installed"
  else
    ok "$pkg $probe"
  fi
done

# Record the absolute interpreter path for startup-gate.sh: PATH at login is
# not the PATH an interactive shell sees, and a LaunchAgent gets even less.
# (Same reason as the pythonw-path.txt file on the Windows side.)
mkdir -p "$PROJ/data"
printf '%s\n' "$PY_BIN" > "$PROJ/data/python-path.txt"
ok "login interpreter recorded: $PROJ/data/python-path.txt"

# ---------------------------------------------------------------- 3. MCP
step 3 "Registering the canvas MCP server with Claude Code"
MCP_REGISTERED=0
CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
for cand in "$HOME/.local/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
  [ -n "$CLAUDE_BIN" ] && break
  [ -x "$cand" ] && CLAUDE_BIN="$cand"
done
if [ -z "$CLAUDE_BIN" ]; then
  warn "claude CLI not found; AI features and Canvas MCP are not configured"
  warn "install/login to Claude Code, then run install.sh again (token is reused)"
else
  "$CLAUDE_BIN" mcp remove canvas -s user >/dev/null 2>&1 || true
  if "$CLAUDE_BIN" mcp add canvas -s user -- "$PY_BIN" "$PROJ/canvas_mcp.py" >/dev/null 2>&1; then
    MCP_REGISTERED=1
    ok "MCP server 'canvas' registered at user scope (all projects)"
  else
    fail "claude mcp add failed"
  fi
fi

# ---------------------------------------------------------------- 4. perms
step 4 "Pre-approving canvas tools for this project"
mkdir -p "$PROJ/.claude"
# Without this, every boot would nag for permission on each canvas tool call.
# The Skill/Bash entries matter just as much: the desktop app talks to
# `claude -p` (non-interactive), which cannot prompt -- anything that would need
# confirmation is denied outright, so the course-files / course-sync skills
# would silently never run. Only those two script prefixes are allowed, not Bash
# in general.
#
# python AND python3 are both allowed: macOS usually has only python3, and this
# file is shared between both machines -- listing both keeps one file correct
# everywhere instead of the two installers overwriting each other.
# Keep this list identical to the one in install.ps1.
cat > "$PROJ/.claude/settings.local.json" <<'JSON'
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
ok "wrote $PROJ/.claude/settings.local.json"

# CLAUDE.md is the assistant's behaviour definition: course table, briefing
# rules, boundaries. It is NOT in the repository -- it holds your own courses
# and situation -- so a fresh clone does not have one. Seed it from the
# template; never overwrite an existing one.
if [ -f "$PROJ/CLAUDE.example.md" ] && [ ! -f "$PROJ/CLAUDE.md" ]; then
  cp "$PROJ/CLAUDE.example.md" "$PROJ/CLAUDE.md"
  ok "created CLAUDE.md from the template -- edit the course table in it"
elif [ -f "$PROJ/CLAUDE.md" ]; then
  ok "CLAUDE.md already there (left alone)"
fi

# ---------------------------------------------------------------- 5. startup
step 5 "Installing the login item"

# The icon. .icns for the Dock; generated from the same code that draws the
# floating orb, so the two always match.
if [ ! -f "$PROJ/gui/icon.icns" ]; then
  "$PY_BIN" "$PROJ/make_icon.py" --icns >/dev/null 2>&1 \
    && ok "generated gui/icon.icns" || warn "could not generate the icon"
fi

chmod +x "$PROJ/startup-gate.sh" "$PROJ/NEU Helper.command" 2>/dev/null || true

AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/com.neuhelper.gate.plist"
mkdir -p "$AGENT_DIR"

# RunAtLoad only -- no KeepAlive. The gate exits immediately on days it has
# already run, and KeepAlive would make launchd restart it in a tight loop.
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.neuhelper.gate</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$PROJ/startup-gate.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJ</string>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$PROJ/data/launchagent.log</string>
  <key>StandardErrorPath</key><string>$PROJ/data/launchagent.log</string>
</dict>
</plist>
PLISTEOF

# bootout first: bootstrap on an already-loaded label is an error, and a stale
# definition would keep running the old command line.
launchctl bootout "gui/$(id -u)/com.neuhelper.gate" >/dev/null 2>&1 || true
if launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1; then
  ok "login item -> $PLIST"
elif launchctl load -w "$PLIST" >/dev/null 2>&1; then
  ok "login item -> $PLIST (loaded via the older launchctl syntax)"
else
  warn "could not register the login item; add it by hand in"
  warn "System Settings -> General -> Login Items"
fi
ok "gate: opens at most one window per day (edit ONCE_PER_DAY in startup-gate.sh)"

# An alias on the Desktop as well -- the login gate refuses to open twice in one
# day, so without this there is no obvious way to start the app by hand.
if ln -sf "$PROJ/NEU Helper.command" "$HOME/Desktop/NEU Helper.command" 2>/dev/null; then
  ok "desktop alias -> ~/Desktop/NEU Helper.command"
else
  warn "could not create the desktop alias (double-click the .command in the project)"
fi

# ---------------------------------------------------------------- 6. verify
step 6 "Verifying"
cd "$PROJ" || exit 1
export PYTHONUTF8=1

who="$("$PY_BIN" -c "from canvas_api import CanvasClient; print(CanvasClient().whoami()['name'])" 2>&1)"
if [ $? -eq 0 ] && [ -n "$who" ]; then
  ok "Canvas API reachable, logged in as: $who"
else
  fail "Canvas API check failed: $who"
fi

# Exercise the desktop app's own HTTP backend, minus the window. This also
# proves the token gate really guards /api, and that the static shell loads
# (if it did not, the UI would render unstyled).
"$PY_BIN" - <<'PY'
import sys, threading, time, urllib.error, urllib.request
sys.path.insert(0, ".")
import server

port = server.free_port()
threading.Thread(target=server.serve, args=(port,), daemon=True).start()
base = f"http://127.0.0.1:{port}"
for _ in range(80):
    try:
        urllib.request.urlopen(f"{base}/api/dashboard?k={server.TOKEN}", timeout=2).read(1)
        break
    except urllib.error.HTTPError:
        break
    except Exception:
        time.sleep(0.15)

def get(path):
    try:
        r = urllib.request.urlopen(base + path, timeout=5)
        return r.status, len(r.read())
    except urllib.error.HTTPError as e:
        return e.code, 0
    except Exception as e:
        return str(e), 0

code, n = get(f"/api/dashboard?k={server.TOKEN}")
print(f"    {'OK   ' if code == 200 else 'FAIL '} backend answers /api/dashboard ({code}, {n} bytes)")
code, _ = get("/api/dashboard")
print(f"    {'OK   ' if code == 403 else 'FAIL '} token gate rejects an unkeyed /api call ({code})")
code, n = get("/")
print(f"    {'OK   ' if code == 200 else 'FAIL '} static shell loads ({code}, {n} bytes)")
PY

# The native side cannot be checked without a window, so check that it at least
# imports -- a missing pyobjc framework shows up here rather than at first launch.
"$PY_BIN" -c "
import platform_id, desktop, native_window, orb_window, toast
print('    OK    native layer imports (%s)' % platform_id.NAME)
print('    OK    dark mode reads as %s' % desktop.system_dark())
" 2>&1 | sed 's/^Traceback/    FAIL  native layer: Traceback/'

echo
echo "=== done ==="
if [ "$MCP_REGISTERED" -eq 1 ]; then
  echo "    AI setup:         Canvas MCP registered; run 'claude mcp list' to verify"
else
  echo "    AI setup:         INCOMPLETE; install/login to Claude Code, then re-run install.sh"
fi
echo "    start it now:      open \"$PROJ/NEU Helper.command\""
echo "    or double-click:   NEU Helper.command  (project folder or Desktop)"
echo "    it also opens once per day at login"
echo
echo "    mail account:      set it up in the app, Settings -> mailbox"
echo "    credentials live in $CFG_DIR (outside the project, never synced)"
echo
