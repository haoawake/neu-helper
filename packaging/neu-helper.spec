# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置:Windows 出两个**单文件** exe,macOS 出一个 .app。

    NEU Helper(.exe)   桌面应用本体,无控制台
    canvas-mcp(.exe)   MCP stdio server,**有**控制台(它靠 stdin/stdout 通信)

为什么要两个:MCP 那个是被 Claude Code 当子进程拉起来的,装机时注册的命令
以前是 `python canvas_mcp.py` —— 打包版里没有 python,所以得给它一个自己的
可执行文件。

════════════════════════════════════════════════════════════════════
**Windows 为什么是 onefile —— 这是被一个真实故障逼出来的,别改回 onedir。**

第一版发的是 onedir。开发机上一切正常,但用户下载之后打不开:

    Failed to execute script 'pyi_rth_pkgres' ...
    ImportError: DLL load failed while importing pyexpat
    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
                  from .../_internal/pythonnet/runtime/Python.Runtime.dll

根因是 **Mark of the Web**:从网上下载的 zip,用资源管理器解压出来的**每个
文件**都会带一条"来自互联网"的备用数据流,而带这个标记的 DLL 和 .NET 程序集
会被系统拒绝正常加载。本地构建出来的文件没有这个标记 ——
**所以这个问题在开发机上永远测不出来**,必须专门给文件打上标记去复现。

实测四种情况(同一份构建产物):

    onedir 干净                    ✓  5.3 / 4.4 秒
    onedir + MOTW                  ✗  崩在 pythonnet 解析
    onedir + MOTW + Unblock-File   ✓  正常
    onefile + MOTW                 ✓  5.3 / 5.0 秒

onefile 免疫的原因很直接:**只有 exe 那一个文件带标记**,启动时解到临时目录
的那些 DLL 是 bootloader 自己写出来的,干净。

而且实测两者**启动一样快** —— 解 43MB 在 SSD 上的开销可以忽略。既然不要钱,
就没有理由让用户去手动 Unblock 才能用。
════════════════════════════════════════════════════════════════════

onefile 下 data/ 仍然落在 exe 旁边:`sys.executable` 指的是真实的 exe 路径,
而 server.py 里冻结时 `HERE = Path(sys.executable).parent`;打进包里的 gui/
走 `sys._MEIPASS`。两者刻意分开,理由见 server.py 那一处注释。

macOS 那边仍然是 .app(Dock 图标和登录项都要它),而且不需要 onefile:
Gatekeeper 的隔离属性是**整个 .app 一条**,`xattr -cr` 一次就清掉,
不像 Windows 那样每个文件一条。

构建:
    Windows   python -m PyInstaller packaging/neu-helper.spec --noconfirm
    macOS     同一条命令(下面按平台挑)
"""
import sys
from pathlib import Path

IS_MAC = sys.platform == "darwin"
PROJ = Path(SPECPATH).resolve().parent          # noqa: F821  (PyInstaller 注入)

# 版本号唯一的来源还是 version.py。读它而不是写死 —— .app 的
# CFBundleShortVersionString 要用(Finder 的「简介」里显示的就是它)
sys.path.insert(0, str(PROJ))
import version as _ver                          # noqa: E402

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
    # setuptools / pkg_resources:项目里**一处都没用**,是 PyInstaller 的钩子
    # 自己带进来的。排掉之后 pyi_rth_pkgres 这个运行时钩子也不会被打包,
    # 于是 pkg_resources -> plistlib -> xml.parsers.expat 这条链整条消失。
    # 那条链是个真实的故障点:用户报过
    #     Failed to execute script 'pyi_rth_pkgres' ...
    #     ImportError: DLL load failed while importing pyexpat
    # 我们既不需要它,就别让它有机会坏。
    # 注意别把 "distutils" 也写进来 —— PyInstaller 内部自己处理它,
    # 排掉会报 `Target module "distutils" already imported as ExcludedModule`
    "setuptools", "pkg_resources", "_distutils_hack", "pip",
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
    # MCP server 不开窗口,webview / .NET / PDF 那几套都用不着
    excludes=_EXCLUDES + ["webview", "pythonnet", "clr", "clr_loader",
                          "pymupdf", "fitz"],
    noarchive=False,
)

app_pyz = PYZ(app_a.pure)                       # noqa: F821
mcp_pyz = PYZ(mcp_a.pure)                       # noqa: F821

ICON = str(PROJ / "gui" / ("icon.icns" if IS_MAC else "icon.ico"))

if IS_MAC:
    # .app 包是 onedir 结构 —— Dock 图标、登录项、bundle identifier 都要它,
    # 而且那边没有 Windows 那种逐文件的 MOTW 问题。
    app_exe = EXE(app_pyz, app_a.scripts, [],           # noqa: F821
                  exclude_binaries=True, name="NEU Helper",
                  console=False, icon=ICON)
    mcp_exe = EXE(mcp_pyz, mcp_a.scripts, [],           # noqa: F821
                  exclude_binaries=True, name="canvas-mcp",
                  console=True, icon=ICON)
    # 两个程序共用的那部分依赖只保留一份
    MERGE((app_a, "app", "NEU Helper"),                 # noqa: F821
          (mcp_a, "canvas_mcp", "canvas-mcp"))
    coll = COLLECT(app_exe, app_a.binaries, app_a.datas,  # noqa: F821
                   mcp_exe, mcp_a.binaries, mcp_a.datas,
                   strip=False, upx=False, name="NEU Helper")
    app = BUNDLE(                                        # noqa: F821
        coll,
        name="NEU Helper.app",
        icon=ICON,
        # 不传的话 PyInstaller 填 "0.0.0",Finder 的「简介」里就是这个数
        version=_ver.VERSION,
        bundle_identifier="com.neuhelper.app",
        info_plist={
            "CFBundleName": "NEU Helper",
            "CFBundleDisplayName": "NEU Helper",
            "NSHighResolutionCapable": True,
            # 悬浮球和弹窗要能浮在别的应用之上,而且应用本身常驻
            "LSUIElement": False,
            # ── 这一条**必须显式写 False**,否则这个 .app 是不可用的 ──
            #
            # PyInstaller 会自己塞 `LSBackgroundOnly = True`,规则是
            # 「EXE 的 console=True 就算后台程序」。而 BUNDLE 的 console
            # 是从 COLLECT 继承的,COLLECT 又是**被最后一个 EXE 覆盖**的 ——
            # 这里 COLLECT(app_exe, …, mcp_exe, …) 里排在后面的正是
            # canvas-mcp,它 console=True(靠 stdin/stdout 说话,必须有控制台)。
            # 于是整个 .app 被标成了后台专用程序。
            #
            # LSBackgroundOnly=True 的后果不是"少个 Dock 图标"那么轻:
            # 系统认定这个进程没有界面,窗口拿不到 key 状态 —— 对话框打出来
            # 也打不了字。v3.3.2 那个 mac 包就是这样发出去的。
            #
            # (想要"有界面但不占 Dock"用的是 LSUIElement,不是这一条。)
            "LSBackgroundOnly": False,
            "NSAppleEventsUsageDescription":
                "把已经在跑的窗口叫到前面时需要。",
        },
    )
else:
    # Windows:两个各自独立的单文件 exe(为什么不是 onedir,见文件开头)。
    # **没有 MERGE** —— 那是给 onedir 共享依赖用的,onefile 下每个都得自包含。
    app_exe = EXE(                                       # noqa: F821
        app_pyz, app_a.scripts, app_a.binaries, app_a.datas, [],
        name="NEU Helper",
        console=False,      # 没有控制台:开机启动时不能闪黑框
        icon=ICON,
        upx=False,
    )
    mcp_exe = EXE(                                       # noqa: F821
        mcp_pyz, mcp_a.scripts, mcp_a.binaries, mcp_a.datas, [],
        name="canvas-mcp",
        console=True,       # **必须有**:它靠 stdin/stdout 说话
        icon=ICON,
        upx=False,
    )
