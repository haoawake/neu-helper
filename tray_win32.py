# -*- coding: utf-8 -*-
"""任务栏通知区(右下角那排小图标)里的托盘图标 —— Windows 实现。

存在的理由:三态里有一态是**整个主窗口隐藏**(收成悬浮球)。球要是被拖到了
屏幕边上、或者用户手快把它连点两下收进了角落,除了任务管理器就没有别的
出路了。托盘图标是那个always-there 的出路:左键叫回窗口,右键一步退出。

════════════════════════════════════════════════════════════════════
**为什么要一个自己的隐藏窗口。**

Shell_NotifyIcon 不是"给我一个图标"那么简单 —— 鼠标事件是**发消息**回来的,
所以必须有一个窗口来收。这个窗口从不显示(建出来就不 ShowWindow),
只当消息靶子。

它**不能是 HWND_MESSAGE 那种纯消息窗口**:右键菜单靠 TrackPopupMenu 弹,
而那个 API 要求 owner 能进前台(菜单是靠"失去激活"来关的)。纯消息窗口
拿不到激活,菜单弹出来之后点别处关不掉。所以这里是一个普通的 WS_POPUP 窗口,
1×1、永不显示。

**Explorer 会重启。** 重启之后通知区被清空,所有托盘图标都没了 ——
不重新 NIM_ADD 的话图标就永远消失,而进程还活得好好的。系统会广播一条
`TaskbarCreated`,这里注册并监听它,收到就把图标加回去。这不是假想:
Explorer 崩溃/重启在 Windows 上很常见。
════════════════════════════════════════════════════════════════════

暴露的接口见 tray.py(分发层),两个平台必须一致。
"""
from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_TRAY = 0x0400 + 41            # WM_APP 附近,自己定的回调消息
WM_TRAY_CMD = 0x0400 + 42        # 外面让托盘线程干活(重贴图标/退出)

CMD_REICON = 1
CMD_CLOSE = 2

NIM_ADD = 0x0000
NIM_MODIFY = 0x0001
NIM_DELETE = 0x0002
NIF_MESSAGE = 0x0001
NIF_ICON = 0x0002
NIF_TIP = 0x0004
NIF_SHOWTIP = 0x0080

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
SM_CXSMICON = 49
SM_CYSMICON = 50

WS_POPUP = 0x80000000
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
IDI_APPLICATION = 32512

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND,
                             wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    """**字段顺序和长度不能动。** 这是系统按内存布局读的结构体,
    szTip 是 128 个 wchar(不是 64,那是 Win95 的老版本),写短了 shell32
    会读越界的内存去当提示文字。cbSize 也必须是这个结构体的真实大小 ——
    系统按它判断你用的是哪一版布局。"""
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# **argtypes 必须显式声明。** 不声明的话 ctypes 按 Python 值猜类型,而
# `WS_POPUP = 0x80000000` 塞不进有符号的 C int —— 报的是
# `ctypes.ArgumentError: int too long to convert`,而且它发生在托盘自己的
# 线程里,不留意的话就是"没有图标,也没有任何报错"。orb_win32 里踩过同一个坑。
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                              wintypes.UINT, ctypes.c_int, ctypes.c_int,
                              wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                  ctypes.c_void_p]
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_void_p,
                               wintypes.LPCWSTR]

# 菜单项的 id。数字本身无所谓,但**不要用 0** —— TrackPopupMenu 用 0 表示
# "什么都没选"(点了别处关掉菜单)
ID_OPEN, ID_EXPAND, ID_ORB, ID_QUIT = 1, 2, 3, 4


class Tray:
    """托盘图标。线程安全的外部接口只有 set_labels / set_tip / close。

    图标活在自己的线程上(有自己的消息循环),和悬浮球、信息弹窗是同一个
    路子 —— 谁也别去堵主线程。
    """

    def __init__(self, on_open=None, on_expand=None, on_orb=None,
                 on_quit=None, icon_path: str = "",
                 tip: str = "NEU Helper", labels: dict | None = None):
        self.on_open = on_open
        self.on_expand = on_expand
        self.on_orb = on_orb
        self.on_quit = on_quit
        self.icon_path = str(icon_path or "")
        self.tip = tip
        self.labels = dict(labels or {})
        self.hwnd = 0
        self.error = ""            # 线程里炸了的话,原因留在这儿
        self._hicon = None
        self._added = False
        self._msg_taskbar = user32.RegisterWindowMessageW("TaskbarCreated")
        self._proc = WNDPROC(self._wndproc)      # 必须留引用,不然会被回收
        self.ready = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True, name="tray")
        self._t.start()
        self.ready.wait(6.0)

    # ─────────── 外部接口 ───────────

    def set_labels(self, labels: dict) -> None:
        """换菜单文案(切了界面语言)。下次右键就是新的,不用重建图标。"""
        self.labels = dict(labels or {})

    def set_tip(self, tip: str) -> None:
        self.tip = tip or ""
        self._post(CMD_REICON)

    def visible(self) -> bool:
        return bool(self._added)

    def close(self) -> None:
        """把图标从通知区摘掉。**退出前一定要调** —— 不摘的话通知区会留一个
        点不动的"幽灵图标",直到用户把鼠标划过去系统才发现那个进程没了。"""
        self._post(CMD_CLOSE)

    def _post(self, cmd: int) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_TRAY_CMD, cmd, 0)

    # ─────────── 自己的线程 ───────────

    def _run(self) -> None:
        # **异常要记下来。** 这是自己的线程 —— 里面炸了的话,app.py 那层
        # try/except 一个字都收不到,表现就是"没有托盘图标,也没有任何日志"。
        try:
            self._setup()
        except Exception:                          # noqa: BLE001
            import traceback
            self.error = traceback.format_exc()
            print("[tray] 起不来:" + self.error, file=sys.stderr, flush=True)
            self.ready.set()
            return
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _setup(self) -> None:
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._proc
        cls.hInstance = kernel32.GetModuleHandleW(None)
        cls.lpszClassName = "NEUHelperTray"
        user32.RegisterClassExW(ctypes.byref(cls))
        # 1×1、永不显示。只是个收 Shell_NotifyIcon 回调的靶子,
        # 外加给 TrackPopupMenu 当 owner(见文件开头)
        self.hwnd = user32.CreateWindowExW(
            0, "NEUHelperTray", "NEU Helper Tray", WS_POPUP,
            0, 0, 1, 1, None, None, cls.hInstance, None)
        if not self.hwnd:
            raise RuntimeError(
                f"CreateWindowExW 失败(GetLastError={kernel32.GetLastError()})")
        self._load_icon()
        self._add()

    def _load_icon(self) -> None:
        """按通知区的实际大小加载 .ico。

        **给明确的宽高,不要 LR_DEFAULTSIZE。** 那个常量取的是 SM_CXICON
        (32px 的大图标),再由系统缩到 16 —— 缩出来是糊的。直接按
        SM_CXSMICON 让 LoadImage 从 .ico 里挑最合适那一档最清楚。
        """
        w = user32.GetSystemMetrics(SM_CXSMICON) or 16
        h = user32.GetSystemMetrics(SM_CYSMICON) or 16
        p = Path(self.icon_path)
        if p.is_file():
            self._hicon = user32.LoadImageW(None, str(p), IMAGE_ICON,
                                            w, h, LR_LOADFROMFILE)
        if not self._hicon:
            # 找不到图标不是错误,顶多难看 —— 用系统默认那个,别让托盘没了
            self._hicon = user32.LoadIconW(
                None, ctypes.cast(IDI_APPLICATION,
                                  wintypes.LPCWSTR))

    def _nid(self, flags: int) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = (self.tip or "")[:127]
        return nid

    def _add(self) -> None:
        """把图标放进通知区。**幂等** —— 加不上就先删一次再加。

        NIM_ADD 对同一对 (hWnd, uID) 加第二次是会失败的。这在两种情况下
        真的会遇到:一是上一个实例崩了、系统还没回收那个"幽灵图标";
        二是 TaskbarCreated 来早了,通知区其实还没清干净。
        先 NIM_DELETE 一下再加,两种都能过去 —— 删不掉也无所谓,本来就是
        "可能不存在"。
        """
        flags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        ok = shell32.Shell_NotifyIconW(NIM_ADD,
                                       ctypes.byref(self._nid(flags)))
        if not ok:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
            ok = shell32.Shell_NotifyIconW(NIM_ADD,
                                           ctypes.byref(self._nid(flags)))
        self._added = bool(ok)

    def _wndproc(self, hwnd, msg, wp, lp):
        if msg == self._msg_taskbar:
            # Explorer 重启了,通知区被清空 —— 把图标加回去
            self._added = False
            self._add()
            return 0
        if msg == WM_TRAY:
            ev = lp & 0xFFFF
            if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._fire(self.on_open)
            elif ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._menu()
            return 0
        if msg == WM_TRAY_CMD:
            if wp == CMD_REICON:
                shell32.Shell_NotifyIconW(
                    NIM_MODIFY,
                    ctypes.byref(self._nid(NIF_ICON | NIF_TIP | NIF_SHOWTIP)))
            elif wp == CMD_CLOSE:
                self._remove()
                user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            self._remove()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _remove(self) -> None:
        if self._added:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
            self._added = False

    def _menu(self) -> None:
        L = self.labels
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        m = user32.CreatePopupMenu()
        user32.AppendMenuW(m, MF_STRING, ID_OPEN, L.get("open") or "打开")
        user32.AppendMenuW(m, MF_STRING, ID_EXPAND,
                           L.get("expand") or "展开完整面板")
        user32.AppendMenuW(m, MF_STRING, ID_ORB, L.get("orb") or "收成悬浮球")
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING, ID_QUIT,
                           L.get("quit") or "退出 NEU Helper")
        # **这一下不能省。** 菜单是靠"owner 失去激活"来关的:不把 owner 提到
        # 前台,点别处菜单不会消失,而且键盘也操作不了它
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.DestroyMenu(m)
        self._fire({ID_OPEN: self.on_open, ID_EXPAND: self.on_expand,
                    ID_ORB: self.on_orb, ID_QUIT: self.on_quit}.get(cmd))

    @staticmethod
    def _fire(fn) -> None:
        """回调丢到另一个线程上跑。

        **不能在这儿直接调。** 这里是托盘自己的消息循环:回调要做的事
        (切形态、关窗口)会去动主窗口、还会等它的动画跑完 —— 在消息循环里
        同步做完,这段时间托盘对系统就是"没响应",右键会卡住。
        """
        if fn is None:
            return
        threading.Thread(target=fn, daemon=True, name="tray-cb").start()
