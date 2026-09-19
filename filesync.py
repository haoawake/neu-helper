# -*- coding: utf-8 -*-
"""把 Canvas 上的课件全部同步到本地,并且分门别类。

目录长这样:

    data/downloads/
      index.json                     所有文件的索引(课程/分类/路径/大小/更新时间)
      DS5110/
        _课程概览.md                 作业清单 + DDL + 要求摘要 + 附件对应关系
        作业/Homework 1/HW1.ipynb
        课件/Week 1/W1.pdf
        其他/some-handout.pdf

分类的依据,按优先级:

  1. **作业的附件** —— 作业描述 HTML 里的 /files/<id> 链接。老师把 HW1.ipynb
     挂在作业说明里就是这种链接,所以这是最准的关联,一点不用猜。
  2. **模块目录** —— /modules?include[]=items 是老师自己排的 Week 1/Week 2,
     同一个模块里的文件和作业天然相关。
  3. 剩下的进「其他」,带上 Canvas 里的文件夹名。

增量:本地文件的 mtime 会被设成 Canvas 的 updated_at。下次同步只要比较这两个
时间,新的就重下,没变的一个字节都不传 —— 所以可以放心地每小时跑一次。

`_课程概览.md` 是给 Claude 读的:一个文件里就有这门课的作业、DDL、要求摘要和
文件在哪儿,不用为了回答"HW1 要交什么"去翻四个接口。
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from canvas_api import parse_ts

# 单个文件的大小上限:课程里偶尔有几百 MB 的录像,不该默认拖下来
MAX_BYTES = 80 * 1024 * 1024
SYNC_EVERY = 3600          # 后台自动同步的间隔(秒)
CAT_HOMEWORK = "作业"
CAT_SLIDES = "课件"
CAT_OTHER = "其他"


# 同步的时候给每个能抽文字的文件写一份 `<原名>.txt` 放在旁边。
#
# 为什么对 PDF 也这么做(Read 本来就支持 PDF):**为了能搜**。桌面应用里那条
# 对话是 `claude -p`,非交互,它调技能脚本用的是 PowerShell 工具,而放行名单里
# 只有 Bash 那条 —— 被拒之后它只能 Grep 纯文本,13 份 PDF 一个都搜不到
# (它自己在回答里写了"这个会话搜不了")。有了 .txt,任何会话用 Read + Grep
# (只读工具,默认可用)就能覆盖全部课件,不跑脚本、不要额外权限。
TEXTIFY = {".docx", ".pptx", ".pdf", ".ipynb"}


def pdf_to_text(path: Path) -> str | None:
    """PDF -> 纯文本,每页前面加 [p.N] 标记(引用时能说清是哪一页)。

    需要 PyMuPDF。同步器跑在项目那个解释器上,那边装着;没有就跳过,
    反正 Read 工具自己能读 PDF。
    """
    try:
        import fitz
    except ImportError:
        return None
    try:
        chunks = []
        with fitz.open(path) as doc:
            for i, page in enumerate(doc, 1):
                t = page.get_text().strip()
                if t:
                    chunks.append(f"[p.{i}]" + chr(10) + t)
        return (chr(10) * 2).join(chunks) or None
    except Exception:
        return None


def ipynb_to_text(path: Path) -> str | None:
    """ipynb -> 纯文本。markdown 原样,代码带 ``` 围栏,输出只留文本。"""
    try:
        nb = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    out = []
    for i, cell in enumerate(nb.get("cells") or [], 1):
        src = "".join(cell.get("source") or []).strip()
        if not src:
            continue
        if cell.get("cell_type") == "code":
            out.append(f"[cell {i} code]" + chr(10) + "```" + chr(10) + src
                       + chr(10) + "```")
        else:
            out.append(f"[cell {i}]" + chr(10) + src)
    return (chr(10) * 2).join(out) or None


def office_to_text(path: Path) -> str | None:
    """docx / pptx -> 纯文本。抽不出来就返回 None,不要抛。"""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if path.suffix.lower() == ".docx":
                parts = ["word/document.xml"]
            else:
                parts = sorted(
                    (n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                    key=lambda n: int(re.findall(r"(\d+)", n)[-1]),
                )
            chunks = []
            for name in parts:
                if name not in names:
                    continue
                xml = z.read(name).decode("utf-8", "replace")
                # 段落/换行标记先换成真换行,再把标签全删掉
                xml = re.sub(r"</w:p>|</a:p>|<w:br/>|<a:br/>", chr(10), xml)
                xml = re.sub(r"<[^>]+>", "", xml)
                chunks.append(html.unescape(xml))
    except Exception:
        return None
    text = chr(10).join(chunks)
    text = re.sub("[ " + chr(9) + chr(0xA0) + "]+", " ", text)
    text = re.sub(chr(10) + "{3,}", chr(10) * 2, text).strip()
    return text or None


def safe(name: str, limit: int = 80) -> str:
    """能当文件名/目录名用的形式。必须挡住 .. 和路径分隔符。"""
    out = []
    for ch in (name or ""):
        out.append("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch)
    s = "".join(out).strip(" .") or "未命名"
    return s[:limit]


class FileSync:
    """按课程把文件同步到本地。线程安全,同一时刻只跑一个同步。"""

    def __init__(self, root: Path, client_getter, course_getter, on_event=None,
                 limit_getter=None):
        self.root = Path(root)
        self.client = client_getter          # () -> CanvasClient
        self.course = course_getter          # (course_id, force) -> 课程聚合数据
        self.on_event = on_event             # 进度回调(推给前端)
        # 单文件上限(字节)。设置里能调 —— 课程录像动辄 200MB,默认不拖,
        # 但想要的人应该能要。
        self.limit_getter = limit_getter or (lambda: MAX_BYTES)
        self._lock = threading.Lock()
        self.state: dict = {
            "running": False, "done": 0, "total": 0, "current": "",
            "last": None, "added": 0, "updated": 0, "skipped": 0,
            "failed": 0, "bytes": 0, "too_big": 0, "errors": [],
        }

    # ------------------------------------------------------------ 对外

    def is_running(self) -> bool:
        return bool(self.state["running"])

    def snapshot(self) -> dict:
        d = dict(self.state)
        d["errors"] = list(d["errors"])[-5:]
        return d

    def sync_async(self, courses: list[dict], force: bool = False) -> bool:
        """后台同步。已经在跑就直接返回 False。"""
        if not self._lock.acquire(blocking=False):
            return False
        threading.Thread(target=self._run, args=(courses, force),
                         daemon=True, name="filesync").start()
        return True

    def start_scheduler(self, courses_getter) -> None:
        """开机同步一次,之后每小时一次。"""

        def loop():
            time.sleep(12)          # 让界面先起来,别和首屏抢带宽
            while True:
                try:
                    self.sync_async(courses_getter())
                except Exception:
                    pass
                time.sleep(SYNC_EVERY)

        threading.Thread(target=loop, daemon=True, name="filesync-sched").start()

    # ------------------------------------------------------------ 内部

    def _emit(self, **kw) -> None:
        self.state.update(kw)
        if self.on_event:
            try:
                self.on_event(self.snapshot())
            except Exception:
                pass

    def _run(self, courses: list[dict], force: bool) -> None:
        # running 必须在**列清单之前**就置上:列清单要拉 5 门课 x 4 个接口,
        # 好几秒。这期间如果状态还是"没在跑",界面和调用方会以为已经结束了。
        self._emit(running=True, total=0, done=0, current="正在整理课程…",
                   added=0, updated=0, skipped=0, failed=0, bytes=0,
                   too_big=0, errors=[])
        try:
            plan: list[tuple] = []
            for c in courses:
                cid = c.get("id")
                if not cid:
                    continue
                data = self.course(cid, force)
                if data.get("error"):
                    self.state["errors"].append(f"{c.get('short')}: {data.get('message')}")
                    continue
                plan.extend(self._plan_course(data))
                self._write_overview(data)
            self._emit(total=len(plan), done=0, current="")
            for i, (dest, f) in enumerate(plan, 1):
                self._emit(done=i - 1, current=f["name"])
                self._fetch_one(dest, f)
            self._write_index()
            self._emit(running=False, done=len(plan), current="",
                       last=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as exc:
            self.state["errors"].append(f"{type(exc).__name__}: {exc}")
            self._emit(running=False, current="")
        finally:
            try:
                self._lock.release()
            except RuntimeError:
                pass

    def _plan_course(self, data: dict) -> list[tuple]:
        """算出这门课每个文件该落在哪儿。返回 [(目标路径, 文件信息)]。"""
        short = safe(data["short"], 40)
        base = self.root / short
        out: list[tuple] = []
        seen: set[int] = set()

        for a in data.get("assignments") or []:
            for att in a.get("attachments") or []:
                if att["id"] in seen:
                    continue
                seen.add(att["id"])
                out.append((base / CAT_HOMEWORK / safe(a["name"]) / safe(att["name"], 120),
                            att))

        for m in data.get("modules") or []:
            for it in m.get("items") or []:
                f = it.get("file")
                if not f or f["id"] in seen:
                    continue
                seen.add(f["id"])
                out.append((base / CAT_SLIDES / safe(m["name"]) / safe(f["name"], 120), f))

        for f in data.get("files") or []:
            if f["id"] in seen:
                continue
            seen.add(f["id"])
            folder = safe(f.get("folder") or "", 40)
            d = base / CAT_OTHER / folder if folder else base / CAT_OTHER
            out.append((d / safe(f["name"], 120), f))
        return out

    def _fetch_one(self, dest: Path, f: dict) -> None:
        """下一个文件。已经是最新的就跳过 —— 靠 mtime 和 Canvas 的 updated_at 比。"""
        size = f.get("size") or 0
        limit = self.limit_getter()
        if size and limit and size > limit:
            self.state["skipped"] += 1
            self.state["too_big"] = self.state.get("too_big", 0) + 1
            return
        remote = parse_ts(f.get("updated_raw") or f.get("updated_at"))
        if dest.exists():
            fresh = True
            if remote is not None:
                fresh = dest.stat().st_mtime >= remote.timestamp() - 2
            elif size:
                fresh = dest.stat().st_size == size
            if fresh:
                self.state["skipped"] += 1
                self._maybe_textify(dest)   # 早就下好但还没抽过文本的,补上
                return
            kind = "updated"
        else:
            kind = "added"

        url = f.get("url")
        if not url:
            # 附件那份摘要里没带 url,按 id 现问一次
            try:
                url = self.client().get(f"/files/{f['id']}").get("url")
            except Exception as exc:
                self.state["failed"] += 1
                self.state["errors"].append(f"{f.get('name')}: {exc}")
                return
        try:
            n = self.client().download(url, dest)
        except Exception as exc:
            self.state["failed"] += 1
            self.state["errors"].append(f"{f.get('name')}: {exc}")
            return
        if remote is not None:
            # 把本地 mtime 对齐 Canvas 的更新时间,增量判断才有依据
            ts = remote.timestamp()
            try:
                os.utime(dest, (ts, ts))
            except OSError:
                pass
        self.state[kind] += 1
        self.state["bytes"] += n
        self._maybe_textify(dest)

    def _maybe_textify(self, dest: Path) -> None:
        """旁边放一份 .txt。

        - docx/pptx:Read 根本不认这两种格式,没有 .txt 就读不了
        - pdf/ipynb:Read 认,但**没法跨文件搜** —— 有了 .txt,Grep 一次就能
          扫完所有课件(见 TEXTIFY 上面那段注释)

        已经有且比源文件新就不重做。"""
        ext = dest.suffix.lower()
        if ext not in TEXTIFY or not dest.exists():
            return
        side = dest.with_suffix(dest.suffix + ".txt")
        if side.exists() and side.stat().st_mtime >= dest.stat().st_mtime:
            return
        if ext == ".pdf":
            text = pdf_to_text(dest)
        elif ext == ".ipynb":
            text = ipynb_to_text(dest)
        else:
            text = office_to_text(dest)
        if not text:
            return
        try:
            side.write_text(text, encoding="utf-8")
            self.state["textified"] = self.state.get("textified", 0) + 1
        except OSError:
            pass

    def _write_overview(self, data: dict) -> None:
        """每门课一份 _课程概览.md,给 Claude 一个文件就能看懂这门课。"""
        short = safe(data["short"], 40)
        lines = [
            f"# {data['short']} · {data.get('name') or ''}",
            "",
            f"course_id `{data['id']}` · 同步于 "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "> 这个文件是 NEU Helper 自动生成的。作业要求只放了摘要,",
            "> 要全文和 rubric 用 `canvas_assignment_detail(course_id, assignment_id)`。",
            "",
            "## 作业",
            "",
        ]
        assigns = data.get("assignments") or []
        if not assigns:
            lines.append("(这门课没有列出作业)")
        for a in assigns:
            state = "已提交" if a.get("submitted") else "未提交"
            bits = [x for x in (a.get("due_local") or "无期限",
                                f"{a.get('points')} 分" if a.get("points") is not None else "",
                                state) if x]
            lines.append(f"### {a['name']}")
            lines.append("")
            lines.append("· ".join(bits))
            lines.append("")
            lines.append(f"assignment_id `{a['id']}`" +
                         (f" · [在 Canvas 打开]({a['url']})" if a.get("url") else ""))
            if a.get("attachments"):
                lines.append("")
                lines.append("附件(已下到本地):")
                for att in a["attachments"]:
                    rel = f"{CAT_HOMEWORK}/{safe(a['name'])}/{safe(att['name'], 120)}"
                    lines.append(f"- `{rel}`")
                    if Path(rel).suffix.lower() in TEXTIFY:
                        lines.append(f"  - 纯文本版(搜/读都用这个):`{rel}.txt`")
            if a.get("brief"):
                lines.append("")
                lines.append("要求摘要:")
                lines.append("")
                for para in a["brief"].split("\n"):
                    if para.strip():
                        lines.append("> " + para.strip())
            lines.append("")

        mods = data.get("modules") or []
        if mods:
            lines += ["## 课件(按老师排的模块)", ""]
            for m in mods:
                lines.append(f"### {m['name']}")
                lines.append("")
                for it in m.get("items") or []:
                    f = it.get("file")
                    if f:
                        rel = f"{CAT_SLIDES}/{safe(m['name'])}/{safe(f['name'], 120)}"
                        lines.append(f"- `{rel}`")
                    else:
                        lines.append(f"- {it.get('type')}:{it.get('title')}")
                lines.append("")

        loose = data.get("files") or []
        if loose:
            lines += ["## 其他文件", ""]
            for f in loose:
                folder = safe(f.get("folder") or "", 40)
                rel = "/".join(x for x in (CAT_OTHER, folder, safe(f["name"], 120)) if x)
                lines.append(f"- `{rel}`")
            lines.append("")

        anns = data.get("announcements") or []
        if anns:
            lines += ["## 最近公告", ""]
            for an in anns[:8]:
                lines.append(f"- **{an.get('title')}**({an.get('posted')})"
                             f" {an.get('excerpt', '')[:160]}")
            lines.append("")

        dest = self.root / short / "_课程概览.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        tmp.replace(dest)

    def _write_index(self) -> None:
        """整棵树扫一遍写 index.json —— 前端和 Claude 都能直接查"哪个文件在哪"。"""
        items = []
        if self.root.exists():
            for p in sorted(self.root.rglob("*")):
                if p.is_file() and p.suffix != ".tmp" and p.name != "index.json":
                    rel = p.relative_to(self.root)
                    parts = rel.parts
                    items.append({
                        "course": parts[0] if parts else "",
                        "category": parts[1] if len(parts) > 2 else "",
                        "group": parts[2] if len(parts) > 3 else "",
                        "name": p.name,
                        "path": str(p),
                        "size": p.stat().st_size,
                        "mtime": datetime.fromtimestamp(
                            p.stat().st_mtime, timezone.utc
                        ).astimezone().strftime("%Y-%m-%d %H:%M"),
                    })
        out = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "count": len(items), "files": items}
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "index.json.tmp"
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.root / "index.json")
