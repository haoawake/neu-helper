# -*- coding: utf-8 -*-
"""托盘 / 菜单栏图标 —— **按平台挑宿主的分发层**。

  Windows   tray_win32.py   任务栏右下角通知区,Shell_NotifyIcon
  macOS     tray_cocoa.py   菜单栏右侧,NSStatusItem

**位置不同是刻意的**:macOS 没有"任务栏通知区"这个东西,菜单栏右侧才是它的
对应物。功能和回调是同一套。

存在的理由:三态里有一态是**整个主窗口隐藏**(收成悬浮球)。球被拖到屏幕
边缘、或者手快点了两下收到角落里的时候,除了任务管理器就没有别的出路。
这个图标是那个 always-there 的出路 —— 右键一步退出。

两份宿主暴露同一个 `Tray` 类:

  构造   Tray(on_open, on_expand, on_orb, on_quit, icon_path, tip, labels)
  属性   hwnd(不透明 int;macOS 那边恒为 0,占位保持签名一致)
  方法   set_labels(labels) / set_tip(tip) / visible() / close()

`labels` 是菜单文案,键固定为 open / expand / orb / quit。
**文案由调用方给**(server.py 用 applang.tr 挑语言)—— 原生菜单不是网页,
gui/i18n.js 那套 DOM 翻译够不着它;把文字留在这一层就得在两个宿主里各写
一份中英,那才是真会跑偏的地方。
"""
from __future__ import annotations

import platform_id

if platform_id.IS_WIN:
    from tray_win32 import Tray              # noqa: F401
elif platform_id.IS_MAC:
    from tray_cocoa import Tray              # noqa: F401
else:
    raise platform_id.unsupported("托盘图标")
