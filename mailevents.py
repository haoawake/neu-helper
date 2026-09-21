# -*- coding: utf-8 -*-
"""邮件里抽出来的日程:拍平、去重、套上用户的删改,再交给周课表画。

**抽取本身不在这儿。** 那件事在 mailai:每封信过目的时候顺手把日程一起抽出来,
和标签、摘要、链接同一次调用,不另外花钱(mailai.clean_events 负责校验)。
结果写在 `data/mail_ai.json` 每封信自己那条记录的 events 里。

所以这个模块是**纯视图层**:读那份存档,产出"这一周该画什么"。三件事:

  拍平   一封信可能有两条日程,几百封信拍成一个列表
  去重   "Reminder: 面试明天" 连发三封 -> 一条。同一件事的多封提醒信里,
         留最新那封的版本(它的时间最可能是改过之后的)
  覆盖   用户删掉的、改过的,记在 `data/mail_events.json` 里,按条目 id 索引

**为什么覆盖层不写进 schedule.json。** 那个文件是课表的(自动抽的课时 + 手改
+ 手加),清空课表会把它整个重置 —— 邮件日程的删除记录跟着一起没,被删掉的
日程就全回来了。两份数据的生命周期不一样,存档也就分开。

**被删掉的不会回来。** 一封信只过一次模型,结论写死,所以删掉之后不会有人
再把它抽一遍。这一点和课表不同:课表每小时重抽,所以那边需要 hidden 标记来
压住"又被请回来"的条目。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import date
from pathlib import Path

# 去重时把标题归一化后取多少字。太短会把"面试初筛"和"面试终面"当成一件事,
# 太长则连"9/23 面试"和"面试(已改时间)"都算两件
DEDUP_CHARS = 12
# 一周最多画多少条邮件日程。**上限是防线不是显示偏好** —— 头一次装上、
# 存量几百封信一起过目的时候,抽出来的东西不该把课表淹掉
MAX_PER_WEEK = 40
_PUNCT = re.compile(r"[\s\-_—·、,,。.:：;;!!??()()\[\]【】「」『』\"'“”‘’/\\|]+")


def norm_title(s: str) -> str:
    """标题归一化 —— 只用来比"是不是同一件事",不显示。"""
    return _PUNCT.sub("", str(s or "")).lower()[:DEDUP_CHARS]


def event_id(mid: str, ev: dict) -> str:
    """条目的稳定 id = 内容哈希。

    和 timetable.item_id 一个道理:**不含用户可能改的字段**,所以改了标题、
    挪了时间之后仍然是同一条(删除和修改记录跟着还在)。日期变了算新条目 ——
    那多半真是另一件事了。
    """
    raw = "|".join([str(mid), str(ev.get("date") or ""),
                    norm_title(ev.get("title"))])
    return "mail:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def dedup_key(ev: dict) -> tuple:
    """同一件事的判断依据:哪天 + 几点(全天算一档)+ 标题的前几个字。"""
    return (str(ev.get("date") or ""),
            "allday" if ev.get("allday") else str(ev.get("start") or ""),
            norm_title(ev.get("title")))


def flatten(rows: list[dict]) -> list[dict]:
    """把每封信的 events 拍平成一个列表,顺手去重。

    rows 要按**收信时间从新到旧**排好(调用方负责)—— 去重留的是先遇到的
    那条,也就是最新那封信的说法。一件事被提醒三遍的时候,最后那封的时间
    最可能是改过之后的。
    """
    out: list[dict] = []
    seen: dict[tuple, dict] = {}
    for r in rows:
        mid = r.get("mid") or ""
        for ev in r.get("events") or []:
            if not isinstance(ev, dict) or not ev.get("date"):
                continue
            key = dedup_key(ev)
            it = dict(ev)
            it.update({
                "id": event_id(mid, ev),
                "mid": mid,
                "from_subject": str(r.get("subject") or "")[:120],
                "mail_day": str(r.get("day") or ""),
            })
            hit = seen.get(key)
            if hit is not None:
                # 同一件事又见到一次:把提了几遍记下来(界面上标"3 封信提过"),
                # 但内容仍然用最新那封的
                hit["dupes"] = int(hit.get("dupes") or 1) + 1
                continue
            seen[key] = it
            out.append(it)
    out.sort(key=lambda x: (x["date"], "" if x.get("allday") else x.get("start") or ""))
    return out


class EventStore:
    """用户对邮件日程的删改。**只存覆盖层**,日程本体在 mail_ai.json 里。"""

    ALLOW = {"title", "start", "end", "date", "kind", "note"}

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._d: dict = {"hidden": {}, "edits": {}}
        try:
            got = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(got, dict):
                for k in ("hidden", "edits"):
                    if isinstance(got.get(k), dict):
                        self._d[k] = got[k]
        except Exception:
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._d, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def hide(self, iid: str, on: bool = True) -> None:
        with self._lock:
            if on:
                self._d["hidden"][iid] = True
            else:
                self._d["hidden"].pop(iid, None)
            self._save()

    def edit(self, iid: str, patch: dict) -> None:
        """改一条。传 {} = 撤销修改,回到抽出来的样子。"""
        with self._lock:
            if not patch:
                self._d["edits"].pop(iid, None)
            else:
                cur = dict(self._d["edits"].get(iid) or {})
                cur.update({k: v for k, v in patch.items() if k in self.ALLOW})
                self._d["edits"][iid] = cur
            self._save()

    def reset(self) -> int:
        with self._lock:
            n = len(self._d["hidden"]) + len(self._d["edits"])
            self._d = {"hidden": {}, "edits": {}}
            self._save()
            return n

    def counts(self) -> dict:
        with self._lock:
            return {"hidden": len(self._d["hidden"]),
                    "edited": len(self._d["edits"])}

    def apply(self, events: list[dict]) -> list[dict]:
        """套上删改。删掉的直接不出现,改过的打上 edited 标记。"""
        with self._lock:
            hidden, edits = dict(self._d["hidden"]), dict(self._d["edits"])
        out = []
        for ev in events:
            iid = ev["id"]
            if hidden.get(iid):
                continue
            patch = edits.get(iid)
            if patch:
                ev = {**ev, **patch, "edited": True}
                # 时刻这一栏**只要出现在 patch 里**就说明用户明确动过它:
                # 填了 = 从全天变成格子里的块,清空 = 反过来变回全天。
                # 光看 "有没有值" 的话,"清空"会被当成"没改",改不回去
                if "start" in patch:
                    ev["allday"] = not patch["start"]
                    if ev["allday"]:
                        ev.pop("start", None)
                        ev.pop("end", None)
            out.append(ev)
        return out


def week_items(events: list[dict], week: list[str]) -> tuple[list[dict], list[dict]]:
    """挑出落在这一周的,分成"格子里的"和"全天的"两摞。

    week 是这一周七天的 YYYY-MM-DD(周日打头,和界面横轴一致)。**全天的
    不画成 00:00–23:59 的块** —— 那会把纵轴撑成 0–24,两门真课挤成一条缝。
    它们单独走表头下面那条全天条。
    """
    pos = {d: i for i, d in enumerate(week or [])}
    timed, allday = [], []
    for ev in events:
        wd = pos.get(ev.get("date"))
        if wd is None:
            continue
        row = {
            "id": ev["id"], "kind": "mail", "mail": True, "auto": False,
            "title": str(ev.get("title") or "")[:60],
            "weekday": wd, "date": ev["date"],
            "who": "", "place": "", "url": "", "course": "", "course_id": None,
            "edited": bool(ev.get("edited")),
            "mid": ev.get("mid") or "",
            "note": _note(ev),
            "mkind": ev.get("kind") or "other",
        }
        if ev.get("allday"):
            allday.append(row)
        else:
            row["start"] = ev.get("start") or "09:00"
            row["end"] = ev.get("end") or "10:00"
            timed.append(row)
        if len(timed) + len(allday) >= MAX_PER_WEEK:
            break
    return timed, allday


def _note(ev: dict) -> str:
    """块上/悬停时那行说明:这条是哪来的、原文写的是什么时候。"""
    bits = ["邮件"]
    if ev.get("was"):
        bits.append(f"原文 {ev['was']}")
    elif ev.get("tz"):
        # 认不出的时区(比如 CST,它既是美国中部也是中国标准时)——
        # 照实标出来,让人自己换算,别替他猜
        bits.append(f"原文写的是 {ev['tz']},没换算")
    if int(ev.get("dupes") or 1) > 1:
        bits.append(f"{ev['dupes']} 封信提过")
    if ev.get("from_subject"):
        bits.append(ev["from_subject"])
    return " · ".join(bits)


def today_rows(events: list[dict], today: str) -> list[dict]:
    """今天的邮件日程 —— 给仪表盘那行「今天接下来是什么」用。"""
    return [e for e in events if e.get("date") == today]


def upcoming(events: list[dict], today: str, days: int = 14) -> list[dict]:
    """今天起 days 天内的,按日期排好。给对话和简报用。"""
    try:
        t0 = date.fromisoformat(today)
    except ValueError:
        return []
    out = []
    for e in events:
        try:
            d = date.fromisoformat(str(e.get("date") or ""))
        except ValueError:
            continue
        if 0 <= (d - t0).days <= days:
            out.append(e)
    return out
