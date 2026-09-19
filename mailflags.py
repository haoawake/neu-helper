# -*- coding: utf-8 -*-
"""邮件的重点标注,以及 AI 还没过目时的内置粗判。

**为什么标注单独存一份**:`data/mail.json` 是个滚动缓存(只留最近 300 封),
标注必须比缓存活得久 —— 你盯的那封信三周后还在盯,不能因为被挤出缓存就丢了。
所以标注存 `data/mail_flags.json`,按邮件 id(`imap:<地址>#<UID>`)索引,
UID 在同一个邮箱里是稳定的。

**重要程度现在是 AI 判的**(见 [mailai.py](mailai.py)):它读结构化的个人信息和
标签目录,一封信只过一次模型,结果存下来永久复用。这里剩下的 `classify` 只有
一个用途 —— **AI 还没过目的那几十秒里给个粗判**,不然第一次启动会是满屏
「待分析」。粗判在界面上是标着的(虚线框),不会假装是定论。

以前这里有一套用户手写的 VIP/屏蔽关键词规则,删了:两个法官各判一遍,谁压谁
说不清,用户还得维护两处。想让某类邮件一律算噪音,写进「我的情况」就行。
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

LEVELS = {3: "盯", 2: "重要", 1: "普通", 0: "低"}
# 图标 + 文字一起给,不靠颜色单独承载信息(灰度打印和色觉障碍下也能区分)
LEVEL_ICONS = {3: "◎", 2: "▲", 1: "·", 0: "○"}

# 内置启发。刻意写得保守 —— 宁可漏判成"普通",也别把真事压成"低"。
VIP_HINTS = (
    "canvas", "assignment", "homework", "deadline", "due ", "exam", "midterm",
    "final", "grade", "professor", "instructor", "advisor", "registrar",
    "interview", "offer", "application", "visa", "i-20", "opt", "cpt",
    "作业", "截止", "考试", "成绩", "教授", "导师", "面试", "录用", "签证",
)
MUTE_HINTS = (
    "unsubscribe", "no-reply", "noreply", "newsletter", "promotion", "webinar",
    "sale", "% off", "coupon", "digest", "notification settings",
    "退订", "优惠", "促销", "活动邀请", "订阅",
)
EDU_DOMAINS = (".edu", ".edu.cn", "instructure.com")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hit(text: str, needles) -> str | None:
    for n in needles:
        if n and n in text:
            return n
    return None


class FlagStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict = {}
        try:
            d = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(d, dict):
                self._data = d
        except Exception:
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def get(self, mid: str) -> dict:
        with self._lock:
            return dict(self._data.get(mid) or {})

    def all(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def set(self, mid: str, *, star: bool | None = None, done: bool | None = None,
            note: str | None = None, meta: dict | None = None) -> dict:
        """改标注。meta 是邮件的摘要信息(主题/发件人/日期)——

        必须一起存下来:缓存滚动之后这封信可能已经不在 mail.json 里了,
        但简报还要提醒你"这封还没完成",那时候只能靠这份快照。
        """
        with self._lock:
            cur = dict(self._data.get(mid) or {})
            if star is not None:
                cur["star"] = bool(star)
                if star and not cur.get("star_at"):
                    cur["star_at"] = _now()
                if not star:
                    cur.pop("star_at", None)
            if done is not None:
                cur["done"] = bool(done)
                cur["done_at"] = _now() if done else None
            if note is not None:
                cur["note"] = note[:400]
            if meta:
                cur["meta"] = {k: meta.get(k) for k in
                               ("subject", "from", "date_local", "ts",
                                "account_label", "web_url")}
            # 既没标重点、也没备注、也没完成 —— 这条记录没有留存价值
            if not (cur.get("star") or cur.get("note") or cur.get("done")):
                self._data.pop(mid, None)
            else:
                cur["updated"] = _now()
                self._data[mid] = cur
            self._save()
            return dict(self._data.get(mid) or {})

    def watching(self) -> list[dict]:
        """还在盯的(标了重点、没标完成)。按标注时间从早到晚 —— 越早的越该催。"""
        with self._lock:
            out = []
            for mid, f in self._data.items():
                if f.get("star") and not f.get("done"):
                    out.append({"id": mid, **f})
        out.sort(key=lambda x: x.get("star_at") or "")
        return out

    def days_since(self, f: dict) -> int | None:
        raw = f.get("star_at")
        if not raw:
            return None
        try:
            t = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return max(0, (datetime.now() - t).days)


def classify(msg: dict, flags: dict) -> dict:
    """内置粗判 —— 只在 AI 还没过目这封信的时候用。

    返回 {level, label, icon, why}。why 是"为什么是这个级别",界面上悬停能看到;
    这个粗判只看关键词,所以 why 里会写清是"像"而不是"是"。
    """
    hay = " ".join([
        (msg.get("subject") or ""), (msg.get("from") or ""),
        (msg.get("snippet") or "")[:400],
    ]).lower()
    sender = (msg.get("from") or "").lower()

    hit = _hit(sender, EDU_DOMAINS)
    if hit:
        return {"level": 2, "label": LEVELS[2], "icon": LEVEL_ICONS[2],
                "why": f"粗判:发件人是学校域名({hit})"}
    hit = _hit(hay, VIP_HINTS)
    if hit:
        return {"level": 2, "label": LEVELS[2], "icon": LEVEL_ICONS[2],
                "why": f"粗判:内容里有「{hit}」"}
    hit = _hit(hay, MUTE_HINTS)
    if hit:
        return {"level": 0, "label": LEVELS[0], "icon": LEVEL_ICONS[0],
                "why": f"粗判:像营销/自动通知(「{hit}」)"}
    return {"level": 1, "label": LEVELS[1], "icon": LEVEL_ICONS[1],
            "why": "粗判:没命中任何特征"}


def briefing_block(store: FlagStore) -> str:
    """简报里的「还在盯」那一段。

    这是"标了重点就一直提醒到完成"的实现:每天把未完成的重点连同"盯了几天"
    一起放进 prompt,并明确要求简报里必须逐条点名。
    """
    watching = store.watching()
    if not watching:
        return ""
    lines = ["<还在盯的邮件>",
             "这些是我自己标成重点、而且还没标完成的。**每一条都要在简报里点名**,",
             "带上盯了几天;盯得越久语气越重。不要因为昨天提过就省略。", ""]
    for w in watching:
        meta = w.get("meta") or {}
        d = store.days_since(w)
        age = f"盯了 {d} 天" if d is not None else "刚标的"
        lines.append(f"- [{age}] {meta.get('date_local') or ''} "
                     f"来自 {meta.get('from') or '?'}")
        lines.append(f"  主题:{meta.get('subject') or '(无主题)'}")
        if w.get("note"):
            lines.append(f"  我的备注:{w['note']}")
    lines.append("</还在盯的邮件>")
    lines.append("")
    return chr(10).join(lines)
