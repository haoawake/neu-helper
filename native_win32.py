# -*- coding: utf-8 -*-
"""用纯 Win32 操作自己的窗口 —— **native_window 在 Windows 上的实现**。

不要直接 import 这个模块,import `native_window`(它按平台挑实现)。
macOS 那份是 native_cocoa.py,两边对外暴露同一组函数名。

为什么不用 pywebview 的 js_api 桥:实测只要挂上 js_api,这个页面就会把窗口
卡死成「未响应」(不挂 responding=True,挂上 responding=False,其余变量全部
控制不变)。桥的注入会从工作线程去探 WebView2 的 COM 属性,而那些成员只允许
UI 线程访问。

这里用的这几个 API —— SetWindowPos / ShowWindow / PostMessage —— 本来就是为
跨线程调用设计的,从 Flask 的工作线程调是安全的,不需要 marshal 回 UI 线程。

拖拽曾经想省事:发一条 WM_NCLBUTTONDOWN + HTCAPTION 让 Windows 自己进移动
循环。**实测不行,一下都拖不动。** 那个循环起手要 SetCapture,而此刻鼠标捕获
在 Chromium 的渲染子窗口手里(而且那是另一个进程,ReleaseCapture 也管不着),
抢不到捕获,循环立刻退出。

所以改成自己跟鼠标:前端 pointerdown 时记一次起点,pointermove 时来一条
请求,后端每次自己 GetCursorPos 算位移 —— 完全不碰坐标换算(前端的 screenX
是 CSS 像素,后端要的是物理像素,换算在多显示器 + 175% 缩放下很容易错)。
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32

# 必须声明 argtypes。不声明的话 Python 的 int 会按 32 位 C int 传递,而 64 位
# Windows 上 HWND 是 64 位 —— HWND_TOPMOST(-1)不会正确符号扩展,变成一个
# 无效句柄,SetWindowPos 静默失败(实测:窗口尺寸改了但置顶没生效)。
user32.SetWindowPos.argtypes = [
    wintypes.HWND,      # hWnd
    wintypes.HWND,      # hWndInsertAfter
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL

user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL

user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostMessageW.restype = wintypes.BOOL

user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetWindowRect.restype = wintypes.BOOL

WM_CLOSE = 0x0010
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2

SW_HIDE = 0
SW_SHOW = 5
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

SM_CXSCREEN = 0
SM_CYSCREEN = 1

MONITOR_DEFAULTTONEAREST = 2


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL


_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def find_own_window(title: str) -> int:
    """找本进程自己的可见顶层窗口。

    不用 FindWindowW(None, title):标题匹配会撞上别的程序(比如编辑器打开了
    同名项目时窗口标题里也含这个词)。按进程 ID 过滤才是准的。
    """
    want = os.getpid()
    found: list[int] = []

    def cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != want or not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == title:
            found.append(hwnd)
            return False        # 找到就停
        return True

    user32.EnumWindows(_ENUM_PROC(cb), 0)
    return found[0] if found else 0


def get_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def dpi_scale(hwnd: int) -> float:
    """窗口所在显示器的缩放比。GetWindowRect 给的是物理像素,
    而我们的尺寸配置写的是逻辑像素,得换算。"""
    try:
        dpi = user32.GetDpiForWindow(hwnd)     # Win10 1607+
        if dpi:
            return dpi / 96.0
    except AttributeError:
        pass
    return 1.0


def render_scale(hwnd: int) -> float:
    """一个窗口单位要画几个位图像素。

    Windows 上窗口 API 收的就是物理像素,所以这个数和 dpi_scale 相等 ——
    留一个同义的名字是为了**让调用方把意图写清楚**:悬浮球和弹窗要的是
    "位图画多大",窗口几何要的是"逻辑尺寸换成窗口单位"。这两件事在 macOS
    上是两个不同的数(那边 Cocoa 用点,Retina 的 backingScaleFactor 是 2),
    名字分开了才不会在那边混用。
    """
    return dpi_scale(hwnd)

def resize_anchor_bottom_right(hwnd: int, logical_w: int, logical_h: int) -> None:
    """改成指定的逻辑尺寸,并锚住窗口的右下角。

    锚右下角是故意的:收成悬浮窗时它会留在原窗口右下角那个位置,
    展开时向左上长回去 —— 视觉上是「从那个角落收起/展开」,而且完全
    不需要知道屏幕尺寸。
    """
    scale = dpi_scale(hwnd)
    pw = max(1, int(round(logical_w * scale)))
    ph = max(1, int(round(logical_h * scale)))
    _, _, right, bottom = get_rect(hwnd)

    x = right - pw
    y = bottom - ph
    # 别让窗口跑到屏幕外面去
    sw = user32.GetSystemMetrics(SM_CXSCREEN)
    sh = user32.GetSystemMetrics(SM_CYSCREEN)
    x = max(0, min(x, max(0, sw - pw)))
    y = max(0, min(y, max(0, sh - ph)))

    user32.SetWindowPos(hwnd, 0, x, y, pw, ph, SWP_NOZORDER | SWP_NOACTIVATE)


# ─────────────────────── 形变动画 ───────────────────────
#
# **窗口区域(SetWindowRgn)对 WebView2 无效,别再试了。** 区域只约束 GDI
# 绘制,而 WebView2 的画面是 DirectComposition 的视觉层,DWM 直接合成、
# 不看窗口区域。实测:窗口缩到 136x136 + 贴一个椭圆区域,GetWindowRgnBox
# 确认区域在(COMPLEXREGION),但屏幕上依旧是个白色圆角方块。
# 所以悬浮球改成自己画的分层窗口,见 orb_window.py。
#
# 动画就是一串 SetWindowPos:每帧一次 Win32 调用,24 帧摊在 280ms 里,
# 开销可以忽略,而且不占 UI 线程(SetWindowPos 是跨线程安全的)。

_anim_lock = threading.Lock()
_anim_gen = 0


def animate_to(
    hwnd: int,
    logical_w: int,
    logical_h: int,
    duration: float = 0.26,
    frames: int = 22,
    anchor_br: tuple[int, int] | None = None,
) -> None:
    """把窗口平滑变形到目标尺寸,锚住右下角。

    同一时刻只允许一个动画:后来的会让前一个提前退出(靠 generation 计数),
    否则连点两个按钮会有两个循环互相抢 SetWindowPos。

    `anchor_br` 给了就以它为右下角(默认是窗口当前的右下角)。
    **这个参数原来只有 cocoa 那份有** —— 分发层的两份签名必须一样,
    不然"在另一个平台上必然 TypeError"这种 bug 只能等用户来报。
    """
    if anchor_br is not None:
        sc = dpi_scale(hwnd)
        w = max(1, int(round(logical_w * sc)))
        h = max(1, int(round(logical_h * sc)))
        ax, ay = anchor_br
        x, y, w, h = nudge_onscreen(hwnd, ax - w, ay - h, w, h)
        animate_rect(hwnd, x, y, w, h, duration, frames)
        return
    global _anim_gen
    with _anim_lock:
        _anim_gen += 1
        gen = _anim_gen

    scale = dpi_scale(hwnd)
    tw = max(1, int(round(logical_w * scale)))
    th = max(1, int(round(logical_h * scale)))
    left, top, right, bottom = get_rect(hwnd)
    sw, sh = right - left, bottom - top

    sw_screen = user32.GetSystemMetrics(SM_CXSCREEN)
    sh_screen = user32.GetSystemMetrics(SM_CYSCREEN)

    for i in range(1, frames + 1):
        if gen != _anim_gen:
            return                      # 被新动画取代,立刻让位
        p = i / frames
        e = 1 - (1 - p) ** 3            # ease-out cubic,收尾柔和
        w = max(1, int(round(sw + (tw - sw) * e)))
        h = max(1, int(round(sh + (th - sh) * e)))
        x = max(0, min(right - w, max(0, sw_screen - w)))
        y = max(0, min(bottom - h, max(0, sh_screen - h)))
        user32.SetWindowPos(hwnd, 0, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
        time.sleep(duration / frames)


def _ease_in_out(p: float) -> float:
    """ease-in-out cubic:两头慢中间快。收放这种"一个东西整体在移动"的动作
    用它最自然;ease-out 起手太猛,像被甩出去的。"""
    return 4 * p ** 3 if p < 0.5 else 1 - (-2 * p + 2) ** 3 / 2


def animate_rect(
    hwnd: int,
    tx: int, ty: int, tw: int, th: int,
    duration: float = 0.30,
    frames: int = 20,
    alpha_from: float | None = None,
    alpha_to: float | None = None,
) -> None:
    """位置和尺寸一起插值,从当前矩形走到目标矩形(全是物理像素)。

    animate_to 只改尺寸、锚住右下角;收放悬浮球需要的是"从那个小点长到这个
    大小",位置也得跟着走,所以单独一个。

    alpha_from / alpha_to 给了的话,整窗透明度跟着同一条缓动一起插值 ——
    收球的最后一段要边缩边淡,这样和球的淡入接得上。
    """
    global _anim_gen
    with _anim_lock:
        _anim_gen += 1
        gen = _anim_gen

    sx, sy, right, bottom = get_rect(hwnd)
    sw, sh = right - sx, bottom - sy
    fade = alpha_from is not None and alpha_to is not None
    for i in range(1, frames + 1):
        if gen != _anim_gen:
            return
        e = _ease_in_out(i / frames)
        user32.SetWindowPos(
            hwnd, 0,
            int(round(sx + (tx - sx) * e)), int(round(sy + (ty - sy) * e)),
            max(1, int(round(sw + (tw - sw) * e))),
            max(1, int(round(sh + (th - sh) * e))),
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
        if fade:
            set_window_alpha(hwnd, alpha_from + (alpha_to - alpha_from) * e)
        time.sleep(duration / frames)


def set_topmost(hwnd: int, on: bool) -> bool:
    ok = user32.SetWindowPos(
        hwnd,
        HWND_TOPMOST if on else HWND_NOTOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE,
    )
    return bool(ok)


def is_topmost(hwnd: int) -> bool:
    WS_EX_TOPMOST = 0x00000008
    GWL_EXSTYLE = -20
    return bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)


def minimize(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_MINIMIZE)


def hide(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_HIDE)


def show(hwnd: int, foreground: bool = True) -> None:
    user32.ShowWindow(hwnd, SW_SHOW)
    if foreground:
        # 后台进程调 SetForegroundWindow 常被系统拒掉,先蹭一下置顶保证看得见
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE)
        user32.SetForegroundWindow(hwnd)


# ─────────────────────── 整窗透明 ───────────────────────
#
# 这是"真的透到桌面"的唯一可行路子:CSS 的 backdrop-filter 只能模糊页面自己的
# 背景,碰不到桌面;pywebview 的 transparent=True 在这台机器上不生效(页面设
# background:transparent 之后背后仍是一块不透明浅灰)。
#
# WS_EX_LAYERED + LWA_ALPHA 是 DWM 在合成时整窗混色,和页面无关 —— 实测有效,
# WebView2 的内容照常渲染、鼠标命中也不受影响(截图验证过:背后的窗口内容
# 清晰地透过整个界面)。
#
# 代价是"整窗":文字也会跟着半透明,所以界面里把它和"表面不透明度"分成两项,
# 并且下限只开到 55%。
#
# 顺带说一句:DWM 的亚克力(SetWindowCompositionAttribute 的
# ACCENT_ENABLE_ACRYLICBLURBEHIND / DwmSetWindowAttribute 的 SYSTEMBACKDROP)
# 两个接口都返回成功,但屏幕上背后的内容依旧是清晰的 —— 磨砂背板在这套
# 窗口上拿不到,别再试了。

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002

user32.SetLayeredWindowAttributes.argtypes = [
    wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]


def set_window_alpha(hwnd: int, alpha: float) -> bool:
    """alpha 是 0.3~1.0。1.0 会把 WS_EX_LAYERED 摘掉(彻底回到不透明那条渲染路径)。"""
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if alpha >= 0.999:
        if ex & WS_EX_LAYERED:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_LAYERED)
        return True
    if not ex & WS_EX_LAYERED:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
    # 这里不设下限:交叉淡化要一路淡到 0。"别调得看不见"是设置面板的事
    # (那根滑块最低 55%),不是这个原语的事。
    v = max(0, min(255, int(round(alpha * 255))))
    return bool(user32.SetLayeredWindowAttributes(hwnd, 0, v, LWA_ALPHA))


WM_SETICON = 0x0080
ICON_SMALL, ICON_BIG = 0, 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]

_icons: list = []          # 留引用:图标句柄不能被提前释放


def set_window_icon(hwnd: int, ico_path: str) -> bool:
    """给窗口挂图标 —— 任务栏和 Alt+Tab 里就不是 Python 的蛇了。

    大小两个尺寸都要设:只设 ICON_BIG 的话任务栏那个小图标还是默认的。
    """
    ok = False
    for which, size in ((ICON_BIG, 32), (ICON_SMALL, 16)):
        h = user32.LoadImageW(None, ico_path, IMAGE_ICON, size, size,
                              LR_LOADFROMFILE)
        if not h:
            continue
        _icons.append(h)
        user32.SendMessageW(hwnd, WM_SETICON, which, h)
        ok = True
    return ok


def is_iconic(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def is_maximized(hwnd: int) -> bool:
    return bool(user32.IsZoomed(hwnd))


def work_area(hwnd: int) -> tuple[int, int, int, int]:
    """窗口所在那块屏幕的工作区(去掉任务栏),物理像素 (x, y, w, h)。

    用 MonitorFromWindow 而不是 SPI_GETWORKAREA:后者只认主显示器,
    窗口拖到副屏上双击会飞回主屏。
    """
    mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        r = mi.rcWork
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    # 拿不到就退回整个主屏
    return (0, 0, user32.GetSystemMetrics(SM_CXSCREEN),
            user32.GetSystemMetrics(SM_CYSCREEN))


# 展开之前那个矩形,按 hwnd 记着 —— 还原要回到它
_pre_expand: dict[int, tuple[int, int, int, int]] = {}
# 决策和记录要原子:连点两下的话,第二下不能把"动画到一半的那个矩形"
# 当成"展开之前的原始尺寸"记下来
_expand_lock = threading.Lock()


def is_expanded(hwnd: int, slack: int = 8) -> bool:
    """当前是不是已经铺满工作区了。

    不看 IsZoomed:我们刻意不用系统的最大化(见模块顶部注释),
    所以判断只能靠矩形对不对得上。slack 给 DPI 取整留点余量。
    """
    if user32.IsZoomed(hwnd):
        return True
    left, top, right, bottom = get_rect(hwnd)
    wx, wy, ww, wh = work_area(hwnd)
    return (abs(left - wx) <= slack and abs(top - wy) <= slack
            and abs((right - left) - ww) <= slack
            and abs((bottom - top) - wh) <= slack)


def unzoom(hwnd: int) -> None:
    """脱掉系统的最大化状态,但屏幕上不动。

    处于 zoomed 状态时 `SetWindowPos` 只改"还原后的尺寸",屏幕上纹丝不动 ——
    所以任何要动画的操作之前都得先脱掉。脱完立刻把矩形贴回原处,
    免得中间闪一下 SW_RESTORE 那个尺寸。
    """
    if not user32.IsZoomed(hwnd):
        return
    left, top, right, bottom = get_rect(hwnd)
    user32.ShowWindow(hwnd, SW_RESTORE)
    set_rect(hwnd, left, top, right - left, bottom - top)


def get_pre_expand(hwnd: int):
    """展开之前那个矩形。收球时要连它一起存起来 —— 否则"从全屏收球 → 点球
    回到全屏 → 双击还原"会跳到一个从没见过的默认尺寸。"""
    return _pre_expand.get(hwnd)


def set_pre_expand(hwnd: int, rect) -> None:
    with _expand_lock:
        if rect and len(rect) == 4:
            _pre_expand[hwnd] = tuple(int(v) for v in rect)
        else:
            _pre_expand.pop(hwnd, None)


def nudge_onscreen(hwnd: int, x: int, y: int, w: int, h: int):
    """把一个存下来的矩形挪回够得着的范围,但**不改它的尺寸、也不强行塞进
    工作区**。

    收球之后到点球之间可能过了一整天:分辨率变了、副屏拔了都有可能,
    照原样摆回去可能整扇窗都在屏幕外。但夹进工作区是过头的 ——
    窗口比工作区高、故意伸出底边是很常见的用法,硬顶上去等于替用户改了布局
    (实测就把一个 y=235 的窗口顶到了 151)。

    只保证两件事:标题栏在屏幕里够得着(不能跑到顶边以上),
    横向至少留 120px 在屏幕上。
    """
    wx, wy, ww, wh = work_area(hwnd)
    w, h = max(120, int(w)), max(80, int(h))
    keep = 120
    x = max(wx - w + keep, min(int(x), wx + ww - keep))
    y = max(wy, min(int(y), wy + wh - 40))
    return x, y, w, h


def toggle_maximize(hwnd: int, duration: float = 0.26, frames: int = 20,
                    threaded: bool = True) -> bool:
    """双击标题栏用的。动画着展开到工作区 / 动画着回到原来那个矩形。

    返回切换后是不是"展开"状态。threaded 时动画在后台跑、函数立刻返回 ——
    HTTP 接口不该被 260ms 的动画堵着。
    """
    unzoom(hwnd)
    wx, wy, ww, wh = work_area(hwnd)
    with _expand_lock:
        if is_expanded(hwnd):
            target = _pre_expand.pop(hwnd, None) or (
                wx + ww // 8, wy + wh // 8, ww * 3 // 4, wh * 3 // 4)
            want = False
        else:
            left, top, right, bottom = get_rect(hwnd)
            _pre_expand[hwnd] = (left, top, right - left, bottom - top)
            target = (wx, wy, ww, wh)
            want = True

    def run():
        animate_rect(hwnd, *target, duration=duration, frames=frames)

    if threaded:
        threading.Thread(target=run, daemon=True).start()
    else:
        run()
    return want


def move_to(hwnd: int, x: int, y: int) -> None:
    user32.SetWindowPos(hwnd, 0, x, y, 0, 0,
                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)


def is_foreground(hwnd: int) -> bool:
    """这扇窗是不是当前前台窗口。浮窗判断"人还在用它"要靠它 ——
    指针可能跑到系统弹出的选择器上,但前台仍然是这扇窗。"""
    return bool(user32.GetForegroundWindow() == hwnd)


def cursor_pos() -> tuple[int, int]:
    """当前鼠标位置(物理像素)。悬停浮窗要靠它判断指针在不在自己身上。"""
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def is_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(hwnd))


def is_window(hwnd: int) -> bool:
    """这个句柄还指向一个活着的窗口吗 —— 句柄缓存要靠它判失效。

    单独提出来是为了不让调用方去碰 user32:那样等于把 Win32 漏到了
    平台无关的代码里,macOS 那边没有对应的东西可接。
    """
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def set_geometry(hwnd: int, logical_w: int, logical_h: int,
                 anchor_br: tuple[int, int] | None = None) -> None:
    """一步到位定尺寸和位置。anchor_br 是希望窗口右下角落在哪(物理像素)。

    从悬浮球展开时用得上:让窗口的右下角对齐球的右下角,看起来是从球那儿
    长出来的。不给 anchor_br 就保持当前位置。
    """
    scale = dpi_scale(hwnd)
    pw = max(1, int(round(logical_w * scale)))
    ph = max(1, int(round(logical_h * scale)))
    if anchor_br is None:
        left, top, _, _ = get_rect(hwnd)
        x, y = left, top
    else:
        x, y = anchor_br[0] - pw, anchor_br[1] - ph
    sw = user32.GetSystemMetrics(SM_CXSCREEN)
    sh = user32.GetSystemMetrics(SM_CYSCREEN)
    x = max(0, min(x, max(0, sw - pw)))
    y = max(0, min(y, max(0, sh - ph)))
    user32.SetWindowPos(hwnd, 0, x, y, pw, ph, SWP_NOZORDER | SWP_NOACTIVATE)


def set_rect(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    """直接按物理像素摆窗口。动画的起始帧要用它(尺寸已经算好了,不再换算)。"""
    user32.SetWindowPos(hwnd, 0, x, y, max(1, w), max(1, h),
                        SWP_NOZORDER | SWP_NOACTIVATE)


def set_size(hwnd: int, logical_w: int, logical_h: int) -> None:
    """立刻改成目标尺寸,不走动画(最大化恢复、启动定型这类场景用)。"""
    scale = dpi_scale(hwnd)
    user32.SetWindowPos(
        hwnd, 0, 0, 0,
        max(1, int(round(logical_w * scale))), max(1, int(round(logical_h * scale))),
        SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
    )


def restore(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_RESTORE)


def close(hwnd: int) -> None:
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


# ─────────────────────── 拖窗口 ───────────────────────
#
# 前端在 pointerdown 时调 drag_start,松手调 drag_end。位移由后端自己
# GetCursorPos 算,前端一个坐标都不用传 —— 省掉 CSS 像素 / 物理像素的换算,
# 也就没有多显示器和 175% 缩放下算错的可能。
#
# **中间那一段是后端自己转的,不是前端每帧发请求。** 原来是 pointermove 里
# 用 rAF 合并到 ~60/s、每帧一条 POST /api/window/drag/move。那条路在这台机器
# 上会一顿一顿地追手,原因量出来了:
#
#   直接调 handler(不走网络)          中位 0.28ms
#   已经建好的 socket 上跑一个来回      中位 0.047ms
#   **新建一条 loopback TCP 连接**      中位 0.49ms,**p90 509ms,最慢 540ms**
#
# 而 werkzeug 的开发服务器是 HTTP/1.0、每个响应都带 Connection: close ——
# 于是**每帧都要新建一条连接**,每帧都在赌那个 500ms 的停顿。实测 2 秒里
# 60 次请求只有 27 次跟得上,25% 超过 33ms(掉两帧以上)。
#
# 所以改成:drag_start 起一条跟随线程,自己按 ~120Hz 读光标挪窗口;
# 拖动全程只有两条请求(start / end)。这条路上一个 TCP 连接都不用新建。
#
# 那个"新建连接很慢"本身不是这个项目的毛病(loopback 上钩了东西的机器都会
# 这样),但既然拖窗是唯一每帧发请求的地方,躲开它比指望机器变好靠谱。

# 拖拽由后端自己跟(见下面的 _follow)。前端据此**不再每帧发请求** ——
# 这个标志由 /api/window/drag/start 回给前端
DRAG_FOLLOWS = True

VK_LBUTTON = 0x01
# 跟随的步长。120Hz 比屏幕刷新率高一截 —— 宁可多算几次,也不要在 60Hz 的
# 屏幕上正好错开一帧
_FOLLOW_STEP = 1 / 120
# 保险丝:松手的消息没收到、drag_end 也没来的话,最多跟这么久
_FOLLOW_MAX = 60.0

_drag_lock = threading.Lock()
_drag: dict | None = None
_follow_on = False

# 返回值是 SHORT。不声明的话 ctypes 按 int 解释,最高位那个"正按着"的标志
# 会被当成符号位,判断就永远是假
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]


def drag_start(hwnd: int) -> bool:
    global _drag
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    left, top, _, _ = get_rect(hwnd)
    with _drag_lock:
        _drag = {"px": pt.x, "py": pt.y, "wx": left, "wy": top, "hwnd": hwnd}
        # 一次拖动只要一条跟随线程。重复调 drag_start(前端重试、或者松手的
        # 消息丢了紧接着又按下)不该越攒越多
        start = not _follow_on
        if start:
            globals()["_follow_on"] = True
    if start:
        threading.Thread(target=_follow, daemon=True, name="win-drag").start()
    return True


def drag_move() -> bool:
    """按光标相对起点的位移移动窗口。没有 drag_start 过就什么都不做。"""
    with _drag_lock:
        d = _drag
    if not d:
        return False
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.SetWindowPos(
        d["hwnd"], 0,
        d["wx"] + pt.x - d["px"], d["wy"] + pt.y - d["py"], 0, 0,
        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
    )
    return True


def _follow() -> None:
    """跟着光标挪窗口,直到松手 / drag_end / 保险丝。

    **松手要自己发现。** pointerup 有可能落在别的窗口上(拖得快的时候指针
    会跑到窗口外面),那种情况下前端的 drag_end 根本不会来 —— 光等它的话
    窗口会一直黏着鼠标。所以这里直接问系统左键还按着没有。
    """
    global _follow_on
    t0 = time.time()
    try:
        while True:
            with _drag_lock:
                if _drag is None:
                    return
            if not (user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000):
                drag_end()
                return
            if time.time() - t0 > _FOLLOW_MAX:
                drag_end()
                return
            drag_move()
            time.sleep(_FOLLOW_STEP)
    finally:
        with _drag_lock:
            _follow_on = False


def drag_end() -> None:
    global _drag
    with _drag_lock:
        _drag = None
