# -*- coding: utf-8 -*-
"""右下角那个横条小弹窗 —— **Windows 宿主**。

不要直接 import 这个模块,import `toast`(它按平台挑实现)。卡片底和外阴影
在 toast_render.py、两个平台共用;这里负责量文字、写文字,以及把位图交给
`UpdateLayeredWindow` 去合成。

原说明:Canvas 或邮箱有更新时报一句。

**为什么是自绘的原生窗口。** 它要在应用收成悬浮球、甚至窗口完全隐藏的时候
照样弹出来,所以不能是主窗口里的一个 div。Windows 10 的系统通知要 WinRT
(不在标准库里),而这个项目的硬约束是零第三方依赖 —— 所以走和悬浮球同一条路:
`WS_EX_LAYERED` + `UpdateLayeredWindow`,GDI 画圆角底 + 文字。

和悬浮球共用 `orb_window` 里那套 DIB/AlphaBlend 基建的思路,但简单得多:
没有动画帧,只有"淡入 → 停留 → 淡出"。淡入淡出用 `SetLayeredWindowAttributes`
整窗 alpha,不用逐帧重画 —— 文字那么多像素,逐帧混合是白费力气。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
import traceback
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
msimg32 = ctypes.windll.msimg32

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080          # 不进 Alt+Tab、不进任务栏
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000          # 弹出来不抢焦点 —— 正在打字就不能被打断

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
SW_HIDE = 0
LWA_ALPHA = 0x00000002
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
SPI_GETWORKAREA = 0x0030

WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_TIMER = 0x0113
WM_CLOSE = 0x0010

DT_WORDBREAK = 0x0010
DT_NOPREFIX = 0x0800
DT_END_ELLIPSIS = 0x8000
DT_CALCRECT = 0x0400
DT_SINGLELINE = 0x0020

# 尺寸、配色、卡片底的画法都在 toast_render(两个平台共用)
from toast_render import (
    FADE_MS, SHOW_MS, card_height, colors, metrics, paint_card,
)


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class LOGFONT(ctypes.Structure):
    _fields_ = [("lfHeight", wintypes.LONG), ("lfWidth", wintypes.LONG),
                ("lfEscapement", wintypes.LONG), ("lfOrientation", wintypes.LONG),
                ("lfWeight", wintypes.LONG), ("lfItalic", ctypes.c_byte),
                ("lfUnderline", ctypes.c_byte), ("lfStrikeOut", ctypes.c_byte),
                ("lfCharSet", ctypes.c_byte), ("lfOutPrecision", ctypes.c_byte),
                ("lfClipPrecision", ctypes.c_byte), ("lfQuality", ctypes.c_byte),
                ("lfPitchAndFamily", ctypes.c_byte),
                ("lfFaceName", ctypes.c_wchar * 32)]


# wparam/lparam **必须**声明成 WPARAM/LPARAM(整数),不能用 c_void_p。
# 用 c_void_p 的话值为 0 时 ctypes 会把它转成 None,再传给 DefWindowProcW
# 就炸 TypeError。而且 orb_win32 对同一个 user32.DefWindowProcW 声明的是
# 整数版 —— **两个模块共用同一个缓存的 DLL 对象,谁后 import 谁的声明生效**,
# 于是行为取决于 import 顺序(踩过:换个顺序 import,弹窗的消息处理就全炸)。
# 两边统一成整数版,顺序就不再有影响。
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.HDC, ctypes.c_void_p, wintypes.COLORREF,
    ctypes.c_void_p, wintypes.DWORD]
user32.SetLayeredWindowAttributes.argtypes = [
    wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
    ctypes.c_void_p, wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateFontIndirectW.argtypes = [ctypes.c_void_p]
gdi32.CreateFontIndirectW.restype = wintypes.HFONT
user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                             ctypes.c_void_p, wintypes.UINT]
# **共享 API 的结构体指针一律用 c_void_p。** ctypes 的函数对象是按 DLL 缓存的
# 同一个对象:这里把 argtypes 设成 POINTER(本模块的结构体),会把别的模块
# (orb_window / toast 互为例子)设的那份顶掉,另一边运行时直接炸。
# byref() 出来的东西 c_void_p 照样收,而且不绑定具体是哪个类。
# **句柄一律要 argtypes。** 64 位下 HDC/HFONT/HBITMAP 都是 8 字节,
# 不声明的话 ctypes 按 int 传,直接 OverflowError(这个坑这个项目踩过两次了)
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT,
                            ctypes.c_void_p]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                         ctypes.c_void_p, wintypes.UINT]


class Toast:
    """一个常驻的隐藏窗口,来消息就画一次、亮 9 秒、淡出。

    常驻而不是每次新建:创建窗口要注册类、要消息循环,反复来回容易漏资源;
    而且同一时刻只想显示一条。
    """

    def __init__(self, on_click=None, scale: float = 1.0, dark: bool = False):
        self.on_click = on_click
        self.scale = scale
        self.dark = dark
        self.hwnd = 0
        self._dc = None
        self._dib = None
        self._old = None
        self._geo = None
        self._font_body = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._hide_at = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="toast")
        self._thread.start()
        self._ready.wait(5)

    # ── 窗口那一半

    def _run(self) -> None:
        try:
            cls = WNDCLASS()
            cls.lpszClassName = "NEUHelperToast"
            cls.lpfnWndProc = ctypes.cast(WNDPROC(self._proc), ctypes.c_void_p)
            cls.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
            self._keep = cls                  # 别让它被回收
            user32.RegisterClassW(ctypes.byref(cls))
            self.hwnd = user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
                | WS_EX_NOACTIVATE,
                "NEUHelperToast", "NEU Helper", WS_POPUP,
                0, 0, 10, 10, None, None, cls.hInstance, None)
            self._ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self._ready.set()

    def _proc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_LBUTTONUP:
                self.hide()
                if self.on_click:
                    self.on_click()
                return 0
            if msg == WM_TIMER:
                if time.time() >= self._hide_at:
                    self.hide()
                return 0
            if msg in (WM_CLOSE, WM_DESTROY):
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            traceback.print_exc(file=sys.stderr)
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    # ── 画那一半

    def _font(self, px: int, bold: bool) -> wintypes.HFONT:
        lf = LOGFONT()
        lf.lfHeight = -int(px * self.scale)
        lf.lfWeight = 700 if bold else 400
        lf.lfCharSet = 1                       # DEFAULT_CHARSET,中文要它
        lf.lfQuality = 5                       # CLEARTYPE_QUALITY
        lf.lfFaceName = "Microsoft YaHei UI"
        return gdi32.CreateFontIndirectW(ctypes.byref(lf))

    def show(self, title: str, body: str, secs: float = SHOW_MS / 1000) -> bool:
        """画一条并弹出来。同一时刻只有一条 —— 新的直接顶掉旧的。"""
        if not self.hwnd:
            return False
        with self._lock:
            try:
                self._paint(title or "NEU Helper", body or "")
            except Exception:
                traceback.print_exc(file=sys.stderr)
                return False
            self._hide_at = time.time() + secs
            user32.SetTimer(self.hwnd, 1, 500, None)
            self._blit(0)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                                | SWP_SHOWWINDOW)
            threading.Thread(target=self._fade, args=(0, 255), daemon=True).start()
        return True

    def hide(self) -> None:
        if not self.hwnd:
            return
        user32.KillTimer(self.hwnd, 1)
        self._fade(255, 0)
        user32.ShowWindow(self.hwnd, SW_HIDE)
        self._free()

    def _fade(self, a0: int, a1: int) -> None:
        steps = 9
        for i in range(1, steps + 1):
            self._blit(int(a0 + (a1 - a0) * i / steps))
            time.sleep(FADE_MS / 1000 / steps)

    def _blit(self, alpha: int) -> None:
        """把画好的位图按给定的整体 alpha 贴上去。

        **不能用 SetLayeredWindowAttributes 做淡化** —— 那会把窗口切到常量
        alpha 模式,UpdateLayeredWindow 画进去的逐像素内容当场作废
        (踩过:窗口"可见"、位置也对,但屏幕上空的)。
        """
        if not self._dib or not self._geo:
            return
        x, y, w, h = self._geo
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, alpha)),
                              AC_SRC_ALPHA)
        screen = user32.GetDC(None)
        user32.UpdateLayeredWindow(
            self.hwnd, screen, ctypes.byref(POINT(x, y)),
            ctypes.byref(SIZE(w, h)), self._dc, ctypes.byref(POINT(0, 0)), 0,
            ctypes.byref(blend), ULW_ALPHA)
        user32.ReleaseDC(None, screen)

    def _free(self) -> None:
        """位图和 DC 是跨调用留着的(淡化每一步都要重贴),收起来时才释放。"""
        if getattr(self, "_dc", None):
            if getattr(self, "_old", None):
                gdi32.SelectObject(self._dc, self._old)
            if getattr(self, "_dib", None):
                gdi32.DeleteObject(self._dib)
            gdi32.DeleteDC(self._dc)
        self._dc = None
        self._dib = None
        self._old = None
        self._geo = None

    def _paint(self, title: str, body: str) -> None:
        self._free()
        s = self.scale
        m = metrics(s)
        pad, mar, w = m["pad"], m["mar"], m["w"]

        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        f_title = self._font(12, True)
        f_body = self._font(11.5, False)

        # 先量正文要多高,再按这个高度建位图 —— 横条的高度跟着内容走。
        # **量文字这一步必须是平台自己做的**:断行规则、字体度量、省略号,
        # 共用渲染层不可能算对,所以分工是"宿主量高度、渲染层画底"。
        gdi32.SelectObject(mem, f_body)
        rc = RECT(0, 0, w - mar * 2 - pad * 2, 0)
        user32.DrawTextW(mem, body, -1, ctypes.byref(rc),
                         DT_WORDBREAK | DT_NOPREFIX | DT_CALCRECT)
        body_h = min(rc.bottom, m["body_max"])
        h = card_height(body_h, s) + mar * 2

        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(bi)
        bi.biWidth = w
        bi.biHeight = -h                      # 负 = 自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(mem, ctypes.byref(bi), 0,
                                     ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(mem, dib)

        # ── 底:圆角卡片 + 外阴影。逐像素算,因为 GDI 的画笔不带 alpha
        buf = (ctypes.c_char * (w * h * 4)).from_address(bits.value)
        _base, tcol, bcol, _sh = colors(self.dark)
        x0, y0 = mar, mar
        x1, y1 = w - mar, h - mar
        # 卡片底 + 外阴影:纯 Python 逐像素,两个平台同一份代码
        mask = paint_card(buf, w, h, s, self.dark)

        # ── 文字
        gdi32.SetBkMode(mem, 1)                # TRANSPARENT
        gdi32.SelectObject(mem, f_title)
        gdi32.SetTextColor(mem, (tcol[2] << 16) | (tcol[1] << 8) | tcol[0])
        rc = RECT(x0 + pad + m["text_x"], y0 + pad, x1 - pad, y0 + pad + m["title_h"] + m["gap"] // 2)
        user32.DrawTextW(mem, title, -1, ctypes.byref(rc),
                         DT_SINGLELINE | DT_NOPREFIX | DT_END_ELLIPSIS)
        gdi32.SelectObject(mem, f_body)
        gdi32.SetTextColor(mem, (bcol[2] << 16) | (bcol[1] << 8) | bcol[0])
        rc = RECT(x0 + pad + m["text_x"], y0 + pad + m["title_h"] + m["gap"] // 2,
                  x1 - pad, y1 - pad)
        user32.DrawTextW(mem, body, -1, ctypes.byref(rc),
                         DT_WORDBREAK | DT_NOPREFIX | DT_END_ELLIPSIS)

        # ── **把 alpha 补回来。** GDI 画字只写 RGB,而且会把字形像素的 alpha
        #    清零 —— 不补的话那些像素在合成时是透明的洞,字就"看不见"了。
        #    只补 alpha 满值那些(卡片内部);圆角和阴影上没有字,不用动。
        for y in range(y0, y1):
            base_o = y * w
            for x in range(x0, x1):
                if mask[base_o + x] == 255:
                    buf[(base_o + x) * 4 + 3] = b"\xff"

        # ── 摆到右下角(工作区里,不压任务栏)
        wa = RECT()
        user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(wa), 0)
        x = wa.right - w + mar - m["inset"]
        y = wa.bottom - h + mar - m["inset"]

        # DC / 位图留着 —— 淡化的每一步都要按新的整体 alpha 重贴一次
        self._dc, self._dib, self._old = mem, dib, old
        self._geo = (x, y, w, h)
        gdi32.DeleteObject(f_title)
        self._font_body = f_body
        user32.ReleaseDC(None, screen)
