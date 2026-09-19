#!/bin/bash
# Build the packaged macOS release.  RUN THIS ON A MAC.
#
#     ./packaging/build_mac.sh
#
# Produces  dist/NEU-Helper-mac-<arch>.zip  containing NEU Helper.app.
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
# freshly built one opens without the right-click dance. The person who
# downloads the zip still has to do it once -- README.txt says how.
xattr -cr "$APP" 2>/dev/null || true

say "zipping"
ARCH="$(uname -m)"
ZIP="dist/NEU-Helper-mac-$ARCH.zip"
rm -f "$ZIP"
# ditto, not zip: it preserves the resource forks and the symlinks inside a
# .app bundle. A plain `zip -r` produces a bundle that will not launch.
( cd dist && ditto -c -k --sequesterRsrc --keepParent "NEU Helper.app" "$(basename "$ZIP")" )

printf '\n\033[32m  done -> %s  (%s)\033[0m\n' "$ZIP" "$(du -h "$ZIP" | cut -f1)"
echo
echo "  Test it before uploading:"
echo "    open \"$APP\""
echo "    tail -f \"\$HOME/Library/Logs/\" 2>/dev/null || true"
echo "    # the app writes its own log next to the executable:"
echo "    tail -20 \"$MACOS/data/app.log\""
