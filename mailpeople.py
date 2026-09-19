# -*- coding: utf-8 -*-
"""发件人分组:必看 / 好友 / 熟人……比标签高一级的东西。

**和标签的区别**:标签说的是"这封信是什么"(一封一个,AI 每封判一次);
分组说的是"这个人是谁"(一次认定,以后他发的每一封都算)。所以分组不跟着
邮件走,跟着**地址**走 —— 同一个人换了主题、换了内容,还是那个人。

存 `data/mail_people.json`,按小写地址索引。不塞进 `data/mail.json`:
那是个只留 300 封的滚动缓存,而"这是我导师"这件事得比缓存活得久。

分组有一条实际作用:**分了组的人,他的信永远不会被坏标签藏进垃圾箱**。
这是"高一级"的具体含义 —— 规则可以判错,你对人的认定不会。
"""
from __future__ import annotations

import email.utils
import json
import threading
from datetime import datetime
from pathlib import Path

# 开箱就有的三档。用户可以在设置里改
DEFAULT_GROUPS = ["必看", "好友", "熟人"]


def addr_of(from_header: str) -> str:
    """从 `"Chen, Alex" <a.chen@example.edu>` 里取出地址,统一小写。

    分组按地址认人,不按显示名 —— 显示名是发信人自己写的,想改就改。
    """
    _, addr = email.utils.parseaddr(from_header or "")
    return (addr or "").strip().lower()


class PeopleStore:
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

    def group_of(self, from_header: str) -> str:
        a = addr_of(from_header)
        if not a:
            return ""
        with self._lock:
            return (self._data.get(a) or {}).get("group", "")

    def set(self, from_header: str, group: str, name: str = "") -> str:
        """把这个地址归到某一组。group 传空字符串 = 取消分组。"""
        a = addr_of(from_header)
        if not a:
            return ""
        with self._lock:
            if group:
                self._data[a] = {
                    "group": group,
                    "name": name or from_header or a,
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            else:
                self._data.pop(a, None)
            self._save()
            return group

    def all(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def counts(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for v in self._data.values():
                g = v.get("group") or ""
                if g:
                    out[g] = out.get(g, 0) + 1
            return out

    def members(self, group: str = "") -> list[dict]:
        """某一组的成员(不给 group 就是全部),按加入时间新的在前。"""
        with self._lock:
            out = [{"addr": k, **v} for k, v in self._data.items()
                   if not group or v.get("group") == group]
        out.sort(key=lambda x: x.get("at") or "", reverse=True)
        return out

    def rename_group(self, old: str, new: str) -> int:
        """改组名。删组的时候也用它(new 传空 = 把成员移出分组)。"""
        n = 0
        with self._lock:
            for k, v in list(self._data.items()):
                if v.get("group") == old:
                    if new:
                        v["group"] = new
                    else:
                        self._data.pop(k, None)
                    n += 1
            if n:
                self._save()
        return n
