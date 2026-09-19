# -*- coding: utf-8 -*-
"""开机速览:先在终端里打一屏,同时把完整数据写进 data/snapshot.md。

分两步是故意的 —— 窗口一弹出来就有东西看,Claude 再去读 snapshot 做分析,
不用等模型响应才知道有没有 DDL。
"""
from __future__ import annotations

import sys
import unicodedata
from datetime import datetime
from pathlib import Path

from canvas_api import CanvasClient, CanvasConfigError

W = 66
WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
HERE = Path(__file__).resolve().parent


def dw(s: str) -> int:
    """显示宽度:CJK 全角字符占 2 列,不然表格会歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - dw(s))


def clip(s: str, width: int) -> str:
    if dw(s) <= width:
        return s
    out = ""
    for c in s:
        if dw(out) + dw(c) > width - 1:
            return out + "…"
        out += c
    return out


def main() -> int:
    try:
        c = CanvasClient()
        me = c.whoami()
        courses = c.courses()
        items = c.upcoming(21)
    except CanvasConfigError as exc:
        print(f"\n  [配置错误] {exc}\n")
        return 1
    except Exception as exc:
        print(f"\n  [Canvas 连接失败] {type(exc).__name__}: {exc}")
        print("  网络不通或 token 失效。可以直接在下面的对话里让我排查。\n")
        return 1

    now = datetime.now()
    short = {x["id"]: (x["code"].split(".")[0] or x["code"])[:8] for x in courses}

    print()
    print("  ╔" + "═" * W + "╗")
    title = f" NEU Helper · {me.get('name')} · {now:%Y-%m-%d} {WEEK[now.weekday()]}"
    print("  ║" + pad(title, W) + "║")
    print("  ╚" + "═" * W + "╝")

    print(f"\n  待办 · 未来 21 天 · 共 {len(items)} 项")
    print("  " + "─" * W)
    if not items:
        print("  (干净,没有临近的 DDL)")
    for it in items[:12]:
        mark = "!!" if it["days_left"] < 1 else ("! " if it["days_left"] < 3 else "  ")
        left = f'{it["days_left"]:.0f} 天' if it["days_left"] >= 1 else "今天"
        done = "已交" if it.get("submitted") else "    "
        print(
            f'  {mark} {pad(left, 6)} {pad(short.get(it["course_id"], "—"), 9)}'
            f'{pad(clip(it["title"].strip(), 30), 31)} {done}'
        )
    if len(items) > 12:
        print(f"  … 还有 {len(items) - 12} 项,见 snapshot.md")

    print("\n  课程总评")
    print("  " + "─" * W)
    for x in courses:
        score = "—" if x["current_score"] is None else f'{x["current_score"]:.1f}'
        print(f'  {pad(clip(x["code"], 34), 35)} {pad(score, 8)} id={x["id"]}')

    # 完整数据落盘,给 Claude 读
    snap = [f"# Canvas 快照 · {now:%Y-%m-%d %H:%M}", "", "## 在读课程", ""]
    for x in courses:
        snap.append(
            f'- **{x["code"]}** (id `{x["id"]}`) — {x["name"]}\n'
            f'  - 当前总评: {x["current_score"]} / 等级 {x["current_grade"]}'
        )
    snap += ["", "## 未来 21 天待办", ""]
    if not items:
        snap.append("(无)")
    for it in items:
        snap.append(
            f'- **{it["title"].strip()}** — {short.get(it["course_id"], "?")} '
            f'[{it["type"]}]\n'
            f'  - 截止 {it["due_local"]} · 剩 {it["days_left"]} 天 · '
            f'{it["points"]} 分 · 已提交={it.get("submitted")}\n'
            f'  - {it.get("url")}'
        )
    snap += ["", "---", "", "数据由 brief.py 抓取。要更细的信息用 canvas_* MCP 工具现查,不要只依赖这份快照。", ""]
    out = HERE / "data" / "snapshot.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(snap), encoding="utf-8")
    print(f"\n  快照 → {out.relative_to(HERE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
