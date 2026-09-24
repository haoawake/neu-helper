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


# ---------------------------------------------------------------- 开机自启

# 开机那一项叫什么。**四条路径必须对得上**:两个装机脚本各建一个,
# 设置里那个开关既要认出它们、也要能自己建一个一样的。
#
#                    Windows 启动文件夹          macOS LaunchAgents
#   源码版装机       NEU Helper.lnk              com.neuhelper.gate.plist
#   (install.*)      -> wscript startup-gate.vbs -> bash startup-gate.sh
#   打包版装机       NEU Helper.lnk              com.neuhelper.app.plist
#   (setup.*)        -> NEU Helper.exe           -> open -a NEU Helper.app
#
# **两种安装方式指向的东西不一样,这是对的。** 源码版走「闸门」——
# 那个脚本每天只放行一次,否则每次登录都弹一个窗口;而闸门是个 .vbs/.sh,
# 它启动的是 Python 脚本,打包版里根本没有 Python,也没有这两个脚本文件
# (zip 里只有两个 exe / 一个 .app)。所以打包版直接指向程序本身 ——
# 每次登录都起,靠单实例锁和"每日简报一天只发一次"兜住,setup.* 里
# 是同一个取舍。
AUTOSTART_NAME = "NEU Helper"

# macOS 上两个 plist 的 Label / 文件名都不一样,所以**读的时候两个都要看**。
# 只认其中一个的后果:打包版明明开着自启,设置里那个开关却显示关;
# 一点开又建出第二个 agent,一点关只删掉其中一个。
MAC_AGENT_SRC = "com.neuhelper.gate.plist"     # 源码版(install.sh)
MAC_AGENT_APP = "com.neuhelper.app.plist"      # 打包版(setup_mac.sh)


def autostart_paths() -> list[Path]:
    """**所有**可能的开机项 —— 读状态、关闭时都按这一组来。"""
    d = autostart_dir()
    if platform_id.IS_MAC:
        return [d / MAC_AGENT_SRC, d / MAC_AGENT_APP]
    return [d / (AUTOSTART_NAME + ".lnk")]


def autostart_path() -> Path:
    """**这一份**该写哪个文件 —— 按当前是打包版还是源码版挑。"""
    d = autostart_dir()
    if platform_id.IS_MAC:
        return d / (MAC_AGENT_APP if platform_id.IS_FROZEN else MAC_AGENT_SRC)
    return d / (AUTOSTART_NAME + ".lnk")


def autostart_on() -> bool:
    return any(p.is_file() for p in autostart_paths())


def autostart_target(here: Path) -> Path | None:
    """开机时**实际会被启动的那个东西**。拿不准就返回 None。

    存在的意义是"建之前先确认它在":指着一个不存在的文件建快捷方式,
    开机时 Windows 会弹一句「找不到脚本文件」、macOS 则是静悄悄什么都不发生,
    而设置里那个开关还显示"已开启"。这种故障没有任何线索。
    """
    here = Path(here)
    if platform_id.IS_MAC:
        app = platform_id.app_bundle(here)
        if app is not None:                        # 打包版:启动 .app 本身
            return app
        gate = here / "startup-gate.sh"            # 源码版:每日闸门
        return gate if gate.is_file() else None
    if platform_id.IS_FROZEN:
        exe = here / "NEU Helper.exe"
        return exe if exe.is_file() else None
    gate = here / "startup-gate.vbs"
    return gate if gate.is_file() else None


_PLIST_SRC = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.neuhelper.gate</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>{target}</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
"""

# 打包版用 `open -a`,不是直接跑 bundle 里那个可执行文件 —— 那样起来的进程
# 没有 .app 的身份(没有 Dock 图标、拿不到激活),和 setup_mac.sh 里的取舍一致。
_PLIST_APP = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.neuhelper.app</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/open</string><string>-a</string>
  <string>{target}</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
"""


def _launchctl(plist: Path, load: bool) -> None:
    """把 agent 装上 / 卸下来。**失败不抛** —— plist 文件本身已经落盘,
    下次登录照样生效;launchctl 只是让它这一次就立刻生效。

    bootstrap/bootout 是新写法,老系统上不认,所以退回 load/unload。
    setup_mac.sh 里用的是同一组回退。
    """
    uid = os.getuid() if hasattr(os, "getuid") else 0
    pairs = ([["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
              ["launchctl", "load", "-w", str(plist)]] if load else
             [["launchctl", "bootout", f"gui/{uid}/{plist.stem}"],
              ["launchctl", "unload", "-w", str(plist)]])
    for cmd in pairs:
        try:
            if subprocess.run(cmd, capture_output=True,
                              timeout=15).returncode == 0:
                return
        except Exception:                          # noqa: BLE001
            pass


def set_autostart(here: Path, on: bool) -> bool:
    """开 / 关开机自启。返回操作之后的**实际**状态,不是你要求的那个。

    关:把 autostart_paths() 里的**每一个**都清掉(macOS 上两个 plist 都删)。
    只删自己那一个的话,另一种安装方式留下的 agent 还在,表现是"关了还是会
    自己启动"。

    开:先确认要指向的东西真的存在(autostart_target),不存在就什么都不建 ——
    宁可开关弹回去,也不要建一个开机报错的死链接。
    """
    here = Path(here)
    if not on:
        for p in autostart_paths():
            if platform_id.IS_MAC and p.is_file():
                _launchctl(p, load=False)
            try:
                p.unlink()
            except (FileNotFoundError, OSError):
                pass
        return autostart_on()

    target = autostart_target(here)
    if target is None:
        return autostart_on()                      # 指不到东西,不建

    dst = autostart_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 另一种安装方式留下的那一个要清掉,免得两个开机项并存
    for p in autostart_paths():
        if p != dst and p.is_file():
            if platform_id.IS_MAC:
                _launchctl(p, load=False)
            try:
                p.unlink()
            except OSError:
                pass

    if platform_id.IS_MAC:
        tpl = _PLIST_APP if platform_id.app_bundle(here) else _PLIST_SRC
        dst.write_text(tpl.format(target=target), encoding="utf-8")
        _launchctl(dst, load=True)
        return autostart_on()

    # Windows:造 .lnk 走 WScript.Shell —— 纯 Python 拼 IShellLink 的 COM 调用
    # 又长又脆,而 PowerShell 是系统自带的。install.ps1 / setup.ps1 建这个
    # 快捷方式用的也正是同一段,三处行为一致。
    if platform_id.IS_FROZEN:
        # 打包版:直接指向 exe。图标从 exe 自己身上取 —— gui/ 在 exe 里面,
        # 磁盘上没有 gui/icon.ico 这个文件
        run, args, icon = str(target), "", f"{target},0"
    else:
        run = str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                  / "System32" / "wscript.exe")
        args = f'"{target}"'
        ico = here / "gui" / "icon.ico"
        icon = f"{ico},0" if ico.is_file() else f"{run},0"
    ps = ";".join([
        "$w = New-Object -ComObject WScript.Shell",
        "$s = $w.CreateShortcut('" + str(dst) + "')",
        "$s.TargetPath = '" + run + "'",
        "$s.Arguments = '" + args + "'",
        "$s.WorkingDirectory = '" + str(here) + "'",
        "$s.Description = 'Open the Canvas study assistant at logon'",
        "$s.IconLocation = '" + icon + "'",
        "$s.WindowStyle = 7",
        "$s.Save()",
    ])
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", ps],
            capture_output=True, timeout=30, creationflags=NO_WINDOW)
    except Exception:                              # noqa: BLE001
        pass
    return autostart_on()


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
            # **拿不到也得把句柄关掉。** CreateMutexW 在"已存在"时照样返回一个
            # **有效句柄**(指向同一个互斥体),不关就等于自己给它续命。
            #
            # 平时无所谓 —— 判完马上就退了。但 `--after-update` 那条路是**循环
            # 重试**的:第一次失败漏一个句柄,之后哪怕旧进程早退干净了,互斥体
            # 也被自己漏出来的那个句柄撑着,于是**每一次重试都必然失败**,
            # 等满 20 秒放弃。用户看到的就是"点了更新并重启,应用没了"。
            #
            # 双进程量过:不关句柄等满 10 秒拿不到,关了 3.1 秒就拿到。
            k32.CloseHandle(ctypes.c_void_p(h))
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
