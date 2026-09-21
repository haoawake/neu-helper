# -*- coding: utf-8 -*-
r"""Canvas LMS 只读 API 客户端。

配置解析顺序:
  1. 环境变量 CANVAS_API_TOKEN / CANVAS_BASE_URL
  2. ~/.canvas-helper/config.json(两个平台同一个位置)

刻意只支持 GET —— 这个助手不能提交作业、不能删东西、不能改成绩。
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

DEFAULT_BASE_URL = "https://northeastern.instructure.com"
CONFIG_PATH = Path(os.path.expanduser("~")) / ".canvas-helper" / "config.json"


class CanvasConfigError(RuntimeError):
    pass


def load_config() -> tuple[str, str]:
    """返回 (base_url, token)。"""
    token = os.environ.get("CANVAS_API_TOKEN", "").strip()
    base = os.environ.get("CANVAS_BASE_URL", "").strip()

    if not token and CONFIG_PATH.exists():
        try:
            # utf-8-sig: PowerShell 写 JSON 时可能带 BOM,json.loads 会被噎住
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            token = str(data.get("token", "")).strip()
            base = base or str(data.get("base_url", "")).strip()
        except Exception as exc:  # 配置坏了要说清楚,不要静默降级
            raise CanvasConfigError(f"无法读取 {CONFIG_PATH}: {exc}") from exc

    if not token:
        raise CanvasConfigError(
            "找不到 Canvas token。请设置环境变量 CANVAS_API_TOKEN,"
            f"或运行 install.ps1 写入 {CONFIG_PATH}"
        )
    return (base or DEFAULT_BASE_URL).rstrip("/"), token


# ---------------------------------------------------------------- 工具函数

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")


# 作业描述里的文件链接:/courses/123/files/456 或 /files/456
_FILE_LINK = re.compile(r"/files/(\d+)")


def file_ids_in(html: str | None) -> list[int]:
    """从一段 HTML 里抠出 Canvas 文件 id,去重保序。

    老师把附件挂在作业说明里就是这种链接,所以这是"作业的附件"最准的来源 ——
    比拿文件名去猜(HW1 对 W1.pdf?)靠谱得多。
    """
    if not html:
        return []
    seen, out = set(), []
    for m in _FILE_LINK.finditer(html):
        fid = int(m.group(1))
        if fid not in seen:
            seen.add(fid)
            out.append(fid)
    return out


def html_to_text(raw: str | None, limit: int = 4000) -> str:
    """把 Canvas 的富文本描述压成可读纯文本。"""
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "  - ", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = _WS_RE.sub("\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit] + f"\n...[截断,原文共 {len(text)} 字符]"
    return text


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_local(value: str | None) -> str:
    """UTC ISO 字符串 -> 本机时区的可读时间。"""
    dt = parse_ts(value)
    if dt is None:
        return "无截止时间"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()


def days_left(value: str | None) -> float | None:
    dt = parse_ts(value)
    if dt is None:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0


def urgency(value: str | None) -> str:
    d = days_left(value)
    if d is None:
        return "无期限"
    if d < 0:
        return f"已过期 {abs(d):.1f} 天"
    if d < 1:
        return f"!! {d * 24:.0f} 小时内"
    if d < 3:
        return f"!  {d:.1f} 天"
    return f"{d:.0f} 天"


# ------------------------------------------------- 学期(过滤上学期的遗留)

# NEU 的学期代码:学期名和课号里都带一串 6 位数字,202710 = 2026 年秋。
# 前 4 位是学年结束的那一年,后 2 位是季度(10 秋 / 30 春 / 40~60 夏),
# 所以整串数字直接比大小就是时间先后。
_TERM_CODE = re.compile(r"(?<!\d)(20\d{4})(?!\d)")

# 淘汰的宽限期。换季之后再留这么多天 —— 期末考完成绩还在陆续出,
# 那几周里上学期的课还得能看见
STALE_GRACE_DAYS = 45


def term_code(course: dict) -> int | None:
    """这门课属于哪个学期,数字越大越新。抽不出来返回 None。

    学期名是权威的(`202710_1 Fall 2026 Semester Full Term`),优先看它;
    没有才退到课号。课号是「课程.CRN.学期」,学期在最后一段,所以取**最后**
    一个匹配 —— CRN 有时也是 6 位数字,取第一个会把它当成学期。
    """
    m = _TERM_CODE.search(str((course.get("term") or {}).get("name") or ""))
    if m:
        return int(m.group(1))
    got = _TERM_CODE.findall(str(course.get("course_code") or ""))
    return int(got[-1]) if got else None


def term_code_at(when: datetime) -> int:
    """那一天该上的是哪个学期 —— 同样是 6 位代码。"""
    when = when.astimezone()
    if when.month >= 8:                     # 8 月底就开学了,8 月算秋季
        return (when.year + 1) * 100 + 10
    if when.month <= 4:
        return when.year * 100 + 30
    return when.year * 100 + 40             # 5~7 月:夏季


def past_term(course: dict, now: datetime | None = None) -> bool:
    """这门课是不是「上学期的遗留」。

    `enrollment_state=active` 只挡掉 Canvas 自己 concluded 掉的注册。NEU 的
    学期一个日期都不填(term 802 的 start_at / end_at 都是 null),老师又常常
    忘了手动结课 —— 于是上个学期的课会一直挂在在读列表里,课表和课件同步
    就跟着把它们一起算进来。

    两条判断,都带 STALE_GRACE_DAYS 的宽限:
      - Canvas 写了结束日期,而且已经过去很久
      - 学期代码比「宽限期之前该上的那个学期」还旧

    抽不出学期代码的课一律保留 —— 培训模块和 Group Courses Term 那些课不跟
    学期走,没有"过期"这回事。门槛是按**日历**算的而不是"取最新的那个学期",
    所以下学期的课提前挂出来也不会把这学期的挤掉。
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=STALE_GRACE_DAYS)
    for ts in ((course.get("term") or {}).get("end_at"), course.get("end_at")):
        dt = parse_ts(ts)
        if dt and dt < cutoff:
            return True
    code = term_code(course)
    return code is not None and code < term_code_at(cutoff)


# ---------------------------------------------------------------- 客户端


class CanvasClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        if base_url and token:
            self.base_url, self.token = base_url.rstrip("/"), token
        else:
            self.base_url, self.token = load_config()
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        )
        # 按 id 查过的文件元数据缓存一下:一个课程视图里同一个附件会被问好几次
        self._file_cache: dict[int, dict | None] = {}

    def get(self, path: str, **params):
        """单次 GET。path 可以带或不带 /api/v1 前缀。"""
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith("/api/"):
            path = "/api/v1" + path
        resp = self.session.get(self.base_url + path, params=params, timeout=30)
        if resp.status_code == 401:
            raise RuntimeError("Canvas 返回 401 —— token 失效或已被撤销,需要重新生成。")
        if resp.status_code == 403:
            raise RuntimeError(f"Canvas 返回 403 —— 无权访问 {path}。")
        if resp.status_code == 404:
            raise RuntimeError(f"Canvas 返回 404 —— 路径不存在: {path}")
        resp.raise_for_status()
        return resp.json()

    def get_all(self, path: str, max_pages: int = 10, **params):
        """跟着 Link header 翻页,返回合并后的列表。"""
        params.setdefault("per_page", 100)
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith("/api/"):
            path = "/api/v1" + path
        url = self.base_url + path
        out, pages = [], 0
        while url and pages < max_pages:
            resp = self.session.get(url, params=params if pages == 0 else None, timeout=30)
            resp.raise_for_status()
            chunk = resp.json()
            if not isinstance(chunk, list):
                return chunk
            out.extend(chunk)
            url = (resp.links.get("next") or {}).get("url")
            pages += 1
        return out

    # ------------------------------------------------------------ 领域方法

    def whoami(self) -> dict:
        return self.get("/users/self/profile")

    def courses(self, current_term_only: bool = True) -> list[dict]:
        """这学期在读的课。

        `current_term_only` 关掉才会把上学期的遗留一起列出来 —— 判断在
        past_term() 里,注释说明了为什么光靠 Canvas 的注册状态不够。
        """
        raw = self.get_all(
            "/courses",
            enrollment_state="active",
            **{"include[]": ["total_scores", "term", "course_progress"]},
        )
        out = []
        for c in raw:
            # access_restricted_by_date 是 Canvas 自己认定的 past/future
            # enrollment:它连课名都不给,只有一个 id
            if c.get("access_restricted_by_date"):
                continue
            if current_term_only and past_term(c):
                continue
            enr = (c.get("enrollments") or [{}])[0]
            out.append(
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "code": c.get("course_code"),
                    "current_score": enr.get("computed_current_score"),
                    "current_grade": enr.get("computed_current_grade"),
                    "term": (c.get("term") or {}).get("name"),
                    "term_code": term_code(c),
                    "url": f"{self.base_url}/courses/{c.get('id')}",
                }
            )
        return out

    # ------------------------------------------------------------ 课表来源

    # 课表这几个方法是给 timetable.py 用的。Canvas **没有**上课时间和 office
    # hour 的结构化字段 —— /appointment_groups 和 /calendar_events 实测都是空
    # 的,sections 里也只有名字。时间全是老师写在首页表格、syllabus、公告里
    # 的散文。所以这里的活儿是"把可能写了时间的正文都捞出来",解析交给模型。

    def my_sections(self) -> dict[int, list[int]]:
        """我在每门课注册的 section id。

        合并课(CS5800 把周一班和周三班并成一门)里,两个 section 的上课时间
        不一样。不知道自己在哪一节,课表就会两节都画上 —— 所以这一步不能省。
        """
        out: dict[int, list[int]] = {}
        raw = self.get_all("/users/self/enrollments", **{"state[]": "active"})
        if not isinstance(raw, list):
            return out
        for e in raw:
            cid, sid = e.get("course_id"), e.get("course_section_id")
            if cid and sid:
                out.setdefault(int(cid), []).append(int(sid))
        return out

    def sections(self, course_id: int) -> list[dict]:
        """课程的所有 section(只有 id 和名字,名字里常带 CRN 和校区代码)。"""
        raw = self.get_all(f"/courses/{course_id}/sections")
        if not isinstance(raw, list):
            return []
        return [{"id": s.get("id"), "name": s.get("name") or ""} for s in raw]

    def syllabus(self, course_id: int) -> str:
        info = self.get(f"/courses/{course_id}", **{"include[]": ["syllabus_body"]})
        return info.get("syllabus_body") or ""

    def page_bodies(self, course_id: int, limit: int = 4) -> list[dict]:
        """首页 + 最近更新的几个页面,连正文一起。

        上课时间和 office hour 最常出现在首页那张表里(CS5800 就是),所以
        首页一定要,而且要排在最前面。列表接口不返回 body,得按 slug 再查一次 ——
        页面数量不多,限 limit 个足够,再多只是在给模型灌无关正文。
        """
        out: list[dict] = []
        seen: set[str] = set()
        try:
            fp = self.get(f"/courses/{course_id}/front_page")
            if fp.get("body"):
                out.append({"title": fp.get("title") or "首页", "body": fp["body"],
                            "front": True})
                seen.add(fp.get("url") or "")
        except Exception:
            pass          # 没设首页的课会 404,不是错误
        try:
            lst = self.get_all(f"/courses/{course_id}/pages",
                               max_pages=1, sort="updated_at", order="desc")
        except Exception:
            return out
        if not isinstance(lst, list):
            return out
        for p in lst:
            if len(out) >= limit:
                break
            slug = p.get("url") or ""
            if not slug or slug in seen:
                continue
            seen.add(slug)
            try:
                full = self.get(f"/courses/{course_id}/pages/{slug}")
            except Exception:
                continue
            if full.get("body"):
                out.append({"title": full.get("title") or slug,
                            "body": full["body"], "front": False})
        return out

    # ------------------------------------------------------------ 课程视图

    def assignments(self, course_id: int, keep_past_days: int = 30) -> list[dict]:
        """一门课的作业,带提交状态、要求全文、以及描述里挂的附件。"""
        raw = self.get_all(
            f"/courses/{course_id}/assignments",
            order_by="due_at",
            **{"include[]": ["submission"]},
        )
        if not isinstance(raw, list):
            return []
        out = []
        for a in raw:
            d = days_left(a.get("due_at"))
            if d is not None and d < -keep_past_days:
                continue
            sub_ = a.get("submission") or {}
            desc = a.get("description") or ""
            out.append(
                {
                    "id": a.get("id"),
                    "name": a.get("name") or "(无标题)",
                    "due_local": to_local(a.get("due_at")),
                    "due_utc": a.get("due_at"),
                    "days_left": round(d, 2) if d is not None else None,
                    "urgency": urgency(a.get("due_at")),
                    "points": a.get("points_possible"),
                    "submission_types": a.get("submission_types") or [],
                    "state": sub_.get("workflow_state") or "unsubmitted",
                    "submitted": (sub_.get("workflow_state") or "unsubmitted")
                    not in ("unsubmitted", "deleted"),
                    "score": sub_.get("score"),
                    "late": sub_.get("late"),
                    "missing": sub_.get("missing"),
                    "url": a.get("html_url"),
                    # 要求正文:列表里只给前一段,全文用 assignment_detail 拉
                    "brief": html_to_text(desc, 600),
                    "has_desc": bool(desc.strip()),
                    # 描述里挂的文件 id —— 这就是"这个作业的附件"
                    "file_ids": file_ids_in(desc),
                }
            )
        return out

    def modules(self, course_id: int) -> list[dict]:
        """老师排的课程目录(Week 1 / Week 2 …),连同每个模块里的条目。

        这是"课件分门别类"最靠谱的来源;文件夹名往往是 course files 一锅端。
        没开放模块的课返回空列表(403/404 一样按空处理)。
        """
        try:
            raw = self.get_all(
                f"/courses/{course_id}/modules",
                max_pages=3, **{"include[]": ["items"]},
            )
        except Exception as exc:
            if "403" in str(exc) or "404" in str(exc):
                return []
            raise
        if not isinstance(raw, list):
            return []
        out = []
        for m in raw:
            items = []
            for it in (m.get("items") or []):
                items.append(
                    {
                        "type": it.get("type"),          # File/Assignment/Page/Quiz/ExternalUrl…
                        "title": it.get("title") or "",
                        "content_id": it.get("content_id"),
                        "url": it.get("html_url"),
                        "indent": it.get("indent") or 0,
                    }
                )
            out.append(
                {
                    "id": m.get("id"),
                    "name": m.get("name") or "(未命名模块)",
                    "state": m.get("state"),
                    "items": items,
                }
            )
        return out

    # ------------------------------------------------------------ 文件

    def files(self, course_id: int, limit: int = 200) -> list[dict]:
        """课程文件列表。

        学生对 /courses/:id/files 不一定有权限(老师可以整个关掉文件区),
        403 就当"这门课没有文件"处理,不要让整个界面失败。
        """
        try:
            raw = self.get_all(
                f"/courses/{course_id}/files",
                max_pages=3, sort="updated_at", order="desc",
            )
        except Exception as exc:
            if "403" in str(exc) or "404" in str(exc):
                return []
            raise
        if not isinstance(raw, list):
            return []
        out = []
        for f in raw[:limit]:
            out.append(
                {
                    "id": f.get("id"),
                    "name": f.get("display_name") or f.get("filename") or f"file-{f.get('id')}",
                    "size": f.get("size"),
                    "type": f.get("content-type") or f.get("content_type") or "",
                    "updated": to_local(f.get("updated_at")),
                    # 原始时间戳:增量同步靠它和本地 mtime 比
                    "updated_raw": f.get("updated_at"),
                    "folder_id": f.get("folder_id"),
                    # 这个 url 自带 verifier,是真正能下的那个地址
                    "url": f.get("url"),
                }
            )
        return out

    def file_meta(self, file_id: int) -> dict | None:
        """按 id 单查一个文件。

        **作业附件必须走这条路。** 老师直接上传到作业里的附件不在
        `/courses/:id/files` 列表里(实测 DS5110 的 HW1.ipynb 就不在,
        22 个文件里没有它);而且有的课整个文件区是关着的(CS5800 返回 403),
        那种课的附件只能按 id 查。
        """
        if file_id in self._file_cache:
            return self._file_cache[file_id]
        try:
            f = self.get(f"/files/{file_id}")
        except Exception:
            self._file_cache[file_id] = None
            return None
        out = {
            "id": f.get("id"),
            "name": f.get("display_name") or f.get("filename") or f"file-{file_id}",
            "size": f.get("size"),
            "type": f.get("content-type") or f.get("content_type") or "",
            "updated": to_local(f.get("updated_at")),
            "updated_raw": f.get("updated_at"),
            "folder_id": f.get("folder_id"),
            "url": f.get("url"),
        }
        self._file_cache[file_id] = out
        return out

    def folders(self, course_id: int) -> dict[int, str]:
        """folder_id -> 目录全名。列表里给文件标个所在目录,不然一堆同名文件分不清。"""
        try:
            raw = self.get_all(f"/courses/{course_id}/folders", max_pages=2)
        except Exception:
            return {}
        if not isinstance(raw, list):
            return {}
        return {
            f["id"]: (f.get("full_name") or f.get("name") or "").replace("course files", "").strip("/ ")
            for f in raw if f.get("id")
        }

    def download(self, url: str, dest: Path) -> int:
        """把一个文件下到本地,返回字节数。

        走同一个 session(带 Bearer),因为 Canvas 的 url 有时是需要鉴权的
        /files/:id/download 而不是预签名的 S3 直链。
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.session.get(url, stream=True, timeout=120,
                              allow_redirects=True) as r:
            r.raise_for_status()
            total = 0
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(65536):
                    if chunk:
                        fh.write(chunk)
                        total += len(chunk)
            tmp.replace(dest)      # 下完再改名,半个文件不会被当成下好了
        return total

    def upcoming(self, days: int = 21,
                 course_ids: set[int] | None = None) -> list[dict]:
        """planner/items 把作业、讨论、quiz、日程聚合在一起,是最省事的待办源。

        planner 是跨课程的,**不认我们在 courses() 里筛掉的那些课** ——
        上学期没结课的课里有个远期截止的作业,它照样会冒出来。所以给了
        `course_ids` 就按它过滤(course_id 为空的是个人待办,留着)。
        """
        start = datetime.now(timezone.utc).date().isoformat()
        raw = self.get_all("/planner/items", start_date=start, max_pages=4)
        out = []
        for item in raw:
            cid = item.get("course_id")
            if course_ids is not None and cid and cid not in course_ids:
                continue
            when = item.get("plannable_date")
            d = days_left(when)
            if d is None or d > days:
                continue
            p = item.get("plannable") or {}
            sub = item.get("submissions") or {}
            out.append(
                {
                    "title": p.get("title") or p.get("name") or "(无标题)",
                    "type": item.get("plannable_type"),
                    # 稳定标识,用于「划掉不管了」的记录。不能用标题 —— 教授改个
                    # 标题就会让忽略失效;plannable_id 在 Canvas 里是不变的。
                    "key": f'{item.get("plannable_type")}:{item.get("plannable_id")}',
                    "plannable_id": item.get("plannable_id"),
                    "course_id": item.get("course_id"),
                    "context": item.get("context_name"),
                    "due_local": to_local(when),
                    "due_utc": when,
                    "days_left": round(d, 2),
                    "urgency": urgency(when),
                    "points": p.get("points_possible"),
                    "submitted": bool(sub.get("submitted")) if isinstance(sub, dict) else None,
                    "graded": bool(sub.get("graded")) if isinstance(sub, dict) else None,
                    "url": (
                        f"{self.base_url}{item['html_url']}" if item.get("html_url") else None
                    ),
                }
            )
        out.sort(key=lambda x: x["days_left"])
        return out
