# -*- coding: utf-8 -*-
"""操作自己那个窗口 —— **按平台挑实现的分发层**。

`server.py` 和 `app.py` 只 import 这个模块,不知道底下是 Win32 还是 Cocoa。
两份实现对外暴露同一组函数名和同一套语义:

  找窗口    find_own_window / is_window
  几何      get_rect / set_rect / set_size / set_geometry / work_area / dpi_scale
  形态      show / hide / minimize / restore / close / is_visible / is_iconic
  置顶      set_topmost / is_topmost
  透明      set_window_alpha
  最大化    is_maximized / is_expanded / unzoom / toggle_maximize
            get_pre_expand / set_pre_expand
  动画      animate_to / animate_rect
  杂        cursor_pos / is_foreground / nudge_onscreen / set_window_icon
  拖拽      drag_start / drag_move / drag_end

**"句柄"是一个 int,但两边含义不同。** Windows 上是 HWND;macOS 上是
NSWindow 的 `windowNumber`(也是个 int,能反查回 NSWindow)。调用方只把它
当不透明的 int 传递就行 —— 正因为两边都能用一个 int 表示,上层才一行都不用改。

**坐标系统一成"原点左上、y 向下"** —— 也就是 Win32 那一套。Cocoa 原生是
原点左下、y 向上,所以 macOS 实现内部会翻一次(只在 native_cocoa.py 的两个
辅助函数里翻,别处不碰)。

这不是随便选的。上层代码里有真实的几何算术**默认 y 向下**,比如备忘录浮窗
贴着球放那一句:

    y = by - ph - 8        # 意思是"放在球的上方"

y 向下时 `by - ph` 更靠上,是对的;换成 Cocoa 的 y 向上,同一行就变成了
"放在球的下方"。这种错不会报异常,只会让窗口出现在离奇的位置 ——
所以宁可在实现里翻一次,也不让两种坐标系同时存在于调用方。

**尺寸有两个"缩放",别混用:**

  dpi_scale(h)     逻辑尺寸 -> 该平台的窗口单位。Windows 是 DPI 比例
                   (窗口 API 收的是物理像素);macOS 返回 1.0,因为 Cocoa
                   本来就用点(point)为单位,自己处理 Retina
  render_scale(h)  一个窗口单位要画几个位图像素。Windows 同样是 DPI 比例;
                   macOS 是 backingScaleFactor(Retina 上是 2.0)

Windows 上这两个数相等,所以以前只有一个函数也没出过问题;macOS 上它们
必须分开 —— 混用的后果是窗口尺寸翻倍、或者球在 Retina 上糊成一团。
"""
from __future__ import annotations

import platform_id

if platform_id.IS_WIN:
    from native_win32 import *          # noqa: F401,F403
    from native_win32 import user32     # noqa: F401  (排障用,新代码别依赖)
elif platform_id.IS_MAC:
    from native_cocoa import *          # noqa: F401,F403
else:
    raise platform_id.unsupported("窗口控制")
