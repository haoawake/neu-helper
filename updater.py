# -*- coding: utf-8 -*-
"""检查更新 / 下载 / 就地替换。

分两种安装方式,能做的事不一样:

  打包版(从 Release 下的)   下载新的 zip -> 解压 -> 换掉 exe -> 重启
  源码版(git clone 的)      git fetch + merge --ff-only -> 重启

════════════════════════════════════════════════════════════════════
**怎么替换一个正在运行的 exe。**

Windows 不让删正在跑的 exe。常见做法是甩一个 .cmd/.vbs 出去等进程退出再拷,
但那会闪一个黑框,而且脚本本身还得自己清理。

这里用另一个办法:**让新版的 exe 自己来装自己**。

  1. 下载 zip,解压到 data/update/staged/
  2. 起 `staged/NEU Helper.exe --apply-update <装在哪> <当前进程的 pid>`
  3. 本进程退出
  4. 那个新进程等旧进程死掉,把 staged 里的东西拷过去,再把装好的那个拉起来

新版的 exe 是 onefile 的、自带全部依赖,所以它在 staged 里就能独立跑 ——
不需要任何外部脚本。app.py 在最开头就会认这个参数,不会去开窗口。

**data/ 和 CLAUDE.md 不动。** 前者是你的邮件、对话、课件;后者是你自己的
课程表。zip 里本来也没有它们,这里再显式挡一道。
════════════════════════════════════════════════════════════════════

源码版走的是另一条路(_run_git),短得多:快进到 origin 上的那个提交,
然后重启。**不 stash、不 reset、不 checkout** —— 只做快进这一种操作。
改动过的文件、和远端分叉了的提交,一律停下来照实说,而不是替用户做决定
把他的东西弄丢。`data/` 和 `CLAUDE.md` 在 .gitignore 里,git 本来就不碰。

macOS 的**打包版**目前只提示、不代劳:.app 的替换没法在这台 Windows 上验证,
拿没验过的代码去动别人的安装目录不合适。点"更新"会打开下载页。
源码版在 macOS 上没这个问题 —— git 是 git。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

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
            "notes": (r.get("body") or "").strip()[:2000],
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
        try:
            p = subprocess.run(
                [exe, *args], cwd=str(here), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                creationflags=_NO_WINDOW)
        except Exception:                          # noqa: BLE001
            return 1, ""
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()

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

    def start(self, url: str) -> bool:
        """开始下载并安装。返回有没有真的开跑。"""
        if not url:
            self._set(phase="error", error="这个版本没有本平台的安装包")
            return False
        if platform_id.IS_MAC:
            # 没在真机上验证过替换 .app 的流程,不拿别人的安装目录做实验
            self._set(phase="error",
                      error="macOS 暂时只能手动更新 —— 已经帮你打开下载页")
            return False
        if not self._lock.acquire(blocking=False):
            return False
        self._set(phase="downloading", pct=0, msg="正在下载…", error="")
        threading.Thread(target=self._run, args=(url,), daemon=True,
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
        p = subprocess.run(
            [self._git_exe, *args], cwd=str(self.here), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=_NO_WINDOW)
        if raw:
            return p.returncode, (p.stdout or "")
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()

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

            self._set(phase="fetching", msg="正在取最新代码…")
            code, out = self._git("fetch", "--tags", remote)
            if code != 0:
                raise RuntimeError(f"git fetch 失败:{out[:200]}")

            code, behind = self._git("rev-list", "--count", f"HEAD..{upstream}")
            code2, ahead = self._git("rev-list", "--count", f"{upstream}..HEAD")
            if code == 0 and behind == "0":
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

    def _run(self, url: str) -> None:
        try:
            work = self.here / "data" / "update"
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            zp = work / "pkg.zip"
            self._download(url, zp)

            self._set(phase="extracting", pct=100, msg="正在解压…")
            staged = work / "staged"
            with zipfile.ZipFile(zp) as z:
                # zip 里是一层 "NEU Helper/" 目录
                z.extractall(work / "raw")
            roots = [p for p in (work / "raw").iterdir() if p.is_dir()]
            src = roots[0] if len(roots) == 1 else (work / "raw")
            shutil.move(str(src), str(staged))

            exe = staged / "NEU Helper.exe"
            if not exe.is_file():
                raise RuntimeError("下载的包里没有 NEU Helper.exe —— 不敢装")

            self._set(phase="applying", msg="正在替换,马上会重启…")
            # 让**新版的 exe** 自己来装自己(理由见模块文档)
            subprocess.Popen(
                [str(exe), "--apply-update", str(self.here), str(os.getpid())],
                cwd=str(staged), close_fds=True)
            time.sleep(1.2)
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
    """在**新版的 exe** 里跑:等旧进程退出,把自己这一份拷过去,再启动它。

    app.py 在最开头就会把 `--apply-update` 交到这里,所以这个模式下
    不会起服务、不会开窗口。
    """
    target = Path(target)
    staged = Path(sys.executable).resolve().parent
    log = target / "data" / "update.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    def say(m: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {m}"
        try:
            with log.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:                          # noqa: BLE001
            pass
        print(line, file=sys.stderr, flush=True)

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
    for item in staged.iterdir():
        rel = item.name
        if rel in KEEP or rel == "pkg.zip":
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
        say("有文件没拷成,回滚")
        for rel in copied:
            old = (target / rel).with_suffix(Path(rel).suffix + ".old")
            if old.exists():
                (target / rel).unlink(missing_ok=True)
                old.rename(target / rel)
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
