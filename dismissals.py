# -*- coding: utf-8 -*-
"""被「划掉」的待办 —— 你主动标记为不用管的那些。

划掉的条目会:
  - 在界面上加删除线并淡化(不是删掉,随时能撤回)
  - 从 hero 倒计时、指标卡、逾期告警里剔除
  - 不再写进 data/snapshot.md,所以**以后的每日简报也不会提它**

标识用 Canvas 的 plannable_id(见 canvas_api.upcoming 的 key 字段),不用标题 ——
教授改个作业名就会让忽略失效。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path


class DismissStore:
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
            # 文件坏了不该让应用起不来,也不该静默丢掉用户的标记 —— 留个备份
            try:
                self.path.rename(self.path.with_suffix(".corrupt.json"))
            except Exception:
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)   # 原子替换,断电不会留半个文件

    # ------------------------------------------------------------ 读写

    def is_dismissed(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def keys(self) -> set[str]:
        with self._lock:
            return set(self._data.keys())

    def set(self, key: str, on: bool, title: str = "") -> bool:
        """on=True 划掉,on=False 撤回。返回操作后的状态。"""
        if not key or key.endswith(":None"):
            return False
        with self._lock:
            if on:
                self._data[key] = {
                    "title": title,
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
            else:
                self._data.pop(key, None)
            self._save()
            return on

    def clear(self) -> int:
        """全部恢复。设置面板里那个「全部恢复」按钮用。"""
        with self._lock:
            n = len(self._data)
            self._data.clear()
            self._save()
            return n

    def all(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._data)
