# -*- coding: utf-8 -*-
"""每周课表:上课时间和 office hour 是**从课程正文里抽出来的**,不是查出来的。

Canvas 没有这两样的结构化接口。实测(2026 秋):

  /appointment_groups          空 —— 两位老师都没用 Canvas 的 Scheduler
  /calendar_events (整学期)    空
  /courses/:id/sections        只有 section 名字,没有 meeting time

时间全是老师写在**首页表格 / syllabus / 公告**里的散文,长这样:

  "Section 21, CRN 19658: Wednesdays 11:00 am - 2:20 pm  Room 1010"
  "Office Hours:  Monday 2:00 – 3:00 PM (on campus)"

所以这里的分工是:Python 负责把可能写了时间的正文都捞出来(gather),模型负责
把散文变成结构化条目(parse)。和 mailai.py 同一个套路 —— 过一次模型、结果
存档,之后界面渲染、拖动、编辑读的都是存档,不会反复烧钱。

**合并课要挑对 section。** CS5800 把周一班和周三班并成一门课,不知道自己注册
的是哪一节就会两节都画上。my_sections() 给出 section id,提示词里点名"只要这
一节",实测能挑对。

**手改优先于解析。** 用户在界面上改过的条目记在 edits 里,按条目内容哈希索引;
重新解析不会把它冲掉。删掉的条目记成 {"hidden": true},同理 —— 不然每次重新
解析都会把它请回来。

存档 data/schedule.json:

  courses: {"<课程id>": {fp, at, model, items: [...], notes: [...]}}
      fp    源文本的指纹。没变就不重新解析(省钱,也免得模型每次抽得不一样)
      notes 模型看出来但排不进格子的话,比如"TA office hour 还没公布"
  manual:  用户自己加的条目(不属于任何一次解析,重新解析不动它)
  edits:   {"<条目id>": {...覆盖字段...} 或 {"hidden": true}}
  cost:    累计花了多少钱
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from canvas_api import html_to_text
from chat_bridge import find_claude
from desktop import NO_WINDOW as _NO_WINDOW

# 横轴是周日→周六,所以 0=周日。和 JS 的 Date.getDay() 对齐,前端不用再换算。
# 注意 Python 的 date.weekday() 是 0=周一 —— 跨过来要用 PY2JS。
WEEK_CN = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
KINDS = ("lecture", "office", "other")
TIMEOUT = 180
# 一门课送进去的正文上限。首页 + syllabus + 公告加起来能有两万字,而时间信息
# 永远在前面几段 —— 截断是为了省钱,不是怕超上下文。
PER_SOURCE = 6000

SYSTEM = ("你从课程网页里抽取每周固定时间安排。只输出 JSON,不解释、不寒暄、"
          "不要用 markdown 围栏。不要调用任何工具。")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def item_id(course_id, it: dict) -> str:
    """条目的稳定 id = 内容哈希。

    故意**不含**用户可能改的那些字段(地点、链接、备注) —— 只要课、类型、星期、
    起止时间没变,就还是同一条,手改就还在。时间真变了则视为新条目,旧的那条
    编辑跟着失效,这是对的:老师把 office hour 挪了,你之前改的备注多半也过期了。
    """
    raw = "|".join([str(course_id), it.get("kind", ""), str(it.get("weekday")),
                    it.get("start", ""), it.get("end", ""), it.get("title", "")])
    return "s" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------- 捞正文


# <a href="X">Y</a> -> Y [X]。html_to_text 只留锚文本,而 office hour 那一栏
# 的 Zoom / 预约链接全在 href 里(DS5110 的锚文本就是"1:1 with Mohammad",
# 光看文字拿不到链接)。得在压成纯文本之前先把 URL 拽出来。
# **方括号不能换成尖括号** —— html_to_text 的去标签正则会把 <http://…> 当成
# 一个标签整个吃掉,链接就又没了。
_A_TAG = re.compile(r"""(?is)<a\b[^>]*\bhref=["']([^"']+)["'][^>]*>(.*?)</a>""")


def _keep_links(raw: str) -> str:
    def sub(m):
        url, inner = m.group(1), m.group(2)
        if url.startswith(("mailto:", "#")) or url in inner:
            return inner
        return f"{inner} [{url}]"
    return _A_TAG.sub(sub, raw or "")


def gather(c, course: dict, section_ids: list[int], anns: list[dict]) -> dict:
    """把一门课里可能写了时间的正文捞成一段纯文本,连同指纹。

    顺序是有讲究的:首页排最前,因为老师最爱把课时表放那儿(CS5800 就是),
    模型看到的第一份材料应该是最可能有答案的那份。
    """
    cid = course["id"]
    parts: list[str] = []

    try:
        secs = c.sections(cid)
    except Exception:
        secs = []
    mine = [s for s in secs if s["id"] in set(section_ids)]
    if secs:
        parts.append("## 这门课的 section\n" + "\n".join(
            f"- {s['name']}" + ("   ← **我注册的就是这一节**" if s in mine else "")
            for s in secs))

    try:
        for p in c.page_bodies(cid):
            txt = html_to_text(_keep_links(p["body"]), PER_SOURCE)
            if txt:
                parts.append(f"## 页面「{p['title']}」"
                             + ("(课程首页)" if p.get("front") else "")
                             + "\n" + txt)
    except Exception:
        pass

    try:
        syl = html_to_text(_keep_links(c.syllabus(cid)), PER_SOURCE)
        if syl:
            parts.append("## Syllabus\n" + syl)
    except Exception:
        pass

    for a in anns[:8]:
        body = (a.get("body") or "")[:1500]
        if body:
            parts.append(f"## 公告「{a.get('title', '')}」({a.get('posted', '')})\n"
                         + body)

    text = "\n\n".join(parts)
    return {"text": text, "fp": hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]}


def build_prompt(course: dict, src: str) -> str:
    return f"""下面是 Canvas 上一门课的页面、syllabus 和公告的正文。把其中**每周固定重复**的时间安排抽出来。

课程:{course.get('short') or course.get('code')} —— {course.get('name')}

要抽的两类:
1. kind="lecture" —— 上课时间。**只要我注册的那一节**(上面标了「我注册的就是这一节」的 section;合并课会列出好几节别人的课,那些一律不要)
2. kind="office" —— 教授或 TA 的 office hour。每个人一条;同一个人一周有两场就两条

规矩:
- 只要**每周都有**的。一次性的考试、某天的讲座、fall break 都不要
- 时间写 24 小时制 "HH:MM"。"11 AM - 2:20 PM" → start "11:00", end "14:20"
- 时间照抄原文,**不要换算时区**
- weekday:周日=0 周一=1 周二=2 周三=3 周四=4 周五=5 周六=6
- 只写了"by appointment"、"TBA"、时间表是空的 —— **不要编**,不出条目,
  改成往 notes 里写一句中文说明(例如 "TA office hour 还没公布")
- 拿不准起止时间的不要出条目,同样写进 notes

每条字段:
  kind     lecture / office / other
  title    短,中文,例如 "CS5800 讲课" / "Hamandi office hour"
  who      老师或 TA 的名字,没有就 ""
  weekday  0-6
  start    "HH:MM"
  end      "HH:MM"
  place    教室号、"on campus"、"Zoom" 之类,没有就 ""
  url      Zoom / 预约链接,没有就 ""
  note     补充一句,例如 "SEC 21 · CRN 19658",没有就 ""

只输出这个形状的 JSON:
{{"items": [{{"kind": "lecture", "title": "…", "who": "…", "weekday": 3, "start": "11:00", "end": "14:20", "place": "1010", "url": "", "note": "…"}}], "notes": ["…"]}}

一条都抽不出来就 {{"items": [], "notes": ["…为什么…"]}}。

========== 正文 ==========
{src}
"""


# ---------------------------------------------------------------- 解析


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")
_HHMM = re.compile(r"^([0-2]?\d):([0-5]\d)$")


def _clean_time(v) -> str | None:
    m = _HHMM.match(str(v or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), m.group(2)
    if h > 24:
        return None
    return f"{h:02d}:{mi}"


def _need_time(v, fallback: str) -> str:
    """手填的时间:没填就用默认,填了但不成立就报错。

    **不能填错了也悄悄用默认值** —— 那会凭空造出一条 09:00 的安排,
    而用户以为自己填的是别的。界面上是 <input type=time> 走不到这儿,
    但接口是接口,不能指望调用方。
    """
    if v in (None, ""):
        return fallback
    t = _clean_time(v)
    if not t:
        raise ValueError(f"看不懂的时间「{v}」,要 HH:MM")
    return t


def parse_result(raw: str) -> dict:
    """把模型输出变成干净条目。**格式不是合同** —— 剥围栏、抠大括号、逐字段过。"""
    txt = _FENCE.sub("", (raw or "").strip())
    d = None
    try:
        d = json.loads(txt)
    except Exception:
        i, j = txt.find("{"), txt.rfind("}")
        if i >= 0 and j > i:
            try:
                d = json.loads(txt[i:j + 1])
            except Exception:
                d = None
    if not isinstance(d, dict):
        return {"items": [], "notes": []}

    items = []
    for x in (d.get("items") or []):
        if not isinstance(x, dict):
            continue
        start, end = _clean_time(x.get("start")), _clean_time(x.get("end"))
        if not start or not end or end <= start:
            continue      # 时间不成立的条目画不出格子,不如丢掉
        try:
            wd = int(x.get("weekday"))
        except (TypeError, ValueError):
            continue
        if not 0 <= wd <= 6:
            continue
        kind = x.get("kind") if x.get("kind") in KINDS else "other"
        items.append({
            "kind": kind,
            "title": str(x.get("title") or "").strip()[:60] or "(无标题)",
            "who": str(x.get("who") or "").strip()[:60],
            "weekday": wd,
            "start": start,
            "end": end,
            "place": str(x.get("place") or "").strip()[:60],
            "url": str(x.get("url") or "").strip()[:400],
            "note": str(x.get("note") or "").strip()[:120],
        })
    notes = [str(n).strip()[:120] for n in (d.get("notes") or []) if str(n).strip()]
    return {"items": items, "notes": notes[:4]}


# ---------------------------------------------------------------- 存档


class ScheduleStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._d: dict = {"courses": {}, "manual": [], "edits": {}, "cost": 0.0}
        try:
            got = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(got, dict):
                self._d.update({k: v for k, v in got.items() if k in self._d})
        except Exception:
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._d, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def fingerprint(self, course_id) -> str:
        with self._lock:
            return (self._d["courses"].get(str(course_id)) or {}).get("fp", "")

    def put_course(self, course_id, fp: str, got: dict, model: str,
                   cost: float = 0.0) -> None:
        with self._lock:
            self._d["courses"][str(course_id)] = {
                "fp": fp, "at": _now(), "model": model,
                "items": got["items"], "notes": got["notes"],
            }
            self._d["cost"] = round(self._d.get("cost", 0.0) + cost, 4)
            self._save()

    def edit(self, iid: str, patch: dict) -> None:
        """改一条自动抽出来的条目。传 {} 就是撤销修改、回到解析出来的样子。"""
        allow = {"title", "who", "weekday", "start", "end", "place", "url",
                 "note", "kind", "hidden"}
        with self._lock:
            if not patch:
                self._d["edits"].pop(iid, None)
            else:
                cur = dict(self._d["edits"].get(iid) or {})
                cur.update({k: v for k, v in patch.items() if k in allow})
                self._d["edits"][iid] = cur
            self._save()

    def add_manual(self, it: dict) -> dict:
        m = {
            "id": "u" + hashlib.sha1(
                (_now() + str(it)).encode("utf-8")).hexdigest()[:9],
            "kind": it.get("kind") if it.get("kind") in KINDS else "other",
            "title": str(it.get("title") or "").strip()[:60] or "(无标题)",
            "who": str(it.get("who") or "").strip()[:60],
            "weekday": max(0, min(6, int(it.get("weekday") or 0))),
            "start": _need_time(it.get("start"), "09:00"),
            "end": _need_time(it.get("end"), "10:00"),
            "place": str(it.get("place") or "").strip()[:60],
            "url": str(it.get("url") or "").strip()[:400],
            "note": str(it.get("note") or "").strip()[:120],
            "course_id": it.get("course_id"),
            "course": str(it.get("course") or "").strip()[:20],
        }
        if m["end"] <= m["start"]:
            raise ValueError("结束时间要晚于开始时间")
        with self._lock:
            self._d["manual"].append(m)
            self._save()
        return m

    def edit_manual(self, iid: str, patch: dict) -> bool:
        allow = {"title", "who", "weekday", "start", "end", "place", "url",
                 "note", "kind", "course", "course_id"}
        with self._lock:
            for m in self._d["manual"]:
                if m["id"] == iid:
                    new = dict(m)
                    for k, v in patch.items():
                        if k not in allow:
                            continue
                        if k in ("start", "end"):
                            v = _need_time(v, m[k])
                        if k == "weekday":
                            v = max(0, min(6, int(v)))
                        new[k] = v
                    if new["end"] <= new["start"]:
                        raise ValueError("结束时间要晚于开始时间")
                    # 先在副本上改完、校验过再落地 —— 中途抛异常不该留下改了一半的条目
                    m.update(new)
                    self._save()
                    return True
        return False

    def delete(self, iid: str) -> bool:
        """删条目。手加的真删;自动抽的只能藏(不然下次解析又回来了)。"""
        with self._lock:
            n = len(self._d["manual"])
            self._d["manual"] = [m for m in self._d["manual"] if m["id"] != iid]
            if len(self._d["manual"]) != n:
                self._save()
                return True
        self.edit(iid, {"hidden": True})
        return True

    def reset(self) -> None:
        """全部忘掉,包括手改和手加的。设置里那个「清空课表」用它。"""
        with self._lock:
            self._d = {"courses": {}, "manual": [], "edits": {}, "cost": 0.0}
            self._save()

    def view(self, courses: list[dict] | None = None) -> dict:
        """界面要的那份:自动条目套上手改、藏起该藏的、并上手加的。

        `courses` 是**现在**在读的那几门。存档是一直往上攒的,所以里面还留着
        上学期解析过的课 —— 那些课时不能再画进格子,否则换了学期旧课表还挂在
        那儿(老师忘了结课的话,它连"这门课没了"都看不出来)。解析结果留着
        不删:万一那门课又回到在读列表,不用再花一次模型钱。

        被删掉的条目走 `hidden` 一栏单独报上去,**不是丢掉** —— 界面要能
        告诉人"这儿本来有三节课,是你删的",并且给一条恢复的路。

        名单是空的(仪表盘还没抓完)就全部照画 —— 那是"还不知道",
        不是"都过期了"。
        """
        with self._lock:
            d = json.loads(json.dumps(self._d))   # 深拷贝,下面要改
        short = {str(x["id"]): x.get("short") or x.get("code")
                 for x in (courses or [])}
        items, notes, parsed, quiet, gone = [], [], [], [], []
        for cid, box in d["courses"].items():
            if short and cid not in short:
                continue
            cs = short.get(cid, cid)
            # 一条都没抽出来的课(培训模块、orientation)只会产出一句
            # "这门课没有每周固定安排" —— 那不是信息,是噪音。收进 quiet,
            # 界面上用一行带过,不占 notes 那几行
            if not box.get("items"):
                quiet.append(cs)
                parsed.append({"course_id": int(cid), "course": cs,
                               "at": box.get("at"), "model": box.get("model"),
                               "n": 0})
                continue
            for it in box.get("items", []):
                iid = item_id(cid, it)
                patch = d["edits"].get(iid) or {}
                if patch.get("hidden"):
                    # **藏起来的也要报上去。** 藏了就不画格子,于是它从界面上
                    # 彻底消失 —— 没有任何入口能把它找回来。更糟的是 item_id
                    # 是按内容算的:重新解析,同一节课回来的还是同一个 id,
                    # 照样被这条 hidden 压住。于是"点了重新解析还是空的"
                    # 会一直持续下去,看着就像解析坏了。踩过。
                    gone.append({
                        "id": iid, "course": cs, "course_id": int(cid),
                        "kind": it.get("kind") or "other",
                        "title": it.get("title") or "",
                        "weekday": it.get("weekday"),
                        "start": it.get("start") or "",
                        "end": it.get("end") or "",
                    })
                    continue
                row = {**it, **{k: v for k, v in patch.items() if k != "hidden"},
                       "id": iid, "course_id": int(cid), "course": cs,
                       "auto": True, "edited": bool(patch)}
                items.append(row)
            for n in box.get("notes", []):
                notes.append({"course": cs, "text": n})
            parsed.append({"course_id": int(cid), "course": cs,
                           "at": box.get("at"), "model": box.get("model"),
                           "n": len(box.get("items", []))})
        for m in d["manual"]:
            items.append({**m, "auto": False, "edited": False,
                          "course": m.get("course") or ""})
        items.sort(key=lambda x: (x["weekday"], x["start"]))
        gone.sort(key=lambda x: (x["course"], x["weekday"] or 0, x["start"]))
        return {"items": items, "notes": notes, "parsed": parsed,
                "quiet": quiet, "hidden": gone,
                "cost": round(d.get("cost", 0.0), 4)}


# ---------------------------------------------------------------- 跑模型


def run_one(project_dir: Path, course: dict, src: str, model: str) -> tuple[dict, float]:
    """解析一门课。返回 (条目, 这次花了多少钱)。"""
    exe = find_claude()
    if not exe:
        raise RuntimeError("找不到 claude 命令")
    argv = [exe, "-p", "--output-format", "json", "--model", model or "sonnet",
            "--system-prompt", SYSTEM, "--strict-mcp-config", "--restricted"]
    p = subprocess.run(argv, input=build_prompt(course, src), capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       cwd=str(project_dir), timeout=TIMEOUT,
                       creationflags=_NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(f"claude 退出码 {p.returncode}:"
                           f"{(p.stderr or p.stdout or '').strip()[:200]}")
    env = json.loads(p.stdout or "{}")
    cost = float(env.get("total_cost_usd") or 0)
    if env.get("is_error"):
        raise RuntimeError(str(env.get("result"))[:200])
    raw = env.get("result", "")
    got = parse_result(raw)
    if not got["items"] and not got["notes"]:
        # 原始输出留在 data/app.log —— 开机是 pythonw 起的,没有控制台可看
        print(f"[schedule] {course.get('short')} 解析不出东西(共 {len(raw)} 字符)。"
              f"开头:{raw[:400]!r}", file=sys.stderr, flush=True)
        raise RuntimeError(f"模型没给出能解析的 JSON(开头是「{raw.strip()[:40]}…」,"
                           f"全文见 data/app.log)")
    return got, cost


# ---------------------------------------------------------------- 备忘录

# memos.py 的 weekday 是 Python 口径(0=周一),课表是 JS 口径(0=周日)
PY2JS = [1, 2, 3, 4, 5, 6, 0]


def memo_items(memos: list[dict], week: list[str]) -> list[dict]:
    """把备忘录里有时刻的那些变成课表条目。

    weekly 每周都画;once 只有落在这一周才画(week 是这一周七天的 YYYY-MM-DD)。
    纯文字和每月的不画 —— 前者没时间,后者一个月一次,占格子不划算。
    备忘只有一个时刻没有时长,一律按 30 分钟的块画。
    """
    out = []
    for m in memos:
        if m.get("done"):
            continue
        kind = m.get("kind")
        hh, mm = int(m.get("hour", 9)), int(m.get("minute", 0))
        wd = None
        if kind == "weekly":
            wd = PY2JS[int(m.get("weekday", 0)) % 7]
        elif kind == "once":
            at = (m.get("info") or {}).get("at") or m.get("at") or ""
            day, _, clock = at.partition(" ")
            if day not in week:
                continue
            wd = week.index(day)
            if ":" in clock:
                hh, mm = int(clock.split(":")[0]), int(clock.split(":")[1])
        if wd is None:
            continue
        start = f"{hh:02d}:{mm:02d}"
        end = f"{min(hh + (mm + 30) // 60, 23):02d}:{(mm + 30) % 60:02d}"
        out.append({
            "id": "memo:" + m["id"], "kind": "memo", "title": m.get("text", "")[:60],
            "who": "", "weekday": wd, "start": start,
            "end": end if end > start else "23:59",
            "place": "", "url": "", "note": "备忘录", "course": "",
            "course_id": None, "auto": False, "edited": False, "memo": True,
        })
    return out
