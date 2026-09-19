# -*- coding: utf-8 -*-
"""备忘录:四种形态、一套倒计时。

  plain    纯文字,没有时间
  once     某一天某一刻
  weekly   每周某天某点
  monthly  每月某号某点

**倒计时在后端算,不在前端。** 前端只按秒重画那几个数字 —— 下一次该在什么
时候、这是第几个周期,这些得有一处权威答案,不然"重复备忘"的周期号会随着
页面什么时候打开而漂。

备忘可以挂一个 `link`:把作业、公告、邮件从别的栏拖到备忘录按钮上就会带上。
挂了 link 的备忘能点回原处(Canvas 链接开浏览器,邮件切到那一封)。
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

KINDS = ("plain", "once", "weekly", "monthly")
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _month_shift(d: datetime, months: int) -> datetime:
    """加/减若干个月,日不够就落在月底(1 月 31 号 + 1 月 = 2 月 28/29 号)。"""
    y, m = d.year, d.month + months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    day = d.day
    while day > 28:
        try:
            return d.replace(year=y, month=m, day=day)
        except ValueError:
            day -= 1
    return d.replace(year=y, month=m, day=day)


def next_time(memo: dict, now: datetime | None = None) -> tuple[datetime | None, int]:
    """下一次(或最近一次)该响的时间,以及这是第几个周期。

    返回 (时间, 周期号)。周期号从 1 开始,纯文字和 once 都是 0 —— 它们没有周期。

    **once 过期了也照样返回那个时间**,前端要显示"已过 n 天"。
    重复类的返回的是**下一次**:过了这次就自然滚到下一次,不用谁来清理。
    """
    kind = memo.get("kind") or "plain"
    now = now or _now()
    if kind == "plain":
        return None, 0
    if kind == "once":
        return _parse(memo.get("at")), 0

    hour = int(memo.get("hour", 9))
    minute = int(memo.get("minute", 0))
    created = _parse(memo.get("created")) or now

    if kind == "weekly":
        want = int(memo.get("weekday", created.weekday())) % 7
        # 从创建那天所在的周开始数
        first = created.replace(hour=hour, minute=minute, second=0)
        first += timedelta(days=(want - first.weekday()) % 7)
        if first < created:
            first += timedelta(days=7)
        n = 0
        t = first
        while t < now:
            t += timedelta(days=7)
            n += 1
        return t, n + 1

    if kind == "monthly":
        day = int(memo.get("day", created.day))
        first = created.replace(hour=hour, minute=minute, second=0)
        d = min(day, 28)
        first = first.replace(day=d)
        if day > 28:
            first = _month_shift(first, 0).replace(day=d)
        if first < created:
            first = _month_shift(first, 1)
        n = 0
        t = first
        while t < now:
            t = _month_shift(t, 1)
            n += 1
        return t, n + 1
    return None, 0


def humanize(delta: timedelta) -> str:
    """把一段时长说成「n 天 m 小时 x 分钟」。天/小时为 0 就不说。"""
    secs = int(abs(delta).total_seconds())
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    bits = []
    if d:
        bits.append(f"{d} 天")
    if h or d:
        bits.append(f"{h} 小时")
    if not bits and m == 0:
        return "不到 1 分钟"
    bits.append(f"{m} 分钟")
    return " ".join(bits)


def describe(memo: dict, now: datetime | None = None) -> dict:
    """算出界面上要显示的那一行。

    plain    -> {}(只显示文字)
    once     -> 还剩 / 已过
    weekly   -> 还剩 …(此为第 x 周期)
    monthly  -> 同上
    """
    now = now or _now()
    t, cycle = next_time(memo, now)
    if t is None:
        return {"when": "", "overdue": False, "cycle": 0, "at": ""}
    delta = t - now
    over = delta.total_seconds() < 0
    txt = ("已过 " if over else "还剩 ") + humanize(delta)
    if cycle:
        txt += f"(此为第 {cycle} 周期)"
    return {"when": txt, "overdue": over, "cycle": cycle,
            "at": t.strftime("%Y-%m-%d %H:%M"),
            "seconds": int(delta.total_seconds())}


class MemoStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._items: list[dict] = []
        try:
            d = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(d, dict) and isinstance(d.get("items"), list):
                self._items = d["items"]
        except Exception:
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": self._items}, ensure_ascii=False,
                                  indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, text: str, kind: str = "plain", **fields) -> dict:
        text = (text or "").strip()[:500]
        if not text:
            raise ValueError("备忘得有内容")
        if kind not in KINDS:
            kind = "plain"
        m = {
            "id": "m" + secrets.token_hex(5),
            "text": text,
            "kind": kind,
            "created": _now().strftime("%Y-%m-%dT%H:%M:%S"),
            "done": False,
            "link": fields.get("link") or None,
        }
        for k in ("at", "weekday", "day", "hour", "minute"):
            if fields.get(k) is not None:
                m[k] = fields[k]
        with self._lock:
            self._items.insert(0, m)
            self._save()
        return m

    def update(self, mid: str, patch: dict) -> dict | None:
        allow = {"text", "kind", "at", "weekday", "day", "hour", "minute",
                 "done", "link"}
        with self._lock:
            for m in self._items:
                if m["id"] == mid:
                    m.update({k: v for k, v in patch.items() if k in allow})
                    self._save()
                    return m
        return None

    def delete(self, mid: str) -> bool:
        with self._lock:
            n = len(self._items)
            self._items = [m for m in self._items if m["id"] != mid]
            if len(self._items) != n:
                self._save()
                return True
        return False

    def all(self, include_done: bool = True) -> list[dict]:
        """按"最该看的排前面"排:有时间的按下一次先到的在前,纯文字垫底。

        已完成的一律沉到最后 —— 留着能回看,但不占视线。
        """
        now = _now()
        with self._lock:
            items = [dict(m) for m in self._items]
        out = []
        for m in items:
            if m.get("done") and not include_done:
                continue
            m["info"] = describe(m, now)
            out.append(m)
        out.sort(key=lambda m: (
            bool(m.get("done")),
            0 if m["info"].get("at") else 1,
            m["info"].get("seconds", 0) if m["info"].get("at") else 0,
        ))
        return out

    def pending_count(self) -> int:
        """没完成的条数 —— 界面上那个角标用它。"""
        with self._lock:
            return sum(1 for m in self._items if not m.get("done"))
