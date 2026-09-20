# -*- coding: utf-8 -*-
"""NEU Helper 桌面应用入口。

起本地服务 + 开一个无边框 WebView2 窗口。业务逻辑都在 server.py,
Canvas 逻辑复用 canvas_api.py。

窗口是无边框的。三态里有两态(对话框 / 完整面板)是这个 WebView 窗口本身,
第三态**悬浮球是另一个窗口** —— orb_window.py 自己画的分层窗口。收成球的时候
WebView 窗口整个隐藏掉,所以球是真的圆、也不会有白边(理由见那个文件的开头)。

**刻意完全不挂 js_api。** 实测只要挂上,这个页面就会把窗口卡死成「未响应」:
控制其余全部变量不变,不挂 responding=True、挂上 responding=False。
详见 native_window.py 的模块文档。
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# 打包之后 data/ 要在 exe 旁边(可写),不能在临时解包目录里 ——
# 理由见 server.py 里同一处的注释。没打包时两者一样。
if getattr(sys, "frozen", False):
    HERE = Path(sys.executable).resolve().parent
else:
    HERE = Path(__file__).resolve().parent
LOG = HERE / "data" / "app.log"


def _maybe_apply_update() -> int | None:
    """`--apply-update <装在哪> <旧进程 pid>`:这一份是**新版的 exe**,
    它的任务是把自己拷到安装目录再把那边拉起来,然后退出。

    **必须在最前面处理** —— 这个模式下不能起服务、不能抢单实例的锁、
    更不能开窗口。判断只看 argv,不 import 任何重东西。
    """
    if "--apply-update" not in sys.argv:
        return None
    i = sys.argv.index("--apply-update")
    try:
        target, pid = sys.argv[i + 1], int(sys.argv[i + 2])
    except (IndexError, ValueError):
        return 2
    import updater
    return updater.apply_update(Path(target), pid)


def _ensure_streams() -> None:
    """pythonw.exe 下 sys.stdout / sys.stderr 是 None。

    这不是小事:任何 print、或者第三方库内部往 stderr 写日志,都会炸
    AttributeError,而且因为没有控制台,你看不到任何线索 —— 表现就是
    「进程在跑但窗口不出来」。统一重定向到 data/app.log。

    必须在 import webview 之前做完。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    LOG.parent.mkdir(exist_ok=True)
    stream = open(LOG, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


_ensure_streams()

import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

import webview  # noqa: E402

import desktop  # noqa: E402
import native_window  # noqa: E402
import orb_window  # noqa: E402
import platform_id  # noqa: E402
import server  # noqa: E402
import toast  # noqa: E402

TITLE = "NEU Helper"

# 逻辑像素。收成球之前窗口会先缩到 170x170 再隐藏(一个连续的动作),
# 所以 min_size 必须比那个更小,否则 WinForms 的 MinimumSize 会把它卡住。
MODES = {
    "chat": (400, 620),
    "full": (1180, 780),
}
# 收球的时候窗口要一路缩到**球本体**那么大(默认 36px 逻辑,最小 26px),
# 所以下限必须比那个还小 —— WinForms 的 MinimumSize 会硬卡住 SetWindowPos。
MIN_SIZE = (16, 16)

def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def wait_for_server(port: int, timeout: float = 20.0) -> bool:
    """等 HTTP 服务能应答再开窗,否则窗口会先白屏一下。"""
    url = f"http://127.0.0.1:{port}/api/dashboard?k={server.TOKEN}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).read(1)
            return True
        except urllib.error.HTTPError:
            return True          # 403/500 也说明服务活着
        except Exception:
            time.sleep(0.15)
    return False


def setup_orb() -> None:
    """等 WebView 窗口出来,再把悬浮球装上。

    球要按显示器的缩放比画,而那个得从主窗口问,所以必须等窗口先存在。
    窗口一般 2 秒内出来,这里最多等 15 秒。

    **macOS 上还要多等一件事:主线程的 run loop。** AppKit 的窗口只能在
    主线程上建,而这个函数跑在后台线程 —— 得等 `webview.start()` 把 run loop
    转起来,投过去的活儿才有人执行。不等的话建球那一步会卡到超时、
    然后退回本线程直接调 AppKit,那是不安全的。
    """
    if platform_id.IS_MAC:
        import native_cocoa
        if not native_cocoa.wait_for_main_loop(20):
            log("主线程 run loop 没起来,悬浮球和弹窗都不装了")
            return
    h = 0
    for _ in range(150):
        h = server._hwnd()
        if h:
            break
        time.sleep(0.1)
    if not h:
        log("等不到窗口句柄,悬浮球没装上")
        return
    # 图标格式两个平台不同(.ico / .icns),挑哪个在 desktop 里
    ico = desktop.app_icon(server.GUI)
    if ico and native_window.set_window_icon(h, str(ico)):
        log(f"窗口图标已挂上({ico.name})")

    prefs = server.read_prefs()
    try:
        o = orb_window.OrbWindow(
            # 单击 = 还原成收起来之前那个形态(从大窗口收的就还原成大窗口);
            # 双击 = 不管之前是什么,直接展开完整面板
            on_click=lambda: server.apply_mode(server.restore_mode()),
            on_expand=lambda: server.apply_mode("full"),
            on_quit=lambda: native_window.close(h),
            # 拖完记住位置,下次从这儿弹出来
            on_moved=lambda x, y: server.write_prefs({"orbX": x, "orbY": y}),
            # 停在球上超过一秒 = 想看备忘录,弹一条窄栏出来
            on_hold=lambda: server.backend.peek_on(),
            on_unhover=lambda: None,   # 收起来由看门线程判断(指针可能移到浮窗上)
            diameter=int(prefs["orbSize"]),
            # **render_scale 不是 dpi_scale。** 球要的是"一个窗口单位画几个
            # 位图像素":Windows 上窗口 API 收物理像素,两者相等;macOS 上
            # 窗口用点、位图要按 Retina 的 backingScaleFactor 超采样,不等。
            # 用错的话球在 Retina 上是糊的(或者尺寸直接翻倍)
            scale=native_window.render_scale(h),
            dark=server.dark_mode(),
            # 设置里关掉过渡动画,球里的光晕也跟着停(一个定时器都不留)
            animate=bool(prefs.get("anim", True)),
        )
    except Exception:
        log("悬浮球初始化失败:" + chr(10) + traceback.format_exc())
        return
    server.attach_orb(o)
    log(f"悬浮球就绪 hwnd={o.hwnd} 直径={o.diameter}px")
    # 右下角那个信息弹窗。点它 = 把应用叫回到上次那个形态
    try:
        server.backend.toast = toast.Toast(
            on_click=lambda: server.apply_mode(server.restore_mode()),
            scale=native_window.render_scale(h),   # 同上,见球那里的注释
            dark=server.dark_mode())
        log(f"信息弹窗就绪 hwnd={server.backend.toast.hwnd}")
    except Exception:
        log("信息弹窗没装上:" + chr(10) + traceback.format_exc(limit=3))


def already_running(after_update: bool = False) -> bool:
    """已经有一个在跑吗?有就把那个窗口叫到前面来,然后让这次启动退掉。

    两个平台都选了"进程死了锁自动释放"的机制(Windows 命名互斥体、
    macOS flock 锁文件)—— pid 文件那种做法会在崩溃后留一把锁在那儿,
    下次就再也起不来了。具体实现在 desktop.py。

    放在这里(而不是某个启动脚本里)是因为启动路径有好几条:桌面快捷方式、
    开机闸门、命令行 —— 守在入口才守得住全部。
    """
    if desktop.acquire_single_instance("NEUHelper"):
        return False
    # **更新之后重启要多等一会儿。** 刚被替换掉的那个进程可能还在收尾,
    # 锁还没松开 —— 这时候直接判定"已经有一个在跑"然后退出,用户看到的是
    # "点了更新,然后应用就没了"。踩过一次,所以这里要重试。
    if after_update:
        for _ in range(40):                        # 最多 20 秒
            time.sleep(0.5)
            if desktop.acquire_single_instance("NEUHelper"):
                log("更新后重启:等到锁释放了")
                return False
        log("更新后重启:等了 20 秒锁还占着,放弃")
        return True
    log("已经有一个实例在跑,把它的窗口叫到前面")
    try:
        desktop.raise_existing(TITLE)
    except Exception:
        pass
    return True


def _sweep_old() -> None:
    """清掉更新留下的 *.old(一个 44MB 的旧 exe,留着白占地方)。

    更新时旧的 exe 会先被改名成 .old 再拷新的。刚重启那会儿系统往往**还锁着
    它**(它是上一秒才退出那个进程的镜像),所以第一下删不掉。

    所以在后台隔一会儿再试几次,而不是只试一次就留到下次启动 ——
    实测重启后立刻删是失败的,几秒之后就能删掉。全都失败也无所谓:
    下次启动还会再来一轮。
    """
    def sweep() -> bool:
        left = list(HERE.glob("*.old"))
        for p in left:
            try:
                p.unlink()
                log(f"清掉更新残留 {p.name}")
            except OSError:
                return False
        return True

    def later() -> None:
        for wait in (0, 4, 15, 45):
            time.sleep(wait)
            try:
                if sweep():
                    return
            except Exception:
                return

    threading.Thread(target=later, daemon=True, name="sweepold").start()


def main() -> int:
    code = _maybe_apply_update()
    if code is not None:
        return code
    # 清理放在单实例判断**之前** —— 撞上"已经有一个在跑"就直接退的话,
    # 那个 44MB 的残留永远没人清
    _sweep_old()
    if already_running(after_update="--after-update" in sys.argv):
        return 0
    if not (server.GUI / "index.html").exists():
        log(f"缺少前端文件: {server.GUI / 'index.html'}")
        return 1

    port = server.free_port()
    log(f"启动本地服务,端口 {port}")
    threading.Thread(target=server.serve, args=(port,), daemon=True).start()

    if not wait_for_server(port):
        log("本地服务启动超时")
        return 1

    # 调度线程只在真的开应用时启动 —— 装机自检里也会 import server 并起服务,
    # 那里不该顺手生成一份简报把额度花掉。
    server.backend.briefings.start_scheduler()
    log("简报调度已启动")

    # 课件同步:开机一次 + 每小时一次。增量的,没变的文件一个字节都不传。
    if server.read_prefs().get("autoSync", True):
        server.backend.sync.start_scheduler(server.backend.sync_courses)
        log("课件同步调度已启动")

    # 课表保鲜:每小时重抽一次,但只在已经手动解析过之后。源文没变不调模型,
    # 所以这一轮通常是白跑的 —— 值就值在老师改了时间你不用自己发现
    server.backend.start_schedule_watch()
    log("课表保鲜已启动")

    # 邮箱:3 分钟一轮拉收件箱 + 每天一次邮件简报(和课业那份完全分开)
    server.backend.mail_fetcher.start_scheduler(
        lambda: bool(server.read_prefs().get("mailOn", True)))
    server.backend.mail_briefings.start_scheduler()
    # IMAP IDLE:服务器一有新信就推,不用等那一轮轮询(轮询留着当兜底)
    server.backend.mail_idle.start()
    log("邮箱 IDLE 监听已启动")
    log("邮箱调度已启动")

    # 检查更新。开机等一会儿再查 —— 启动那几秒要留给窗口和仪表盘
    server.backend.start_update_watch()

    threading.Thread(target=setup_orb, daemon=True).start()

    url = f"http://127.0.0.1:{port}/?k={server.TOKEN}"
    # 排障/测试用:CANVAS_HELPER_MODE=orb 可以直接以某个形态启动
    start_mode = os.environ.get("CANVAS_HELPER_MODE", "")
    if start_mode in MODES or start_mode == "orb":
        url += f"&mode={start_mode}"
    # CANVAS_HELPER_PANEL=prefs 启动就把设置面板打开,方便直接看它
    panel = os.environ.get("CANVAS_HELPER_PANEL", "")
    if panel:
        url += f"&panel={panel}"
    # CANVAS_HELPER_PAGE=mail 启动就切到邮箱子页面
    page = os.environ.get("CANVAS_HELPER_PAGE", "")
    if page:
        url += f"&page={page}"
    # CANVAS_HELPER_COURSE=<id> 启动就切到课程单页(配 CANVAS_HELPER_SEG=hw|mat|ann)
    course = os.environ.get("CANVAS_HELPER_COURSE", "")
    if course:
        url += f"&course={course}&seg={os.environ.get('CANVAS_HELPER_SEG', 'hw')}"
    log(f"打开窗口 -> {url}")

    webview.create_window(
        TITLE,
        url,
        width=MODES["full"][0],
        height=MODES["full"][1],
        min_size=MIN_SIZE,
        # 排障开关:CANVAS_HELPER_FRAMED=1 退回带系统边框的窗口
        frameless=os.environ.get("CANVAS_HELPER_FRAMED") != "1",
        # frameless 下 easy_drag 默认 True,会导致在任何地方拖动都移动窗口
        # (列表没法滚、文字没法选)。拖拽自己实现,见 native_window.start_drag。
        easy_drag=False,
        # 和玻璃底色一致 —— 加载那一瞬间不会白闪一下
        background_color="#dfe5f0",
    )

    webview.start(debug=False)
    log("窗口已关闭,退出")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # pythonw 下没有控制台,不落盘就等于什么都没发生过
        log("未捕获异常:\n" + traceback.format_exc())
        sys.exit(1)
