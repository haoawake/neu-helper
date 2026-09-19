# -*- coding: utf-8 -*-
"""这是哪个平台 —— 全项目唯一的判定处。

散在各处写 `sys.platform == "win32"` 迟早写歪一个(比如漏掉 `darwin` 的
拼写、或者把 `sys.platform.startswith("win")` 和 `== "win32"` 混用)。
统一从这里取。

这个模块**不许 import 任何平台专有的东西** —— 它会被所有分发层最先导入,
里面一旦有 `import ctypes.wintypes` 之类,在 Mac 上就直接炸在第一行。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# 界面上和日志里报平台用这个,别在别处拼
NAME = "Windows" if IS_WIN else "macOS" if IS_MAC else sys.platform


def home() -> Path:
    """用户主目录。

    刻意用 `expanduser` 而不是 `Path.home()`:Windows 上前者认 %USERPROFILE%,
    后者在某些服务账号下会拿到别的地方。两边行为要一致。
    """
    return Path(os.path.expanduser("~"))


def config_dir() -> Path:
    """凭据目录 `~/.canvas-helper`。

    **刻意放在项目目录之外** —— 项目可能被分享/打包/推到仓库,token 和邮箱
    授权码绝不能跟着走。两个平台同一个位置,迁移的时候拷这一个目录就行。
    """
    return home() / ".canvas-helper"


def unsupported(what: str) -> RuntimeError:
    """分发层挑不到实现时抛这个,错误信息里带上平台名。"""
    return RuntimeError(f"{what} 还没有 {NAME} 的实现")
