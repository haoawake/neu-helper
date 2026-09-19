#!/bin/bash
# Double-click this in Finder to open NEU Helper (macOS).
#
# The counterpart of launch.vbs: opens the app directly, with no once-per-day
# gate. The .command extension is what makes Finder run it in Terminal on a
# double-click -- a plain .sh would open in an editor instead.
#
# The Terminal window that appears is unavoidable for a .command file. It is
# closed again below as soon as the app is up, so it only flashes.
set -u

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
  echo "python3 not found. Run ./install.sh in the project folder once."
  echo "Press return to close."
  read -r _
  exit 1
fi

cd "$PROJ" || exit 1

# Detach so the app outlives this shell; output goes to the same log the app
# writes anyway, so nothing is lost when the terminal closes.
mkdir -p "$PROJ/data"
nohup "$PY" "$PROJ/app.py" >> "$PROJ/data/app.log" 2>&1 &

# Close the Terminal window. `exit 0` alone is not enough -- Terminal keeps the
# window around showing "Process completed" unless told otherwise. This asks it
# to close the frontmost window and is harmless if it was launched some other
# way (from a real shell, the osascript just fails quietly).
osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
exit 0
