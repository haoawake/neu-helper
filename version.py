# -*- coding: utf-8 -*-
"""版本号 —— **全项目唯一的一处**。

发版流程是:改这里 -> 构建 -> 打 tag `v<VERSION>` -> 发 Release。
`packaging/build_win.ps1` 会核对这里的版本和最近那个 tag 对不对得上,
对不上就拒绝构建 —— 版本号和 tag 不一致的话,更新检查会永远认为
"有新版本"或者"已经最新",两种都很难查。
"""
from __future__ import annotations

VERSION = "2.0.10"

# 更新从这个仓库的 Releases 拿
REPO = "haoawake/neu-helper"


def parse(v: str) -> tuple:
    """"v1.2.3" / "1.2.3" -> (1, 2, 3)。认不出来的段当 0。

    刻意只比数字段:预发布后缀(-beta 之类)一律忽略,因为这个项目不发
    预发布版。真要发的话得先想清楚"1.0.2-beta 和 1.0.2 谁新"再改这里。
    """
    s = (v or "").strip().lstrip("vV").split("+")[0].split("-")[0]
    out = []
    for part in s.split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def is_newer(remote: str, local: str | None = None) -> bool:
    """远端那个版本比本地新吗。

    **相等不算新** —— 不然每次检查都会弹一次"有新版本"。

    local 默认取 VERSION,但**不能写成默认参数** `local: str = VERSION`:
    那个值在函数定义时就绑死了,之后改 `version.VERSION` 不会生效。
    生产里 VERSION 不变所以看不出问题,但测试("装成老版本去查更新")
    会得到一个自相矛盾的结果:current 是 1.0.1、latest 是 1.0.2、
    newer 却是 False。踩过一次。
    """
    try:
        return parse(remote) > parse(local if local is not None else VERSION)
    except Exception:                              # noqa: BLE001
        return False
