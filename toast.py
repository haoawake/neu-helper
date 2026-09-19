# -*- coding: utf-8 -*-
"""右下角信息弹窗 —— **按平台挑宿主的分发层**。

卡片底和外阴影在 toast_render.py,**两个平台共用一份**(纯 Python 逐像素,
输出预乘 alpha 的 BGRA)。这里挑的只是"怎么量文字、怎么交给系统合成":

  Windows   toast_win32.py   GDI 量/写文字 + UpdateLayeredWindow
  macOS     toast_cocoa.py   Core Text 量/写文字 + CGImage

两份宿主暴露同一个 `Toast` 类:

  构造   Toast(on_click, scale, dark)
  属性   hwnd(不透明 int)、dark(可随时改,下一条就跟着走)
  方法   show(title, body, secs) -> bool、hide()
"""
from __future__ import annotations

import platform_id

if platform_id.IS_WIN:
    from toast_win32 import Toast            # noqa: F401
elif platform_id.IS_MAC:
    from toast_cocoa import Toast            # noqa: F401
else:
    raise platform_id.unsupported("信息弹窗")
