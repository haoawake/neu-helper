# -*- coding: utf-8 -*-
"""右下角那条信息弹窗 —— **macOS 宿主**。

不要直接 import 这个模块,import `toast`(它按平台挑实现)。卡片底和外阴影
在 toast_render.py、**两个平台共用那一份**;这里负责量文字、写文字,以及
把位图交给 Core Graphics 合成。

分工和 Windows 那边一样:

  这里          用 Core Text 量出正文要几行高
  toast_render  按算出来的高度画卡片底 + 外阴影(纯 Python,预乘 BGRA)
  这里          把文字叠在上面

**文字是叠在视图上的,不往像素缓冲里栅格化** —— 和悬浮球的角标同一个思路。
好处不只是省一块代码:Windows 那边 GDI 会把字形像素的 alpha 清零、必须事后
按遮罩补回来,而这里文字根本没碰那块内存,那个坑天然不存在。
(`paint_card` 返回的遮罩在这边用不上,接着收下是为了两边共用同一个签名。)

**淡入淡出用窗口的 alpha,不重画位图。** 位图那么多像素,逐帧重算是白费力气;
`setAlphaValue_` 由系统合成,而且不会像 Windows 那边一样和逐像素 alpha 打架
(Win32 上 `UpdateLayeredWindow` 和 `SetLayeredWindowAttributes` 是互斥的,
所以那边只能走 `BLENDFUNCTION` 的 SourceConstantAlpha)。
"""
from __future__ import annotations

import threading
import traceback

import objc
import Quartz
from AppKit import (
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSParagraphStyleAttributeName,
    NSMutableParagraphStyle,
    NSScreen,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
)
from Foundation import (
    NSMakeRect,
    NSMakeSize,
    NSMutableDictionary,
    NSObject,
    NSString,
    NSTimer,
)

import native_cocoa
from toast_render import (
    FADE_MS, SHOW_MS, card_height, colors, metrics, paint_card,
)

NSBackingStoreBuffered = 2
NSFloatingWindowLevel = 3
NSLineBreakByWordWrapping = 0
NSLineBreakByTruncatingTail = 4
# 量文字必须带这个,否则多行的高度算不对(它让每一行按 line fragment 排)
NSStringDrawingUsesLineFragmentOrigin = 1 << 0

_CS = Quartz.CGColorSpaceCreateDeviceRGB()
_BITMAP_INFO = (Quartz.kCGImageAlphaPremultipliedFirst
                | Quartz.kCGBitmapByteOrder32Little)

TITLE_PT = 12.0
BODY_PT = 11.5
FADE_STEPS = 9


def _ns_color(rgb, alpha: float = 1.0):
    r, g, b = rgb
    return NSColor.colorWithSRGBRed_green_blue_alpha_(
        r / 255.0, g / 255.0, b / 255.0, alpha)


def _attrs(pt: float, bold: bool, rgb, wrap: bool):
    d = NSMutableDictionary.dictionary()
    d.setObject_forKey_(NSFont.boldSystemFontOfSize_(pt) if bold
                        else NSFont.systemFontOfSize_(pt), NSFontAttributeName)
    d.setObject_forKey_(_ns_color(rgb), NSForegroundColorAttributeName)
    ps = NSMutableParagraphStyle.alloc().init()
    ps.setLineBreakMode_(NSLineBreakByWordWrapping if wrap
                         else NSLineBreakByTruncatingTail)
    d.setObject_forKey_(ps, NSParagraphStyleAttributeName)
    return d


class ToastView(NSView):
    """画卡片 + 写字 + 收点击。"""

    def initWithToast_frame_(self, toast, frame):
        self = objc.super(ToastView, self).initWithFrame_(frame)
        if self is not None:
            self._toast = toast
        return self

    def isOpaque(self):
        return False

    def isFlipped(self):
        """**必须是 False(AppKit 的默认)。**

        Quartz 里 CGImage 的第一行是图像的顶行,在**非翻转**的上下文里
        `CGContextDrawImage` 画出来是正的。如果这里返回 True(翻转坐标系),
        卡片和阴影会上下颠倒 —— 阴影跑到上边去。写出来是为了防止以后
        有人"顺手"改成 True。
        """
        return False

    def drawRect_(self, rect):
        try:
            self._toast._draw_into(self)
        except Exception:                          # noqa: BLE001
            traceback.print_exc()

    def mouseUp_(self, ev):
        self._toast._on_click()


class _Helper(NSObject):
    """NSTimer 要 target + selector,Python 函数不是 selector。"""

    def initWithToast_(self, toast):
        self = objc.super(_Helper, self).init()
        if self is not None:
            self._toast = toast
        return self

    def expire_(self, timer):
        try:
            self._toast.hide()
        except Exception:                          # noqa: BLE001
            traceback.print_exc()

    def fade_(self, timer):
        try:
            self._toast._fade_step()
        except Exception:                          # noqa: BLE001
            traceback.print_exc()


class Toast:
    """一个常驻的隐藏窗口,来消息就画一次、亮 9 秒、淡出。

    常驻而不是每次新建:建窗口要注册视图、挂定时器,反复来回容易漏资源;
    而且同一时刻只想显示一条。接口和 toast_win32.Toast 完全一致。
    """

    def __init__(self, on_click=None, scale: float = 1.0, dark: bool = False):
        self.on_click = on_click
        self.scale = scale           # 位图超采样倍率(Retina 上 2.0)
        self.dark = dark
        self.hwnd = 0

        self._win = None
        self._view = None
        self._helper = None
        self._expire = None
        self._fade_timer = None
        self._fade_from = 0.0
        self._fade_to = 1.0
        self._fade_i = 0
        self._img = None
        self._keep = None
        self._title = ""
        self._body = ""
        self._geo = None             # (x, y, w, h) 点,y 向下
        self._lock = threading.Lock()
        self._ready = threading.Event()

        native_cocoa._main(self._build)
        self._ready.wait(5)

    # ─────────── 建窗口(主线程) ───────────

    def _build(self) -> None:
        try:
            self._win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, 10, 10), NSWindowStyleMaskBorderless,
                NSBackingStoreBuffered, False)
            w = self._win
            w.setOpaque_(False)
            w.setBackgroundColor_(NSColor.clearColor())
            w.setHasShadow_(False)             # 阴影是自己画进位图的
            w.setLevel_(NSFloatingWindowLevel)
            w.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorStationary
                | NSWindowCollectionBehaviorFullScreenAuxiliary)
            w.setReleasedWhenClosed_(False)
            # **弹出来不能抢焦点** —— 正在打字的时候被打断是最招人烦的
            w.setIgnoresMouseEvents_(False)
            self._view = ToastView.alloc().initWithToast_frame_(
                self, NSMakeRect(0, 0, 10, 10))
            w.setContentView_(self._view)
            self.hwnd = int(w.windowNumber())
            self._helper = _Helper.alloc().initWithToast_(self)
        except Exception:                          # noqa: BLE001
            traceback.print_exc()
        finally:
            self._ready.set()

    # ─────────── 对外接口 ───────────

    def show(self, title: str, body: str, secs: float = SHOW_MS / 1000) -> bool:
        """画一条并弹出来。同一时刻只有一条 —— 新的直接顶掉旧的。"""
        if not self.hwnd:
            return False

        def go():
            try:
                self._paint(title or "NEU Helper", body or "")
            except Exception:                      # noqa: BLE001
                traceback.print_exc()
                return False
            x, y, w, h = self._geo
            self._win.setFrame_display_(native_cocoa._to_cocoa(x, y, w, h), False)
            self._win.setAlphaValue_(0.0)
            self._win.orderFrontRegardless()
            self._view.setNeedsDisplay_(True)
            self._arm_expire(secs)
            self._start_fade(0.0, 1.0)
            return True
        return bool(native_cocoa._main(go))

    def hide(self) -> None:
        def go():
            self._cancel_expire()
            self._start_fade(
                float(self._win.alphaValue()) if self._win is not None else 1.0,
                0.0)
        native_cocoa._main(go)

    # ─────────── 画 ───────────

    def _paint(self, title: str, body: str) -> None:
        """量文字 -> 让共用渲染层画卡片底 -> 存下位图和几何。"""
        s = self.scale
        m = metrics(s)
        pad, mar, w = m["pad"], m["mar"], m["w"]

        self._title, self._body = title, body
        self._ta = _attrs(TITLE_PT, True, colors(self.dark)[1], wrap=False)
        self._ba = _attrs(BODY_PT, False, colors(self.dark)[2], wrap=True)

        # 量正文要多高。**这一步必须平台自己做** —— 断行规则、字体度量、
        # 省略号,共用渲染层算不对。量出来的是点,乘 scale 变成位图像素。
        avail_pt = (w - mar * 2 - pad * 2) / s
        box = NSString.stringWithString_(body).boundingRectWithSize_options_attributes_(
            NSMakeSize(avail_pt, 1.0e6), NSStringDrawingUsesLineFragmentOrigin,
            self._ba)
        body_h = min(int(round(float(box.size.height) * s)), m["body_max"])
        h = card_height(body_h, s) + mar * 2

        buf = bytearray(w * h * 4)
        paint_card(buf, w, h, s, self.dark)        # 返回的遮罩这边用不上
        self._keep = bytes(buf)
        provider = Quartz.CGDataProviderCreateWithCFData(self._keep)
        self._img = Quartz.CGImageCreate(
            w, h, 8, 32, w * 4, _CS, _BITMAP_INFO, provider, None, False,
            Quartz.kCGRenderingIntentDefault)

        # 摆到右下角的可用区域里(不压 Dock、不压菜单栏)
        scr = NSScreen.mainScreen()
        wx, wy, ww, wh = (native_cocoa._from_cocoa(scr.visibleFrame())
                          if scr is not None else (0, 0, w, h))
        # 位图是像素,窗口是点 —— 这里换一次
        w_pt, h_pt = int(round(w / s)), int(round(h / s))
        mar_pt, inset_pt = int(round(mar / s)), int(round(m["inset"] / s))
        x = wx + ww - w_pt + mar_pt - inset_pt
        y = wy + wh - h_pt + mar_pt - inset_pt
        self._geo = (x, y, w_pt, h_pt)
        self._view.setFrame_(NSMakeRect(0, 0, w_pt, h_pt))

    def _draw_into(self, view) -> None:
        if self._img is None:
            return
        gctx = NSGraphicsContext.currentContext()
        if gctx is None:
            return
        cg = gctx.CGContext()
        b = view.bounds()
        Quartz.CGContextDrawImage(cg, b, self._img)

        # 文字。位图里的尺寸是像素,视图是点,所以这里全部除掉 scale;
        # 而且视图 y 向上、排版坐标 y 向下,矩形要翻一次。
        s = self.scale
        m = metrics(s)
        pad, mar = m["pad"] / s, m["mar"] / s
        tx = m["text_x"] / s
        h_pt = float(b.size.height)
        w_pt = float(b.size.width)
        inner_w = w_pt - mar * 2 - pad * 2 - tx

        title_top = mar + pad
        title_h = m["title_h"] / s
        NSString.stringWithString_(self._title).drawInRect_withAttributes_(
            NSMakeRect(mar + pad + tx, h_pt - title_top - title_h,
                       inner_w, title_h),
            self._ta)

        body_top = title_top + title_h + m["gap"] / s / 2
        body_h = h_pt - mar - pad - body_top
        if body_h > 0:
            NSString.stringWithString_(self._body).drawInRect_withAttributes_(
                NSMakeRect(mar + pad + tx, h_pt - body_top - body_h,
                           inner_w, body_h),
                self._ba)

    # ─────────── 淡入淡出 / 停留 ───────────

    def _start_fade(self, a0: float, a1: float) -> None:
        self._stop_fade()
        self._fade_from, self._fade_to, self._fade_i = a0, a1, 0
        self._fade_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            FADE_MS / 1000.0 / FADE_STEPS, self._helper, b"fade:", None, True)

    def _fade_step(self) -> None:
        self._fade_i += 1
        t = min(1.0, self._fade_i / FADE_STEPS)
        if self._win is not None:
            self._win.setAlphaValue_(
                self._fade_from + (self._fade_to - self._fade_from) * t)
        if t >= 1.0:
            self._stop_fade()
            # 淡到 0 了就真的收起来 —— 留着一个 alpha=0 的窗口会挡鼠标
            if self._fade_to <= 0.0 and self._win is not None:
                self._win.orderOut_(None)
                self._free()

    def _stop_fade(self) -> None:
        if self._fade_timer is not None:
            self._fade_timer.invalidate()
            self._fade_timer = None

    def _arm_expire(self, secs: float) -> None:
        self._cancel_expire()
        self._expire = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            max(1.0, float(secs)), self._helper, b"expire:", None, False)

    def _cancel_expire(self) -> None:
        if self._expire is not None:
            self._expire.invalidate()
            self._expire = None

    def _free(self) -> None:
        """位图跨调用留着(淡化每一步都要重画),收起来时才丢。"""
        self._img = None
        self._keep = None

    def _on_click(self) -> None:
        self.hide()
        if self.on_click:
            # 回调里会去动窗口、发 HTTP —— 在主线程上同步跑会堵住 run loop
            threading.Thread(target=self.on_click, daemon=True).start()
