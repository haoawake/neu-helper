# -*- coding: utf-8 -*-
"""悬浮球 —— **macOS 宿主(无边框透明 NSWindow + CGImage)**。

不要直接 import 这个模块,import `orb_window`(它按平台挑实现)。
球长什么样在 orb_render.py,**和 Windows 共用那一份**;这里只负责宿主。

和 Windows 那份宿主的四个结构性差别:

**一、没有自己的线程。**
Win32 版起了一条线程跑自己的消息循环(Windows 的消息按线程投递,窗口必须在
跑循环的那条线程里创建)。AppKit 不是这样 —— 所有窗口都必须在**主线程**上,
而主线程的 run loop 由 pywebview 的 `webview.start()` 在跑。所以这里不建线程,
建窗口和刷新都转到主线程,外部调用(show / set_badge / …)一律走
`native_cocoa._main`。

**二、动画是定时器驱动的帧序列,不能 sleep。**
Win32 版在自己那条线程上 `time.sleep(POP_MS)` 一帧一帧贴,睡的是它自己。
这里所有代码都在主线程上跑,一睡就把整个 run loop 堵住 —— WebView 会跟着
卡住 150ms,能看出来。所以弹出/收起改成**把帧排进一个队列**,由一个短周期
NSTimer 一帧一帧取,`show()` / `hide()` 排完就返回。
(顺带:Win32 那边 `hide()` 也是 PostMessage 出去就返回,所以"异步"这一点
两边本来就一致。)

**三、hover 的交叉淡化交给 Core Graphics,不自己混像素。**
Win32 版用 GDI 的 `AlphaBlend` 把 hover 帧叠在静止帧上。这里更省事:在
`drawRect_` 里**把两张 CGImage 叠着画**,第二张设一个 `CGContextSetAlpha(t)`,
由系统(GPU)合成。试过纯 Python 逐像素混,一帧 62x62 要 4.3ms,Retina 上
是 4 倍像素 ≈ 17ms —— 26ms 的刷新间隔吃不下,而叠两张图基本不花时间。

**四、角标的数字不往像素缓冲里栅格化。**
`orb_render.draw_badge` 会把圆盘填好(纯 Python),然后调画布的
`draw_text_centered` 写数字。Win32 那边是 GDI 直接写进 DIB;这里**只把请求
记下来**,由 NSView 在画完球之后用 `NSAttributedString` 叠上去。这样就完全
不需要"往裸内存里跑 Core Text",少一大块容易出错的代码,而且文字由 AppKit
渲染、自带 Retina 和字距。

**尺寸单位:几何用点(point),位图用像素。**
NSWindow 的 frame 是点,而位图要按 `backingScaleFactor` 超采样才不糊。
所以这里 `diameter` / `pad` / `size` 返回的是**点**,位图边长是它们乘 `scale`
(app.py 传进来的是 `native_window.render_scale`)。Windows 那边两者相等,
所以那份代码里这两个概念是混着的 —— 这里必须分开。
"""
from __future__ import annotations

import array
import threading
import time
import traceback

import objc
import Quartz
from AppKit import (
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSMenu,
    NSMenuItem,
    NSScreen,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
)
from Foundation import (
    NSMakePoint,
    NSMakeRect,
    NSMutableDictionary,
    NSObject,
    NSString,
    NSTimer,
)

import native_cocoa
from orb_render import canvas_size, draw_badge, render_ball

NSBackingStoreBuffered = 2
NSFloatingWindowLevel = 3

# ── 时间和手感的常数。**必须和 orb_win32.py 里那一套一致** ——
#    同一个应用在两个系统上手感不同是要避免的,所以这些数字是照抄的。
POP = (0.52, 0.74, 0.90, 1.02, 1.05, 1.0)
POP_MS = 0.026
GLOW_FRAMES = 24
GLOW_MS = 110
GLOW_MS_HOVER = 44
TWEEN_MS = 26
HOVER_TWEEN = 0.50
GLOW_PERIOD = 2.6
GLOW_PERIOD_HOVER = 1.3
HOVER_FRAMES = 8
HOVER_SCALE = 1.05
HOVER_BRIGHT = 1.30
IDLE_ALPHA = 226
HOVER_ALPHA = 255
HOLD_SECONDS = 1.0
EDGE_MARGIN = 6
DRAG_SLOP = 3

_CS = Quartz.CGColorSpaceCreateDeviceRGB()
# 预乘 alpha + 小端 32 位 = 内存里 B,G,R,A —— 和 orb_render 输出的字节序一致,
# 也和 Windows 的 UpdateLayeredWindow 要的一致。**改这两个标志就会串色。**
_BITMAP_INFO = (Quartz.kCGImageAlphaPremultipliedFirst
                | Quartz.kCGBitmapByteOrder32Little)


class Frame:
    """一张预渲染好的位图。

    `px` 是 `array('I')`(小端机器上就是 BGRA),`orb_render` 直接按下标写。
    `cgimage()` 把它变成一张 CGImage 给视图画。

    `draw_text_centered` **不真的画字** —— 只把请求记到 `text_ops`,由视图
    在画完球之后用 AppKit 叠上去(理由见模块文档)。
    """

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.px = array.array("I", bytes(w * h * 4))
        self.text_ops: list[tuple] = []
        self._img = None
        self._keep = None

    def draw_text_centered(self, text: str, rect, px: int, bold: bool,
                           color: tuple[int, int, int]) -> None:
        self.text_ops.append((text, tuple(rect), px, bold, color))

    def cgimage(self):
        """缓存一张 CGImage。

        位图内容画完就不再变(每套帧都是重新 render 出来的),所以缓存是
        安全的;不缓存的话每帧都要重建一次 CGImage,白花时间。
        """
        if self._img is None:
            # CGDataProvider **不持有**传进去的数据,所以这份 bytes 必须
            # 自己留着引用 —— 被 GC 掉的话画出来是花屏
            self._keep = bytes(self.px)
            provider = Quartz.CGDataProviderCreateWithCFData(self._keep)
            self._img = Quartz.CGImageCreate(
                self.w, self.h, 8, 32, self.w * 4, _CS, _BITMAP_INFO,
                provider, None, False, Quartz.kCGRenderingIntentDefault)
        return self._img

    def destroy(self) -> None:
        self._img = None
        self._keep = None


def _ns_color(rgb: tuple[int, int, int], alpha: float = 1.0):
    r, g, b = rgb
    return NSColor.colorWithSRGBRed_green_blue_alpha_(
        r / 255.0, g / 255.0, b / 255.0, alpha)


class OrbView(NSView):
    """画球 + 收鼠标。事件回调都在主线程上,可以直接动窗口。"""

    def initWithOrb_frame_(self, orb, frame):
        self = objc.super(OrbView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._orb = orb
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways
            | NSTrackingInVisibleRect,
            self, None)
        self.addTrackingArea_(area)
        return self

    def isOpaque(self):
        return False

    def isFlipped(self):
        """**必须是 False(AppKit 的默认)。**

        Quartz 里 CGImage 的第一行是图像的顶行,在**非翻转**的上下文里
        `CGContextDrawImage` 画出来是正的。这里要是返回 True(翻转坐标系),
        球会上下颠倒 —— 左上那块高光跑到左下、投影跑到上面去。写出来是为了
        防止以后有人"顺手"改成 True。
        """
        return False

    def drawRect_(self, rect):
        try:
            self._orb._draw_into(self)
        except Exception:                          # noqa: BLE001
            traceback.print_exc()

    def mouseDown_(self, ev):
        self._orb._on_down(ev)

    def mouseDragged_(self, ev):
        self._orb._on_drag(ev)

    def mouseUp_(self, ev):
        self._orb._on_up(ev)

    def mouseEntered_(self, ev):
        self._orb._set_hover(True)

    def mouseExited_(self, ev):
        self._orb._set_hover(False)

    def rightMouseUp_(self, ev):
        self._orb._menu(ev)


class _Helper(NSObject):
    """NSTimer 和 NSMenuItem 都要一个 ObjC 的 target + selector。

    Python 的普通函数不是 selector,所以要这么一个 NSObject 壳。菜单项的
    动作也挂在这里 —— 比"弹完菜单再去猜选了哪一项"可靠得多。
    """

    def initWithOrb_(self, orb):
        self = objc.super(_Helper, self).init()
        if self is not None:
            self._orb = orb
        return self

    def tick_(self, timer):
        try:
            self._orb._tick()
        except Exception:                          # noqa: BLE001
            traceback.print_exc()

    def seq_(self, timer):
        try:
            self._orb._seq_step()
        except Exception:                          # noqa: BLE001
            traceback.print_exc()

    def expand_(self, sender):
        self._orb._fire(self._orb.on_expand)

    def quit_(self, sender):
        self._orb._fire(self._orb.on_quit)


class OrbWindow:
    """悬浮球。对外的方法可以从任意线程调用(内部转主线程)。

    接口和 orb_win32.OrbWindow 完全一致 —— app.py / server.py 不分平台。
    """

    def __init__(self, on_click=None, on_expand=None, on_quit=None,
                 on_moved=None, on_hold=None, on_unhover=None,
                 diameter: int = 40, scale: float = 1.0,
                 dark: bool = False, animate: bool = True):
        self.on_click = on_click
        self.on_expand = on_expand
        self.on_quit = on_quit
        self.on_moved = on_moved
        self.on_hold = on_hold
        self.on_unhover = on_unhover

        self.logical_d = diameter
        self.scale = scale                  # 位图超采样倍率(Retina 上 2.0)
        self.dark = dark
        self.animate = animate
        self.count = 0

        self.hwnd = 0
        self.ready = threading.Event()
        self._win = None
        self._view = None
        self._helper = None
        self._timer = None
        self._seq_timer = None

        self._frames: list[Frame] = []
        self._glow: list[Frame] = []
        self._hot: list[Frame] = []
        self._phase = 0.0
        self._hover = False
        self._hlevel = 0.0
        self._fade_alpha: int | None = None
        self._pos: tuple[int, int] | None = None
        self._drag = None
        self._held = 0.0
        self._fired_hold = False
        self._interval = 0.0
        self._forced: Frame | None = None   # 帧序列播放期间强制画这一张
        self._seq: list[Frame] = []
        self._seq_done = None
        self._lock = threading.Lock()

        native_cocoa._main(self._build)
        self.ready.wait(5)

    # ─────────── 尺寸 ───────────
    #
    # 注意和 Windows 那份的区别:那边这几个返回物理像素,这里返回**点**。
    # 位图边长 = 这些值 * self.scale,见 _px()。

    @property
    def diameter(self) -> int:
        return max(16, int(round(self.logical_d)))

    @property
    def pad(self) -> int:
        # 0.36:阴影要有地方衰减完。留窄了的话到位图边界还剩几个百分点的
        # 不透明度,被硬截断 —— 四条边各留一道直线,看起来就是个方块
        return max(8, int(round(self.logical_d * 0.36)))

    @property
    def size(self) -> int:
        return self.diameter + self.pad * 2

    def _px(self) -> tuple[int, int, int]:
        """位图那一套尺寸:(直径, 留白, 边长),单位是像素。"""
        d = max(16, int(round(self.logical_d * self.scale)))
        p = max(8, int(round(self.logical_d * 0.36 * self.scale)))
        return d, p, canvas_size(d, p)

    def ball_rect(self, x: int | None = None, y: int | None = None):
        """球本体(不含四周那圈投影留白)在屏幕上的矩形:(x, y, 直径),点。

        交接的时候 WebView 窗口要收到**这个**矩形,不是整张画布 ——
        画布外圈是透明的投影,对不上就会看到球"跳"一下位置。
        """
        if x is None:
            x, y = self._pos or (0, 0)
        return (x + self.pad, y + self.pad, self.diameter)

    # ─────────── 建窗口(主线程) ───────────

    def _build(self) -> None:
        try:
            n = self.size
            self._win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, n, n), NSWindowStyleMaskBorderless,
                NSBackingStoreBuffered, False)
            w = self._win
            w.setOpaque_(False)
            w.setBackgroundColor_(NSColor.clearColor())
            # 投影是自己画的(画布外圈那一圈),系统再加一层就重了
            w.setHasShadow_(False)
            # 浮动层:盖在普通窗口之上,但**不**盖住输入法候选框和系统通知。
            # 用更高的层(状态栏层)会挡住那些东西,很招人烦。
            w.setLevel_(NSFloatingWindowLevel)
            # 跟着所有 Space、也能浮在别的应用全屏之上,而且不随 Space 滑动
            w.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorStationary
                | NSWindowCollectionBehaviorFullScreenAuxiliary)
            w.setIgnoresMouseEvents_(False)
            # 关掉之后不要被释放 —— 这个球是整个进程期间复用的
            w.setReleasedWhenClosed_(False)

            self._view = OrbView.alloc().initWithOrb_frame_(
                self, NSMakeRect(0, 0, n, n))
            w.setContentView_(self._view)
            self.hwnd = int(w.windowNumber())

            self._helper = _Helper.alloc().initWithOrb_(self)
            self._render()
        except Exception:                          # noqa: BLE001
            traceback.print_exc()
        finally:
            self.ready.set()

    # ─────────── 渲染 ───────────

    def _render(self) -> None:
        """重画所有帧。只在尺寸/主题/角标/动画开关变了的时候走这里。

        角标是烤进位图的圆盘 + 记在帧上的文字请求,所以角标一变也要重画。
        """
        for f in self._frames + self._glow + self._hot:
            f.destroy()
        d, p, n = self._px()
        self._frames = []
        for sc in POP:
            f = render_ball(Frame(n, n), d, p, self.dark, sc)
            if sc == 1.0:
                draw_badge(f, d, p, self.count)
            self._frames.append(f)

        self._glow = []
        for i in range(GLOW_FRAMES if self.animate else 1):
            f = render_ball(Frame(n, n), d, p, self.dark, 1.0,
                            phase=i / GLOW_FRAMES)
            draw_badge(f, d, p, self.count)
            self._glow.append(f)

        self._hot = []
        for i in range(HOVER_FRAMES if self.animate else 1):
            f = render_ball(Frame(n, n), d, p, self.dark, HOVER_SCALE,
                            phase=i / HOVER_FRAMES, bright=HOVER_BRIGHT)
            draw_badge(f, d, p, self.count)
            self._hot.append(f)
        self._phase = 0.0

    def _alpha(self) -> float:
        """整体不透明度也跟着 hover 电平走,不然提亮会在过渡里跳一下。"""
        with self._lock:
            fade = self._fade_alpha
        if fade is not None:
            return max(0, min(255, fade)) / 255.0
        return (IDLE_ALPHA + (HOVER_ALPHA - IDLE_ALPHA) * self._hlevel) / 255.0

    def _pick(self, fs: list[Frame]) -> Frame:
        """按当前相位在一套帧里取一张。两套帧长度不同,所以相位存的是 0~1。"""
        if not fs:
            return self._frames[-1]
        return fs[int(self._phase * len(fs)) % len(fs)]

    def _draw_into(self, view) -> None:
        """把当前这一帧画进视图。

        hover 过渡期间**叠两张图**:静止帧画满,hover 帧按电平 t 叠上去,
        由 Core Graphics 合成 —— 比自己混像素快得多(见模块文档)。
        """
        if not self._frames:
            return
        gctx = NSGraphicsContext.currentContext()
        if gctx is None:
            return
        cg = gctx.CGContext()
        b = view.bounds()
        base_a = self._alpha()
        Quartz.CGContextSetAlpha(cg, base_a)

        if self._forced is not None:               # 弹出/收起序列里的某一帧
            Quartz.CGContextDrawImage(cg, b, self._forced.cgimage())
            self._draw_badge_text(view, self._forced)
            return

        if not self._glow or not self._hot:
            f = self._frames[-1]
            Quartz.CGContextDrawImage(cg, b, f.cgimage())
            self._draw_badge_text(view, f)
            return

        t = self._hlevel * self._hlevel * (3 - 2 * self._hlevel)   # smoothstep
        if t <= 0.02:
            f = self._pick(self._glow)
            Quartz.CGContextDrawImage(cg, b, f.cgimage())
        elif t >= 0.98:
            f = self._pick(self._hot)
            Quartz.CGContextDrawImage(cg, b, f.cgimage())
        else:
            idle = self._pick(self._glow)
            f = self._pick(self._hot)
            Quartz.CGContextDrawImage(cg, b, idle.cgimage())
            Quartz.CGContextSaveGState(cg)
            Quartz.CGContextSetAlpha(cg, base_a * t)
            Quartz.CGContextDrawImage(cg, b, f.cgimage())
            Quartz.CGContextRestoreGState(cg)
        self._draw_badge_text(view, f)

    def _draw_badge_text(self, view, frame: Frame) -> None:
        """把角标里的数字叠上去。

        圆盘已经在位图里了(纯 Python 填的),这里只补文字。要换两次坐标:
        位图是 y 向下、单位像素;视图是 y 向上、单位点。
        """
        if not frame.text_ops:
            return
        h_pt = float(view.bounds().size.height)
        for text, rect, px, bold, color in frame.text_ops:
            x0, y0, x1, y1 = rect
            fs = px / max(self.scale, 0.01)
            font = (NSFont.boldSystemFontOfSize_(fs) if bold
                    else NSFont.systemFontOfSize_(fs))
            attrs = NSMutableDictionary.dictionary()
            attrs.setObject_forKey_(font, NSFontAttributeName)
            attrs.setObject_forKey_(_ns_color(color),
                                    NSForegroundColorAttributeName)
            s = NSString.stringWithString_(text)
            sz = s.sizeWithAttributes_(attrs)
            cx = (x0 + x1) / 2.0 / self.scale          # 圆盘中心(点)
            cy_top = (y0 + y1) / 2.0 / self.scale
            s.drawAtPoint_withAttributes_(
                NSMakePoint(cx - float(sz.width) / 2.0,
                            h_pt - cy_top - float(sz.height) / 2.0),
                attrs)

    def _refresh(self) -> None:
        if self._view is not None:
            self._view.setNeedsDisplay_(True)

    def _fire(self, cb) -> None:
        """回调丢到后台线程去跑。

        回调里通常要调 server.apply_mode(),那会去动窗口、发 HTTP ——
        在主线程上同步跑会把 run loop 堵住,而且可能和 _main 互等。
        """
        if cb is not None:
            threading.Thread(target=cb, daemon=True).start()

    # ─────────── 对外接口(任意线程) ───────────

    def show(self, x: int | None = None, y: int | None = None,
             animate: bool = True, alpha: int | None = None) -> None:
        """alpha 给了就用它当初始整体 alpha 并跳过弹出动画(交叉淡化的起点)。"""
        def go():
            if self._win is None:
                return
            px, py = self._clamp(*self._resolve(x, y))
            self._pos = (px, py)
            n = self.size
            self._win.setFrame_display_(
                native_cocoa._to_cocoa(px, py, n, n), False)
            with self._lock:
                self._fade_alpha = alpha
            self._win.orderFrontRegardless()
            if animate and alpha is None and self.animate:
                # 弹出:缩放序列过冲一下再落回,看着有"啵"的一下的弹性
                self._play(list(self._frames))
            else:
                self._refresh()
                self._start_timer()
        native_cocoa._main(go)

    def hide_now(self) -> None:
        """立刻藏掉,不放收起动画(交叉淡化已经把它淡走了)。"""
        def go():
            self._stop_seq()
            self._stop_timer()
            self._hover = False
            self._hlevel = 0.0
            if self._win is not None:
                self._win.orderOut_(None)
        native_cocoa._main(go)

    def hide(self) -> None:
        def go():
            if self._win is None:
                return
            if self.animate:
                # 倒着播缩放序列,播完再真的藏起来
                self._play(list(reversed(self._frames)), then=self._order_out)
            else:
                self._order_out()
        native_cocoa._main(go)

    def _order_out(self) -> None:
        self._stop_timer()
        if self._win is not None:
            self._win.orderOut_(None)

    def set_fade(self, alpha: int) -> None:
        """按给定的整体 alpha 重画(0~255),用于和主窗口交叉淡化。

        期间不让光晕的定时器插手 —— 两个动画同时改同一个窗口会打架。
        """
        with self._lock:
            self._fade_alpha = max(0, min(255, int(alpha)))
        def go():
            self._stop_timer()
            self._refresh()
        native_cocoa._main(go)

    def settle(self) -> None:
        """淡化结束:清掉 fade 状态,恢复正常的 alpha 和光晕定时器。"""
        with self._lock:
            self._fade_alpha = None
        def go():
            self._refresh()
            self._start_timer()
        native_cocoa._main(go)

    def set_badge(self, count: int) -> None:
        if count == self.count:
            return
        self.count = count
        def go():
            self._render()
            self._refresh()
        native_cocoa._main(go)

    def set_look(self, diameter: int | None = None, dark: bool | None = None,
                 scale: float | None = None, animate: bool | None = None) -> None:
        changed = False
        if diameter and diameter != self.logical_d:
            self.logical_d, changed = diameter, True
        if dark is not None and dark != self.dark:
            self.dark, changed = dark, True
        if scale and abs(scale - self.scale) > 0.01:
            self.scale, changed = scale, True
        if animate is not None and animate != self.animate:
            self.animate, changed = animate, True
        if not changed:
            return

        def go():
            self._render()
            if self._win is not None and self._pos is not None:
                n = self.size
                px, py = self._clamp(*self._pos)
                self._pos = (px, py)
                self._win.setFrame_display_(
                    native_cocoa._to_cocoa(px, py, n, n), False)
            self._refresh()
            self._start_timer()
        native_cocoa._main(go)

    def position(self) -> tuple[int, int] | None:
        return self._pos

    def visible(self) -> bool:
        if self._win is None:
            return False
        return bool(native_cocoa._main(lambda: bool(self._win.isVisible())))

    # ─────────── 位置 ───────────

    def _resolve(self, x, y) -> tuple[int, int]:
        """没给坐标就用上次的位置,再没有就贴可用区域的右下角。"""
        if x is not None and y is not None:
            return int(x), int(y)
        if self._pos:
            return self._pos
        wx, wy, ww, wh = self._work_area()
        n = self.size
        return (wx + ww - n - EDGE_MARGIN, wy + wh - n - EDGE_MARGIN)

    def _work_area(self) -> tuple[int, int, int, int]:
        """球所在那块屏的可用区域,y 向下(已排除菜单栏和 Dock)。"""
        scr = self._win.screen() if self._win is not None else None
        scr = scr or NSScreen.mainScreen()
        if scr is None:
            return (0, 0, 0, 0)
        return native_cocoa._from_cocoa(scr.visibleFrame())

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        """把球夹在可用区域里。

        不夹的话很容易把球拖到 Dock 底下、或者收起来时正好落在屏幕外 ——
        那时候它既看不见也点不着,只能去改 prefs.json 才能救回来。
        夹的是**球本体**,不是画布:画布四周那圈是投影用的透明留白。
        """
        wx, wy, ww, wh = self._work_area()
        if ww <= 0:
            return int(x), int(y)
        p, d = self.pad, self.diameter
        bx = max(wx + EDGE_MARGIN, min(x + p, wx + ww - d - EDGE_MARGIN))
        by = max(wy + EDGE_MARGIN, min(y + p, wy + wh - d - EDGE_MARGIN))
        return int(bx - p), int(by - p)

    # ─────────── 帧序列(弹出/收起) ───────────
    #
    # **不能 sleep** —— 这些代码都在主线程上,睡一下整个界面就卡一下。
    # 所以排进队列,由一个 POP_MS 周期的定时器一帧一帧取。

    def _play(self, frames: list[Frame], then=None) -> None:
        self._stop_seq()
        self._stop_timer()
        self._seq = frames
        self._seq_done = then
        self._seq_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            POP_MS, self._helper, b"seq:", None, True)
        self._seq_step()                  # 第一帧立刻出来,不等一个周期

    def _seq_step(self) -> None:
        if not self._seq:
            self._stop_seq()
            self._forced = None
            done, self._seq_done = self._seq_done, None
            if done is not None:
                done()
            else:
                self._refresh()
                self._start_timer()
            return
        self._forced = self._seq.pop(0)
        self._refresh()

    def _stop_seq(self) -> None:
        if self._seq_timer is not None:
            self._seq_timer.invalidate()
            self._seq_timer = None
        self._seq = []
        self._forced = None

    # ─────────── 光晕定时器 ───────────

    def _start_timer(self) -> None:
        """按当前状态排一个刷新定时器。

        动画关掉了就一个定时器都不留 —— 设置里那个开关要真的能省电。
        """
        self._stop_timer()
        if not self.animate or self._win is None:
            return
        with self._lock:
            if self._fade_alpha is not None:
                return                # 交叉淡化期间不让光晕插手
        ms = TWEEN_MS if 0.0 < self._hlevel < 1.0 else (
            GLOW_MS_HOVER if self._hover else GLOW_MS)
        self._interval = ms / 1000.0
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            self._interval, self._helper, b"tick:", None, True)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        self._interval = 0.0

    def _tick(self) -> None:
        """推进光晕相位、缓动 hover 电平、数"在球上停了多久"。"""
        step_dt = self._interval or (TWEEN_MS / 1000.0)
        target = 1.0 if self._hover else 0.0
        if abs(self._hlevel - target) > 0.001:
            step = step_dt / HOVER_TWEEN
            self._hlevel = (min(target, self._hlevel + step) if self._hlevel < target
                            else max(target, self._hlevel - step))
            self._start_timer()          # 过渡期间要更密的节拍
        period = GLOW_PERIOD_HOVER if self._hover else GLOW_PERIOD
        self._phase = (self._phase + step_dt / period) % 1.0
        # 停满一秒 -> 弹备忘录窄栏(只触发一次,直到离开再进来)
        if self._hover and not self._fired_hold and self._held:
            if time.time() - self._held >= HOLD_SECONDS:
                self._fired_hold = True
                self._fire(self.on_hold)
        self._refresh()

    # ─────────── 鼠标(都在主线程上) ───────────

    def _set_hover(self, on: bool) -> None:
        if on == self._hover:
            return
        self._hover = on
        if on:
            self._held = time.time()
            self._fired_hold = False
        else:
            self._held = 0.0
            self._fired_hold = False
            # 收起来由那边的看门线程判断 —— 指针可能正是"移到浮窗上"才离开球的
            self._fire(self.on_unhover)
        self._start_timer()
        self._refresh()

    def _on_down(self, ev) -> None:
        cx, cy = native_cocoa.cursor_pos()
        x, y = self._pos or (0, 0)
        self._drag = {"cx": cx, "cy": cy, "x": x, "y": y, "moved": False,
                      "double": int(ev.clickCount()) >= 2}

    def _on_drag(self, ev) -> None:
        if not self._drag:
            return
        cx, cy = native_cocoa.cursor_pos()
        dx, dy = cx - self._drag["cx"], cy - self._drag["cy"]
        if not self._drag["moved"] and (abs(dx) + abs(dy)) < DRAG_SLOP:
            return                       # 抖动不算拖 —— 否则点一下就被当成移动
        self._drag["moved"] = True
        px, py = self._clamp(self._drag["x"] + dx, self._drag["y"] + dy)
        self._pos = (px, py)
        n = self.size
        if self._win is not None:
            self._win.setFrame_display_(
                native_cocoa._to_cocoa(px, py, n, n), False)

    def _on_up(self, ev) -> None:
        d, self._drag = self._drag, None
        if d is None:
            return
        if d["moved"]:
            # 拖完记住位置,下次从这儿弹出来
            if self.on_moved and self._pos:
                px, py = self._pos
                threading.Thread(target=self.on_moved, args=(px, py),
                                 daemon=True).start()
            return
        # 双击 = 直接展开完整面板;单击 = 还原成收起来之前那个形态
        self._fire(self.on_expand if d["double"] else self.on_click)

    def _menu(self, ev) -> None:
        """右键菜单:展开 / 退出。

        动作挂在 `_Helper` 的 selector 上 —— 那是 AppKit 的正道。试过"弹完
        再去读 highlightedItem 猜用户选了哪一项",那是不可靠的。
        """
        menu = NSMenu.alloc().init()
        if self.on_expand is not None:
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "展开面板", b"expand:", "")
            it.setTarget_(self._helper)
            menu.addItem_(it)
        if self.on_quit is not None:
            menu.addItem_(NSMenuItem.separatorItem())
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "退出", b"quit:", "")
            it.setTarget_(self._helper)
            menu.addItem_(it)
        if menu.numberOfItems():
            NSMenu.popUpContextMenu_withEvent_forView_(menu, ev, self._view)
