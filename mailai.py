# -*- coding: utf-8 -*-
"""邮件的 AI 过目:打标签、评重要程度、写一句话摘要。

**每封信只过一次模型。** 结果存 `data/mail_ai.json`,按邮件 id 索引;之后无论是
列表渲染、按标签筛选,还是每天的邮件简报,读的都是这份存档,不会再把正文送进
任何 prompt。这是这个模块存在的理由 —— 82 封信分析一次几毛钱,但要是每天简报
都把 60 封正文重念一遍,那就是每天几毛钱、而且大部分是重复的。

调用形状是实测出来的(见 scratchpad/probe_eval.py):

  claude -p --output-format json --model sonnet
         --system-prompt <两句话> --strict-mcp-config --restricted

- **prompt 走 stdin**,不走 argv。Windows 命令行上限 32767 字符,一批 25 封
  正文就能到 10KB,走 argv 早晚要撞墙
- `--system-prompt` 把默认那套系统提示整个换掉;`--strict-mcp-config` 让它
  忽略 canvas 那个 MCP server(否则 9 个工具的定义白占上下文);`--restricted`
  砍掉执行类工具。这是个纯分类任务,不需要任何工具
- `--model` 可配。sonnet 实测 20 封 $0.10 / 16 秒,质量够用(它认得出
  「某某公寓」是我的住处、领英推送是求职噪音);嫌贵可以在设置里换 haiku

返回的是干净 JSON(实测不带 ```` ``` ```` 围栏),但解析仍然做了剥围栏的兜底 ——
模型输出格式不是合同,不能赌。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from chat_bridge import find_claude

from desktop import NO_WINDOW as _NO_WINDOW  # 起子进程的标志位,见 desktop.py

# 一批多少封。固定开销(我的情况 + 标签目录 + 格式说明)大概 800 字符,
# 批越大摊得越薄;但太大了模型容易漏条、也更容易超时。25 是实测的折中。
BATCH = 25
# 一轮最多分析多少封 —— 防止第一次装上就一口气烧掉几块钱
MAX_PER_RUN = 120
TIMEOUT = 180
# 存档上限。掉出来的是最老的 —— 那些邮件早就滚出 mail.json 了
KEEP = 2000

DEFAULT_TAGS = [
    "学业", "求职", "工作", "会议", "住房", "财务",
    "行政", "社交", "广告", "娱乐", "出行", "健康",
    "验证码", "系统通知",
]

SYSTEM = ("你是一个邮件分类器。只输出 JSON,不解释、不寒暄、不要用 markdown 围栏。"
          "不要调用任何工具。")

LEVELS = {3: "要紧", 2: "留意", 1: "普通", 0: "噪音"}
ICONS = {3: "◎", 2: "▲", 1: "·", 0: "○"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class TagStore:
    """AI 过目的结果。一封信一条,写进去之后就不再动它。"""

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

    def has(self, mid: str) -> bool:
        with self._lock:
            return mid in self._data

    def put_many(self, results: dict[str, dict]) -> None:
        with self._lock:
            self._data.update(results)
            if len(self._data) > KEEP:
                keep = sorted(self._data.items(),
                              key=lambda kv: kv[1].get("at") or "",
                              reverse=True)[:KEEP]
                self._data = dict(keep)
            self._save()

    def drop(self, mids) -> int:
        """扔掉这些信的分析结果 —— 下一轮会重新过目。改了个人信息或者标签目录
        之后想让它重判,就用这个。"""
        with self._lock:
            n = 0
            for mid in mids:
                if self._data.pop(mid, None) is not None:
                    n += 1
            if n:
                self._save()
            return n

    def clear(self) -> int:
        with self._lock:
            n = len(self._data)
            self._data = {}
            self._save()
            return n

    def count(self) -> int:
        with self._lock:
            return len(self._data)

    def tag_counts(self, ids=None) -> dict[str, int]:
        """每个标签底下有几封。ids 给了就只统计这些信(列表里实际有的那些)。"""
        with self._lock:
            items = (self._data.items() if ids is None
                     else ((k, v) for k, v in self._data.items() if k in ids))
            out: dict[str, int] = {}
            for _, v in items:
                for t in v.get("tags") or []:
                    out[t] = out.get(t, 0) + 1
            return out


def _facts_block(facts) -> list[str]:
    """把设置里那几行「我的专业 / 我的住址 / …」摊成 prompt 里的一段。

    刻意是**结构化**的键值对而不是一大段自述:键值对写起来没负担(填一行就多
    一条依据),模型读起来也不会把"我在找实习"和"我住在哪儿"混成一团。
    """
    rows = []
    for f in facts or []:
        k = str(f.get("k") or "").strip()
        v = str(f.get("v") or "").strip()
        if k and v:
            rows.append(f"- {k}:{v}")
        elif v:
            rows.append(f"- {v}")
    return rows


def build_prompt(msgs: list[dict], facts, tags) -> str:
    catalog = [t for t in (tags or DEFAULT_TAGS) if str(t).strip()]
    p = ["给下面每封邮件打标签、评重要程度、写一句话摘要。", ""]
    rows = _facts_block(facts)
    if rows:
        p += ["我的情况(判断「这封对我重不重要」以这个为准,不要套通用标准):"] + rows + [""]
    p += [
        "现有标签(**优先从里面选**;都不合适才新造一个 —— "
        "新造的要简短、是个类别而不是一句描述):",
        "  " + "、".join(catalog),
        "",
        "重要程度 level:",
        "  3 = 必须我亲自处理,而且有期限",
        "  2 = 需要我知道或者要回一句",
        "  1 = 普通,知道就行",
        "  0 = 噪音(营销、自动通知、社交网站推送)",
        "",
        # 实测里 5 封验证码全被判成 2 —— 模型把"账号安全"当成了要紧事。
        # 验证码用过就没价值了,但"我没发起过的"密码修改提醒是真要紧,得分开说
        "两个容易判错的:验证码本身用过就没用了,算 0;但**我没发起过**的",
        "密码修改、异地登录提醒算 2。钓鱼/诈骗邮件也算 2,并且在 summary 里",
        "直接说别点。",
        "",
        "每封**至少一个标签**,最多三个。summary 一句话说清「这封信要我干什么」,"
        "不要复述主题。why 一句话说清为什么是这个 level。",
        "",
        "只输出一个 JSON 数组,每封一项,i 是下面的序号:",
        '[{"i":1,"tags":["学业"],"level":2,"summary":"…","why":"…"}]',
        "",
        "邮件:",
    ]
    for i, m in enumerate(msgs, 1):
        snip = (m.get("snippet") or "").replace("\n", " ").replace("\r", " ")
        p.append(f"{i}. 日期 {m.get('date_local')} 发件人 {m.get('from')}")
        p.append(f"   主题 {m.get('subject')}")
        if snip.strip():
            p.append(f"   正文 {snip[:300]}")
    return "\n".join(p)


def parse_result(raw: str, n: int) -> list[dict]:
    """把模型那一坨变成 n 条结果。缺的、乱的一律丢掉,不硬凑。"""
    s = (raw or "").strip()
    data = None
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        # 兜底:剥 markdown 围栏,再按最外层的方括号截
        if s.startswith("```"):
            s = s.split("\n", 1)[-1].rsplit("```", 1)[0]
        a, b = s.find("["), s.rfind("]")
        if a >= 0 and b > a:
            try:
                data = json.loads(s[a:b + 1])
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if not 1 <= i <= n:
            continue
        tags = [str(t).strip() for t in (item.get("tags") or []) if str(t).strip()]
        try:
            lv = int(item.get("level"))
        except (TypeError, ValueError):
            lv = 1
        out.append({
            "i": i,
            # 一封至少一个标签 —— 模型没给就兜个"未分类",不能留空
            "tags": tags[:3] or ["未分类"],
            "level": max(0, min(3, lv)),
            "summary": str(item.get("summary") or "")[:200],
            "why": str(item.get("why") or "")[:200],
        })
    return out


class Analyzer:
    """后台把还没过目的邮件分批送去分析。

    只在有新邮件进来、或者用户手点的时候跑;跑完一封就再也不会碰它。
    """

    def __init__(self, store, tags: TagStore, project_dir: Path,
                 facts_getter=None, catalog_getter=None, model_getter=None,
                 on_event=None, on_new_tags=None):
        self.store = store                  # MailStore
        self.tags = tags
        self.project_dir = Path(project_dir)
        self.facts_getter = facts_getter or (lambda: [])
        self.catalog_getter = catalog_getter or (lambda: DEFAULT_TAGS)
        self.model_getter = model_getter or (lambda: "sonnet")
        self.on_event = on_event
        self.on_new_tags = on_new_tags       # 模型新造的标签 -> 存回目录
        self._lock = threading.Lock()
        self.state = {"running": False, "done": 0, "total": 0, "last": None,
                      "cost": 0.0, "errors": [], "model": ""}

    # ── 状态

    def snapshot(self) -> dict:
        d = dict(self.state)
        d["errors"] = list(d["errors"])[-3:]
        d["pending"] = len(self.pending())
        d["analyzed"] = self.tags.count()
        return d

    def _emit(self) -> None:
        if self.on_event:
            try:
                self.on_event(self.snapshot())
            except Exception:
                pass

    # ── 待办

    def pending(self, limit: int | None = None) -> list[dict]:
        """本地有、但还没过目的。新的排前面 —— 先分析用户马上会看到的那些。"""
        out = [m for m in self.store.all(limit=10000) if not self.tags.has(m["id"])]
        return out[:limit] if limit else out

    # ── 跑

    def analyze_now(self, force_all: bool = False) -> bool:
        """返回有没有真的开跑(已经在跑、或者没什么可分析的都返回 False)。"""
        if force_all:
            self.tags.clear()
        if not self.pending():
            return False
        if not self._lock.acquire(blocking=False):
            return False
        # running 必须在这里(调用方的线程里)就置上:调用方拿到 True 之后
        # 马上就会去轮询它,置位留给工作线程做的话中间那几毫秒是 False,
        # "等它跑完"的循环一进去就出来了
        self.state.update({"running": True, "done": 0, "total": 0})
        threading.Thread(target=self._run, daemon=True, name="mailai").start()
        return True

    def _run(self) -> None:
        try:
            todo = self.pending(MAX_PER_RUN)
            self.state.update({"total": len(todo),
                               "model": self.model_getter()})
            self._emit()
            for start in range(0, len(todo), BATCH):
                batch = todo[start:start + BATCH]
                try:
                    self._do_batch(batch)
                except Exception as exc:
                    self.state["errors"].append(f"{type(exc).__name__}: {exc}"[:200])
                self.state["done"] = min(start + len(batch), len(todo))
                self._emit()
            self.state["last"] = _now()
        finally:
            self.state["running"] = False
            self._lock.release()
            self._emit()

    def _do_batch(self, batch: list[dict]) -> None:
        exe = find_claude()
        if not exe:
            raise RuntimeError("找不到 claude 命令")
        prompt = build_prompt(batch, self.facts_getter(), self.catalog_getter())
        argv = [exe, "-p", "--output-format", "json",
                "--model", self.model_getter() or "sonnet",
                "--system-prompt", SYSTEM,
                "--strict-mcp-config", "--restricted"]
        p = subprocess.run(
            argv, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(self.project_dir),
            timeout=TIMEOUT, creationflags=_NO_WINDOW)
        if p.returncode != 0:
            raise RuntimeError(
                f"claude 退出码 {p.returncode}:"
                f"{(p.stderr or p.stdout or '').strip()[:200]}")
        env = json.loads(p.stdout or "{}")
        self.state["cost"] = round(
            self.state.get("cost", 0.0) + float(env.get("total_cost_usd") or 0), 4)
        if env.get("is_error"):
            raise RuntimeError(str(env.get("result"))[:200])

        got = parse_result(env.get("result", ""), len(batch))
        if not got:
            raise RuntimeError("模型没给出能解析的 JSON")
        model = self.model_getter() or "sonnet"
        out, fresh_tags = {}, set()
        for item in got:
            m = batch[item["i"] - 1]
            out[m["id"]] = {"tags": item["tags"], "level": item["level"],
                            "summary": item["summary"], "why": item["why"],
                            "at": _now(), "model": model}
            fresh_tags.update(item["tags"])
        self.tags.put_many(out)

        # 模型新造的标签收进目录,下次它自己也能看到、用户也能删
        known = set(self.catalog_getter() or [])
        added = [t for t in sorted(fresh_tags) if t not in known and t != "未分类"]
        if added and self.on_new_tags:
            try:
                self.on_new_tags(added)
            except Exception:
                pass


def rank_of(mid: str, tags: TagStore, flags: dict,
            fallback=None) -> dict:
    """一封信最终显示成什么级别。

    顺序是有讲究的:
      1. 自己标了重点又没标完成 —— 那是用户的直接意志,压过一切
      2. AI 过目的结果
      3. 还没过目 —— 给个"待分析",**不要**假装已经判过了
    """
    if flags.get("star") and not flags.get("done"):
        return {"level": 3, "label": "盯", "icon": "◎",
                "why": "你标成了重点,还没标完成", "tags": [], "pending": False}
    a = tags.get(mid)
    if a:
        lv = int(a.get("level", 1))
        return {"level": lv, "label": LEVELS.get(lv, "普通"),
                "icon": ICONS.get(lv, "·"), "why": a.get("why") or "",
                "summary": a.get("summary") or "",
                "tags": a.get("tags") or [], "pending": False}
    if fallback:
        d = dict(fallback)
        d.setdefault("tags", [])
        d["pending"] = True
        return d
    return {"level": 1, "label": "待分析", "icon": "…", "why": "还没过目",
            "tags": [], "pending": True}
