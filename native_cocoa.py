# -*- coding: utf-8 -*-
"""操作自己那个窗口 —— **native_window 在 macOS 上的实现**。

不要直接 import 这个模块,import `native_window`(它按平台挑实现)。
Windows 那份是 native_win32.py,两边对外暴露同一组函数名。

三件事和 Win32 不一样,是这个文件绝大部分复杂度的来源:

**一、"句柄"是 NSWindow 的 windowNumber。**
上层把句柄当一个不透明的 int 传来传去,所以 macOS 这边挑了 `windowNumber()`
—— 它也是个 int,而且能用 `NSApp.windowWithWindowNumber_()` 反查回 NSWindow。
正因为两边都能用一个 int 表示,server.py 才一行都不用改。

**二、坐标对外是"原点左上、y 向下",和 Win32 一致。**
Cocoa 原生是原点左下、y 向上。翻转只发生在 `_to_cocoa` / `_from_cocoa`
这一对函数里,别的地方一律按 y 向下算。理由见 native_window.py 的模块文档
(上层有真实的几何算术默认 y 向下,比如"把浮窗放在球的上方")。

翻转的基准是**主屏**(`NSScreen.screens()[0]`,也就是原点在 (0,0) 那块),
不是 `mainScreen()` —— 后者是"当前有键盘焦点的那块屏",会跟着鼠标变,
拿它当基准的话多屏下窗口位置会随焦点漂。

**三、AppKit 只能在主线程上动。**
而这个应用的窗口操作全是从 Flask 的工作线程发起的(HTTP 请求进来就调)。
Win32 的 SetWindowPos/ShowWindow 本来就为跨线程设计,直接调没问题;
AppKit 不是 —— 从后台线程改 NSWindow 是未定义行为,轻则不刷新重则崩。

所以这里所有调用都经过 `_main` 转到主线程,而且是**同步等结果**的。
必须同步,因为上层有"写完立刻读"的代码:

    native_window.set_geometry(h, w, ht, anchor_br=anchor)
    target = native_window.get_rect(h)      # 要读到刚设好的那个

异步投递的话这里会读到旧值,展开动画的起点就错了。
"""
from __future__ import annotations

import threading
import time

from AppKit import (
    NSApplication,
    NSEvent,
    NSFloatingWindowLevel,
    NSImage,
    NSNormalWindowLevel,
    NSScreen,
)
from Foundation import NSMakeRect, NSThread
from PyObjCTools import AppHelper

# 动画:和 Windows 那边同一套节奏,曲线也一样(_ease_in_out)
_ANIM_LOCK = threading.Lock()

# 展开前的矩形。和 Win32 版一样按句柄记 —— 收起来再点开要回到原来的大小
_pre_expand: dict[int, tuple[int, int, int, int]] = {}
_expand_lock = threading.Lock()

# 拖拽状态:起点的光标位置 + 窗口原点(和 Win32 版同一个思路,见下)
_drag: dict | None = None

_MAIN_TIMEOUT = 2.0


# ─────────────────────────── 主线程转发 ───────────────────────────


def _main(fn, *args):
    """把 fn 丢到主线程执行并等它的返回值。

    已经在主线程上就直接调 —— 不然会自己等自己,死锁。

    超时的兜底是**直接在本线程调**。这不安全,但比整个界面卡住好:主线程
    真的被什么东西堵住了(比如系统弹了个模态框)的时候,宁可冒一次风险,
    也不要让用户点什么都没反应。超时了会打一行日志,方便事后查。
    """
    if NSThread.isMainThread():
        return fn(*args)

    box: list = []
    done = threading.Event()

    def run():
        try:
            box.append(fn(*args))
        except Exception as exc:                # noqa: BLE001
            box.append(exc)
        finally:
            done.set()

    AppHelper.callAfter(run)
    if not done.wait(_MAIN_TIMEOUT):
        print(f"[native_cocoa] 主线程 {_MAIN_TIMEOUT}s 没响应,"
              f"{getattr(fn, '__name__', fn)} 退回本线程直接调")
        return fn(*args)
    out = box[0] if box else None
    if isinstance(out, Exception):
        raise out
    return out


def _app():
    return NSApplication.sharedApplication()


def wait_for_main_loop(timeout: float = 20.0) -> bool:
    """等主线程的 run loop 真正开始转。

    启动时序上有个坑:`setup_orb()` 跑在一条后台线程上,而它要建 NSWindow,
    那必须转到主线程 —— 可主线程此刻还在 pywebview 的初始化里,`webview.start()`
    还没开始转 run loop。这时候投过去的活儿没人执行,`_main` 只能等到超时
    再退回本线程直接调(那对建窗口来说是不安全的)。

    所以先在这儿等一下:投一个空活儿过去,它被执行到了就说明 run loop 活了。
    """
    got = threading.Event()
    AppHelper.callAfter(got.set)
    return got.wait(timeout)


# ─────────────────────────── 坐标换算 ───────────────────────────


def _primary_height() -> float:
    """主屏高度 —— y 翻转的基准。

    刻意用 `screens()[0]`(原点在 (0,0) 那块)而不是 `mainScreen()`:
    后者是"当前有焦点的屏",会跟着鼠标走,拿它当基准多屏下窗口会漂。
    """
    scr = NSScreen.screens()
    if not scr:
        return 0.0
    return float(scr[0].frame().size.height)


def _to_cocoa(x: float, y: float, w: float, h: float):
    """(左, 上, 宽, 高) y 向下  ->  Cocoa 的 NSRect(原点左下)。"""
    return NSMakeRect(x, _primary_height() - (y + h), w, h)


def _from_cocoa(rect) -> tuple[int, int, int, int]:
    """Cocoa 的 NSRect  ->  (左, 上, 宽, 高) y 向下。"""
    w = float(rect.size.width)
    h = float(rect.size.height)
    return (int(round(rect.origin.x)),
            int(round(_primary_height() - rect.origin.y - h)),
            int(round(w)), int(round(h)))


# ─────────────────────────── 找窗口 ───────────────────────────


def find_own_window(title: str) -> int:
    """按标题找本进程的窗口,返回 windowNumber(找不到给 0)。

    只看本进程的窗口(`NSApp.windows()` 本来就只有自己的),所以不像
    Win32 那边还要比对进程 id。
    """
    def go():
        for w in _app().windows():
            try:
                if str(w.title()) == title:
                    return int(w.windowNumber())
            except Exception:                   # noqa: BLE001,PERF203
                continue
        return 0
    return _main(go)


def _win(handle: int):
    """句柄 -> NSWindow。窗口已经关了就返回 None。"""
    if not handle:
        return None
    return _app().windowWithWindowNumber_(int(handle))


def is_window(handle: int) -> bool:
    return bool(handle) and _main(lambda: _win(handle) is not None)


# ─────────────────────────── 几何 ───────────────────────────


def get_rect(handle: int) -> tuple[int, int, int, int]:
    """(左, 上, 右, 下) —— 和 Win32 的 GetWindowRect 同构,y 向下。"""
    def go():
        w = _win(handle)
        if w is None:
            return (0, 0, 0, 0)
        x, y, ww, hh = _from_cocoa(w.frame())
        return (x, y, x + ww, y + hh)
    return _main(go)


def dpi_scale(handle: int) -> float:
    """逻辑尺寸 -> 窗口单位。

    **macOS 上恒为 1.0**,因为 Cocoa 的窗口 API 收的是点(point),Retina
    的事情由系统处理。要"位图画多大"请用 render_scale —— 两者在 Windows 上
    相等,在这里不相等,混用会让窗口尺寸直接翻倍。
    """
    return 1.0


def render_scale(handle: int) -> float:
    """一个点要画几个位图像素 —— Retina 上是 2.0。悬浮球和弹窗按这个画。"""
    def go():
        w = _win(handle)
        scr = (w.screen() if w is not None else None) or NSScreen.mainScreen()
        if scr is None:
            return 1.0
        return float(scr.backingScaleFactor())
    return _main(go)


def _screen_of(handle: int):
    w = _win(handle)
    return (w.screen() if w is not None else None) or NSScreen.mainScreen()


def work_area(handle: int) -> tuple[int, int, int, int]:
    """窗口所在那块屏的可用区域 (x, y, w, h),y 向下,已经排除菜单栏和 Dock。

    `visibleFrame` 就是"去掉菜单栏和 Dock 之后剩下的",正好对应 Win32 的
    `rcWork`。注意它随 Dock 的显示/隐藏变化,所以每次都现问,不缓存。
    """
    def go():
        scr = _screen_of(handle)
        if scr is None:
            return (0, 0, 0, 0)
        return _from_cocoa(scr.visibleFrame())
    return _main(go)


def set_rect(handle: int, x: int, y: int, w: int, h: int) -> None:
    def go():
        win = _win(handle)
        if win is None:
            return
        win.setFrame_display_(_to_cocoa(x, y, w, h), True)
    _main(go)


def move_to(handle: int, x: int, y: int) -> None:
    def go():
        win = _win(handle)
        if win is None:
            return
        _, _, w, h = _from_cocoa(win.frame())
        win.setFrameOrigin_(_to_cocoa(x, y, w, h).origin)
    _main(go)


def set_size(handle: int, logical_w: int, logical_h: int) -> None:
    left, top, _, _ = get_rect(handle)
    set_rect(handle, left, top, logical_w, logical_h)


def set_geometry(handle: int, logical_w: int, logical_h: int,
                 anchor_br: tuple[int, int] | None = None) -> None:
    """改尺寸。给了 anchor_br 就让**右下角**钉在那个点上。

    右下角锚定是为了"从对话框长成大面板"时视觉上从右下往左上展开 ——
    和 Win32 版同一个语义。
    """
    if anchor_br is None:
        set_size(handle, logical_w, logical_h)
        return
    ax, ay = anchor_br
    x, y, w, h = nudge_onscreen(handle, ax - logical_w, ay - logical_h,
                                logical_w, logical_h)
    set_rect(handle, x, y, w, h)


def resize_anchor_bottom_right(handle: int, logical_w: int,
                               logical_h: int) -> None:
    _, _, right, bottom = get_rect(handle)
    set_geometry(handle, logical_w, logical_h, anchor_br=(right, bottom))


def nudge_onscreen(handle: int, x: int, y: int, w: int, h: int):
    """把矩形挪到"至少还能抓得住"的地方,返回调整后的 (x, y, w, h)。

    **刻意不把窗口硬塞进工作区**,和 Win32 版保持一致:用户可能就是想让窗口
    一部分出屏(比如贴着边只留一条)。只保证两件事 —— 标题那一条在屏幕里
    (不然没法拖回来)、横向至少留 120 点可见。
    """
    wx, wy, ww, wh = work_area(handle)
    if ww <= 0:
        return (x, y, w, h)
    keep = min(120, w)
    x = max(wx - (w - keep), min(x, wx + ww - keep))
    # 上边不能跑到菜单栏上面去(Cocoa 不会帮你拦),下边至少留一条能抓
    y = max(wy, min(y, wy + wh - 32))
    return (int(x), int(y), int(w), int(h))


# ─────────────────────────── 显隐 / 层级 ───────────────────────────


def show(handle: int, foreground: bool = True) -> None:
    def go():
        win = _win(handle)
        if win is None:
            return
        if foreground:
            _app().activateIgnoringOtherApps_(True)
            win.makeKeyAndOrderFront_(None)
        else:
            # 不抢焦点地显示。orderFrontRegardless 连"应用没激活"都无视 ——
            # 备忘录浮窗要的就是这个:弹出来但不打断你正在打的字
            win.orderFrontRegardless()
    _main(go)


def hide(handle: int) -> None:
    def go():
        win = _win(handle)
        if win is not None:
            win.orderOut_(None)
    _main(go)


def is_visible(handle: int) -> bool:
    def go():
        win = _win(handle)
        return bool(win is not None and win.isVisible())
    return _main(go)


def minimize(handle: int) -> None:
    def go():
        win = _win(handle)
        if win is not None:
            win.miniaturize_(None)
    _main(go)


def restore(handle: int) -> None:
    def go():
        win = _win(handle)
        if win is None:
            return
        if win.isMiniaturized():
            win.deminiaturize_(None)
        win.orderFrontRegardless()
    _main(go)


def is_iconic(handle: int) -> bool:
    def go():
        win = _win(handle)
        return bool(win is not None and win.isMiniaturized())
    return _main(go)


def close(handle: int) -> None:
    def go():
        win = _win(handle)
        if win is not None:
            win.performClose_(None)
    _main(go)


def set_topmost(handle: int, on: bool) -> bool:
    """置顶 = 提到浮动层。

    用 NSFloatingWindowLevel 而不是更高的层:再高会盖住系统的输入法候选框和
    通知,那种"永远在最前面"是招人烦的。
    """
    def go():
        win = _win(handle)
        if win is None:
            return False
        win.setLevel_(NSFloatingWindowLevel if on else NSNormalWindowLevel)
        return True
    return bool(_main(go))


def is_topmost(handle: int) -> bool:
    def go():
        win = _win(handle)
        return bool(win is not None and int(win.level()) > NSNormalWindowLevel)
    return _main(go)


def set_window_alpha(handle: int, alpha: float) -> bool:
    def go():
        win = _win(handle)
        if win is None:
            return False
        win.setAlphaValue_(max(0.0, min(1.0, float(alpha))))
        return True
    return bool(_main(go))


def set_window_icon(handle: int, icon_path: str) -> bool:
    """换 Dock 图标。

    和 Windows 不同,**这是整个应用级别的**,不是某个窗口的 —— macOS 的窗口
    没有自己的图标。签名保持一致(收 handle)是为了让 app.py 不用分叉。
    """
    def go():
        img = NSImage.alloc().initWithContentsOfFile_(icon_path)
        if img is None:
            return False
        _app().setApplicationIconImage_(img)
        return True
    return bool(_main(go))


def is_foreground(handle: int) -> bool:
    """这个窗口是当前的焦点窗口吗。

    要求应用自己是激活状态 **且** 这个窗口是 key window —— 少了前一个条件,
    应用切到后台时它还会返回真,备忘录浮窗的那个看门线程就永远不收起来。
    """
    def go():
        win = _win(handle)
        return bool(win is not None and _app().isActive() and win.isKeyWindow())
    return _main(go)


def cursor_pos() -> tuple[int, int]:
    """光标位置,y 向下。

    `NSEvent.mouseLocation()` 不用在主线程上取(它读的是全局事件状态,
    不碰任何窗口),所以这里不转发 —— 拖窗口时每次 move 都要问一遍,
    省掉一次主线程往返明显更跟手。
    """
    p = NSEvent.mouseLocation()
    return (int(round(p.x)), int(round(_primary_height() - p.y)))


# ─────────────────────────── 最大化 ───────────────────────────


def is_maximized(handle: int) -> bool:
    def go():
        win = _win(handle)
        return bool(win is not None and win.isZoomed())
    return _main(go)


def is_expanded(handle: int, slack: int = 8) -> bool:
    """铺满工作区了吗。

    **不能只信 isZoomed()。** 这个应用是自己 setFrame 铺满的(为了能做动画),
    不走系统的 zoom,所以 isZoomed 那条路经常是 False。按几何判才准 ——
    和 Win32 版同一个做法。
    """
    x, y, right, bottom = get_rect(handle)
    wx, wy, ww, wh = work_area(handle)
    if ww <= 0:
        return False
    return (abs(x - wx) <= slack and abs(y - wy) <= slack
            and abs((right - x) - ww) <= slack
            and abs((bottom - y) - wh) <= slack)


def unzoom(handle: int) -> None:
    """如果是系统 zoom 出来的,先退出来 —— 不然 setFrame 会被系统弹回去。"""
    def go():
        win = _win(handle)
        if win is not None and win.isZoomed():
            win.zoom_(None)
    _main(go)


def get_pre_expand(handle: int):
    with _expand_lock:
        return _pre_expand.get(int(handle))


def set_pre_expand(handle: int, rect) -> None:
    with _expand_lock:
        if rect:
            _pre_expand[int(handle)] = tuple(rect)
        else:
            _pre_expand.pop(int(handle), None)


def toggle_maximize(handle: int, duration: float = 0.26, frames: int = 20,
                    threaded: bool = True) -> dict:
    """双击标题栏:铺满 <-> 还原。带过渡动画,不用系统的 zoom。

    自己做而不是调 `zoom_(None)` 的原因和 Windows 那边一样:系统的最大化是
    瞬间跳变,而且还原的目标由系统记着、和这个应用的三态对不上。
    """
    unzoom(handle)
    wx, wy, ww, wh = work_area(handle)
    if ww <= 0:
        return {"ok": False, "expanded": False}

    if is_expanded(handle):
        back = get_pre_expand(handle)
        if not back:
            back = (wx + ww // 8, wy + wh // 8, ww * 3 // 4, wh * 3 // 4)
        set_pre_expand(handle, None)
        target, expanded = back, False
    else:
        left, top, right, bottom = get_rect(handle)
        set_pre_expand(handle, (left, top, right - left, bottom - top))
        target, expanded = (wx, wy, ww, wh), True

    def run():
        animate_rect(handle, *target, duration=duration, frames=frames)

    if threaded:
        threading.Thread(target=run, daemon=True, name="zoomanim").start()
    else:
        run()
    return {"ok": True, "expanded": expanded}


# ─────────────────────────── 动画 ───────────────────────────


def _ease_in_out(p: float) -> float:
    """和 Win32 版同一条曲线 —— 两边手感必须一致。"""
    return 4 * p * p * p if p < 0.5 else 1 - pow(-2 * p + 2, 3) / 2


def animate_rect(handle: int, x: int, y: int, w: int, h: int,
                 duration: float = 0.26, frames: int = 20) -> None:
    """逐帧 setFrame 到目标矩形。

    **刻意不用 `setFrame:display:animate:`。** 那个是同步阻塞的(动画跑完才
    返回)、时长由系统定、而且缓动曲线和 Windows 那边不一样 —— 同一个应用
    在两个系统上手感不同是要避免的。逐帧自己走,曲线和帧数就都对得上。
    """
    left, top, right, bottom = get_rect(handle)
    if not (right - left):
        set_rect(handle, x, y, w, h)
        return
    x0, y0, w0, h0 = left, top, right - left, bottom - top
    with _ANIM_LOCK:
        for i in range(1, frames + 1):
            t = _ease_in_out(i / frames)
            set_rect(handle,
                     int(x0 + (x - x0) * t), int(y0 + (y - y0) * t),
                     int(w0 + (w - w0) * t), int(h0 + (h - h0) * t))
            time.sleep(duration / frames)
        set_rect(handle, x, y, w, h)


def animate_to(handle: int, logical_w: int, logical_h: int,
               duration: float = 0.26, frames: int = 20,
               anchor_br: tuple[int, int] | None = None) -> None:
    if anchor_br is None:
        left, top, _, _ = get_rect(handle)
        animate_rect(handle, left, top, logical_w, logical_h, duration, frames)
        return
    ax, ay = anchor_br
    x, y, w, h = nudge_onscreen(handle, ax - logical_w, ay - logical_h,
                               logical_w, logical_h)
    animate_rect(handle, x, y, w, h, duration, frames)


# ─────────────────────────── 拖拽 ───────────────────────────
#
# 和 Windows 那边**同一个做法**:前端 pointerdown 时记一次起点,pointermove
# 时来一条请求,后端每次自己问光标位置算位移。
#
# 不用 Cocoa 的 `performWindowDragWithEvent:` 是因为它要一个真实的 NSEvent
# 对象 —— 而这里的触发来自一条 HTTP 请求,手里没有事件。而且前端给的
# `screenX` 是 CSS 像素,后端要的是点,换算在多屏 + 不同缩放下很容易错,
# 索性就完全不碰它。


def drag_start(handle: int) -> bool:
    global _drag
    left, top, _, _ = get_rect(handle)
    cx, cy = cursor_pos()
    _drag = {"h": int(handle), "cx": cx, "cy": cy, "wx": left, "wy": top}
    return True


def drag_move() -> bool:
    if not _drag:
        return False
    cx, cy = cursor_pos()
    move_to(_drag["h"],
            _drag["wx"] + (cx - _drag["cx"]),
            _drag["wy"] + (cy - _drag["cy"]))
    return True


def drag_end() -> None:
    global _drag
    _drag = None
