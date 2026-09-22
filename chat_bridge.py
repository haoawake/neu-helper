# -*- coding: utf-8 -*-
"""把 claude CLI 包成一个流式聊天后端。

用 `claude -p --output-format stream-json` 起一个一次性进程,边读边把事件推给前端。
会话连续性靠记住 session_id、后续请求带 --resume,而不是维持一个长驻进程 ——
长驻进程在 GUI 里崩了很难恢复,一问一进程则每次都是干净状态。

事件不走 evaluate_js 回推 —— WebView2 的 COM 对象只能在 UI 线程访问,从工作线程
调 evaluate_js 会把 UI 线程锁死(窗口直接「未响应」)。所以这里只往队列里塞,
由前端轮询 drain() 取走,于是所有 WebView2 交互都发生在 UI 线程上。

事件流里真正有用的三种(其余忽略):
  content_block_start + content_block.type=tool_use  -> 正在调工具
  content_block_delta + delta.type=text_delta        -> 正文增量
  result                                             -> 结束,带成本和 session_id
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

# 不加这个,Windows 上每次提问都会闪一个黑色控制台窗口
import desktop
from desktop import NO_WINDOW as _NO_WINDOW  # 起子进程的标志位,见 desktop.py


def find_claude() -> str | None:
    exe = shutil.which("claude")
    if exe:
        return exe
    # 启动路径不走登录 shell(Windows 是 wscript、macOS 是 LaunchAgent),
    # 那时的 PATH 和终端里的不是一回事 —— 兜底找几个常见位置,
    # 两个平台各自的清单在 desktop.claude_candidates
    for cand in desktop.claude_candidates():
        if cand.exists():
            return str(cand)
    return None


# 工具名 -> 界面上显示的中文,免得用户看到 mcp__canvas__canvas_upcoming 这种东西
TOOL_LABELS = {
    "mcp__canvas__canvas_whoami": "验证 Canvas 身份",
    "mcp__canvas__canvas_courses": "读取课程列表",
    "mcp__canvas__canvas_upcoming": "读取待办清单",
    "mcp__canvas__canvas_assignments": "读取作业列表",
    "mcp__canvas__canvas_assignment_detail": "读取作业详情",
    "mcp__canvas__canvas_grades": "读取成绩",
    "mcp__canvas__canvas_announcements": "读取公告",
    "mcp__canvas__canvas_course_content": "读取课程内容",
    "mcp__canvas__canvas_get": "查询 Canvas API",
    "Read": "读取文件",
    "Glob": "查找文件",
    "Grep": "搜索内容",
    "WebFetch": "抓取网页",
    "ToolSearch": "准备工具",
}


def tool_label(name: str) -> str:
    if name in TOOL_LABELS:
        return TOOL_LABELS[name]
    if name.startswith("mcp__canvas__"):
        return "查询 Canvas"
    return name


class ChatSession:
    """一问一进程,靠 session_id 续上下文。"""

    def __init__(self, project_dir: Path, channel: str = "chat", on_done=None):
        self.project_dir = Path(project_dir)
        # 每个实例是一条独立通道。简报和用户对话各用一条,互不打断,
        # 前端靠事件里的 channel 字段分流。
        self.channel = channel
        self.on_done = on_done
        self.session_id: str | None = None
        self.total_cost = 0.0
        self.last_text = ""        # 本轮回答的完整正文,给需要落盘的调用方用
        self.last_cost = 0.0
        self._last_error = ""      # 最近一次失败的 stderr,决定要不要重试时用
        self._pending_error = ""   # result 事件里的异常,等确定不重试了再报
        self._proc: subprocess.Popen | None = None
        self._busy = threading.Lock()
        self._events: deque[dict] = deque()
        self._elock = threading.Lock()

    # ------------------------------------------------------------ 事件队列

    def _put(self, ev: dict) -> None:
        with self._elock:
            self._events.append({**ev, "channel": self.channel})

    def drain(self) -> list[dict]:
        """取走并清空缓冲的事件。相邻的正文增量会合并成一条,减少往返负载。"""
        with self._elock:
            raw = list(self._events)
            self._events.clear()
        merged: list[dict] = []
        for ev in raw:
            if (
                ev.get("kind") == "delta"
                and merged
                and merged[-1].get("kind") == "delta"
            ):
                merged[-1] = {
                    "kind": "delta",
                    "text": merged[-1]["text"] + ev["text"],
                    "channel": self.channel,
                }
            else:
                merged.append(ev)
        return merged

    # ------------------------------------------------------------ 对外接口

    def is_busy(self) -> bool:
        return self._busy.locked()

    def reset(self) -> None:
        """开一段新对话,丢掉上下文。"""
        self.cancel()
        self.session_id = None
        self.total_cost = 0.0

    def fork(self) -> None:
        """砍掉历史之后换一条时间线:丢掉 session_id,但**不清成本**。

        和 reset() 的区别是后者代表"新对话",连累计花销一起归零;改一句重发
        还是同一段对话,那些钱已经花掉了。CLI 的会话没法回退(里头还留着被
        砍掉的那一问一答),所以只能重开一段、把前文自己补回去。
        """
        self.cancel()
        self.session_id = None

    def cancel(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def send_async(self, message: str, fallback_context: str = "") -> None:
        """fallback_context:resume 续不上时用来重建上下文的前文(见 _run)。"""
        threading.Thread(target=self._run, args=(message, fallback_context),
                         daemon=True).start()

    # ------------------------------------------------------------ 内部实现

    def _run(self, message: str, fallback_context: str = "") -> None:
        if not self._busy.acquire(blocking=False):
            self._put({"kind": "error", "text": "上一个问题还在回答,等它结束。"})
            return
        self.last_text = ""
        self.last_cost = 0.0
        self._pending_error = ""
        try:
            ok = self._stream(message)
            # 第一次失败而且一个字都没吐出来 —— 最常见的原因是 --resume 指的那个
            # 会话没了(CLI 直接退 1、stderr 还是空的)。无条件重试一次:清掉
            # session_id,有存档前文就带上,没有就只发原消息。
            # 这里**不**再要求"必须有前文",原来那个条件让没落盘过的新对话直接
            # 卡死在"退出码 1"上。
            if not ok and self.session_id is not None:
                self._put({"kind": "tool", "text": "会话续不上,重开一段再试"})
                self.session_id = None
                self.last_text = ""
                retry = (fallback_context + chr(10) * 2 + message
                         if fallback_context else message)
                ok = self._stream(retry)
            if not ok:
                self._put({"kind": "error",
                           "text": "claude 执行失败:" + (self._last_error or "没有更多信息")})
            elif self._pending_error and not self.last_text.strip():
                # 跑完了但一个字都没出来,那条攒着的异常这时候才有意义
                self._put({"kind": "error", "text": self._pending_error})
        except Exception as exc:
            self._put({"kind": "error", "text": f"{type(exc).__name__}: {exc}"})
        finally:
            self._busy.release()
            self._put({"kind": "done"})
            if self.on_done:
                # 回调里要落盘,不能让它的异常吞掉整条通道
                try:
                    self.on_done(self)
                except Exception as exc:
                    self._put({"kind": "error", "text": f"保存失败: {exc}"})

    def _stream(self, message: str) -> bool:
        """跑一轮。返回是否成功(失败且没出过正文 -> 调用方可以考虑重试)。"""
        exe = find_claude()
        if not exe:
            self._put(
                {
                    "kind": "error",
                    "text": "找不到 claude 命令。确认 Claude Code 已安装且在 PATH 里。",
                }
            )
            return False

        argv = [
            exe,
            "-p",
            message,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if self.session_id:
            argv += ["--resume", self.session_id]

        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"

        self._put({"kind": "start"})
        self._proc = subprocess.Popen(
            argv,
            cwd=str(self.project_dir),   # 为了让 CLAUDE.md 和 MCP 权限生效
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=_NO_WINDOW,
        )

        assert self._proc.stdout is not None
        got_text = False
        tail_lines: list[str] = []          # 留最后几行,失败时当线索
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            tail_lines.append(line[:400])
            if len(tail_lines) > 6:
                tail_lines.pop(0)
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if self._handle(ev):
                got_text = True

        self._proc.wait()
        stderr = (self._proc.stderr.read() if self._proc.stderr else "") or ""
        if self._proc.returncode != 0 and not got_text:
            code = self._proc.returncode
            detail = stderr.strip()[:500]
            # CLI 失败时经常 stderr 是空的,线索全在 stdout 的最后几行(比如
            # is_error 的 result 事件)。都捞上来写进 app.log,不然下次照样两眼一黑。
            tail = chr(10).join(tail_lines[-4:])
            print(f"[chat] claude 退出码 {code} channel={self.channel} "
                  f"resume={'有' if self.session_id else '无'}" + chr(10)
                  + (f"  stderr: {detail}" + chr(10) if detail else "")
                  + (f"  stdout 尾: {tail[:900]}" if tail else "  stdout 空"),
                  file=sys.stderr, flush=True)
            self._last_error = detail or f"退出码 {code}(细节见 data/app.log)"
            return False
        return True

    def _handle(self, ev: dict) -> bool:
        """处理一个事件;返回是否产生了正文文本。"""
        etype = ev.get("type")

        # session_id 出现在多种事件里,抓到就记住
        sid = ev.get("session_id")
        if sid and not self.session_id:
            self.session_id = sid

        if etype == "stream_event":
            inner = ev.get("event") or {}
            itype = inner.get("type")
            if itype == "content_block_start":
                block = inner.get("content_block") or {}
                if block.get("type") == "tool_use":
                    self._put({"kind": "tool", "text": tool_label(block.get("name") or "")})
                elif block.get("type") == "thinking":
                    self._put({"kind": "thinking"})
            elif itype == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text") or ""
                    if text:
                        self.last_text += text
                        self._put({"kind": "delta", "text": text})
                        return True
            return False

        if etype == "result":
            cost = ev.get("total_cost_usd")
            if isinstance(cost, (int, float)):
                self.total_cost += cost
                self.last_cost = cost
            if ev.get("subtype") != "success":
                # 先攒着别报:下面可能还要重试一次(会话过期是最常见的失败),
                # 重试成功的话用户不该看到一条莫名的报错
                self._pending_error = f'本次回答异常结束:{ev.get("subtype")}'
            self._put({"kind": "cost", "session_cost": round(self.total_cost, 4)})
        return False
