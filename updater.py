# -*- coding: utf-8 -*-
"""检查更新 / 下载 / 就地替换。

分两种安装方式,能做的事不一样:

  打包版(从 Release 下的)   下载新的 zip -> 解压 -> 换掉 exe -> 重启
  源码版(git clone 的)      git fetch + merge --ff-only -> 重启

════════════════════════════════════════════════════════════════════
**怎么替换一个正在运行的程序。**

Windows 不让删正在跑的 exe;macOS 的 .app 是个带签名的整体,也不能拆着换。
常见做法是甩一个 .cmd/.sh 出去等进程退出再拷,但那会闪一个黑框,而且脚本
本身还得自己清理。

这里用另一个办法:**让新版的程序自己来装自己**。

  1. 下载 zip,解压到暂存目录
  2. 起 `新程序 --apply-update <装在哪> <当前进程的 pid>`
  3. 本进程退出
  4. 那个新进程等旧进程死掉,把自己装到位,再把装好的那个拉起来

新版的程序自带全部依赖,所以它在暂存目录里就能独立跑 —— 不需要任何外部
脚本。app.py 在最开头就会认这个参数,不会去开窗口。

**两个平台在第 4 步分岔**(骨架、握手、回滚都是共用的):

  Windows  逐个文件拷进安装目录。exe 放最后拷 —— 版本号是从它读的,
           它一换更新就"生效"了,所以前面任何一样没拷成,回滚之后
           应用还是完完整整的旧版。见 apply_update
  macOS    **整包替换**:把新 .app 挪到位,旧的改名留着当退路。
           不能逐个文件换 —— 那会破坏 bundle 的封印,而 Apple 芯片上
           连 ad-hoc 签名都是执行的前提。见 _apply_update_mac

**data/ 和 CLAUDE.md 不动。** 前者是你的邮件、对话、课件;后者是你自己的
课程表。Windows 上它们在安装目录里,拷的时候显式跳过;macOS 上它们在
`.app/Contents/MacOS/` 里面 —— 整包换掉之前必须先搬进新 bundle,
不然一次更新就把所有数据清了。

也因为这个,**macOS 的暂存目录在 bundle 外面**(`~/.canvas-helper/update`):
放里面的话,一挪旧 bundle 就把暂存、以及正在跑的那个暂存进程自己的可执行
文件一起挪走了。见 stage_root。
════════════════════════════════════════════════════════════════════

源码版走的是另一条路(_run_git),短得多:快进到 origin 上的那个提交,
然后重启。**不 stash、不 reset、不 checkout** —— 只做快进这一种操作。
改动过的文件、和远端分叉了的提交,一律停下来照实说,而不是替用户做决定
把他的东西弄丢。`data/` 和 `CLAUDE.md` 在 .gitignore 里,git 本来就不碰。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import applang
import platform_id
import version as ver
from desktop import NO_WINDOW as _NO_WINDOW   # 起子进程不闪黑框

# 检查更新的间隔。**不要更频繁** —— GitHub 对未认证请求是每小时 60 次,
# 而且这事一天知道一次就够了
CHECK_EVERY = 6 * 3600
TIMEOUT = 20

# 装好之后**绝对不能被更新覆盖**的东西 —— 这些是"你的",不是"程序的"。
# zip 里本来就没有它们,这里是第二道闸。
#
#   顶层的按名字挡(下面循环比的就是顶层名字)
KEEP = {"data", "CLAUDE.md"}
#   目录里面的按相对路径挡。copytree 是合并式的,所以光靠 KEEP 挡不住
#   ——必须在 copytree 的时候显式跳过
KEEP_INSIDE = {(".claude", "settings.local.json")}

# 新版 exe 一进门就写这个文件 —— 旧进程等到它才敢退。见 Updater._run。
STARTED = "started"
# "我打算更新到哪一版"。**放在 data/ 下、不放在 data/update/ 里** ——
# 后者每次更新开头会被整个删掉,而这张纸条必须活过重启。
PENDING = "update_pending.json"


def _asset_name() -> str:
    """本平台该下哪个附件。"""
    if platform_id.IS_MAC:
        import platform
        return f"NEU-Helper-mac-{platform.machine()}.zip"
    return "NEU-Helper-win-x64.zip"


def install_kind(here: Path) -> str:
    """这是怎么装的。

    packaged  从 Release 下的打包版(下新 zip 换 exe)
    git       git clone 的源码版(fetch + merge --ff-only)
    source    源码但没有 .git(无从更新起,只能提示去下载)
    """
    if getattr(sys, "frozen", False):
        return "packaged"
    return "git" if (here / ".git").is_dir() else "source"


# ─────────────────────────── 查 ───────────────────────────


# 一次拉多少个 Release。要的是"从你这个版本到最新版之间的全部更新说明",
# 所以不能只问 releases/latest —— 落后三个版本的人该看到三份说明
RELEASES_PER_PAGE = 30


def _get_json(path: str):
    """GET 一个 GitHub 接口。失败抛异常,调用方兜。"""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{ver.REPO}{path}", headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"NEUHelper/{ver.VERSION}",
        })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read() or b"{}")


# ---------------------------------------------------------------- 代理

# git 认的是 http.proxy / https.proxy 这两条**配置**,和环境变量、和"我平时
# 能不能直连"都没关系。在国内装过 Clash 之类的人,这两条多半一直写在全局配置
# 里 —— 代理开着的时候它是快路,代理没开的时候每一条 git 网络操作都会卡在
#     Failed to connect to 127.0.0.1 port 7897
# 而报错里只字不提"这是你自己配的代理",从用户那头看就是"更新坏了"。
_PROXY_DEAD = re.compile(
    r"Failed to connect to .*? port \d+|Couldn't connect to proxy|"
    r"proxy CONNECT aborted|CONNECT tunnel failed", re.I)

# 绕过代理跑一次用的前缀。**只作用于这一条命令**,不动用户的配置 ——
# 他开着代理的时候那条配置是对的,不该被我们悄悄抹掉。
NO_PROXY_ARGS = ["-c", "http.proxy=", "-c", "https.proxy="]


def proxy_is_dead(out: str) -> bool:
    """这条 git 是不是栽在"代理连不上"上。"""
    return bool(_PROXY_DEAD.search(out or ""))


def _notes_since(releases: list, current: str) -> list[dict]:
    """比 current 新的那些 Release 的说明,新的在前。

    **为什么要全部而不只是最新那个。** 一个落后三个版本的人看到的"更新内容"
    应该是这三个版本加起来改了什么 —— 只给最新那份,中间两版做了什么他永远
    不知道,而那里面可能正有他一直在等的修复。
    """
    out = []
    for r in releases:
        if r.get("draft") or r.get("prerelease"):
            continue
        tag = str(r.get("tag_name") or "")
        if not tag or not ver.is_newer(tag, current):
            continue
        out.append({
            "version": tag.lstrip("vV"),
            "date": (r.get("published_at") or "")[:10],
            # 6000 而不是 2000:一篇像样的更新说明轻松过两千字,截在
            # 2000 会把正文腰斩(v3.0.0 那篇 3330 字,英文那一段整个没了)。
            # 面板本来就是可滚的,而这点体积在一次 HTTPS 往返里不值一提。
            "notes": (r.get("body") or "").strip()[:6000],
        })
    out.sort(key=lambda x: ver.parse(x["version"]), reverse=True)
    return out


def check() -> dict:
    """问一下 GitHub 有没有新版本。

    只读公开的 Releases 接口,不带任何凭据 —— 但**这会把你的 IP 告诉
    GitHub**,所以设置里能关掉(prefs.updateCheck)。

    拉的是 releases 列表(不是 releases/latest):除了"有没有新版本",
    还要凑出**从你这个版本到最新版之间每一版的说明**,见 _notes_since。
    """
    try:
        lst = _get_json(f"/releases?per_page={RELEASES_PER_PAGE}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"GitHub 返回 {e.code}"}
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(lst, list):
        return {"ok": False, "error": "GitHub 返回的不是列表"}
    live = [r for r in lst if not r.get("draft") and not r.get("prerelease")]
    if not live:
        # 一个 Release 都没发过 —— 那就是"已经最新",不是错误
        return {"ok": True, "current": ver.VERSION, "latest": "", "newer": False,
                "notes": "", "history": [], "page": "", "asset": "", "size": 0,
                "published": "", "checked": time.strftime("%Y-%m-%d %H:%M")}
    live.sort(key=lambda r: ver.parse(str(r.get("tag_name") or "")), reverse=True)
    d = live[0]
    history = _notes_since(live, ver.VERSION)

    tag = str(d.get("tag_name") or "")
    want = _asset_name()
    asset = next((a for a in (d.get("assets") or [])
                  if a.get("name") == want), None)
    return {
        "ok": True,
        "current": ver.VERSION,
        "latest": tag.lstrip("vV"),
        "newer": ver.is_newer(tag),
        "notes": (d.get("body") or "")[:4000],
        # 你这个版本到最新版之间每一版的说明(新的在前)。横幅上那句
        # 「更新内容如下」念的就是它
        "history": history,
        "page": d.get("html_url") or "",
        "asset": (asset or {}).get("browser_download_url") or "",
        "size": (asset or {}).get("size") or 0,
        "published": (d.get("published_at") or "")[:10],
        "checked": time.strftime("%Y-%m-%d %H:%M"),
    }


def mark_pending(here: Path, want: str) -> None:
    """记一笔"我打算从 A 变成 B"。在旧进程退出之前写。"""
    if not want:
        return
    try:
        p = Path(here) / "data" / PENDING
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "from": ver.VERSION, "to": want, "where": str(here),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass                                       # 记不下也不该挡住更新


def take_pending(here: Path) -> dict:
    """启动时核对上一次更新到底成没成。成了就把纸条撕掉。

    **这是整条链上唯一一处能说真话的地方。** 换文件的活是另一个进程干的,
    它干完就退了 —— 它成功与否,发起更新的那个进程永远看不到(那时候它已经
    `os._exit(0)` 了)。所以只能等装好的这一份启动起来,拿自己的版本号和
    纸条上写的那个比一比。

    对得上 -> 真的更新了,撕掉纸条。
    对不上 -> 界面上说清楚:你点的是 3.2.3,现在跑的还是 3.2.0,
              日志在哪、装在哪都一并给出来,别让人对着一个没变的版本号发呆。
    """
    p = Path(here) / "data" / PENDING
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict) or not d.get("to"):
        p.unlink(missing_ok=True)
        return {}
    if ver.parse(ver.VERSION) >= ver.parse(str(d["to"])):
        p.unlink(missing_ok=True)                  # 到位了(或者更新)
        return {}
    d["log"] = _log_tail(Path(here))
    d["now"] = ver.VERSION
    return d


def _log_tail(here: Path, lines: int = 12) -> str:
    try:
        txt = (here / "data" / "update.log").read_text(encoding="utf-8")
    except OSError:
        return ""
    return chr(10).join(txt.strip().splitlines()[-lines:])


def _run_quiet(cmd: list[str], timeout: int = 60) -> bool:
    """跑一条外部命令,失败也不抛 —— 都是"尽力而为"的收尾动作
    (清隔离属性、补执行位),成不成都不该让整次更新失败。"""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception:                              # noqa: BLE001
        return False


def bundle_of(here: Path) -> Path | None:
    """`here` 在某个 .app 里面的话,返回那个 .app 的路径。

    打包版的 macOS 上 `here` 是 `NEU Helper.app/Contents/MacOS` ——
    要换的是整个 .app,不是这一层。
    """
    if not platform_id.IS_MAC:
        return None
    for p in (here, *here.parents):
        if p.suffix == ".app":
            return p
    return None


def stage_root(here: Path) -> Path:
    """解压和暂存放哪儿。

    Windows 放 `<装在哪>/data/update`,就在安装目录里,顺手。

    **macOS 不能这么放。** 那边 `data/` 在 `.app/Contents/MacOS/` 里面,
    而替换的做法是"把整个 .app 换掉" —— 暂存目录要是也在 .app 里,
    一挪旧 bundle 就把暂存(以及正在跑的那个暂存进程自己的可执行文件)
    一起挪走了。所以 macOS 放到 `~/.canvas-helper/update`,在 bundle 外面。
    """
    if bundle_of(here) is not None:
        return platform_id.config_dir() / "update"
    return Path(here) / "data" / "update"


def where_problem(here: Path) -> str:
    """这个安装目录能就地更新吗。不能就返回一句人话,能就返回空串。

    **这一条是照着一个真实故障写的**:有人点更新,界面说装好了 v3.2.3,
    重启回来还是 v3.2.0,反复如此。机制本身没毛病(拿真包端到端量过,
    6 项全拷成、exe 确实换了),问题在于他跑的那一份根本不在他以为的地方。

    两种走法都会这样:

    - **在压缩包里直接双击 exe**。资源管理器会把 zip 解到
      `%TEMP%` 下面一个 `Temp1_xxx.zip` 目录再运行 —— 更新确实写进了那儿,
      可下次从压缩包打开,又是原样解压一份旧的出来
    - 解压在了 Program Files 之类的地方,当前用户写不进去

    第二种在拷文件时会报错(至少还看得见),第一种是彻头彻尾的静默失败:
    每一步都成功,就是不生效。所以只能在动手之前拦。
    """
    here = Path(here)
    # macOS 打包版:换的是整个 .app,所以要问的是"它所在的目录能不能写",
    # 而不是 bundle 里面能不能写(那是两件事:/Applications 下的 .app
    # 里面往往可写,但目录本身归 root,换不了)
    app = bundle_of(here)
    probe_dir = app.parent if app is not None else here
    try:
        probe = probe_dir / ".write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        why = exc.strerror or exc
        return applang.tr(
            f"装不进去:{probe_dir} 写不了({why})。"
            "把整个文件夹挪到你自己的目录下(比如「文档」)再试。",
            f"Cannot install: {probe_dir} is not writable ({why}). "
            "Move the whole folder somewhere you own (Documents, say) "
            "and try again.")
    # 临时目录。TEMP 本身就是一个很深的路径,所以比的是"在不在它下面",
    # 不是相等。**TMPDIR 是 macOS/Linux 那边的名字** —— 只认 TEMP/TMP 的话
    # 这道闸在非 Windows 上等于不存在
    for var in ("TEMP", "TMP", "TMPDIR"):
        root = os.environ.get(var)
        if not root:
            continue
        try:
            if here.resolve().is_relative_to(Path(root).resolve()):
                return applang.tr(
                    "你这一份是**从压缩包里直接运行**的(它在系统临时目录里)。"
                    "更新会写进去,但下次打开压缩包又是旧的那份 —— 先把 zip "
                    "里那个文件夹整个解压到别处,从那儿启动,再更新。",
                    "This copy is running straight out of the zip (it sits in "
                    "the system temp folder). An update would be written there, "
                    "but opening the zip again just unpacks the old one - "
                    "extract the folder from the zip somewhere else, start it "
                    "from there, then update.")
        except (OSError, ValueError):
            pass
    return ""


def disk_version(here: Path) -> str:
    """**磁盘上** version.py 写的版本号。

    和内存里的 VERSION 不一样,就说明代码已经换过了、而这个进程还是旧的
    (git pull 过、或者开发时直接改了文件)。**不联网**就能回答"要不要重启"
    —— 比拿 GitHub Release 去和内存里的版本比准得多:那个比法在"代码已经
    追平但进程没重启"的时候会说"有新版本",点下去却无事可做。
    """
    try:
        txt = (here / "version.py").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', txt, re.M)
    return m.group(1) if m else ""


def restart(here: Path) -> bool:
    """把自己重起一份。

    源码版有个别处没有的状态:**代码已经换成新的了,但跑着的这个进程还是
    旧的**(git pull 过、或者像开发时那样直接改了工作区)。那时候"更新"
    无事可做 —— 真正要做的是重启。所以这一步单独给出来。
    """
    entry = here / "app.py"
    if not entry.exists():
        return False
    subprocess.Popen([sys.executable, str(entry), "--after-update"],
                     cwd=str(here), close_fds=True, creationflags=_NO_WINDOW)
    return True


def check_git(here: Path) -> dict:
    """源码版:除了 Release,再看一眼 origin 上有没有新提交。

    **为什么要这一步。** check() 问的是 `releases/latest`,拿 tag 跟本地
    VERSION 比 —— 这对打包版是对的,但源码版的更新根本不经过 Release:
    代码一推上 main 就能快进拿到。不看 origin 的话,推上去的修复对源码版
    用户同样是静默的,他得自己想起来点一下「检查更新」。

    只做**读**:fetch + 数提交。`git fetch` 会动本地的远端引用,那是它的
    分内事;工作区、分支、HEAD 一概不碰(真要快进在 Updater._run_git 里,
    那里还会先查工作区脏不脏)。

    返回 {"behind": N, "upstream": "origin/main", "subjects": [...]};
    不是 git 仓库、没有上游、fetch 失败,一律返回 {"behind": 0} ——
    查不到新提交不是错误,不该拿它去烦人。
    """
    out: dict = {"behind": 0, "upstream": "", "subjects": []}
    exe = shutil.which("git")
    if not exe or not (here / ".git").is_dir():
        return out

    def git(*args: str, timeout: int = 60) -> tuple[int, str]:
        def run(pre):
            return subprocess.run(
                [exe, *pre, *args], cwd=str(here), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout,
                creationflags=_NO_WINDOW)
        try:
            p = run([])
            out = ((p.stdout or "") + (p.stderr or "")).strip()
            if p.returncode != 0 and proxy_is_dead(out):
                p = run(NO_PROXY_ARGS)          # 同上,只绕这一条命令
                out = ((p.stdout or "") + (p.stderr or "")).strip()
        except Exception:                          # noqa: BLE001
            return 1, ""
        return p.returncode, out

    code, upstream = git("rev-parse", "--abbrev-ref",
                         "--symbolic-full-name", "@{u}")
    if code != 0 or "/" not in upstream:
        return out
    out["upstream"] = upstream
    if git("fetch", "--quiet", upstream.split("/")[0])[0] != 0:
        return out
    code, behind = git("rev-list", "--count", f"HEAD..{upstream}")
    if code != 0 or not behind.isdigit():
        return out
    out["behind"] = int(behind)
    if out["behind"]:
        # 提交标题拿来当"改了什么" —— 源码版没有 Release notes 可看
        code, log = git("log", "--format=%s", "-3", f"HEAD..{upstream}")
        if code == 0:
            out["subjects"] = [ln.strip() for ln in log.splitlines() if ln.strip()]
    return out


# ─────────────────────────── 下 + 换 ───────────────────────────


class Updater:
    """一次更新的全过程。状态给界面看。"""

    def __init__(self, here: Path, on_event=None):
        self.here = Path(here)
        self.on_event = on_event
        self.state = {"phase": "idle", "pct": 0, "msg": "", "error": ""}
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        return dict(self.state)

    def _set(self, **kw) -> None:
        self.state.update(kw)
        if self.on_event:
            try:
                self.on_event(self.snapshot())
            except Exception:                      # noqa: BLE001
                pass

    def start(self, url: str, want: str = "") -> bool:
        """开始下载并安装。返回有没有真的开跑。

        `want` 是要装成哪一版 —— 只用来记一笔"我打算变成 3.2.3",
        重启回来核对不上就说出来。
        """
        if not url:
            # 这句话得能让人动手。会走到这儿的现实情况只有一种:**Intel Mac** ——
            # GitHub 已经下线了 Intel 的 macOS runner,CI 出不了 x86_64 的包
            # (见 .github/workflows/release.yml 里 macos 那个 job)。
            # 光说"没有本平台的安装包"等于把人堵在墙上。
            import platform as _plat
            arch = _plat.machine()
            if platform_id.IS_MAC and arch not in ("arm64",):
                self._set(phase="error", error=applang.tr(
                    f"没有 {arch} 这个架构的预构建包 —— GitHub 已经不提供 Intel 的"
                    "构建机了。在这台 Mac 上跑一次 ./packaging/build_mac.sh "
                    "就能自己出一个(几分钟),之后照样能用「检查更新」看版本。",
                    f"No prebuilt package for {arch} — GitHub no longer offers "
                    "Intel macOS runners. Run ./packaging/build_mac.sh once on "
                    "this Mac to build your own (a few minutes)."))
            else:
                self._set(phase="error", error=applang.tr(
                    "这个版本没有本平台的安装包",
                    "This release has no package for your platform"))
            return False
        # macOS 这条路**现在是通的**(见 _apply_update_mac)。原来这儿会直接
        # 早退、让用户去下载页 —— 那时候替换 .app 的流程还没写。
        bad = where_problem(self.here)
        if bad:
            # **先查再下。** 装不进去的话 25MB 下完了也是白下 —— 而且下完之后
            # 这个进程就退了,那时候再报错根本没人看得见
            self._set(phase="error", error=bad)
            return False
        if not self._lock.acquire(blocking=False):
            return False
        self._set(phase="downloading", pct=0, msg="正在下载…", error="")
        threading.Thread(target=self._run, args=(url, want), daemon=True,
                         name="update").start()
        return True

    # ─────────────────── 源码版:快进到最新提交 ───────────────────

    def start_git(self) -> bool:
        """源码版的更新。和打包版是两条完全不同的路,所以分开一个入口。"""
        if not self._lock.acquire(blocking=False):
            return False
        self._set(phase="checking", pct=0, msg="正在检查工作区…", error="")
        threading.Thread(target=self._run_git, daemon=True,
                         name="update-git").start()
        return True

    def _git(self, *args: str, timeout: int = 120, raw: bool = False):
        """跑一条 git。返回 (退出码, 输出)。

        默认把 stdout 和 stderr 拼起来再 strip —— 报错信息要的是这个。

        **但 `status --porcelain` 必须传 raw=True。** 它的第一列是"暂存区
        状态",没暂存的改动那一列**就是个空格**(` M server.py`),strip 会把
        它吃掉,于是按固定宽度切出来的文件名少一个字符,界面上显示成
        「本地改过这些文件:erver.py」。测试逮到过一次。
        """
        def run(pre):
            return subprocess.run(
                [self._git_exe, *pre, *args], cwd=str(self.here),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, creationflags=_NO_WINDOW)

        p = run([])
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        # 栽在"代理连不上"就绕过代理再来一次。**不改用户的配置** ——
        # 他开着代理的时候那条是对的,只是现在没开(见 proxy_is_dead 上面那段)
        if p.returncode != 0 and proxy_is_dead(out):
            print("[update] git 走代理没通,绕过代理重试一次",
                  file=sys.stderr, flush=True)
            p2 = run(NO_PROXY_ARGS)
            if p2.returncode == 0:
                if raw:
                    return 0, (p2.stdout or "")
                return 0, ((p2.stdout or "") + (p2.stderr or "")).strip()
            # 直连也不行:把**两次**都报出来,不然只看到后一半会以为压根没配代理
            out = out + chr(10) + "直连也失败:" + (
                (p2.stdout or "") + (p2.stderr or "")).strip()
            p = p2
        if raw:
            return p.returncode, (p.stdout or "")
        return p.returncode, out

    def _run_git(self) -> None:
        try:
            self._git_exe = shutil.which("git")
            if not self._git_exe:
                raise RuntimeError("PATH 上找不到 git —— 装一个,或者去下载页手动更新")

            # 工作区有改动就停下。**只看被跟踪的文件** —— 多一个没加进 git
            # 的临时文件不该挡着更新,而改过的源码一旦被覆盖就找不回来了。
            code, out = self._git("status", "--porcelain", raw=True)
            if code != 0:
                raise RuntimeError("这好像不是个 git 仓库 —— 更新不了,"
                                   "去下载页拿打包版吧")
            dirty = [ln[3:] for ln in out.splitlines() if ln[:2] != "??"]
            if dirty:
                raise RuntimeError(
                    "本地改过这些文件,先自己提交或撤销再更新:"
                    + "、".join(dirty[:4]) + ("…" if len(dirty) > 4 else ""))

            code, upstream = self._git("rev-parse", "--abbrev-ref",
                                       "--symbolic-full-name", "@{u}")
            if code != 0:
                raise RuntimeError("当前分支没有对应的远端分支,不知道该跟谁更新")
            remote = upstream.split("/")[0]

            # **先看磁盘,再上网。** 磁盘上的代码已经比这个进程新的话,该做的
            # 就是重启 —— 这个判断一个字节的网络都不需要。原来它排在 fetch
            # 后面,于是代理没开的人永远走不到:明明只差一次重启,却被一条
            # 连不上的 fetch 挡死,报错还指向 git。这正是用户撞上的那一幕。
            disk = disk_version(self.here)
            if disk and ver.is_newer(disk, ver.VERSION):
                self._set(phase="restarting",
                          msg=f"代码已经是 v{disk} 了,正在重启…")
                self._respawn()
                time.sleep(1.0)
                os._exit(0)

            self._set(phase="fetching", msg="正在取最新代码…")
            code, out = self._git("fetch", "--tags", remote)
            if code != 0:
                # 代理连不上这一类,光把 git 的原话抛出去等于让人去查错方向 ——
                # 那串 127.0.0.1:PORT 看着像程序自己在乱连,其实是他自己
                # 很久以前配在 git 全局里的代理(见 proxy_is_dead 上面那段)
                if proxy_is_dead(out):
                    raise RuntimeError(
                        "git 走代理没通,绕过代理直连也没成。你的 git 全局配置里"
                        "写着一个本地代理 —— 代理没开就会这样。要么把代理打开,"
                        "要么执行:git config --global --unset http.proxy 和 "
                        "--unset https.proxy。原文:" + out[:200])
                raise RuntimeError(f"git fetch 失败:{out[:200]}")

            code, behind = self._git("rev-list", "--count", f"HEAD..{upstream}")
            code2, ahead = self._git("rev-list", "--count", f"{upstream}..HEAD")
            if code == 0 and behind == "0":
                # 没东西可快进。"磁盘比进程新就重启"那一支已经在 fetch 之前
                # 做过了(挪上去的理由见那儿),走到这里就是真的没什么可做
                self._set(phase="idle", msg="", error="")
                self._set(phase="done", msg="已经是最新的代码了")
                return
            if code2 == 0 and ahead not in ("0", ""):
                # 本地有没推上去的提交 —— 快进不了。**不碰它**,
                # 硬来的话用户自己的提交就没了
                raise RuntimeError(
                    f"本地有 {ahead} 个提交还没推上去,和远端分叉了 —— "
                    f"先 git push 或者 rebase,这里只做快进")

            self._set(phase="applying", msg=f"正在快进 {behind} 个提交…")
            code, out = self._git("merge", "--ff-only", upstream)
            if code != 0:
                raise RuntimeError(f"快进失败:{out[:300]}")

            self._set(phase="restarting", msg="代码换好了,正在重启…")
            self._respawn()
            time.sleep(1.0)
            os._exit(0)
        except Exception as exc:                   # noqa: BLE001
            self._set(phase="error", error=f"{type(exc).__name__}: {exc}"[:300])
        finally:
            try:
                self._lock.release()
            except RuntimeError:
                pass

    def _respawn(self) -> None:
        """用当前这个解释器把 app.py 再起一份。

        **带 --after-update**:新进程会多等一会儿单实例那把锁 —— 本进程
        还在收尾,锁没松开,不等的话新进程会判定"已经有一个在跑"然后退出,
        用户看到的就是"点了更新,应用没了"。打包版那条路踩过这个坑。
        """
        entry = self.here / "app.py"
        subprocess.Popen([sys.executable, str(entry), "--after-update"],
                         cwd=str(self.here), close_fds=True,
                         creationflags=_NO_WINDOW)

    # ─────────────────── 打包版:换 exe ───────────────────

    def _run(self, url: str, want: str = "") -> None:
        """下载 -> 解压 -> 让新版程序自己来装自己 -> 本进程退出。

        两个平台同一条骨架,只有"解压怎么解"和"要跑哪个可执行文件"分岔:

            Windows   zip 里一层 `NEU Helper/`,里面是 exe + 几个文件
            macOS     zip 里一个 `NEU Helper.app`,得用 ditto 解
                      (Python 的 zipfile 会丢掉执行位和符号链接 ——
                       解出来的 .app 根本起不来)
        """
        try:
            app = bundle_of(self.here)
            work = stage_root(self.here)
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            zp = work / "pkg.zip"
            self._download(url, zp)

            self._set(phase="extracting", pct=100, msg="正在解压…")
            raw = work / "raw"
            staged = work / "staged"
            if app is not None:
                self._unzip_mac(zp, raw)
            else:
                with zipfile.ZipFile(zp) as z:
                    z.extractall(raw)
            roots = [q for q in raw.iterdir() if q.is_dir()]
            src = roots[0] if len(roots) == 1 else raw
            shutil.move(str(src), str(staged))

            if app is not None:
                # 解出来的那个就是 .app 本体(zip 里一层 NEU Helper.app)
                staged_app = staged if staged.suffix == ".app" else None
                if staged_app is None:
                    staged_app = next((q for q in staged.rglob("*.app")), None)
                if staged_app is None:
                    raise RuntimeError("下载的包里没有 NEU Helper.app —— 不敢装")
                exe = staged_app / "Contents" / "MacOS" / "NEU Helper"
                if not exe.is_file():
                    raise RuntimeError(".app 里没有可执行文件 —— 不敢装")
                # 下载来的东西可能带隔离属性,带着的话它自己都起不来
                _run_quiet(["xattr", "-cr", str(staged_app)])
                _run_quiet(["chmod", "+x", str(exe)])
                target = str(app)
            else:
                exe = staged / "NEU Helper.exe"
                if not exe.is_file():
                    raise RuntimeError("下载的包里没有 NEU Helper.exe —— 不敢装")
                target = str(self.here)

            self._set(phase="applying", msg="正在替换,马上会重启…")
            # 让**新版的程序**自己来装自己(理由见模块文档)
            flag = work / STARTED
            flag.unlink(missing_ok=True)
            proc = subprocess.Popen(
                [str(exe), "--apply-update", target, str(os.getpid())],
                cwd=str(exe.parent), close_fds=True)
            # **确认它真的起来了再退。** 这一步原来是 `sleep(1.2)` 然后就
            # `os._exit(0)` —— 那等于把命交给一个还没见着面的进程:杀毒软件
            # 拦下这个没签名的程序、或者它自己一启动就崩,用户看到的是
            # 「点了更新,应用没了」,而且一个字的解释都没有。
            # 新版程序一进门就写 started 这个文件,等到它才算数。
            for _ in range(150):                   # 最多 15 秒
                if flag.exists():
                    break
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"新版程序刚起来就退了(退出码 {proc.returncode})—— "
                        + ("macOS 可能拦了这个没签名的程序:"
                           "到「系统设置 → 隐私与安全性」放行一次。"
                           if app is not None else
                           "多半是被杀毒软件拦了。把安装目录加进白名单再试。"))
                time.sleep(0.1)
            else:
                raise RuntimeError(
                    "新版程序起不来(等了 15 秒没动静)—— "
                    "什么都没改,你这一份还是好的。")
            # 记下"我打算变成哪一版"。下次启动核对不上就说出来,
            # 而不是让人对着一个没变的版本号发呆
            mark_pending(self.here, want)
            self._set(phase="restarting", msg="正在重启…")
            # 主窗口关掉,进程退出 —— 新进程在等这一刻
            os._exit(0)
        except Exception as exc:                   # noqa: BLE001
            self._set(phase="error", error=f"{type(exc).__name__}: {exc}"[:300])
        finally:
            try:
                self._lock.release()
            except RuntimeError:
                pass

    @staticmethod
    def _unzip_mac(zp: Path, dest: Path) -> None:
        """用 ditto 解,**不用 zipfile**。

        Python 的 `ZipFile.extractall` 不还原 Unix 权限位、也不还原符号链接 ——
        而 .app 里 `Contents/MacOS/NEU Helper` 必须可执行,`Frameworks/` 里
        一堆 dylib 是符号链接。用 zipfile 解出来的 bundle 是死的。
        build_mac.sh 打包时用的也正是 ditto。
        """
        dest.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["ditto", "-x", "-k", str(zp), str(dest)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError("解压失败(ditto):"
                               + (r.stderr or "").strip()[:200])

    def _download(self, url: str, dst: Path) -> None:
        req = urllib.request.Request(url, headers={
            "User-Agent": f"NEUHelper/{ver.VERSION}"})
        with urllib.request.urlopen(req, timeout=60) as r, dst.open("wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            last = 0.0
            while chunk := r.read(1 << 18):
                f.write(chunk)
                got += len(chunk)
                # 别每个块都推一次事件 —— 50MB 的包会推几百次
                if total and time.time() - last > 0.4:
                    last = time.time()
                    self._set(phase="downloading", pct=int(got * 100 / total),
                              msg=f"正在下载 {got // 1048576}/{total // 1048576} MB")
        if total and dst.stat().st_size != total:
            raise RuntimeError("下载不完整,没装")


# ─────────────────── 新版 exe 的"装自己"模式 ───────────────────


def apply_update(target: Path, wait_pid: int) -> int:
    """在**新版的程序**里跑:等旧进程退出,把自己这一份装到 target,再启动它。

    app.py 在最开头就会把 `--apply-update` 交到这里,所以这个模式下
    不会起服务、不会开窗口。

    `target` 的含义按平台不同:
        Windows   安装目录(exe 所在的那个文件夹)
        macOS     `NEU Helper.app` 本身 —— 那边换的是整个 bundle
    """
    target = Path(target)
    staged = Path(sys.executable).resolve().parent
    mac_app = target if target.suffix == ".app" else None

    # 日志往哪儿写。macOS 上 data/ 在 bundle 里面,而 bundle 整个要被换掉 ——
    # 所以几头都写一份:暂存区那份总在,旧 bundle 那份留着(万一换失败),
    # 新 bundle 那份换完就是现场。
    if mac_app is not None:
        logs = [staged.parent / "update.log",
                mac_app / "Contents" / "MacOS" / "data" / "update.log",
                staged / "data" / "update.log"]
    else:
        logs = [target / "data" / "update.log"]
    for q in logs:
        try:
            q.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    # **第一件事就是打这个招呼。** 旧进程正卡在这儿等 —— 等到了才敢退。
    # 起不来(被拦了、包坏了)的话它就不退,用户至少还有个能用的应用和
    # 一句解释,而不是"点了更新,应用没了"。
    try:
        (staged.parent / STARTED).write_text(
            time.strftime("%H:%M:%S"), encoding="utf-8")
    except OSError:
        pass

    def say(m: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {m}"
        for q in logs:
            try:
                with q.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:                      # noqa: BLE001
                pass
        print(line, file=sys.stderr, flush=True)

    if mac_app is not None:
        return _apply_update_mac(mac_app, staged, wait_pid, say)

    say(f"准备把 {staged} 装到 {target},等 pid {wait_pid} 退出")
    for _ in range(300):                           # 最多等 30 秒
        if not _alive(wait_pid):
            break
        time.sleep(0.1)
    else:
        say("旧进程一直没退出,放弃(什么都没改)")
        return 1
    time.sleep(0.8)                                # 给它一点时间松开文件句柄

    copied, failed = [], []
    # **exe 放到最后拷。** 版本号是从 exe 里读出来的,它一换,这次更新在
    # 用户眼里就"生效"了。所以它必须是最后一步 —— 前面任何一样没拷成,
    # 回滚之后应用还是完完整整的旧版;要是反过来先换 exe,中途失败就得到
    # 一个新 exe 配旧资源的半成品。
    items = sorted(staged.iterdir(),
                   key=lambda p: p.name.lower().endswith(".exe"))
    for item in items:
        rel = item.name
        if rel in KEEP or rel == "pkg.zip" or rel == STARTED:
            continue
        dst = target / rel
        # 换文件时"被占用"是这类操作最典型的偶发失败 —— 旧进程刚退出,
        # 系统可能还没松开它的镜像文件。重试几次基本都能过去。
        for attempt in range(6):
            try:
                if item.is_dir():
                    # 合并式拷贝,但把 KEEP_INSIDE 里那些跳过 —— 比如
                    # .claude/settings.local.json 是你的权限设置,不是程序的
                    skip = {f for d, f in KEEP_INSIDE if d == rel}
                    shutil.copytree(item, dst, dirs_exist_ok=True,
                                    ignore=lambda _d, names: skip & set(names))
                else:
                    # 正在跑的 exe 删不掉,但**改名可以** —— 先挪开再拷新的。
                    # 留着 .old 是为了拷失败时还能退回去。
                    if dst.exists():
                        old = dst.with_suffix(dst.suffix + ".old")
                        old.unlink(missing_ok=True)
                        dst.rename(old)
                    shutil.copy2(item, dst)
                copied.append(rel)
                break
            except Exception as exc:               # noqa: BLE001
                if attempt < 5:
                    time.sleep(0.6)
                    continue
                failed.append(f"{rel}: {exc}")
                say(f"!! 拷 {rel} 失败(试了 6 次):{exc}")

    if failed:
        # 有东西没拷成:把改了名的退回去,别留一个半新半旧的安装
        say(f"有 {len(failed)} 样没拷成,回滚:" + "; ".join(failed))
        for rel in copied:
            old = (target / rel).with_suffix(Path(rel).suffix + ".old")
            if old.exists():
                (target / rel).unlink(missing_ok=True)
                old.rename(target / rel)
            elif (target / rel).is_dir():
                # 目录是合并式拷进去的,没有 .old 可退。**照实说** ——
                # 这一句才让日志对得上现场:回滚之后 exe 是旧的,而这些
                # 目录里已经混进了新版的文件
                say(f"({rel} 是合并拷贝的,退不回去 —— 里面是新版的内容)")
        say("回滚完成,启动原来那个版本")
    else:
        say(f"装好了:{len(copied)} 项")
        # **清理 .old 必须是"尽力而为"。** 那个文件是刚刚退出的那个进程的
        # 镜像,Windows 往往还锁着它几秒 —— 在这儿抛异常的后果是:文件明明
        # 都换好了,退出码却变成 1、还记一条未捕获异常。删不掉不要紧,
        # 应用下次启动会顺手清(见 app.py 的 _sweep_old)。
        for rel in copied:
            old = (target / rel).with_suffix(Path(rel).suffix + ".old")
            try:
                old.unlink(missing_ok=True)
            except OSError:
                say(f"({old.name} 还被锁着,留给下次启动清)")

    # **这一步绝对不能抛异常。** 文件这时候已经换好了 —— 再炸一次的话
    # 用户得到的是"更新完但应用没起来,而且屏幕上什么都没有"。
    # 起不来就把原因写进日志,让人还能自己双击。
    exe = target / "NEU Helper.exe"
    try:
        if exe.is_file():
            say("启动 " + str(exe))
            # 带上 --after-update:新进程会多等一会儿单实例的锁,
            # 而不是撞见"已经有一个在跑"就立刻退出(那样用户看到的是
            # "点了更新,应用就没了")
            subprocess.Popen([str(exe), "--after-update"],
                             cwd=str(target), close_fds=True)
        else:
            say("!! 装完之后找不到 NEU Helper.exe")
    except Exception as exc:                       # noqa: BLE001
        say(f"!! 起不来({type(exc).__name__}: {exc})—— 手动双击一下")
    return 0 if not failed else 1


def _apply_update_mac(app: Path, staged: Path, wait_pid: int, say) -> int:
    """macOS:把整个 `.app` 换掉。

    **和 Windows 那条路的做法不一样,原因是 bundle。** Windows 是逐个文件拷
    进安装目录;macOS 的 .app 是一个带签名的整体 —— 往里面逐个换文件会破坏
    bundle 的封印,而且 Apple 芯片上连 ad-hoc 签名都是执行的前提。所以这边是
    "整包替换":把新 bundle 挪到位,旧的改名留着当退路。

    你的东西怎么带过去:`data/`(邮件、对话、课件、设置)和 `CLAUDE.md` 在
    `Contents/MacOS/` 里面 —— 换包之前先把它们拷进新 bundle,不然一次更新
    就把所有数据清了。

    `staged` 是新程序可执行文件所在的目录(`新.app/Contents/MacOS`),
    所以新 bundle 就是它往上两层。
    """
    new_app = staged.parent.parent           # .../新 NEU Helper.app
    old_side = app.with_name(app.name + ".old")
    say(f"macOS:准备用 {new_app} 换掉 {app},等 pid {wait_pid} 退出")
    for _ in range(300):                     # 最多等 30 秒
        if not _alive(wait_pid):
            break
        time.sleep(0.1)
    else:
        say("旧进程一直没退出,放弃(什么都没改)")
        return 1
    time.sleep(0.5)

    # ① 把"你的东西"搬进新 bundle
    old_inner = app / "Contents" / "MacOS"
    new_inner = new_app / "Contents" / "MacOS"
    for name in sorted(KEEP):
        src = old_inner / name
        if not src.exists():
            continue
        dst = new_inner / name
        try:
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
            else:
                shutil.copy2(src, dst)
            say(f"带过去了 {name}")
        except Exception as exc:             # noqa: BLE001
            say(f"!! {name} 没能带过去:{exc} —— 放弃,什么都没改")
            return 1
    # .claude 里用户自己那份设置也留着
    for d, f in sorted(KEEP_INSIDE):
        src = old_inner / d / f
        if src.exists():
            try:
                (new_inner / d).mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, new_inner / d / f)
            except Exception:                # noqa: BLE001
                pass

    # ② 换。**先把旧的挪开、再把新的挪进来** —— 不是先删旧的:
    #    第二步万一失败,旧的还能原样挪回去
    try:
        shutil.rmtree(old_side, ignore_errors=True)
        app.rename(old_side)
    except OSError as exc:
        say(f"!! 挪不开旧的 .app({exc})—— 多半是所在目录没有写权限。"
            "什么都没改")
        return 1
    try:
        try:
            new_app.rename(app)              # 同卷:原子,一瞬间
        except OSError:
            # 跨卷(暂存在 ~ 而应用在别的盘)—— rename 不行,老老实实拷
            say("跨卷,改成拷贝")
            shutil.copytree(new_app, app, symlinks=True)
    except Exception as exc:                 # noqa: BLE001
        say(f"!! 新的 .app 没装上({exc})—— 把旧的挪回来")
        try:
            if app.exists():
                shutil.rmtree(app, ignore_errors=True)
            old_side.rename(app)
            say("旧版本已还原")
        except OSError as exc2:
            say(f"!! 连还原都失败了({exc2})。旧版本在 {old_side},"
                "手动改个名就能用")
        return 1

    # ③ 收尾:清隔离属性、补执行位。都是尽力而为
    _run_quiet(["xattr", "-cr", str(app)])
    _run_quiet(["chmod", "+x", str(app / "Contents" / "MacOS" / "NEU Helper")])
    shutil.rmtree(old_side, ignore_errors=True)
    say(f"装好了:{app}")

    # ④ 起新的。**这一步绝对不能抛异常** —— 文件都换好了,再炸一次的话
    #    用户得到的是"更新完但应用没起来,而且屏幕上什么都没有"
    try:
        # `open` 而不是直接 Popen 可执行文件:让 LaunchServices 正常注册
        # 这个应用(Dock 图标、激活状态都靠它),而且不会把新进程挂在
        # 这个马上要退出的进程下面
        subprocess.Popen(["open", "-n", str(app), "--args", "--after-update"])
        say("启动 " + str(app))
    except Exception as exc:                 # noqa: BLE001
        say(f"!! 起不来({type(exc).__name__}: {exc})—— 手动打开一下")
    return 0


def _alive(pid: int) -> bool:
    """这个进程还活着吗。

    **不能只看 OpenProcess 成不成功。** 进程已经死了、但还有人持有它的句柄
    时(比如启动它的那个进程还没关掉 handle),OpenProcess 照样成功 ——
    于是"等它退出"会一直等到超时,更新就白等一场。踩过一次:实测杀掉旧进程
    之后日志仍然写着"旧进程一直没退出,放弃"。

    看退出码才准:还在跑的进程退出码是 STILL_ACTIVE(259)。
    """
    if pid <= 0:
        return False
    if platform_id.IS_WIN:
        import ctypes
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong(0)
        got = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(got) and code.value == STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
