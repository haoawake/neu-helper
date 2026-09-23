# -*- coding: utf-8 -*-
"""挑出这个 tag 的 Release 标题和正文,写成 release-title.txt / release-body.md。

给 .github/workflows/release.yml 用(打 tag 之后那一步)。

**为什么是一个脚本而不是几行 shell。** 原来那段是写在 YAML 块标量里的 bash,
后来要读 JSON,就得在块标量里再套一个 heredoc —— 缩进一破坏,整个工作流文件
连解析都过不去,而且这种错误只有推了 tag 才看得见。放进单独的脚本,本机就能跑、
能测,YAML 那边只剩一行调用。

来源的优先级:

  1. `packaging/release_descriptions.json` —— **唯一权威**。
     `sync-release-descriptions.yml` 用的也是它(那个工作流负责把改动同步到
     已经发出去的 Release 上)。两边读同一份,就不会出现"谁后跑谁赢"。
  2. `packaging/notes/<tag>.md` —— 早期版本用的那套,第一行是标题。
     保留它只是为了老 tag 还能重发。
  3. 都没有 —— 只留一个 compare 链接,并且在 Actions 里打一条 warning。
     这是最差情况:updater.py 会把 Release 正文取进应用里给用户看,
     空正文等于刚更新完的人什么都没被告知。

compare 链接只在正文里**还没有**的时候才追加 —— JSON 里那些条目自己带了
「完整变更」一节,再加一次就重复了。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TITLE_OUT = ROOT / "release-title.txt"
BODY_OUT = ROOT / "release-body.md"


def _git(*args: str) -> str:
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True,
                           cwd=str(ROOT), timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:                                  # noqa: BLE001
        return ""


def pick(tag: str) -> tuple[str, str, str]:
    """返回 (标题, 正文, 从哪儿来的)。"""
    j = ROOT / "packaging" / "release_descriptions.json"
    if j.is_file():
        try:
            d = json.loads(j.read_text(encoding="utf-8")).get(tag)
        except (OSError, ValueError) as exc:
            print(f"::warning::{j.name} 读不了:{exc}")
            d = None
        if isinstance(d, dict) and d.get("title") and d.get("body"):
            return str(d["title"]), str(d["body"]), j.name

    md = ROOT / "packaging" / "notes" / f"{tag}.md"
    if md.is_file():
        lines = md.read_text(encoding="utf-8").splitlines()
        title = (lines[0] if lines else tag).lstrip("# ").strip() or tag
        return title, "\n".join(lines[1:]).strip(), md.name

    print(f"::warning::{tag} 在 release_descriptions.json 和 notes/ 里都没有 —— "
          "这次发布只会带一个 changelog 链接")
    return tag, "", "(没有)"


def main() -> int:
    tag = os.environ.get("GITHUB_REF_NAME") or (
        sys.argv[1] if len(sys.argv) > 1 else "")
    if not tag:
        print("::error::拿不到 tag 名(GITHUB_REF_NAME)")
        return 1
    title, body, src = pick(tag)

    if "/compare/" not in body:
        prev = _git("describe", "--tags", "--abbrev=0", f"{tag}^")
        if prev:
            server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
            repo = os.environ.get("GITHUB_REPOSITORY", "")
            body = (body.rstrip() + "\n\n---\n\n**Full Changelog**: "
                    f"{server}/{repo}/compare/{prev}...{tag}\n").lstrip()

    TITLE_OUT.write_text(title, encoding="utf-8")
    BODY_OUT.write_text(body, encoding="utf-8")
    print(f"{tag}:标题取自 {src},正文 {len(body)} 字符")
    return 0


if __name__ == "__main__":
    sys.exit(main())
