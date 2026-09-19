# -*- coding: utf-8 -*-
"""命令行触发一次课件同步(和桌面应用里那个是同一套 filesync)。

    sync_now.py            增量同步
    sync_now.py --force    连课程数据一起重拉(默认用 5 分钟内的缓存)
    sync_now.py --status   只看状态,不同步

要用项目那个解释器(requests 装在那边),当前的不对就自动换。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 这个脚本在 <项目>/.claude/skills/course-sync/scripts/ 下
PROJ = Path(__file__).resolve().parents[4]
DOWNLOADS = PROJ / "data" / "downloads"


def _project_python() -> Path | None:
    rec = PROJ / "data" / "pythonw-path.txt"
    try:
        pyw = Path(rec.read_text(encoding="utf-8-sig").strip())
    except Exception:
        return None
    cand = pyw.with_name("python.exe")
    return cand if cand.exists() else None


def _reexec_if_needed() -> None:
    """该有的库都在吗?缺就换项目的解释器重跑。

    子进程而不是 os.execve —— Windows 上那个是模拟的,实测会段错误。
    **fitz 也要查**:缺了它 PDF 抽不出纯文本,同步会静默少写十几份 .txt,
    而日志看起来一切正常(系统 python 有 requests 但没 PyMuPDF,踩过)。
    """
    missing = []
    for mod in ("requests", "fitz"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if not missing:
        return
    py = _project_python()
    if not py or os.environ.get("SYNCNOW_REEXEC"):
        raise SystemExit(
            f"这个 python 缺 {chr(12289).join(missing)},也找不到项目记录的解释器。"
            "跑一次 install.ps1,或者手动指定装了这些库的 python。")
    env = dict(os.environ, SYNCNOW_REEXEC="1", PYTHONUTF8="1",
               PYTHONIOENCODING="utf-8")
    r = subprocess.run([str(py), os.path.abspath(__file__)] + sys.argv[1:], env=env)
    sys.exit(r.returncode)


def show_status() -> None:
    idx = DOWNLOADS / "index.json"
    if not idx.exists():
        print("本地还没有任何课件(没有 index.json),需要同步一次。")
        return
    d = json.loads(idx.read_text(encoding="utf-8-sig"))
    files = d.get("files", [])
    total = sum(f.get("size") or 0 for f in files)
    print(f"索引生成于 {d.get('generated')} · {len(files)} 个文件 · "
          f"{total / 1024 / 1024:.1f} MB")
    by_course: dict[str, int] = {}
    for f in files:
        by_course[f.get("course") or "?"] = by_course.get(f.get("course") or "?", 0) + 1
    for c, n in sorted(by_course.items(), key=lambda kv: -kv[1]):
        print(f"  {c:<16} {n} 个")
    # 最近改动的几个,判断"新东西下来了没"就看这个
    recent = sorted(files, key=lambda f: f.get("mtime") or "", reverse=True)[:5]
    print("最近更新的:")
    for f in recent:
        print(f"  {f.get('mtime')}  {f.get('course')}/{f.get('name')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="同步 Canvas 课件到本地")
    ap.add_argument("--force", action="store_true", help="连课程数据一起重拉")
    ap.add_argument("--status", action="store_true", help="只看状态")
    args = ap.parse_args()

    if args.status:
        show_status()
        return

    _reexec_if_needed()
    sys.path.insert(0, str(PROJ))
    import server                       # 复用桌面应用那套 Backend / FileSync

    b = server.backend
    d = b.dashboard()
    if d.get("error"):
        raise SystemExit(f"读不到 Canvas:{d.get('message')}")
    courses = d.get("courses") or []
    print(f"{len(courses)} 门课:" + "、".join(c["short"] for c in courses))

    t0 = time.time()
    if not b.sync.sync_async(courses, force=args.force):
        print("已经有一个同步在跑了,等它结束。")
        return
    time.sleep(0.6)
    last = ""
    while b.sync.is_running() and time.time() - t0 < 900:
        st = b.sync.snapshot()
        line = f"  {st['done']}/{st['total']} {st['current'][:46]}"
        if line != last:
            print(line, flush=True)
            last = line
        time.sleep(1.2)

    st = b.sync.snapshot()
    print()
    print(f"完成于 {datetime.now().strftime('%H:%M:%S')},用了 {time.time() - t0:.0f}s")
    print(f"  新增 {st['added']} · 更新 {st['updated']} · 跳过 {st['skipped']}"
          f"(其中太大 {st.get('too_big', 0)}) · 失败 {st['failed']}"
          f" · 传输 {st['bytes'] / 1024 / 1024:.1f} MB")
    if st.get("textified"):
        print(f"  给 {st['textified']} 份 Office 文档抽了纯文本(.txt)")
    for e in st.get("errors") or []:
        print("  错误:", e)
    print()
    show_status()


if __name__ == "__main__":
    main()
