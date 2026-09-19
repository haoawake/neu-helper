# -*- coding: utf-8 -*-
"""每日简报:存档 + 定时触发。

口径是「一天一份,绝不重复」:
  - 每天 09:00 是触发时刻
  - 如果 9 点后才开机,开起来就补一份(不然机器关着的那天就永远缺一条)
  - 如果机器连开好几天,调度线程会在每天 9 点自己触发
  - 当天档案里已经有简报了,任何路径都不会再生成

这样「重启触发」和「定点触发」就统一成了一种:**每天 09:00 之后的第一次机会**。

新简报会带上前几天的简报原文,让它能说出变化(「昨天提醒的 HW1 你还没动」),
而不是每天重复一遍同样的清单。历史用显式拼进 prompt 的方式传,不用 --resume ——
后者的上下文会无边界地长,而且会话过期就断。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

BRIEF_HOUR = 9          # 触发时刻(本机时区的整点)
KEEP_DAYS = 60          # 档案保留条数
HISTORY_IN_PROMPT = 3   # 塞进 prompt 的历史简报条数


class BriefingStore:
    """按日期存档的简报。落盘成一个 JSON,条目少、读写都不频繁。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                self._data = raw
        except Exception:
            # 档案坏了不该让应用起不来;备份一份再从空开始
            try:
                self.path.rename(self.path.with_suffix(".corrupt.json"))
            except Exception:
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        # 只留最近的若干天,免得无限长
        keys = sorted(self._data.keys(), reverse=True)[:KEEP_DAYS]
        trimmed = {k: self._data[k] for k in keys}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)   # 原子替换,断电不会留半个文件
        self._data = trimmed

    # ------------------------------------------------------------ 读

    def has(self, date: str) -> bool:
        with self._lock:
            return date in self._data and bool(self._data[date].get("text"))

    def get(self, date: str) -> dict | None:
        with self._lock:
            return self._data.get(date)

    def index(self) -> list[dict]:
        """按日期倒序的目录,不含正文。"""
        with self._lock:
            return [
                {
                    "date": k,
                    "generated_at": self._data[k].get("generated_at", ""),
                    "chars": len(self._data[k].get("text", "")),
                }
                for k in sorted(self._data.keys(), reverse=True)
            ]

    def recent_texts(self, n: int = HISTORY_IN_PROMPT, before: str | None = None):
        with self._lock:
            keys = sorted(self._data.keys(), reverse=True)
            if before:
                keys = [k for k in keys if k < before]
            return [(k, self._data[k].get("text", "")) for k in keys[:n]]

    # ------------------------------------------------------------ 写

    def put(self, date: str, text: str, cost: float = 0.0) -> None:
        with self._lock:
            self._data[date] = {
                "date": date,
                "text": text,
                "cost": round(cost, 4),
                "generated_at": datetime.now().strftime("%H:%M"),
            }
            self._save()


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def build_prompt(store: BriefingStore, date: str, history_n: int | None = None,
                 focus: str = "") -> str:
    """拼出简报 prompt:触发词 + 重点课程 + 前几天的简报原文。

    CLAUDE.md 里定义了 daily-briefing 的行为;历史放在标签里,让它对比出变化。
    """
    parts = ["daily-briefing"]
    focus_lines = [x.strip() for x in (focus or "").splitlines() if x.strip()]
    if focus_lines:
        parts.append("")
        parts.append("<重点课程>")
        parts.append("我这段时间主要盯这几门:" + "、".join(focus_lines) + "。")
        parts.append("简报的篇幅优先给它们。其他课除非有硬截止或者新公告,"
                     "一句带过就行,不用每天重念。")
        parts.append("</重点课程>")
    history = store.recent_texts(
        n=history_n if history_n else HISTORY_IN_PROMPT, before=date)
    if history:
        parts.append("")
        parts.append("<以往简报>")
        for d, text in reversed(history):   # 旧的在前,读起来是时间顺序
            parts.append(f"### {d}")
            parts.append(text.strip())
            parts.append("")
        parts.append("</以往简报>")
    else:
        parts.append("")
        parts.append("(没有历史简报,这是第一份)")
    return "\n".join(parts)


class BriefingRunner:
    """把存档、触发、生成串起来。"""

    def __init__(self, store: BriefingStore, session, ensure_fresh_data,
                 hour_provider=None, prompt_builder=None, label="简报",
                 precondition=None, history_provider=None):
        self.store = store
        self.session = session              # 专供简报的 ChatSession
        self.ensure_fresh_data = ensure_fresh_data   # 生成前刷新快照的回调
        # prompt 的构造可替换:课业简报读 snapshot.md,邮件简报直接把邮件头
        # 塞进 prompt。调度、"一天只发一次"、补报这些逻辑两边完全一样,
        # 没必要抄一遍。
        self.prompt_builder = prompt_builder or (
            lambda store, date: build_prompt(store, date, self.history_provider()))
        self.label = label
        # 前置条件:返回 (能不能跑, 不能跑的原因)。邮件简报用它挡住"一个账号都
        # 没配却还是去问一次模型"—— 那次调用除了说"没有邮件"什么也拿不到。
        self.precondition = precondition
        # 简报里带几天的历史。带得多,"说变化"的判断更准,但 prompt 也更长。
        self.history_provider = history_provider or (lambda: HISTORY_IN_PROMPT)
        # 播报时刻可以在设置里改,所以每次判断都问一下,不缓存
        self.hour_provider = hour_provider or (lambda: BRIEF_HOUR)
        self.pending_date: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 生成

    def trigger(self, force: bool = False) -> dict:
        """尝试生成今天的简报。返回 {started, reason}。"""
        date = today_str()
        if self.precondition:
            ok, why = self.precondition()
            if not ok:
                return {"started": False, "reason": why}
        with self._lock:
            if self.session.is_busy():
                return {"started": False, "reason": f"{self.label}正在生成中"}
            if not force and self.store.has(date):
                return {"started": False, "reason": "今天的简报已经有了"}
            hour = self.hour_provider()
            if not force and datetime.now().hour < hour:
                return {
                    "started": False,
                    "reason": f"还没到 {hour}:00,{self.label}先不播报",
                }
            self.pending_date = date

        # 简报要读 data/snapshot.md,先确保它是这一刻的数据
        try:
            self.ensure_fresh_data()
        except Exception:
            pass

        self.session.reset()    # 每份简报都是干净会话,历史靠 prompt 带
        self.session.send_async(self.prompt_builder(self.store, date))
        return {"started": True, "reason": ""}

    def on_session_done(self, session) -> None:
        """ChatSession 跑完时回调,把正文存档。"""
        date = self.pending_date
        self.pending_date = None
        text = (session.last_text or "").strip()
        if date and text:
            self.store.put(date, text, session.last_cost)

    # ------------------------------------------------------------ 调度

    def start_scheduler(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        # 启动时先试一次 —— 这就是「9 点后才开机」的补报路径
        time.sleep(3)
        self.trigger()
        while True:
            time.sleep(60)
            try:
                self.trigger()
            except Exception:
                pass
