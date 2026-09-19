#!/bin/bash
# Build the packaged macOS release.  RUN THIS ON A MAC.
#
#     ./packaging/build_mac.sh
#
# Produces dist/NEU-Helper-mac-<arch>.zip containing NEU Helper.app and its
# first-run README.
#
# There is no way around doing this on a Mac: PyInstaller freezes the
# interpreter and the native libraries of the machine it runs on, so a macOS
# build cannot be produced from Windows (and vice versa). The .spec file is
# shared -- it picks the icon, the hidden imports and the .app bundling by
# platform on its own.
#
# The zip is arch-specific. An Apple-silicon build will not run on an Intel
# Mac; build on whichever one you use (or on both).
#
# ASCII-only, same reason as install.sh.
set -eu

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ"

say() { printf '\033[36m  %s\033[0m\n' "$1"; }

PY=""
for cand in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$(command -v "$cand")"; break; fi
done
[ -n "$PY" ] || { echo "python3 not found"; exit 1; }
say "python: $PY"

# Everything the frozen app needs at runtime, plus PyInstaller itself.
say "checking build dependencies"
"$PY" -m pip install --quiet --upgrade pyinstaller 2>/dev/null \
  || "$PY" -m pip install --quiet --upgrade --user --break-system-packages pyinstaller
for pkg in requests flask pywebview pyobjc-core pyobjc-framework-Cocoa \
           pyobjc-framework-Quartz pyobjc-framework-WebKit; do
  "$PY" -c "from importlib.metadata import version; version('$pkg')" 2>/dev/null \
    || "$PY" -m pip install --quiet "$pkg" 2>/dev/null \
    || "$PY" -m pip install --quiet --user --break-system-packages "$pkg"
done

# The icon comes from the same code that draws the floating orb, so the two
# never drift. Cheap to regenerate; always do it.
say "generating the icon"
"$PY" make_icon.py --icns

say "running PyInstaller (a few minutes)"
rm -rf dist build
"$PY" -m PyInstaller packaging/neu-helper.spec --noconfirm --distpath dist --workpath build

APP="dist/NEU Helper.app"
[ -d "$APP" ] || { echo "NEU Helper.app was not produced"; exit 1; }

# These must sit next to the executable inside the bundle, NOT in the frozen
# resources: the app runs `claude -p` with the executable's directory as the
# working directory, so that is where Claude Code looks for CLAUDE.md and
# .claude/. (Same reasoning as the Windows build script.)
say "copying the files that must sit next to the executable"
MACOS="$APP/Contents/MacOS"
cp CLAUDE.example.md "$MACOS/"
cp packaging/setup_mac.sh "$MACOS/"
chmod +x "$MACOS/setup_mac.sh"
mkdir -p "$MACOS/.claude"
cp -R .claude/skills "$MACOS/.claude/skills"

# Unsigned apps get a quarantine flag on download; strip it locally so the
# freshly built one opens without the right-click dance. A downloaded copy
# may get the flag again; the README packaged beside the app explains that.
xattr -cr "$APP" 2>/dev/null || true

say "writing first-run instructions"
STAGE="dist/NEU Helper macOS"
rm -rf "$STAGE"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/NEU Helper.app"
cat > "$STAGE/README.txt" <<'EOF'
NEU Helper -- packaged build for macOS (no Python needed)

Before setup
  AI chat, briefings, mail AI, and Canvas MCP require Claude Code. Install it,
  log in, and make sure it can answer a normal question:

    curl -fsSL https://claude.ai/install.sh | bash
    claude --version
    claude

  Claude Code requires a supported subscription/account or API billing.
  Canvas, mailbox, and memos can work without it.

First run
  1. Open Terminal and cd into this extracted folder.
  2. If macOS blocks the unsigned app, remove its download quarantine:

       xattr -cr "./NEU Helper.app"

  3. Run setup with a Canvas token:

       "./NEU Helper.app/Contents/MacOS/setup_mac.sh" --token "14523~...."

     Get a token at: Canvas -> Account -> Settings -> New Access Token.

  4. Edit the generated course table, then launch:

       open -e "./NEU Helper.app/Contents/MacOS/CLAUDE.md"
       open "./NEU Helper.app"

Verify the complete setup
  - NEU Helper shows your real Canvas courses and assignments
  - `claude` can answer a normal question
  - `claude mcp list` shows `canvas`
  - asking Claude "What courses are in my Canvas?" returns real data

If Claude Code was missing during setup
  Install and log in to Claude Code, then run setup again without --token:

    "./NEU Helper.app/Contents/MacOS/setup_mac.sh"

Manual MCP fallback for THIS packaged build
  Run from this folder:

    APP="$(pwd)/NEU Helper.app"
    claude mcp remove canvas -s user
    claude mcp add canvas -s user -- "$APP/Contents/MacOS/canvas-mcp"

Source and issues:
  https://github.com/haoawake/neu-helper
EOF

say "zipping"
ARCH="$(uname -m)"
ZIP="dist/NEU-Helper-mac-$ARCH.zip"
rm -f "$ZIP"
# ditto, not zip: it preserves the resource forks and the symlinks inside a
# .app bundle. A plain `zip -r` produces a bundle that will not launch.
( cd dist && ditto -c -k --sequesterRsrc --keepParent "NEU Helper macOS" "$(basename "$ZIP")" )

printf '\n\033[32m  done -> %s  (%s)\033[0m\n' "$ZIP" "$(du -h "$ZIP" | cut -f1)"
echo
echo "  Test it before uploading:"
echo "    open \"$APP\""
echo "    tail -f \"\$HOME/Library/Logs/\" 2>/dev/null || true"
echo "    # the app writes its own log next to the executable:"
echo "    tail -20 \"$MACOS/data/app.log\""
