# -*- coding: utf-8 -*-
"""和桌面环境打交道的那些杂事,按平台分好。

深浅色、打开文件、在文件管理器里定位、开机自启的位置、单实例 ——
每一件在两个系统上都是不同的 API,但都只有几行。散在 server.py / app.py 里
会变成一堆 `if sys.platform`,所以集中到这里。

窗口、悬浮球、弹窗那三块**不在**这里:它们量大且成体系,各自有
`native_window` / `orb_window` / `toast` 三个分发层。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import platform_id

# 起子进程的标志位。Windows 上用 pythonw 启动时没有控制台,不带这个标志
# 每次调 claude 都会闪一个黑框;macOS 没有这回事,所以是 0。
# **两处在用**(chat_bridge / mailai),所以放在这里,别各自再定义一遍。
NO_WINDOW = subprocess.CREATE_NO_WINDOW if platform_id.IS_WIN else 0


# ─────────────────────────── 深浅色 ───────────────────────────


def system_dark() -> bool:
    """系统现在是深色模式吗。

    只在偏好设为 `auto` 时才会问到这里 —— 用户明确选了深色/浅色的话
    上层直接按那个来,不查系统。
    """
    if platform_id.IS_WIN:
        try:
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
        except Exception:                          # noqa: BLE001
            return False
    if platform_id.IS_MAC:
        # 浅色模式下这个键**根本不存在**(不是等于 "Light"),所以 `defaults
        # read` 会以非零退出 —— 那就是浅色,不是出错。
        try:
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=3)
            return out.returncode == 0 and "dark" in out.stdout.strip().lower()
        except Exception:                          # noqa: BLE001
            return False
    return False


# ─────────────────────── 打开文件 / 定位 ───────────────────────


def open_path(path: str | Path) -> None:
    """用系统默认程序打开一个文件或目录。"""
    p = str(path)
    if platform_id.IS_WIN:
        os.startfile(p)                            # noqa: S606
        return
    if platform_id.IS_MAC:
        subprocess.Popen(["open", p])
        return
    raise platform_id.unsupported("打开文件")


def reveal_path(path: str | Path) -> None:
    """在文件管理器里打开这个文件所在的目录**并把它选中**。

    目录本身传进来的话就直接打开它(而不是打开父目录再选中它)——
    两个平台的"定位"参数都只对文件有意义。
    """
    p = Path(path)
    if not p.is_file():
        open_path(p)
        return
    if platform_id.IS_WIN:
        # explorer 的 /select 要求逗号紧跟在参数名后面,写成两个参数才对
        subprocess.Popen(["explorer", "/select,", str(p)])
        return
    if platform_id.IS_MAC:
        subprocess.Popen(["open", "-R", str(p)])   # -R = reveal in Finder
        return
    raise platform_id.unsupported("在文件管理器里定位")


# ─────────────────────────── 开机自启 ───────────────────────────


def autostart_dir() -> Path:
    """放"开机自启"那个东西的目录。

    Windows   启动文件夹里放一个 .vbs 快捷方式
    macOS     ~/Library/LaunchAgents 里放一个 .plist

    界面上「打开自启目录」那个按钮用它。两边都可能还不存在(全新账号),
    所以调用方记得 mkdir。
    """
    if platform_id.IS_WIN:
        return Path(os.path.expandvars(
            r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"))
    if platform_id.IS_MAC:
        return platform_id.home() / "Library" / "LaunchAgents"
    raise platform_id.unsupported("开机自启")


# ─────────────────────────── 单实例 ───────────────────────────
#
# 目标一样:第二次启动时不要再开一个窗口,而是把已经在跑的那个叫到前面。
# 手段不一样,但都选了"进程死了自动释放"的机制 —— pid 文件那种做法会在
# 崩溃后留一把锁在那儿,下次就再也起不来了。

_keep = None                      # 故意不释放:进程活着,锁就该一直占着


def acquire_single_instance(name: str = "NEUHelper") -> bool:
    """抢到"我是唯一的那个实例"吗?抢到返回 True。

    Windows   命名互斥体。内核维护,进程怎么死的都会自动释放
    macOS     锁文件 + `flock(LOCK_EX | LOCK_NB)`。同样由内核维护,
              进程退出时文件描述符一关锁就没了
    """
    global _keep
    if platform_id.IS_WIN:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        # Local\ 前缀 = 只在当前登录会话内唯一
        h = k32.CreateMutexW(None, False, f"Local\\{name}Singleton")
        if h and k32.GetLastError() == 183:        # ERROR_ALREADY_EXISTS
            return False
        _keep = h
        return True
    if platform_id.IS_MAC:
        import fcntl
        d = platform_id.config_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = open(d / f"{name}.lock", "w")          # noqa: SIM115
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        _keep = f
        return True
    return True                                    # 没实现就别挡着启动


def raise_existing(title: str) -> None:
    """把已经在跑的那个实例叫到前面来。

    macOS 上不能直接去动别的进程的窗口 —— 只能让系统把那个应用激活。
    `open -a` 认的是应用名,脚本方式跑的时候进程名是 Python,所以这里用
    AppleScript 按名字激活;失败也不要紧,大不了用户自己去点一下。
    """
    if platform_id.IS_WIN:
        import ctypes

        import native_window as nw
        # **必须用 FindWindowW 而不是 native_window.find_own_window** ——
        # 后者只找本进程的窗口,而这里要找的恰恰是**另一个**进程的
        ctypes.windll.user32.FindWindowW.restype = ctypes.c_void_p
        h = ctypes.windll.user32.FindWindowW(None, title)
        if h:
            if nw.is_iconic(h):
                nw.restore(h)
            nw.show(h)
        return
    if platform_id.IS_MAC:
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to set frontmost of '
                 f'(first process whose name contains "{title}") to true'],
                capture_output=True, timeout=5)
        except Exception:                          # noqa: BLE001
            pass


# ─────────────────────────── 找 claude CLI ───────────────────────────


def claude_candidates() -> list[Path]:
    """PATH 里找不到时的兜底位置。

    需要兜底是因为启动路径不走登录 shell(Windows 是 wscript、macOS 是
    LaunchAgent),那时的 PATH 和交互式终端里的 PATH 不是一回事 ——
    **Homebrew 的路径尤其容易不在里面**。
    """
    home = platform_id.home()
    if platform_id.IS_WIN:
        return [
            home / ".local" / "bin" / "claude.exe",
            home / ".local" / "bin" / "claude",
            home / "AppData" / "Local" / "Programs" / "claude" / "claude.exe",
        ]
    return [
        home / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),          # Apple 芯片的 Homebrew
        Path("/usr/local/bin/claude"),             # Intel 的 Homebrew
        home / ".bun" / "bin" / "claude",
        home / ".npm-global" / "bin" / "claude",
    ]


def app_icon(gui_dir: Path) -> Path | None:
    """窗口/Dock 图标。两个系统认的格式不一样,所以在这里挑。

    找不到就返回 None —— 没图标不是错误,只是难看一点,不该拦住启动。
    """
    name = "icon.icns" if platform_id.IS_MAC else "icon.ico"
    p = Path(gui_dir) / name
    return p if p.exists() else None
