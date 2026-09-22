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
# 两条标题的碎片重合到这个比例就算同一件事(见 same_thing)。
# 0.6 是拿真实标题试出来的:"沃尔玛订单今日送达" vs "沃尔玛订单送达通知"
# 是 0.62 过线,"CS5800 期中考试" vs "CS5800 作业截止" 只撞一个碎片被挡下
SAME_RATIO = 0.6
# 一条日程最多记几封来源信。合并本身不设上限(该合的都合),但界面上
# 列十几封没有意义,存档也不该为一封营销信的连环提醒撑大
MAX_SOURCES = 8
# 一周最多画多少条邮件日程。**上限是防线不是显示偏好** —— 头一次装上、
# 存量几百封信一起过目的时候,抽出来的东西不该把课表淹掉
MAX_PER_WEEK = 40
_PUNCT = re.compile(r"[\s\-_—·、,,。.:：;;!!??()()\[\]【】「」『』\"'“”‘’/\\|]+")
_ASCII = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]+")
# 到处都是、撞上了也说明不了什么的词。**别往里加"order""订单"那类** ——
# 那恰恰是"沃尔玛订单送达"里最能认人的部分
_STOP = {"the", "a", "an", "of", "for", "to", "on", "at", "in", "is", "are",
         "your", "you", "we", "our", "and", "or", "it", "this", "that",
         "re", "fwd", "reminder", "notice", "update", "info"}


def norm_title(s: str) -> str:
    """标题归一化 —— 只用来比"是不是同一件事",不显示。"""
    return _PUNCT.sub("", str(s or "")).lower()[:DEDUP_CHARS]


def shingles(s: str) -> set:
    """把标题拆成可比的碎片。

    **中文按相邻两字、英文按整词。** 中文没有空格,按单字比会把"面试"和
    "考试"算成半像(共用一个"试");按 bigram 才抓得住词。英文反过来 ——
    "walmart" 拆成 bigram 会和一堆不相干的词沾亲,整词更准。
    """
    low = str(s or "").lower()
    out = {w for w in _ASCII.findall(low) if w not in _STOP and len(w) > 1}
    for run in _CJK.findall(low):
        if len(run) == 1:
            out.add(run)
            continue
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def same_thing(a: str, b: str) -> bool:
    """这两条日程说的是同一件事吗。

    用重合系数(交集 / 较短那个)而不是 Jaccard:"沃尔玛订单送达"是
    "沃尔玛订单预计今天送达"的子集,前者该判同,而 Jaccard 会因为后者更长
    把分数压下去。

    **至少要撞两个碎片。** 只撞一个的话"订单"这种烂大街的词就能把两件
    不相干的事粘一起 —— 合错比不合更糟,合错等于有一件事从课表上消失了。
    """
    x, y = shingles(a), shingles(b)
    if not x or not y:
        return False
    inter = len(x & y)
    if inter < 2:
        return False
    return inter / min(len(x), len(y)) >= SAME_RATIO


def _mins(hhmm: str) -> int:
    try:
        return int(hhmm[:2]) * 60 + int(hhmm[3:5])
    except (ValueError, IndexError):
        return 0


def same_slot(a: dict, b: dict) -> bool:
    """两条落在同一个时间位置上吗 —— 判"是不是同一件事"之前的那道闸。

    同一天是底线。两条都有时刻的还要**时间段真的相交**:同一天的
    「面试 10:00」和「面试 14:00」多半是两场,或者是改过期的,那种保持
    两条、让人自己看得见更安全。

    全天的和有时刻的算同一个位置 —— 全天本来就盖住一整天。快递最常见的
    就是这种组合:一封说"今天送到"(全天),一封说"15:00–16:00 送到"。
    """
    if a.get("date") != b.get("date"):
        return False
    if a.get("allday") or b.get("allday"):
        return True
    return (_mins(a.get("start") or "") < _mins(b.get("end") or "")
            and _mins(b.get("start") or "") < _mins(a.get("end") or ""))


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
    """同一件事的**严格**判断:哪天 + 几点(全天算一档)+ 标题的前几个字。

    宽松那套见 same_slot + same_thing。这个还留着是因为它零成本且绝不误判,
    一模一样的两条(同一封信被重抽、连发三遍的提醒)走这条快路就够了。
    """
    return (str(ev.get("date") or ""),
            "allday" if ev.get("allday") else str(ev.get("start") or ""),
            norm_title(ev.get("title")))


def _merge_into(hit: dict, it: dict) -> None:
    """把 it 并进 hit。hit 是先遇到的,也就是**更新的那封信**给的版本。

    内容用 hit 的(最新那封的时间最可能是改过之后的),但**时刻取更具体的
    那份**:一封说"今天送到"、一封说"15:00 送到",该画在 15:00 的格子里,
    而不是因为那封信更新就退回全天。
    """
    hit.setdefault("sources", []).append({
        "mid": it.get("mid") or "",
        "subject": it.get("from_subject") or "",
        "day": it.get("mail_day") or "",
    })
    hit["dupes"] = len(hit["sources"])
    # 所有并进来的 id 都留着:用户删的可能是其中任何一条,apply() 要全查一遍,
    # 不然"删掉的不会回来"这个承诺在合并之后就破了
    hit.setdefault("ids", []).append(it["id"])
    if hit.get("allday") and not it.get("allday"):
        hit["allday"] = False
        hit["start"] = it.get("start")
        hit["end"] = it.get("end")
        if it.get("was"):
            hit["was"] = it["was"]


def flatten(rows: list[dict]) -> list[dict]:
    """把每封信的 events 拍平成一个列表,顺手把同一件事合成一条。

    rows 要按**收信时间从新到旧**排好(调用方负责)—— 合并留的是先遇到的
    那条,也就是最新那封信的说法。一件事被提醒三遍的时候,最后那封的时间
    最可能是改过之后的。

    合并分两步走,和用户描述这件事的顺序一样:先看**同一天/同一个时段**
    有没有已经排上的(same_slot),有的话再判**是不是同一件事**
    (same_thing)。两步都过才合 —— 合错等于有一件事从课表上消失了,
    所以宁可多留一条。
    """
    out: list[dict] = []
    exact: dict[tuple, dict] = {}      # 一模一样的走这条快路
    by_date: dict[str, list[dict]] = {}
    for r in rows:
        mid = r.get("mid") or ""
        for ev in r.get("events") or []:
            if not isinstance(ev, dict) or not ev.get("date"):
                continue
            it = dict(ev)
            it.update({
                "id": event_id(mid, ev),
                "mid": mid,
                "from_subject": str(r.get("subject") or "")[:120],
                "mail_day": str(r.get("day") or ""),
            })
            key = dedup_key(ev)
            hit = exact.get(key)
            if hit is None:
                hit = next((h for h in by_date.get(it["date"], [])
                            if same_slot(h, it)
                            and same_thing(h.get("title"), it.get("title"))), None)
            if hit is not None:
                if len(hit.get("sources") or []) < MAX_SOURCES:
                    _merge_into(hit, it)
                else:
                    hit["dupes"] = int(hit.get("dupes") or 1) + 1
                continue
            # 头一条:自己也算一封来源信,这样界面上不用分"一封"和"多封"两套
            it["sources"] = [{"mid": mid, "subject": it["from_subject"],
                              "day": it["mail_day"]}]
            it["ids"] = [it["id"]]
            it["dupes"] = 1
            exact[key] = it
            by_date.setdefault(it["date"], []).append(it)
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
        """套上删改。删掉的直接不出现,改过的打上 edited 标记。

        **一条合并过的日程要按它所有的 id 查一遍。** 合并之后活下来的是最新
        那封信的 id,可用户当初删/改的可能是被并掉的那一条 —— 只查主 id 的话,
        一封新的提醒信进来就能把删掉的东西请回来,"删掉的不会回来"就成了空话。
        """
        with self._lock:
            hidden, edits = dict(self._d["hidden"]), dict(self._d["edits"])
        out = []
        for ev in events:
            iid = ev["id"]
            ids = ev.get("ids") or [iid]
            if any(hidden.get(x) for x in ids):
                continue
            patch = next((edits[x] for x in ids if edits.get(x)), None)
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
            # 合并进来的几封信。**总是给一个列表**(哪怕只有一封),
            # 界面就不用分"单封"和"多封"两套写法
            "mails": [
                {"mid": x.get("mid") or "", "subject": x.get("subject") or "",
                 "day": x.get("day") or ""}
                for x in (ev.get("sources") or [])[:8]
            ],
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
    n = int(ev.get("dupes") or 1)
    if n > 1:
        bits.append(f"{n} 封信说的是这件事")
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
