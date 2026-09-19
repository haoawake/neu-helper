# -*- coding: utf-8 -*-
"""悬浮球 —— **按平台挑宿主的分发层**。

球的**视觉在 orb_render.py,两个平台共用一份**(纯 Python 逐像素算,输出预乘
alpha 的 BGRA)。这里挑的只是"怎么把那块位图交给系统去合成":

  Windows   orb_win32.py   WS_EX_LAYERED 窗口 + UpdateLayeredWindow
  macOS     orb_cocoa.py   无边框透明 NSWindow + CGImage

两份宿主暴露同一个 `OrbWindow` 类,构造参数和方法名完全一致 ——
`app.py` 那边一行都不用按平台分叉。

`OrbWindow` 的约定(两边都得满足):

  构造      OrbWindow(on_click, on_expand, on_quit, on_moved, on_hold,
                      on_unhover, diameter, scale, dark, animate)
  属性      hwnd(不透明 int)、diameter
  方法      show / hide / hide_now / set_fade / settle / set_badge /
            set_look / position / visible
"""
from __future__ import annotations

import platform_id

if platform_id.IS_WIN:
    from orb_win32 import OrbWindow          # noqa: F401
elif platform_id.IS_MAC:
    from orb_cocoa import OrbWindow          # noqa: F401
else:
    raise platform_id.unsupported("悬浮球")
