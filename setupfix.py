# -*- coding: utf-8 -*-
"""安装完整吗?不完整就在应用里补上 —— 不用再去跑脚本。

为什么需要这个:装机脚本可能**跑到一半失败**(实测过一次:PowerShell 把
`claude mcp remove` 写到 stderr 的一句正常提示当成致命错误,脚本在第 2 步
终止,MCP、快捷方式、CLAUDE.md 全没做)。这时候应用本身是能跑的,但缺了
半套配置 —— 而让用户"再装一遍"是个很差的答复,尤其是他刚装完。

所以:应用自己检查、自己补。检查是只读的、很便宜,启动时做一次。

**和「更新」是两回事。** 更新换的是代码(exe、前端、技能脚本);这里补的是
**代码之外的配置**:MCP 注册、桌面/开机快捷方式、CLAUDE.md。更新不碰它们,
因为它们是"这台机器上的安装状态",不是程序的一部分。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import platform_id
from desktop import NO_WINDOW

# 和 install.ps1 / setup.ps1 里那份**必须一致**
ALLOW = [
    "mcp__canvas",
    "Read(./data/**)",
    "Skill(course-files)",
    "Skill(course-sync)",
    "Bash(python .claude/skills/course-files/scripts/coursedoc.py:*)",
    "Bash(python3 .claude/skills/course-files/scripts/coursedoc.py:*)",
    "Bash(python .claude/skills/course-sync/scripts/sync_now.py:*)",
    "Bash(python3 .claude/skills/course-sync/scripts/sync_now.py:*)",
    "PowerShell(python .claude/skills/course-files/scripts/coursedoc.py:*)",
    "PowerShell(python .claude/skills/course-sync/scripts/sync_now.py:*)",
]


def _run(argv, timeout=60):
    """跑一个外部命令,只看退出码。

    **不看 stderr 有没有东西** —— `claude mcp remove` 在没注册过的时候会往
    stderr 写一句"没有这个",那完全正常。按 stderr 判成败是 setup.ps1
    当初翻车的原因。
    """
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:                       # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def _claude() -> str | None:
    from chat_bridge import find_claude
    return find_claude()


def _mcp_target(here: Path) -> list[str]:
    """MCP 该注册成什么命令。

    打包版指向自带的 canvas-mcp 可执行文件(那里面没有 python);
    源码版指向 `<本解释器> canvas_mcp.py`。
    """
    if getattr(sys, "frozen", False):
        exe = here / ("canvas-mcp.exe" if platform_id.IS_WIN else "canvas-mcp")
        return [str(exe)]
    return [sys.executable, str(here / "canvas_mcp.py")]


def state(here: Path) -> dict:
    """现在缺什么。只读,不改任何东西。"""
    here = Path(here)
    cfg = platform_id.config_dir() / "config.json"
    claude = _claude()

    mcp = "unknown"
    if claude:
        code, out = _run([claude, "mcp", "list"], timeout=45)
        mcp = "ok" if (code == 0 and "canvas:" in out) else "missing"

    return {
        "token": cfg.is_file(),
        "claude": bool(claude),
        "mcp": mcp,
        "claude_md": (here / "CLAUDE.md").is_file(),
        "perms": (here / ".claude" / "settings.local.json").is_file(),
        "shortcut": _has_shortcut(),
        "packaged": getattr(sys, "frozen", False),
    }


def missing(st: dict) -> list[str]:
    """人话版的"缺什么"。token 不在这里 —— 那个得用户自己填。"""
    out = []
    if st.get("mcp") == "missing":
        out.append("Canvas MCP 没注册(在别的 Claude Code 会话里问不了课程)")
    if not st.get("claude_md"):
        out.append("缺 CLAUDE.md(简报会讲错课)")
    if not st.get("perms"):
        out.append("缺权限配置(简报和对话会被拦下来)")
    if st.get("packaged") and not st.get("shortcut"):
        out.append("没有桌面/开机快捷方式")
    return out


def _has_shortcut() -> bool:
    if not platform_id.IS_WIN:
        return (platform_id.home() / "Library" / "LaunchAgents"
                / "com.neuhelper.app.plist").is_file()
    desk = Path(os.path.expandvars(r"%USERPROFILE%\Desktop")) / "NEU Helper.lnk"
    return desk.is_file()


def fix(here: Path) -> dict:
    """把缺的补上。返回每一项做了什么 —— 界面要照实说,不能只说"好了"。"""
    here = Path(here)
    done, failed = [], []

    # ── CLAUDE.md:从模板复制,**绝不覆盖已有的**(那是用户的课程表)
    md, tpl = here / "CLAUDE.md", here / "CLAUDE.example.md"
    if not md.is_file() and tpl.is_file():
        try:
            md.write_text(tpl.read_text(encoding="utf-8"), encoding="utf-8")
            done.append("建了 CLAUDE.md(记得把课程表换成你自己的)")
        except Exception as exc:                   # noqa: BLE001
            failed.append(f"CLAUDE.md:{exc}")

    # ── 权限
    perms = here / ".claude" / "settings.local.json"
    if not perms.is_file():
        try:
            perms.parent.mkdir(parents=True, exist_ok=True)
            perms.write_text(
                json.dumps({"permissions": {"allow": ALLOW}},
                           ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            done.append("写了 .claude/settings.local.json")
        except Exception as exc:                   # noqa: BLE001
            failed.append(f"权限配置:{exc}")

    # ── MCP
    claude = _claude()
    if not claude:
        failed.append("找不到 claude 命令 —— 装一下 Claude Code 再补")
    else:
        # 先清干净。没注册过时它会报"没有这个",那不是失败
        _run([claude, "mcp", "remove", "canvas", "-s", "user"])
        code, out = _run([claude, "mcp", "add", "canvas", "-s", "user", "--"]
                         + _mcp_target(here), timeout=90)
        if code == 0:
            done.append("注册了 Canvas MCP")
        else:
            failed.append(f"注册 MCP 失败:{out.strip()[:160]}")

    # ── 快捷方式(只有打包版需要;源码版是装机脚本建的)
    if getattr(sys, "frozen", False):
        okc, why = _make_shortcuts(here)
        (done if okc else failed).append(why)

    return {"done": done, "failed": failed, "state": state(here)}


def _make_shortcuts(here: Path) -> tuple[bool, str]:
    exe = here / "NEU Helper.exe"
    if platform_id.IS_WIN:
        if not exe.is_file():
            return False, "找不到 NEU Helper.exe,没法建快捷方式"
        # 建 .lnk 要走 COM。PowerShell 一行就能做,而且 Windows 一定有它 ——
        # 比为这一件事引入 pywin32 划算
        ps = (
            "$w = New-Object -ComObject WScript.Shell; "
            "foreach ($d in @([Environment]::GetFolderPath('Desktop'),"
            "[Environment]::GetFolderPath('Startup'))) { "
            "$l = $w.CreateShortcut((Join-Path $d 'NEU Helper.lnk')); "
            f"$l.TargetPath = '{exe}'; "
            f"$l.WorkingDirectory = '{here}'; "
            f"$l.IconLocation = '{exe}'; "
            "$l.Description = 'NEU Helper'; $l.Save() }"
        )
        code, out = _run(["powershell", "-NoProfile", "-ExecutionPolicy",
                          "Bypass", "-Command", ps], timeout=60)
        return (code == 0, "建了桌面和开机快捷方式" if code == 0
                else f"建快捷方式失败:{out.strip()[:160]}")
    # macOS:登录项是一个 LaunchAgent
    app = here.parent.parent if here.name == "MacOS" else here
    plist = (platform_id.home() / "Library" / "LaunchAgents"
             / "com.neuhelper.app.plist")
    try:
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.neuhelper.app</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/open</string><string>-a</string>
  <string>{app}</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
""", encoding="utf-8")
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/com.neuhelper.app"])
        _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        return True, "装了登录项"
    except Exception as exc:                       # noqa: BLE001
        return False, f"装登录项失败:{exc}"
