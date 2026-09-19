# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置:一次构建出两个可执行文件。

    NEU Helper(.exe)   桌面应用本体,无控制台
    canvas-mcp(.exe)   MCP stdio server,**有**控制台(它靠 stdin/stdout 通信)

为什么要两个:MCP 那个是被 Claude Code 当子进程拉起来的,装机时注册的命令
以前是 `python canvas_mcp.py` —— 打包版里没有 python,所以得给它一个自己的
可执行文件。

**用 onedir 不用 onefile。** onefile 每次启动都要把几十兆解到临时目录再跑,
冷启动要多好几秒;而且那个临时目录退出就删,任何"写在程序旁边"的东西都会丢。
这个应用恰恰要在自己旁边写 data/(邮件、对话、课件、偏好)。

构建:
    Windows   python -m PyInstaller packaging/neu-helper.spec --noconfirm
    macOS     同一条命令(下面按平台挑图标和 .app 打包)
"""
import sys
from pathlib import Path

IS_MAC = sys.platform == "darwin"
PROJ = Path(SPECPATH).resolve().parent          # noqa: F821  (PyInstaller 注入)

# ── Anaconda 的坑:标准库的扩展模块会依赖 Library/bin 里的 DLL,
#    而 PyInstaller 只收得到 .pyd、收不到它背后的那个 DLL。
#
#    踩过的:`pyexpat.pyd` 进来了但 `libexpat.dll` 没有,启动直接
#    `ImportError: DLL load failed while importing pyexpat`。而且它是被
#    pkg_resources -> plistlib -> xml.parsers.expat 链式拉进来的,
#    代码里根本没写过 xml,光看 import 图看不出来。
#
#    不是 conda 环境的话这个目录不存在,下面整段就是空的 —— 无副作用。
_CONDA_BIN = Path(sys.base_prefix) / "Library" / "bin"
_NEED_DLL = ["libexpat.dll", "liblzma.dll", "libbz2.dll", "ffi.dll",
             "libffi-8.dll", "sqlite3.dll", "LIBBZ2.dll"]
binaries = [(str(_CONDA_BIN / n), ".")
            for n in _NEED_DLL if (_CONDA_BIN / n).is_file()]

# ── 打包进去的只读资源。
#
# **只有 gui/。** 另外三样刻意不打进来,由构建脚本原样放到 exe 旁边:
#
#   CLAUDE.md / .claude/    应用调 `claude -p` 时把 cwd 设成 exe 所在目录
#                           (server.py 里 project_dir = HERE),所以 Claude Code
#                           是在**那儿**找这两个东西的。打进 _internal 就等于
#                           它们不存在 —— 助手会没有行为定义、MCP 权限也不生效
#   data/                   运行时才生成、而且要写,更不能在包里
#
# gui/ 反过来:它是纯只读的,而且 server.py 用 RES(解包目录)去读,所以
# 留在包里正合适。
datas = [
    (str(PROJ / "gui"), "gui"),
]

# pywebview 的后端是**运行时按平台挑**的,静态分析看不出来 —— 不点名就不会
# 被打进去,表现是"进程在跑但窗口永远不出来"
hidden = [
    "webview.platforms.cocoa" if IS_MAC else "webview.platforms.edgechromium",
    "webview.platforms.winforms",
] if not IS_MAC else ["webview.platforms.cocoa"]
hidden += [
    # Flask/Werkzeug 的一些东西也是动态导入的
    "werkzeug.middleware.proxy_fix",
    # 邮箱那边按需要才 import 的编解码器
    "email.mime.text", "email.mime.multipart",
]
if IS_MAC:
    hidden += ["objc", "AppKit", "Foundation", "Quartz", "WebKit",
               "PyObjCTools.AppHelper"]

# **Qt 必须排掉。** pywebview 支持 Qt 后端,而这台机器的 Anaconda 里装着
# PyQt5 —— 静态分析看到那条 import 就把整个 Qt(含 QtWebEngine)打进来了。
# 实测:不排是 358MB,排掉是下面这个数。我们在 Windows 上走 edgechromium、
# 在 macOS 上走 cocoa,一次都用不到 Qt。
#
# 别的几个是科学计算栈,Anaconda 环境下很容易被顺带拉进去。
_EXCLUDES = [
    "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "sip",
    "webview.platforms.qt", "webview.platforms.cef",
    "webview.platforms.gtk", "webview.platforms.mshtml",
    "tkinter", "matplotlib", "numpy", "pandas", "scipy", "sympy",
    "PIL", "pytest", "IPython", "notebook", "jupyter", "sklearn",
]

app_a = Analysis(                               # noqa: F821
    [str(PROJ / "app.py")],
    pathex=[str(PROJ)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=_EXCLUDES,
    noarchive=False,
)
mcp_a = Analysis(                               # noqa: F821
    [str(PROJ / "canvas_mcp.py")],
    pathex=[str(PROJ)],
    binaries=binaries,
    datas=[],
    hiddenimports=[],
    excludes=_EXCLUDES + ["webview"],
    noarchive=False,
)

# 两个程序共用的那部分依赖只保留一份
MERGE((app_a, "app", "NEU Helper"),             # noqa: F821
      (mcp_a, "canvas_mcp", "canvas-mcp"))

app_pyz = PYZ(app_a.pure)                       # noqa: F821
mcp_pyz = PYZ(mcp_a.pure)                       # noqa: F821

ICON = str(PROJ / "gui" / ("icon.icns" if IS_MAC else "icon.ico"))

app_exe = EXE(                                  # noqa: F821
    app_pyz, app_a.scripts, [],
    exclude_binaries=True,
    name="NEU Helper",
    console=False,          # 没有控制台:开机启动时不能闪黑框
    icon=ICON,
)
mcp_exe = EXE(                                  # noqa: F821
    mcp_pyz, mcp_a.scripts, [],
    exclude_binaries=True,
    name="canvas-mcp",
    console=True,           # **必须有**:它靠 stdin/stdout 说话
    icon=ICON,
)

coll = COLLECT(                                 # noqa: F821
    app_exe, app_a.binaries, app_a.datas,
    mcp_exe, mcp_a.binaries, mcp_a.datas,
    strip=False, upx=False,
    name="NEU Helper",
)

if IS_MAC:
    # .app 包:Dock 图标和名字才是对的,也才能被"登录项"认出来
    app = BUNDLE(                               # noqa: F821
        coll,
        name="NEU Helper.app",
        icon=ICON,
        bundle_identifier="com.neuhelper.app",
        info_plist={
            "CFBundleName": "NEU Helper",
            "CFBundleDisplayName": "NEU Helper",
            "NSHighResolutionCapable": True,
            # 悬浮球和弹窗要能浮在别的应用之上,而且应用本身常驻
            "LSUIElement": False,
            "NSAppleEventsUsageDescription":
                "把已经在跑的窗口叫到前面时需要。",
        },
    )
