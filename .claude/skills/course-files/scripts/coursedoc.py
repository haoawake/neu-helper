# -*- coding: utf-8 -*-
"""读本地课件/作业文件:PDF、PPTX、DOCX、XLSX、ipynb、纯文本。

四个子命令:
    list  [课程]              列出已同步的文件(从 index.json 读)
    read  <路径> [--pages A-B] 抽正文,带页码/slide 号标记
    outline <路径>            只要目录:PDF 每页首行、PPTX 每张标题
    find  <关键词> [--course X] [--ext pdf] 在所有能抽文字的文件里搜

**解释器自选。** docx/pptx/xlsx/ipynb 是 zip+XML 或 JSON,标准库就能读;
PDF 需要 PyMuPDF。当前解释器没有的话,自动换成项目记录的那个
(data/pythonw-path.txt 旁边的 python.exe),那边装着 fitz。
所以这个脚本用任何 python 跑都行。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

# 这个脚本在 <项目>/.claude/skills/course-files/scripts/ 下
PROJ = Path(__file__).resolve().parents[4]
DOWNLOADS = PROJ / "data" / "downloads"
MAX_CHARS = 60000          # 一次最多吐这么多字,省上下文


# ───────────────────────────── 解释器自选 ─────────────────────────────

def _project_python() -> Path | None:
    """项目记录的那个解释器(装了 PyMuPDF / python-docx 的那个)。"""
    rec = PROJ / "data" / "pythonw-path.txt"
    try:
        pyw = Path(rec.read_text(encoding="utf-8-sig").strip())
    except Exception:
        return None
    cand = pyw.with_name("python.exe")
    return cand if cand.exists() else None


def _reexec_if_needed(need: str) -> None:
    """当前解释器缺这个模块,就用项目的解释器把自己重跑一遍。"""
    try:
        __import__(need)
        return
    except ImportError:
        pass
    py = _project_python()
    if not py or os.environ.get("COURSEDOC_REEXEC"):
        return                      # 换过一次还是不行就算了,让调用方看到降级提示
    # 用子进程而不是 os.execve:Windows 上 execve 是模拟的,实测在 Git Bash 里
    # 直接段错误(exit 139)。
    env = dict(os.environ, COURSEDOC_REEXEC="1", PYTHONUTF8="1",
               PYTHONIOENCODING="utf-8")
    r = subprocess.run([str(py), os.path.abspath(__file__)] + sys.argv[1:], env=env)
    sys.exit(r.returncode)


# ───────────────────────────── 各格式的抽取 ─────────────────────────────

def _xml_text(xml: str) -> str:
    """把 Office 的 XML 片段变成纯文本:段落标记换行,标签删掉,实体还原。"""
    xml = re.sub(r"</w:p>|</a:p>|<w:br/>|<a:br/>|</w:tr>", chr(10), xml)
    xml = re.sub(r"</w:tc>", chr(9), xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml)


def read_docx(path: Path) -> list[tuple[str, str]]:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        parts = ["word/document.xml"] + sorted(
            n for n in names if re.match(r"word/(header|footer)\d*\.xml$", n)
        )
        out = []
        for n in parts:
            if n in names:
                t = _xml_text(z.read(n).decode("utf-8", "replace")).strip()
                if t:
                    out.append((n.split("/")[-1], t))
    return out


def read_pptx(path: Path) -> list[tuple[str, str]]:
    with zipfile.ZipFile(path) as z:
        slides = sorted(
            (n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
            key=lambda n: int(re.findall(r"(\d+)", n)[-1]),
        )
        notes = {
            int(re.findall(r"(\d+)", n)[-1]): n
            for n in z.namelist()
            if re.match(r"ppt/notesSlides/notesSlide\d+\.xml$", n)
        }
        out = []
        for n in slides:
            i = int(re.findall(r"(\d+)", n)[-1])
            body = _xml_text(z.read(n).decode("utf-8", "replace")).strip()
            if i in notes:
                nt = _xml_text(z.read(notes[i]).decode("utf-8", "replace")).strip()
                if nt:
                    body += chr(10) + "[讲者备注] " + nt
            out.append((f"slide {i}", body))
    return out


def read_xlsx(path: Path) -> list[tuple[str, str]]:
    """只抽单元格文字,不管公式和格式 —— 课件里的 xlsx 基本是数据表。"""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            raw = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
            shared = [html.unescape(re.sub(r"<[^>]+>", "", m))
                      for m in re.findall(r"<si>(.*?)</si>", raw, re.S)]
        out = []
        sheets = sorted(n for n in z.namelist()
                        if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        for n in sheets:
            raw = z.read(n).decode("utf-8", "replace")
            rows = []
            for row in re.findall(r"<row[^>]*>(.*?)</row>", raw, re.S):
                cells = []
                for c in re.findall(r"<c[^>]*?(?:\s t=\"(\w+)\")?[^>]*>(.*?)</c>", row, re.S):
                    typ, inner = c
                    v = re.findall(r"<v>(.*?)</v>", inner, re.S)
                    if not v:
                        continue
                    val = v[0]
                    if typ == "s":
                        try:
                            val = shared[int(val)]
                        except (ValueError, IndexError):
                            pass
                    cells.append(html.unescape(re.sub(r"<[^>]+>", "", val)).strip())
                if any(cells):
                    rows.append(chr(9).join(cells))
            if rows:
                out.append((n.split("/")[-1], chr(10).join(rows)))
    return out


def read_ipynb(path: Path) -> list[tuple[str, str]]:
    nb = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    out = []
    for i, cell in enumerate(nb.get("cells", []), 1):
        src = "".join(cell.get("source") or [])
        if not src.strip():
            continue
        kind = cell.get("cell_type")
        if kind == "code":
            body = "```python" + chr(10) + src + chr(10) + "```"
            # 只带文本输出,图片和 base64 一概不要
            for o in (cell.get("outputs") or []):
                txt = "".join(o.get("text") or []) or \
                    "".join((o.get("data") or {}).get("text/plain") or [])
                if txt.strip():
                    body += chr(10) + "输出: " + txt.strip()[:500]
        else:
            body = src
        out.append((f"cell {i} ({kind})", body))
    return out


def read_pdf(path: Path) -> list[tuple[str, str]]:
    """PDF 要库。fitz(PyMuPDF)优先,退而用 pypdf。"""
    try:
        import fitz                                    # PyMuPDF
    except ImportError:
        fitz = None
    if fitz is not None:
        out = []
        with fitz.open(path) as doc:
            for i, page in enumerate(doc, 1):
                out.append((f"p.{i}", page.get_text().strip()))
        return out
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader                # 老名字
        except ImportError:
            raise RuntimeError(
                "这个解释器里没有 PyMuPDF 也没有 pypdf,抽不了 PDF 文字。"
                "直接用 Read 工具读这个 PDF —— 它本来就支持 PDF,而且能看见图。")
    r = PdfReader(str(path))
    return [(f"p.{i}", (pg.extract_text() or "").strip())
            for i, pg in enumerate(r.pages, 1)]


READERS = {
    ".pdf": read_pdf, ".docx": read_docx, ".pptx": read_pptx,
    ".xlsx": read_xlsx, ".ipynb": read_ipynb,
}
PLAIN = {".txt", ".md", ".csv", ".py", ".json", ".tex", ".r"}


def extract(path: Path) -> list[tuple[str, str]]:
    ext = path.suffix.lower()
    if ext in READERS:
        return READERS[ext](path)
    if ext in PLAIN:
        return [("全文", path.read_text(encoding="utf-8", errors="replace"))]
    raise RuntimeError(f"不认识 {ext} 这种格式。能读的:"
                       + "、".join(sorted(READERS) + sorted(PLAIN)))


# ───────────────────────────── 子命令 ─────────────────────────────

def resolve(arg: str) -> Path:
    """路径可以给全的,也可以只给文件名 —— 后者在已同步的树里找。"""
    p = Path(arg)
    if p.exists():
        return p
    if not p.is_absolute():
        q = (PROJ / arg)
        if q.exists():
            return q
    hits = [f for f in DOWNLOADS.rglob("*")
            if f.is_file() and f.name.lower() == p.name.lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise SystemExit("有好几个同名文件,给完整路径:" + chr(10)
                         + chr(10).join("  " + str(h) for h in hits[:10]))
    raise SystemExit(f"找不到 {arg}。先跑 list 看本地都有什么。")


def parse_pages(spec: str | None) -> tuple[int, int] | None:
    if not spec:
        return None
    m = re.match(r"^(\d+)(?:\s*-\s*(\d+))?$", spec.strip())
    if not m:
        raise SystemExit("--pages 写成 5 或 3-7 这种")
    a = int(m.group(1))
    return (a, int(m.group(2) or a))


def cmd_read(args) -> None:
    path = resolve(args.path)
    blocks = extract(path)
    rng = parse_pages(args.pages)
    print(f"# {path.name}")
    print(f"{path}")
    print(f"共 {len(blocks)} 段(页 / slide / cell)")
    print()
    total = 0
    for i, (label, text) in enumerate(blocks, 1):
        if rng and not (rng[0] <= i <= rng[1]):
            continue
        text = re.sub(chr(10) + r"{3,}", chr(10) * 2, text).strip()
        if not text:
            continue
        print(f"────── {label} ──────")
        print(text)
        print()
        total += len(text)
        if total > MAX_CHARS:
            print(f"(到 {MAX_CHARS} 字上限了,用 --pages 指定范围接着看)")
            break


def cmd_outline(args) -> None:
    path = resolve(args.path)
    blocks = extract(path)
    print(f"# {path.name} 目录({len(blocks)} 段)")
    for label, text in blocks:
        first = next((l.strip() for l in text.splitlines() if l.strip()), "(空)")
        print(f"  {label:<12} {first[:72]}")


def cmd_list(args) -> None:
    idx = DOWNLOADS / "index.json"
    if not idx.exists():
        raise SystemExit("还没同步过(没有 index.json)。用 course-sync 技能同步一次。")
    d = json.loads(idx.read_text(encoding="utf-8-sig"))
    files = d.get("files", [])
    if args.course:
        files = [f for f in files if args.course.lower() in (f.get("course") or "").lower()]
    print(f"已同步 {len(files)} 个文件(索引生成于 {d.get('generated')})")
    cur = None
    for f in sorted(files, key=lambda x: (x.get("course") or "", x.get("category") or "",
                                          x.get("group") or "", x.get("name") or "")):
        key = (f.get("course"), f.get("category"), f.get("group"))
        if key != cur:
            cur = key
            print()
            print("  " + " / ".join(x for x in key if x))
        print(f"    {f['name']:<52} {f['size'] / 1024:>8.0f} KB  {f['mtime']}")


def build_pattern(keyword: str) -> "re.Pattern":
    """关键词里的空白当成"可有可无"。

    老师写 "Heapsort" 还是 "Heap Sort" 是随机的,学生不该为这个猜关键词。
    所以按空白切开、用 \\s* 连起来。
    """
    toks = [re.escape(t) for t in keyword.split() if t]
    if not toks:
        raise SystemExit("关键词不能是空的")
    return re.compile(r"\s*".join(toks), re.I)


def cmd_find(args) -> None:
    pat = build_pattern(args.keyword)
    exts = ({"." + e.lower().lstrip(".") for e in args.ext.split(",")}
            if args.ext else set(READERS) | PLAIN)
    hits = 0
    for path in sorted(DOWNLOADS.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        # index.json 和 _课程概览.md 是元数据,里面命中的是文件名不是内容,
        # 会把真正的正文命中挤下去
        if path.name in ("index.json",) or path.name.startswith("_课程概览"):
            continue
        # docx/pptx 旁边那份 .txt 是同一份内容,两边都报就成了重复命中
        if re.search(r"\.(docx|pptx)\.txt$", path.name, re.I):
            continue
        if args.course and args.course.lower() not in str(path).lower():
            continue
        try:
            blocks = extract(path)
        except Exception:
            continue
        for label, text in blocks:
            # 先把空白压平,这样跨行断开的短语也能中
            flat = re.sub(r"\s+", " ", text)
            m = pat.search(flat)
            if m:
                rel = path.relative_to(DOWNLOADS)
                a, b = max(0, m.start() - 60), min(len(flat), m.end() + 60)
                print(f"{rel}  [{label}]")
                print(f"    …{flat[a:b].strip()}…")
                hits += 1
        if hits > 60:
            print("(命中太多,只列前面这些)")
            return
    if not hits:
        print(f"本地课件里没搜到「{args.keyword}」。"
              "可能这份材料还没同步,或者在超过 80MB 没下的那几个录像里。")


def main() -> None:
    ap = argparse.ArgumentParser(description="读本地课件/作业文件")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("read", help="抽正文")
    p.add_argument("path")
    p.add_argument("--pages", help="页 / slide / cell 范围,比如 3-7")
    p.set_defaults(fn=cmd_read)

    p = sub.add_parser("outline", help="只要目录")
    p.add_argument("path")
    p.set_defaults(fn=cmd_outline)

    p = sub.add_parser("list", help="列出已同步的文件")
    p.add_argument("course", nargs="?")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("find", help="在本地课件里搜关键词")
    p.add_argument("keyword")
    p.add_argument("--course")
    p.add_argument("--ext", help="只搜这些后缀,逗号分隔,比如 pdf,docx")
    p.set_defaults(fn=cmd_find)

    args = ap.parse_args()
    # PDF 那条路要 fitz;缺了就换成项目的解释器重跑
    needs_pdf = (
        (getattr(args, "path", "") or "").lower().endswith(".pdf")
        or (args.cmd == "find" and (not args.ext or "pdf" in args.ext.lower()))
    )
    if needs_pdf:
        _reexec_if_needed("fitz")
    args.fn(args)


if __name__ == "__main__":
    main()
