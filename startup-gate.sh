#!/bin/bash
# NEU Helper login gate (macOS).
#
# The counterpart of startup-gate.vbs. launchd runs this at login; it decides
# whether to start the app today, and if so launches it. Doing the check here
# (rather than inside the app) means nothing at all happens on logins that are
# already handled.
#
# ---------------------------------------------------------------------------
# Two things that bite on macOS and do not on Windows:
#
#   PATH -- a LaunchAgent gets a minimal environment, not your login shell's.
#   `python3` is very often NOT on it (Homebrew lives in /opt/homebrew/bin).
#   install.sh records the resolved interpreter in data/python-path.txt for
#   exactly this reason; the fallbacks below are a second line of defence.
#
#   Timing -- launchd may start this before the login session can show a
#   window. There is no reliable "desktop is ready" signal to wait on, so the
#   app itself handles being started early (it waits for its own HTTP backend
#   before opening the window). No sleep here.
# ---------------------------------------------------------------------------
set -u

# Set to 0 if you want the app launched on EVERY login instead of once a day.
# (The daily briefing is separately capped at one per calendar day, so extra
# launches will not produce extra briefings either way.)
ONCE_PER_DAY=1

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER="$PROJ/data/last-launch.txt"
TODAY="$(date +%Y-%m-%d)"

LAST=""
[ -f "$MARKER" ] && LAST="$(head -n 1 "$MARKER" | tr -d '[:space:]')"

if [ "$ONCE_PER_DAY" = "1" ] && [ "$LAST" = "$TODAY" ]; then
  exit 0
fi

mkdir -p "$PROJ/data"
printf '%s\n' "$TODAY" > "$MARKER"

PY=""
[ -f "$PROJ/data/python-path.txt" ] && PY="$(head -n 1 "$PROJ/data/python-path.txt" | tr -d '[:space:]')"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  # **先清空。** 记录的那个路径已经不能用了(换了 python、Homebrew 搬了家),
  # 不清的话下面的循环一个都没命中时 $PY 还留着那个坏值,于是去 exec 它,
  # 用户看到的是一句晦涩的 "No such file or directory",而不是下面那句
  # "去跑 install.sh"。
  PY=""
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -x "$cand" ] && PY="$cand" && break
  done
fi
if [ -z "$PY" ]; then
  echo "[$(date '+%F %T')] no python3 found; run ./install.sh once" >&2
  exit 1
fi

cd "$PROJ" || exit 1
# No nohup/& juggling: launchd already owns this process, and the app detaches
# itself by being the thing we exec into. exec also means launchd sees the
# app's exit status rather than the gate's.
exec "$PY" "$PROJ/app.py"
