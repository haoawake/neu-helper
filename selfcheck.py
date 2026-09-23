# -*- coding: utf-8 -*-
"""双端自检:在这台机器上,该有的东西齐不齐、该跑的路径通不通。

    python selfcheck.py           跑全部
    python selfcheck.py --quick    跳过要联网的那几项

**为什么有这个文件。** 这个项目的原生那一层(窗口 / 悬浮球 / 信息弹窗)分了
Windows 和 macOS 两套实现,而开发是在 Windows 上做的 —— macOS 那半边只能
在 Mac 上真跑一次才算数。这个脚本把"不开窗口也能验的部分"全查一遍:依赖齐不齐、
分发层挑对了没、共用的渲染层画出来对不对、后端接口通不通、凭据在不在。

它**不能**替代真机上看一眼:球的层级对不对、字画得好不好看、拖起来跟不跟手,
这些只有眼睛能判。最后会把这几条列出来让你自己点一遍。
"""
from __future__ import annotations

# 同 make_icon.py:输出是中文的,终端不一定编得出来(英文版 Windows 是 cp1252)。
# 自检本身不该因为"打不出这几个字"而崩掉。
import sys as _sys
try:
    _sys.stdout.reconfigure(errors="replace")
    _sys.stderr.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

import importlib
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

QUICK = "--quick" in sys.argv

_C = {"ok": "\033[32m", "bad": "\033[31m", "warn": "\033[33m",
      "head": "\033[36m", "off": "\033[0m"}
if sys.platform == "win32":
    # 老版本的 Windows 控制台不认 ANSI,输出一堆乱码比没颜色难看
    import os
    if not os.environ.get("WT_SESSION"):
        _C = dict.fromkeys(_C, "")

results: list[tuple[str, bool, str]] = []


def head(t: str) -> None:
    print(f"\n{_C['head']}── {t}{_C['off']}")


def chk(name: str, cond, detail: str = "", warn_only: bool = False) -> bool:
    good = bool(cond)
    tag = (f"{_C['ok']}OK  {_C['off']}" if good else
           f"{_C['warn']}WARN{_C['off']}" if warn_only else
           f"{_C['bad']}FAIL{_C['off']}")
    print(f"  {tag} {name}" + (f" —— {detail}" if detail else ""))
    if not warn_only:
        results.append((name, good, detail))
    return good


def probe(name: str, fn, warn_only: bool = False):
    """跑一段可能抛异常的检查,把异常收成一条失败而不是让脚本挂掉。"""
    try:
        cond, detail = fn()
        chk(name, cond, detail, warn_only)
        return cond
    except Exception as exc:                       # noqa: BLE001
        chk(name, False, f"{type(exc).__name__}: {exc}", warn_only)
        if "-v" in sys.argv:
            traceback.print_exc()
        return False


# ═══════════════════════════════════════════════ 1. 平台和依赖

head("平台和依赖")
import platform_id

print(f"       {platform_id.NAME} / Python "
      f"{'.'.join(map(str, sys.version_info[:3]))} / {sys.executable}")
chk("平台是认识的", platform_id.IS_WIN or platform_id.IS_MAC, platform_id.NAME)

def _ver(mod: str) -> str:
    """版本号从包元数据读,不读模块的 __version__ —— Flask 3.1 起那个会警告。"""
    try:
        from importlib.metadata import version
        return version(mod)
    except Exception:                              # noqa: BLE001
        return ""


for mod, dist, why in (("requests", "requests", "Canvas API"),
                       ("flask", "flask", "本地后端"),
                       ("webview", "pywebview", "窗口")):
    probe(f"{mod}({why})",
          lambda m=mod, d=dist: (importlib.import_module(m) is not None, _ver(d)))

if platform_id.IS_MAC:
    # pyobjc 是分包的,少一个就少一整块功能,所以逐个点名
    for mod, why in (("objc", "pyobjc-core"),
                     ("AppKit", "pyobjc-framework-Cocoa:窗口和视图"),
                     ("Quartz", "pyobjc-framework-Quartz:位图合成"),
                     ("WebKit", "pyobjc-framework-WebKit:pywebview 用")):
        probe(f"{mod}({why})",
              lambda m=mod: (importlib.import_module(m) is not None, ""))

# ═══════════════════════════════════════════════ 2. 分发层

head("原生层:分发挑对了吗")


def _dispatch():
    import native_window as nw
    import orb_window
    import toast
    want = "win32" if platform_id.IS_WIN else "cocoa"
    got = orb_window.OrbWindow.__module__
    return (want in got and want in toast.Toast.__module__,
            f"orb={got} toast={toast.Toast.__module__}")


probe("悬浮球和弹窗挑了本平台的宿主", _dispatch)


def _surface():
    """两套实现的函数面必须一样 —— 少一个就是运行时才炸。"""
    import native_window as nw
    need = """animate_rect animate_to close cursor_pos dpi_scale render_scale
    drag_end drag_move drag_start find_own_window get_pre_expand get_rect hide
    is_expanded is_foreground is_iconic is_maximized is_topmost is_visible
    is_window minimize nudge_onscreen restore set_geometry set_pre_expand
    set_rect set_size set_topmost set_window_alpha set_window_icon show
    toggle_maximize unzoom work_area""".split()
    miss = [n for n in need if not callable(getattr(nw, n, None))]
    return not miss, str(miss) if miss else f"{len(need)} 个函数都在"


probe("native_window 的函数面齐全", _surface)


def _scales():
    import native_window as nw
    d, r = nw.dpi_scale(0), nw.render_scale(0)
    if platform_id.IS_MAC:
        # macOS:窗口用点,位图按 Retina 超采样 —— 这两个数就该不一样
        return d == 1.0, f"dpi_scale={d}(该是 1.0) render_scale={r}"
    return d == r, f"两个都是 {d}(Windows 上必须相等)"


probe("两个缩放的语义对", _scales)

# ═══════════════════════════════════════════════ 3. 共用渲染层

head("共用渲染层(两个平台同一份代码)")


def _orb_render():
    import orb_render as R

    class Canvas:
        def __init__(self, w, h):
            import array
            self.w, self.h = w, h
            self.px = array.array("I", bytes(w * h * 4))

        def draw_text_centered(self, *a, **k):
            self.asked = True

    n = R.canvas_size(36, 13)
    f = R.render_ball(Canvas(n, n), 36, 13, dark=False)
    nz = sum(1 for i in range(n * n) if f.px[i])
    # 最外一圈必须全透明:阴影要在位图边界之前衰减完,否则那一刀切出来是方块
    edge = max([f.px[i] >> 24 for i in range(n)]
               + [f.px[(n - 1) * n + i] >> 24 for i in range(n)])
    R.draw_badge(f, 36, 13, 7)
    cx, cy, br = R.badge_geometry(36, 13)
    solid = (f.px[cy * n + cx] >> 24) == 0xFF
    return (n == 62 and nz > 2600 and edge == 0 and solid,
            f"画布 {n}x{n},非透明 {nz},最外圈 alpha {edge},角标实心 {solid}")


probe("球画得出来,投影在边界归零", _orb_render)


def _toast_render():
    import toast_render as T
    m = T.metrics(1.0)
    h = T.card_height(40, 1.0) + m["mar"] * 2
    buf = bytearray(m["w"] * h * 4)
    mask = T.paint_card(buf, m["w"], h, 1.0, dark=True)
    top = max(buf[(0 * m["w"] + x) * 4 + 3] for x in range(m["w"]))
    bot = max(buf[((h - 1) * m["w"] + x) * 4 + 3] for x in range(m["w"]))
    inner = mask[(h // 2) * m["w"] + m["w"] // 2]
    return (m["w"] == 380 and top == 0 and bot == 0 and inner == 255,
            f"卡片 {m['w']}x{h},上下外圈 alpha {top}/{bot},内部遮罩 {inner}")


probe("弹窗卡片画得出来,外阴影归零", _toast_render)

# ═══════════════════════════════════════════════ 4. 桌面杂事

head("和桌面环境打交道")
import desktop

probe("读得到系统深浅色",
      lambda: (isinstance(desktop.system_dark(), bool),
               f"现在是{'深色' if desktop.system_dark() else '浅色'}"))
probe("自启目录", lambda: (desktop.autostart_dir().exists(),
                           str(desktop.autostart_dir())), warn_only=True)
probe("应用图标在",
      lambda: (desktop.app_icon(HERE / "gui") is not None,
               str(desktop.app_icon(HERE / "gui"))
               or ("缺 gui/icon.icns —— 跑 python make_icon.py --icns"
                   if platform_id.IS_MAC else "缺 gui/icon.ico")))

import chat_bridge

probe("找得到 claude CLI",
      lambda: (bool(chat_bridge.find_claude()),
               chat_bridge.find_claude() or "PATH 和兜底位置都没有 —— "
               "对话和简报会用不了"))

# ═══════════════════════════════════════════════ 5. 凭据

head("凭据(在项目目录之外,不会被同步走)")
cfg = platform_id.config_dir()
probe("Canvas token 文件", lambda: ((cfg / "config.json").exists(),
                                    str(cfg / "config.json")))
if platform_id.IS_MAC and (cfg / "config.json").exists():
    import stat
    mode = stat.S_IMODE((cfg / "config.json").stat().st_mode)
    chk("token 文件只有自己能读", mode & 0o077 == 0, f"权限 {oct(mode)}")
probe("邮箱凭据", lambda: ((cfg / "mail.json").exists(),
                           str(cfg / "mail.json") + "(没配的话在应用里设置)"),
      warn_only=True)

# ═══════════════════════════════════════════════ 6. 后端

head("本地后端(不开窗口)")


def _backend():
    import urllib.error
    import urllib.request
    import server
    port = server.free_port()
    threading.Thread(target=server.serve, args=(port,), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            urllib.request.urlopen(f"{base}/api/dashboard?k={server.TOKEN}",
                                   timeout=2).read(1)
            break
        except urllib.error.HTTPError:
            break
        except Exception:                          # noqa: BLE001
            time.sleep(0.15)

    def get(path):
        try:
            r = urllib.request.urlopen(base + path, timeout=8)
            return r.status, len(r.read())
        except urllib.error.HTTPError as e:
            return e.code, 0
        except Exception as e:                     # noqa: BLE001
            return str(e), 0

    c1, n1 = get(f"/api/dashboard?k={server.TOKEN}")
    chk("后端应答 /api/dashboard", c1 == 200, f"{c1},{n1} 字节")
    c2, _ = get("/api/dashboard")
    chk("token 闸门挡住没带 key 的请求", c2 == 403, str(c2))
    c3, n3 = get("/")
    chk("静态页加载得了", c3 == 200, f"{c3},{n3} 字节")
    c4, n4 = get(f"/api/memos?k={server.TOKEN}")
    chk("备忘录接口", c4 == 200, f"{c4},{n4} 字节")
    c5, n5 = get(f"/api/schedule?k={server.TOKEN}")
    chk("课程表接口", c5 == 200, f"{c5},{n5} 字节")
    return True, f"端口 {port}"


probe("后端起得来", _backend)

if not QUICK:
    def _canvas():
        from canvas_api import CanvasClient
        return True, CanvasClient().whoami()["name"]

    probe("Canvas 连得上", _canvas)

    def _courses():
        """顺手把学期筛子的结果报出来 —— 被问到"我的课怎么少了一门"时,
        这一行直接说明筛掉了几门往期的。"""
        from canvas_api import CanvasClient
        c = CanvasClient()
        cur, whole = c.courses(), c.courses(False)
        drop = len(whole) - len(cur)
        note = f"这学期 {len(cur)} 门"
        if drop:
            note += f",筛掉 {drop} 门往期的"
        # 只有一种情况算没过:有课但全被筛掉了 —— 那是学期代码判断出了岔子
        return bool(cur or not whole), note

    probe("课程列表", _courses)

# ═══════════════════════════════════════════════ 汇总

bad = [n for n, good, _ in results if not good]
print(f"\n{'─' * 52}")
if bad:
    print(f"{_C['bad']}{len(results) - len(bad)}/{len(results)} 项通过,"
          f"以下没过:{_C['off']}")
    for n in bad:
        print(f"  · {n}")
else:
    print(f"{_C['ok']}{len(results)}/{len(results)} 项全过。{_C['off']}")

print(f"""
{_C['head']}下面这几条脚本验不了,要你自己在屏幕上看一眼:{_C['off']}
  1. 开应用 -> 窗口出来了,界面是玻璃质感、不是无样式的裸 HTML
  2. 双击界面 -> 全屏/还原有过渡动画,不是瞬间跳
  3. 按那个按钮收成悬浮球 -> **页面先缩小再变成球**,球是真的圆、没有方边
  4. 鼠标移到球上 -> 会变大变亮;停一秒 -> 弹出备忘录窄栏
  5. 拖动球 -> 跟手,松手后位置被记住(下次从那儿弹出来)
  6. 球上右键 -> 菜单能弹,"展开面板"和"退出"都管用
  7. 设置 -> 通用 -> 信息弹窗 -> 「试一条」-> 右下角弹出来,**文字看得清**、
     底部阴影是柔和化开的(不是一块方影),深色模式下是深卡片浅字
  8. 重启一次 -> 登录后自动开一次(同一天再登录不重复开)
""")
sys.exit(1 if bad else 0)
