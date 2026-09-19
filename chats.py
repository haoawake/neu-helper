# -*- coding: utf-8 -*-
"""对话存档。

每段对话存一份完整消息,外加 claude CLI 给的 session_id —— 上下文优先靠
`--resume <session_id>` 续(那是真正的上下文,连工具调用都带着)。但 CLI 的
会话不保证一直在(清理、过期、跨机器都可能没了),所以这里同时留着消息原文:
resume 失败时把最近若干轮拼成一个 `<以往对话>` 块塞回去,起码不会失忆。

写盘和简报那边同一套路:先写 .tmp 再 replace,断电不会留半个文件。
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

KEEP_CHATS = 40          # 最多留几段对话(超了从最旧的删)
KEEP_MSGS = 400          # 单段对话最多留几条消息
CONTEXT_TURNS = 10       # resume 失败时至少带回去几轮


def _now() -> str:
    # 带秒:index() 按 updated 排序,只到分钟的话同一分钟内建的几段顺序是乱的
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _title_of(text: str) -> str:
    """用第一句用户消息当标题。"""
    t = " ".join((text or "").split())
    return (t[:26] + "…") if len(t) > 27 else (t or "新对话")


class ChatStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict = {"active": None, "chats": []}
        self._load()

    # ------------------------------------------------------------ 读写盘

    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(d, dict) and isinstance(d.get("chats"), list):
                self._data = d
        except FileNotFoundError:
            pass
        except Exception:
            # 文件坏了不能让应用起不来:改名留证据,重新开一份
            try:
                self.path.replace(self.path.with_suffix(".bad"))
            except Exception:
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------ 查

    def _find(self, cid: str) -> dict | None:
        for c in self._data["chats"]:
            if c["id"] == cid:
                return c
        return None

    def index(self) -> list[dict]:
        """目录,最近更新的在前。不带消息正文,列表不用拖那么多数据。"""
        with self._lock:
            out = [
                {
                    "id": c["id"],
                    "title": c.get("title") or "新对话",
                    "updated": c.get("updated", ""),
                    "turns": sum(1 for m in c.get("messages", []) if m["role"] == "user"),
                    "cost": round(c.get("cost", 0.0), 4),
                }
                for c in self._data["chats"]
            ]
        return sorted(out, key=lambda x: x["updated"], reverse=True)

    def get(self, cid: str) -> dict | None:
        with self._lock:
            c = self._find(cid)
            return json.loads(json.dumps(c)) if c else None

    def active_id(self) -> str:
        """当前对话。没有就开一段新的。"""
        with self._lock:
            cid = self._data.get("active")
            if cid and self._find(cid):
                return cid
            return self.new()

    # ------------------------------------------------------------ 改

    def new(self) -> str:
        with self._lock:
            # 先扫掉之前留下的空对话:点了"新对话"又没说话、或者删掉当前段时
            # 自动补的那一段,不清理的话目录里会攒一堆"新对话"
            self._data["chats"] = [c for c in self._data["chats"] if c["messages"]]
            cid = uuid.uuid4().hex[:12]
            self._data["chats"].append({
                "id": cid,
                "title": "新对话",
                "session_id": None,
                "created": _now(),
                "updated": _now(),
                "cost": 0.0,
                "messages": [],
            })
            # 超量了从最旧的开始删(按 updated 排)
            if len(self._data["chats"]) > KEEP_CHATS:
                self._data["chats"].sort(key=lambda c: c.get("updated", ""))
                self._data["chats"] = self._data["chats"][-KEEP_CHATS:]
            self._data["active"] = cid
            self._save()
            return cid

    def set_active(self, cid: str) -> bool:
        with self._lock:
            if not self._find(cid):
                return False
            self._data["active"] = cid
            self._save()
            return True

    def add(self, cid: str, role: str, text: str, cost: float = 0.0,
            ctx: list | None = None) -> None:
        """记一条消息。空文本不记 —— 出错的那一轮不该在历史里留个空气泡。

        ctx 是这一轮拖进来关联的对象(作业/公告/文件),只存标题一类的摘要,
        回看历史时能看出"当时在问哪个东西"。
        """
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            c = self._find(cid)
            if c is None:
                return
            msg = {"role": role, "text": text, "at": _now()}
            if ctx:
                msg["ctx"] = [
                    {"kind": x.get("kind"), "title": x.get("title"),
                     "course": x.get("course")}
                    for x in ctx[:6]
                ]
            c["messages"].append(msg)
            if len(c["messages"]) > KEEP_MSGS:
                c["messages"] = c["messages"][-KEEP_MSGS:]
            if role == "user" and (c.get("title") in (None, "", "新对话")):
                c["title"] = _title_of(text)
            c["cost"] = round(c.get("cost", 0.0) + (cost or 0.0), 6)
            c["updated"] = _now()
            self._save()

    def set_session(self, cid: str, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            c = self._find(cid)
            if c is not None and c.get("session_id") != session_id:
                c["session_id"] = session_id
                self._save()

    def delete(self, cid: str) -> bool:
        with self._lock:
            c = self._find(cid)
            if c is None:
                return False
            self._data["chats"].remove(c)
            if self._data.get("active") == cid:
                self._data["active"] = None
            self._save()
            return True

    def clear_all(self) -> int:
        with self._lock:
            n = len(self._data["chats"])
            self._data = {"active": None, "chats": []}
            self._save()
            return n

    # ------------------------------------------------------------ 上下文

    def context_block(self, cid: str, turns: int = CONTEXT_TURNS) -> str:
        """`--resume` 失败时的兜底:把最近 N 轮拼成一段前文。

        只放文本,不放工具调用记录 —— 那些重放没意义,而且很占额度。
        """
        with self._lock:
            c = self._find(cid)
            if not c or not c["messages"]:
                return ""
            msgs = c["messages"][-turns * 2:]
        lines = ["<以往对话>", "(这是同一段对话的前文,接着往下答就行,不用复述)", ""]
        for m in msgs:
            who = "我" if m["role"] == "user" else "你"
            lines.append(f"{who}:{m['text']}")
            lines.append("")
        lines.append("</以往对话>")
        return "\n".join(lines)
