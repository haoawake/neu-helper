# -*- coding: utf-8 -*-
"""悬浮球 —— **Windows 宿主(分层窗口 + GDI 合成)**。

不要直接 import 这个模块,import `orb_window`(它按平台挑实现)。
视觉在 orb_render.py,两个平台共用;这里只负责"怎么把位图交给系统合成"。

球本身是一个自己画的分层窗口(WS_EX_LAYERED + UpdateLayeredWindow)。

**为什么不让网页去画这个球。** 实测 SetWindowRgn 裁不住 WebView2:窗口区域
只约束 GDI 绘制,而 WebView2 的画面是 DirectComposition 的视觉层,由 DWM 直接
合成,不受窗口区域影响。所以把 WebView 窗口缩到 78x78 再套一个椭圆区域,
屏幕上仍然是一个白色圆角方块(截图实测:方块里装着一个圆球)——
也就是「悬浮球外面还有矩阵白边」。

换成完全自己画:32 位 BGRA 位图 + UpdateLayeredWindow,逐像素 alpha。
球是真的圆、边缘抗锯齿、还带一层柔和投影。而且这个窗口跟 WebView 无关,
收成球的时候整个 WebView 窗口直接隐藏 —— 渲染进程也跟着歇了,更省电。

拖动也顺带解决了:窗口是我们自己的,SetCapture 就在自己线程里,
不像 WebView 那边捕获被 Chromium 的子窗口占着(见 native_window.start_drag)。
"""
from __future__ import annotations

import ctypes
import math
import sys
import threading
import time
import traceback
from ctypes import wintypes

# 视觉两个平台共用 —— 这里只是宿主
from orb_render import (
    DARK, LIGHT, TAU, _clamp, badge_geometry, canvas_size, draw_badge, render_ball,
)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
msimg32 = ctypes.windll.msimg32

TAU = math.pi * 2

# ─────────────────────────── Win32 常量 ───────────────────────────
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080        # 不出现在任务栏和 Alt+Tab 里
WS_EX_NOACTIVATE = 0x08000000        # 点球不抢焦点

SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
ULW_ALPHA = 0x00000002
AC_SRC_OVER, AC_SRC_ALPHA = 0x00, 0x01

WM_DESTROY = 0x0002
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_MOUSEMOVE = 0x0200
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_ORB_CMD = WM_APP + 7              # 别的线程让球做事,统一走这条消息

CS_DBLCLKS = 0x0008
IDC_HAND = 32649
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800

SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
HWND_TOPMOST = -1
SPI_GETWORKAREA = 0x0030
EDGE_MARGIN = 6                      # 球和屏幕边缘之间留一点缝

DRAG_SLOP = 3                        # 位移小于这个算点击,不算拖动
CLICK_MS = 700


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.HDC, ctypes.c_void_p, wintypes.COLORREF,
    ctypes.c_void_p, wintypes.DWORD,
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND] + [ctypes.c_int] * 4 + [wintypes.UINT]
# WS_POPUP 是 0x80000000,按默认的 C int 传会溢出报错,必须显式声明成 DWORD
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                             ctypes.c_void_p, wintypes.UINT]
user32.SetCapture.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                  ctypes.c_void_p]
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_void_p,
                               wintypes.LPCWSTR]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                         ctypes.c_void_p, wintypes.UINT]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT,
                            ctypes.c_void_p]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_void_p]

# GDI 这几个的句柄在 64 位下都是指针宽度。不声明的话 ctypes 按 C int 传,
# 句柄一旦超过 2^31 就 OverflowError —— 实测 SelectObject 第一个就炸。
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HANDLE
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.CreateSolidBrush.restype = wintypes.HANDLE
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
gdi32.CreatePen.restype = wintypes.HANDLE
gdi32.Ellipse.argtypes = [wintypes.HDC] + [ctypes.c_int] * 4
gdi32.CreateFontW.argtypes = [ctypes.c_int] * 13 + [wintypes.LPCWSTR]
gdi32.CreateFontW.restype = wintypes.HANDLE
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
gdi32.BitBlt.argtypes = [wintypes.HDC] + [ctypes.c_int] * 4 + [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
# AlphaBlend 的 BLENDFUNCTION 是按值传的(4 个字节),不是指针
msimg32.AlphaBlend.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    BLENDFUNCTION,
]
msimg32.AlphaBlend.restype = wintypes.BOOL
SRCCOPY = 0x00CC0020


# ─────────────────────────── 画球 ───────────────────────────
#
# 逐像素算,不用 GDI 的图元:GDI 画不出抗锯齿的圆,也做不了软投影。
# 一颗 70px 的球约 1 万个像素,纯 Python 算一遍十几毫秒 —— 只在尺寸/主题/
# 角标变化时重算,平时一帧都不用画。

class Frame:
    """一张预渲染好的位图(自带 DC),可以直接喂给 UpdateLayeredWindow。"""

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.dc = gdi32.CreateCompatibleDC(None)
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(bi)
        bi.biWidth = w
        bi.biHeight = -h                    # 负数 = 自上而下,省得翻转
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0                # BI_RGB
        self.bits = ctypes.c_void_p()
        self.dib = gdi32.CreateDIBSection(
            self.dc, ctypes.byref(bi), 0, ctypes.byref(self.bits), None, 0)
        self.old = gdi32.SelectObject(self.dc, self.dib)
        self.px = (ctypes.c_uint32 * (w * h)).from_address(self.bits.value)

    def draw_text_centered(self, text: str, rect, px: int, bold: bool,
                           color: tuple[int, int, int]) -> None:
        """在 rect 里居中写一行字 —— 渲染层唯一需要宿主提供的原语。

        **GDI 会把字形像素的 alpha 清成 0**(32 位 DIB 上一定会),所以调用方
        写完必须自己把 alpha 补回去。这个坑在弹窗那边也踩过一次。
        """
        r, g, b = color
        font = gdi32.CreateFontW(-px, 0, 0, 0, 700 if bold else 400,
                                 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
        oldf = gdi32.SelectObject(self.dc, font)
        gdi32.SetBkMode(self.dc, 1)                     # TRANSPARENT
        gdi32.SetTextColor(self.dc, (b << 16) | (g << 8) | r)   # COLORREF 是 BGR
        rc = RECT(rect[0], rect[1], rect[2], rect[3])
        # DT_CENTER | DT_VCENTER | DT_SINGLELINE
        user32.DrawTextW(self.dc, text, -1, ctypes.byref(rc),
                         0x0001 | 0x0004 | 0x0020)
        gdi32.SelectObject(self.dc, oldf)
        gdi32.DeleteObject(font)

    def destroy(self) -> None:
        gdi32.SelectObject(self.dc, self.old)
        gdi32.DeleteObject(self.dib)
        gdi32.DeleteDC(self.dc)


# ─────────────────────────── 球窗口 ───────────────────────────
#
# 窗口必须在跑消息循环的那个线程里创建(消息按线程投递)。所以这个类起一条
# 自己的线程,外面的调用(show/hide/角标/换肤)统一 PostMessage 进来,
# 不跨线程碰 GDI。

# 弹出动画的缩放序列:略微过冲一下再落回 1.0,看着有"啵"的一下的弹性
POP = (0.52, 0.74, 0.90, 1.02, 1.05, 1.0)
POP_MS = 0.026

# 光晕流转:24 帧相位预渲染好,靠 WM_TIMER 轮播。
# 逐帧现算是不行的 —— 一帧十几毫秒,还在窗口过程里,会把消息循环拖住。
GLOW_FRAMES = 24
# 110ms ≈ 9fps、2.6 秒转一圈。实测这颗球开着表大约占 2% 一核(每帧一次
# UpdateLayeredWindow,DWM 要重新合成);再快就只是多烧电,慢流动本来也更好看。
GLOW_MS = 110
GLOW_MS_HOVER = 44           # hover 时转得快一点
TWEEN_MS = 26                # hover 过渡期间的刷新间隔,26ms ≈ 38fps
HOVER_TWEEN = 0.50           # hover 进出的过渡时长(秒)—— 就是要的这 0.5 秒
GLOW_PERIOD = 2.6            # 光晕转一圈的秒数(静止)
GLOW_PERIOD_HOVER = 1.3      # hover 时转一圈的秒数

# hover 用单独一套帧:更大(1.05)更亮(1.3)。只靠常量 alpha 提亮太弱 ——
# 球本来就半透明,而且光晕偏蓝,加 alpha 在浅色背景上反而显得更"重"。
# 相位给 8 帧就够,hover 是瞬时状态,多花约 100ms 渲染和 0.4MB。
HOVER_FRAMES = 8
HOVER_SCALE = 1.05
HOVER_BRIGHT = 1.30
IDLE_ALPHA = 226
HOVER_ALPHA = 255
HOVER_LIFT = 2               # hover 时把球抬起 2 物理像素
HOLD_SECONDS = 1.0           # 停在球上多久算"想看看备忘录"

WM_TIMER = 0x0113
WM_MOUSELEAVE = 0x02A3
TIMER_GLOW = 1
TME_LEAVE = 0x00000002

CMD_SHOW, CMD_HIDE, CMD_REDRAW, CMD_QUIT = 1, 2, 3, 4
CMD_FADE, CMD_PLACE, CMD_FADE_DONE = 5, 6, 7


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]


class OrbWindow:
    """悬浮球。线程安全的外部接口只有 show / hide / set_badge / set_look。"""

    def __init__(self, on_click=None, on_expand=None, on_quit=None,
                 on_moved=None, on_hold=None, on_unhover=None,
                 diameter: int = 40, scale: float = 1.0,
                 dark: bool = False, animate: bool = True):
        # on_hold:鼠标停在球上超过 HOLD_SECONDS 时调一次(展开备忘录)
        # on_unhover:鼠标离开球时调一次(那边据此决定要不要收起来)
        self.on_hold = on_hold
        self.on_unhover = on_unhover
        self._held = 0.0
        self._fired_hold = False
        self.on_click = on_click
        self.on_expand = on_expand
        self.on_quit = on_quit
        self.on_moved = on_moved
        self.logical_d = diameter
        self.scale = scale
        self.dark = dark
        self.animate = animate
        self.count = 0

        self.hwnd = 0
        self.ready = threading.Event()
        self._frames: list[Frame] = []   # 弹出/收起用的缩放帧(相位 0)
        self._glow: list[Frame] = []     # 光晕相位帧(静止态)
        self._hot: list[Frame] = []       # 光晕相位帧(hover 态:更大更亮)
        self._phase = 0.0                # 当前相位 0~1,两套帧共用
        self._hover = False           # 鼠标在不在球上(目标状态)
        self._hlevel = 0.0            # 当前的 hover 电平 0~1,朝目标缓动
        self._lift = 0                # 当前已经抬起了多少像素
        self._scratch: Frame | None = None   # AlphaBlend 的暂存位图
        self._last_tick = 0.0
        self._interval = 0
        self._fade_alpha: int | None = None   # 交叉淡化期间的整体 alpha
        self._pos = None                 # 静止位置(物理像素,左上角;不含 hover 的抬升)
        self._drag = None
        self._lock = threading.Lock()
        self._proc = WNDPROC(self._wndproc)   # 必须留引用,否则回调被 GC 掉
        self._thread = threading.Thread(target=self._run, daemon=True, name="orb")
        self._thread.start()
        self.ready.wait(5)

    # ── 尺寸换算:逻辑像素 -> 物理像素
    @property
    def diameter(self) -> int:
        return max(16, int(round(self.logical_d * self.scale)))

    @property
    def pad(self) -> int:
        # 0.28 -> 0.36:阴影要有地方衰减完。原来到位图边界时还剩 5% 左右的
        # 不透明度,然后被硬截断 —— 四条边各留一道直线,看起来就是个方块
        return max(8, int(round(self.logical_d * 0.36 * self.scale)))

    @property
    def size(self) -> int:
        return self.diameter + self.pad * 2

    def ball_rect(self, x: int | None = None, y: int | None = None):
        """球本体(不含四周那圈投影边距)在屏幕上的矩形:(x, y, 直径)。

        交接的时候 WebView 窗口要收到**这个**矩形,不是整张画布 ——
        画布外圈是透明的投影,对不上就会看到球"跳"一下位置。
        """
        if x is None:
            x, y = self._pos or (0, 0)
        return (x + self.pad, y + self.pad, self.diameter)

    # ─────────── 对外接口(可从任意线程调用)───────────
    def show(self, x: int | None = None, y: int | None = None, animate: bool = True,
             alpha: int | None = None) -> None:
        """alpha 给了就用它当初始整体 alpha 并跳过弹出动画(交叉淡化的起点)。"""
        with self._lock:
            self._pending_show = (x, y, animate and alpha is None)
            self._fade_alpha = alpha
        self._post(CMD_SHOW)

    def hide_now(self) -> None:
        """立刻藏掉,不放收起动画(交叉淡化已经把它淡走了)。"""
        self._post(CMD_PLACE)

    def hide(self) -> None:
        self._post(CMD_HIDE)

    def set_fade(self, alpha: int) -> None:
        """按给定的整体 alpha 重贴当前帧(0~255),用于和主窗口交叉淡化。

        期间不让光晕的定时器插手 —— 两个动画同时改同一个窗口会打架。
        """
        with self._lock:
            self._fade_alpha = max(0, min(255, int(alpha)))
        self._post(CMD_FADE)

    def settle(self) -> None:
        """淡化结束:清掉 fade 状态,恢复正常的 alpha 和光晕定时器。"""
        with self._lock:
            self._fade_alpha = None
        self._post(CMD_FADE_DONE)

    def set_badge(self, count: int) -> None:
        if count == self.count:
            return
        self.count = count
        self._post(CMD_REDRAW)

    def set_look(self, diameter: int | None = None, dark: bool | None = None,
                 scale: float | None = None, animate: bool | None = None,
                 force: bool = False) -> None:
        # force:外面有东西变了而这儿看不见 —— 目前只有换主题(配色在
        # orb_render 那个模块级开关里,这里的四个参数一个都不会动)
        changed = bool(force)
        if diameter and diameter != self.logical_d:
            self.logical_d, changed = diameter, True
        if dark is not None and dark != self.dark:
            self.dark, changed = dark, True
        if scale and abs(scale - self.scale) > 0.01:
            self.scale, changed = scale, True
        if animate is not None and animate != self.animate:
            # 关掉动画就冻在第 0 帧,表也停掉 —— 一个定时器都不留
            self.animate, changed = animate, True
        if changed:
            self._post(CMD_REDRAW)

    def position(self) -> tuple[int, int] | None:
        return self._pos

    def visible(self) -> bool:
        return bool(self.hwnd and user32.IsWindowVisible(self.hwnd))

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        """把球夹在工作区里(工作区 = 屏幕减掉任务栏)。

        不夹的话很容易把球拖到任务栏底下,或者收起来时正好落在屏幕外面 ——
        那时候它既看不见也点不着,只能去改 prefs.json 才能救回来。
        夹的是**球本体**,不是画布:画布四周那圈是投影用的透明边距。
        """
        wa = RECT()
        if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(wa), 0):
            wa = RECT(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
        pad, d = self.pad, self.diameter
        lo_x = wa.left - pad + EDGE_MARGIN
        hi_x = wa.right - pad - d - EDGE_MARGIN
        lo_y = wa.top - pad + EDGE_MARGIN
        hi_y = wa.bottom - pad - d - EDGE_MARGIN
        return (max(lo_x, min(x, hi_x)), max(lo_y, min(y, hi_y)))

    def _post(self, cmd: int) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_ORB_CMD, cmd, 0)

    # ─────────── 球自己的线程 ───────────
    def _run(self) -> None:
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.style = CS_DBLCLKS
        cls.lpfnWndProc = self._proc
        cls.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        cls.hCursor = user32.LoadCursorW(None, IDC_HAND)
        cls.lpszClassName = "NEUHelperOrb"
        user32.RegisterClassExW(ctypes.byref(cls))

        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "NEUHelperOrb", "NEU Helper Orb", WS_POPUP,
            0, 0, self.size, self.size, None, None, cls.hInstance, None)
        self._render()
        self.ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _render(self) -> None:
        """重画所有帧:缩放帧(弹出/收起)+ 相位帧(光晕流转)。

        只在尺寸/主题/角标/动画开关变化时走这里。默认尺寸下大约 350ms,
        跑在球自己的线程上,不挡别人。角标是烤进位图的,所以角标一变也得重画。
        """
        for f in self._frames + self._glow + self._hot:
            f.destroy()
        if self._scratch is not None:
            self._scratch.destroy()
            self._scratch = None
        d, p = self.diameter, self.pad
        self._frames = []
        n = canvas_size(d, p)
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
        self._scratch = Frame(self._frames[-1].w, self._frames[-1].h)
        self._phase = 0.0

    def _alpha(self) -> int:
        """整体 alpha 也跟着 hover 电平走,不然提亮会在过渡里"跳"一下。"""
        return int(round(IDLE_ALPHA + (HOVER_ALPHA - IDLE_ALPHA) * self._hlevel))

    def _pick(self, fs: list[Frame]) -> Frame:
        """按当前相位在一套帧里取一张。两套帧长度不同,所以相位存的是 0~1。"""
        return fs[int(self._phase * len(fs)) % len(fs)]

    def _current(self) -> Frame:
        """按 hover 电平合成当前这一帧。

        电平在 0 和 1 之间时,把 hover 帧以 t 的常量 alpha 叠在静止帧上 ——
        源位图都是预乘 alpha 的,AlphaBlend 带 AC_SRC_ALPHA 算出来就是正确的
        over,视觉上是一次交叉淡化(球同时在变大变亮)。
        """
        if not self._glow or not self._hot:
            return self._frames[-1]
        t = self._hlevel * self._hlevel * (3 - 2 * self._hlevel)   # smoothstep
        if t <= 0.02:
            return self._pick(self._glow)
        if t >= 0.98:
            return self._pick(self._hot)
        sc = self._scratch
        if sc is None:
            return self._pick(self._hot if t > 0.5 else self._glow)
        idle, hot = self._pick(self._glow), self._pick(self._hot)
        gdi32.BitBlt(sc.dc, 0, 0, sc.w, sc.h, idle.dc, 0, 0, SRCCOPY)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, int(t * 255), AC_SRC_ALPHA)
        msimg32.AlphaBlend(sc.dc, 0, 0, sc.w, sc.h,
                           hot.dc, 0, 0, hot.w, hot.h, blend)
        return sc

    def _blit(self, frame: Frame, x: int | None = None, y: int | None = None,
              alpha: int | None = None) -> None:
        if alpha is None:
            alpha = self._alpha()
        screen = user32.GetDC(None)
        size = SIZE(frame.w, frame.h)
        src = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, alpha, AC_SRC_ALPHA)
        pos = POINT(x, y) if x is not None else None
        user32.UpdateLayeredWindow(
            self.hwnd, screen, ctypes.byref(pos) if pos else None,
            ctypes.byref(size), frame.dc, ctypes.byref(src), 0,
            ctypes.byref(blend), ULW_ALPHA)
        user32.ReleaseDC(None, screen)

    # ─────────── 消息处理 ───────────
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_ORB_CMD:
                self._on_cmd(wparam)
                return 0
            if msg == WM_TIMER and wparam == TIMER_GLOW:
                self._tick()
                return 0
            if msg == WM_MOUSELEAVE:
                self._set_hover(False)
                return 0
            if msg == WM_LBUTTONDOWN:
                # 先把 hover 的抬升撤掉,再记拖动起点,否则拖完会整体偏 2px
                self._set_hover(False)
                user32.SetCapture(hwnd)
                pt = POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                r = RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                self._drag = {"px": pt.x, "py": pt.y, "wx": r.left, "wy": r.top,
                              "t": time.time(), "moved": False}
                return 0
            if msg == WM_MOUSEMOVE and not self._drag:
                self._set_hover(True)
                return 0
            if msg == WM_MOUSEMOVE and self._drag:
                pt = POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                dx, dy = pt.x - self._drag["px"], pt.y - self._drag["py"]
                if not self._drag["moved"] and abs(dx) + abs(dy) > DRAG_SLOP:
                    self._drag["moved"] = True
                if self._drag["moved"]:
                    x, y = self._clamp(self._drag["wx"] + dx, self._drag["wy"] + dy)
                    user32.SetWindowPos(hwnd, 0, x, y, 0, 0,
                                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                    self._pos = (x, y)
                return 0
            if msg == WM_LBUTTONUP:
                user32.ReleaseCapture()
                d, self._drag = self._drag, None
                if d and not d["moved"] and (time.time() - d["t"]) * 1000 < CLICK_MS:
                    if self.on_click:
                        self.on_click()
                elif d and d["moved"] and self.on_moved and self._pos:
                    self.on_moved(*self._pos)
                return 0
            if msg == WM_LBUTTONDBLCLK:
                # 双击直接展开完整面板 —— 单击只弹对话框
                if self.on_expand:
                    self.on_expand()
                return 0
            if msg == WM_RBUTTONUP:
                self._menu()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            # 窗口过程里抛异常会直接崩掉消息循环,所以必须吞掉 ——
            # 但一定要留痕,不然球"不动了"这种问题完全无从下手(踩过)
            traceback.print_exc(file=sys.stderr)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _tick(self) -> None:
        """定时器的一拍:推进 hover 电平和光晕相位,然后重贴一帧。

        两者都按**时间**算,不按拍数 —— 过渡期间定时器要跳到 26ms 才够顺,
        如果按拍数推进,光晕就会跟着一起变快。
        """
        now = time.perf_counter()
        dt = min(0.25, max(0.0, now - self._last_tick)) if self._last_tick else 0.02
        # 悬停到点了就喊一声。**只喊一次** —— 回调那边要开窗口,
        # 每一拍都调等于每 26ms 开一次
        if self._hover:
            self._held += dt
            if self._held >= HOLD_SECONDS and not self._fired_hold:
                self._fired_hold = True
                if self.on_hold:
                    try:
                        self.on_hold()
                    except Exception:
                        traceback.print_exc(file=sys.stderr)
        self._last_tick = now

        target = 1.0 if self._hover else 0.0
        if self._hlevel != target:
            step = dt / HOVER_TWEEN
            if self._hlevel < target:
                self._hlevel = min(target, self._hlevel + step)
            else:
                self._hlevel = max(target, self._hlevel - step)

        if len(self._glow) > 1:
            period = GLOW_PERIOD + (GLOW_PERIOD_HOVER - GLOW_PERIOD) * self._hlevel
            self._phase = (self._phase + dt / period) % 1.0

        # 抬升跟着电平走(整数像素,所以就两级)
        lift = int(round(HOVER_LIFT * self._hlevel))
        if lift != self._lift and self._pos:
            self._lift = lift
            x, y = self._pos
            user32.SetWindowPos(self.hwnd, 0, x, y - lift, 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

        self._blit(self._current())
        self._start_timer()

    def _set_hover(self, on: bool) -> None:
        """只设目标,真正的变化由 _tick 缓动过去(0.5 秒)。"""
        if on:
            self._track_leave()          # 每次 move 都要续订,不然只报一次
        if on == self._hover:
            return
        self._hover = on
        # 停留计时:进来时清零重新数,离开时通知一声
        self._held = 0.0
        if not on:
            self._fired_hold = False
            if self.on_unhover:
                try:
                    self.on_unhover()
                except Exception:
                    traceback.print_exc(file=sys.stderr)
        if not self.animate:
            # 关了动画就没有过渡:直接跳到位
            self._hlevel = 1.0 if on else 0.0
            self._lift = HOVER_LIFT if on else 0
            if self._pos:
                x, y = self._pos
                user32.SetWindowPos(self.hwnd, 0, x, y - self._lift, 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            self._blit(self._current())
            return
        self._last_tick = 0.0
        self._start_timer()

    def _track_leave(self) -> None:
        t = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, self.hwnd, 0)
        user32.TrackMouseEvent(ctypes.byref(t))

    def _start_timer(self) -> None:
        """按当前状态选刷新间隔。过渡期间 26ms(要顺),settled 之后慢下来省电。"""
        if not self.animate or not self.visible():
            if self._interval:
                user32.KillTimer(self.hwnd, TIMER_GLOW)
                self._interval = 0
            return
        moving = 0.001 < self._hlevel < 0.999
        want = TWEEN_MS if moving else (GLOW_MS_HOVER if self._hover else GLOW_MS)
        if want != self._interval:
            self._interval = want
            user32.SetTimer(self.hwnd, TIMER_GLOW, want, None)

    def _on_cmd(self, cmd: int) -> None:
        if cmd == CMD_FADE:
            # 交叉淡化的一步:用指定的整体 alpha 重贴当前帧。
            # 定时器先停掉,免得光晕的那一拍把 alpha 又覆盖回去。
            if self._interval:
                user32.KillTimer(self.hwnd, TIMER_GLOW)
                self._interval = 0
            with self._lock:
                a = self._fade_alpha
            self._blit(self._current(), None, None, a if a is not None else 255)
            return
        if cmd == CMD_FADE_DONE:
            self._blit(self._current())
            self._start_timer()
            return
        if cmd == CMD_PLACE:
            user32.KillTimer(self.hwnd, TIMER_GLOW)
            self._interval = 0
            self._hover = False
            self._hlevel = 0.0
            self._lift = 0
            user32.ShowWindow(self.hwnd, SW_HIDE)
            return
        if cmd == CMD_SHOW:
            with self._lock:
                x, y, animate = getattr(self, "_pending_show", (None, None, True))
            if x is None and self._pos:
                x, y = self._pos
            if x is None:
                # 没指定就贴工作区右下角
                x = user32.GetSystemMetrics(0)
                y = user32.GetSystemMetrics(1)
            x, y = self._clamp(int(x), int(y))
            self._pos = (x, y)
            with self._lock:
                start_alpha = self._fade_alpha
            frames = self._frames
            # 交叉淡化的起点:先按给定 alpha(通常是 0)把最终形态贴上去,
            # 别放弹出动画 —— 那是"凭空出现"时才需要的
            if start_alpha is not None:
                self._hlevel = 0.0
                self._blit(self._current(), x, y, start_alpha)
            else:
                self._blit(frames[0] if animate else frames[-1], x, y,
                           120 if animate else 255)
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOSIZE | 0x0002 | SWP_NOACTIVATE)
            if start_alpha is not None:
                return                      # 后面的淡入由 set_fade 一步步推
            if animate:
                for i, f in enumerate(frames):
                    self._blit(f, None, None, min(self._alpha(), 110 + i * 34))
                    time.sleep(POP_MS)
            self._blit(self._current())
            self._start_timer()
        elif cmd == CMD_HIDE:
            user32.KillTimer(self.hwnd, TIMER_GLOW)
            self._interval = 0
            self._hover = False
            self._hlevel = 0.0
            self._lift = 0
            # 反着放一遍 + 渐隐,别"啪"地消失
            for i in range(len(self._frames) - 1, -1, -1):
                self._blit(self._frames[i], None, None, max(0, 40 * i))
                time.sleep(POP_MS * 0.7)
            user32.ShowWindow(self.hwnd, SW_HIDE)
        elif cmd == CMD_REDRAW:
            was = self.visible()
            user32.KillTimer(self.hwnd, TIMER_GLOW)
            self._interval = 0
            self._render()
            if was:
                x, y = self._pos or (None, None)
                if x is not None:
                    x, y = self._clamp(x, y)
                    self._pos = (x, y)
                user32.SetWindowPos(self.hwnd, 0, x, y, self.size, self.size,
                                    SWP_NOZORDER | SWP_NOACTIVATE)
                self._blit(self._current(), x, y)
                self._start_timer()
        elif cmd == CMD_QUIT:
            user32.DestroyWindow(self.hwnd)

    def _menu(self) -> None:
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        m = user32.CreatePopupMenu()
        user32.AppendMenuW(m, MF_STRING, 1, "打开对话")
        user32.AppendMenuW(m, MF_STRING, 2, "展开完整面板")
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING, 3, "退出 NEU Helper")
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.DestroyMenu(m)
        if cmd == 1 and self.on_click:
            self.on_click()
        elif cmd == 2 and self.on_expand:
            self.on_expand()
        elif cmd == 3 and self.on_quit:
            self.on_quit()
