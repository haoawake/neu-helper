# -*- coding: utf-8 -*-
"""NEU Helper 的 MCP stdio server —— 除 requests 外零第三方依赖。

MCP stdio 的传输格式就是「一行一个 JSON-RPC 消息」,所以这里手写协议,
省掉装 mcp SDK 的麻烦。

铁律:stdout 只能出现 JSON-RPC,所有日志走 stderr,否则会话直接崩。
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timedelta, timezone

from canvas_api import CanvasClient, days_left, html_to_text, to_local, urgency

SERVER_NAME = "canvas"
SERVER_VERSION = "1.0.0"
FALLBACK_PROTOCOL = "2025-06-18"

_client: CanvasClient | None = None


def log(msg: str) -> None:
    print(f"[canvas-mcp] {msg}", file=sys.stderr, flush=True)


def client() -> CanvasClient:
    """延迟初始化 —— 配置出错时要变成工具报错,而不是启动即崩(那样什么线索都看不到)。"""
    global _client
    if _client is None:
        _client = CanvasClient()
        log(f"connected to {_client.base_url}")
    return _client


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 工具实现


def t_whoami(_args):
    p = client().whoami()
    return jdump(
        {
            "name": p.get("name"),
            "email": p.get("primary_email"),
            "login_id": p.get("login_id"),
            "time_zone": p.get("time_zone"),
            "canvas_user_id": p.get("id"),
        }
    )


def t_courses(_args):
    return jdump(client().courses())


def t_upcoming(args):
    days = int(args.get("days", 21))
    c = client()
    # 多要一次课程列表,为的是把上学期遗留课程的待办挡在外面(planner
    # 是跨课程的,它不认 courses() 那道筛子)
    items = c.upcoming(days, {x["id"] for x in c.courses()})
    if not items:
        return f"未来 {days} 天内没有待办事项。"
    return jdump(items)


def t_assignments(args):
    cid = args["course_id"]
    include_past = bool(args.get("include_past", False))
    raw = client().get_all(
        f"/courses/{cid}/assignments",
        order_by="due_at",
        **{"include[]": ["submission"]},
    )
    out = []
    for a in raw:
        d = days_left(a.get("due_at"))
        if not include_past and d is not None and d < -14:
            continue
        sub = a.get("submission") or {}
        out.append(
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "due_local": to_local(a.get("due_at")),
                "urgency": urgency(a.get("due_at")),
                "points_possible": a.get("points_possible"),
                "submission_types": a.get("submission_types"),
                "workflow_state": sub.get("workflow_state"),
                "score": sub.get("score"),
                "late": sub.get("late"),
                "missing": sub.get("missing"),
                "url": a.get("html_url"),
            }
        )
    return jdump(out) if out else "这门课没有(近期)作业。"


def t_assignment_detail(args):
    cid, aid = args["course_id"], args["assignment_id"]
    a = client().get(f"/courses/{cid}/assignments/{aid}", **{"include[]": ["submission"]})
    sub = a.get("submission") or {}
    score_note = f" / 得分 {sub['score']}" if sub.get("score") is not None else ""
    lines = [
        f"# {a.get('name')}",
        "",
        f"- 截止: {to_local(a.get('due_at'))}  ({urgency(a.get('due_at'))})",
        f"- 分值: {a.get('points_possible')}",
        f"- 提交方式: {', '.join(a.get('submission_types') or []) or '未指定'}",
        f"- 我的状态: {sub.get('workflow_state', 'unsubmitted')}{score_note}",
        f"- 链接: {a.get('html_url')}",
        "",
        "## 题目要求",
        "",
        html_to_text(a.get("description")) or "(这个作业没写描述,要求可能在附件或课件里)",
    ]
    rubric = a.get("rubric")
    if rubric:
        lines += ["", "## 评分标准 (rubric)", ""]
        for r in rubric:
            lines.append(f"- [{r.get('points')} 分] {r.get('description')}")
            if r.get("long_description"):
                lines.append(f"    {html_to_text(r['long_description'], 500)}")
    return "\n".join(lines)


def t_grades(args):
    cid = args.get("course_id")
    c = client()
    courses = [x for x in c.courses() if not cid or str(x["id"]) == str(cid)]
    out = []
    for course in courses:
        entry = {
            "course": course["code"],
            "course_id": course["id"],
            "current_score": course["current_score"],
            "current_grade": course["current_grade"],
            "graded_items": [],
        }
        try:
            raw = c.get_all(
                f"/courses/{course['id']}/assignments", **{"include[]": ["submission"]}
            )
        except Exception as exc:
            entry["error"] = str(exc)
            out.append(entry)
            continue
        for a in raw:
            sub = a.get("submission") or {}
            if sub.get("score") is None:
                continue
            entry["graded_items"].append(
                {
                    "name": a.get("name"),
                    "score": sub.get("score"),
                    "out_of": a.get("points_possible"),
                }
            )
        out.append(entry)
    return jdump(out)


def t_announcements(args):
    days = int(args.get("days", 14))
    c = client()
    codes = [f"course_{x['id']}" for x in c.courses()]
    now = datetime.now(timezone.utc)
    raw = c.get_all(
        "/announcements",
        start_date=(now - timedelta(days=days)).date().isoformat(),
        end_date=(now + timedelta(days=1)).date().isoformat(),
        **{"context_codes[]": codes},
    )
    out = [
        {
            "title": a.get("title"),
            "course": a.get("context_code"),
            "posted": to_local(a.get("posted_at")),
            "author": (a.get("author") or {}).get("display_name"),
            "body": html_to_text(a.get("message"), 1500),
            "url": a.get("html_url"),
        }
        for a in raw
    ]
    return jdump(out) if out else f"最近 {days} 天没有公告。"


def t_course_content(args):
    cid = args["course_id"]
    kind = args.get("kind", "modules")
    c = client()

    if kind == "modules":
        raw = c.get_all(f"/courses/{cid}/modules", **{"include[]": ["items"]})
        return jdump(
            [
                {
                    "name": m.get("name"),
                    "state": m.get("state"),
                    "items": [
                        {
                            "title": i.get("title"),
                            "type": i.get("type"),
                            "url": i.get("html_url"),
                        }
                        for i in (m.get("items") or [])
                    ],
                }
                for m in raw
            ]
        )

    if kind == "files":
        raw = c.get_all(f"/courses/{cid}/files", sort="updated_at", order="desc")
        return jdump(
            [
                {
                    "name": f.get("display_name"),
                    "size_kb": round((f.get("size") or 0) / 1024, 1),
                    "updated": to_local(f.get("updated_at")),
                    "url": f.get("url"),
                }
                for f in raw[:60]
            ]
        )

    if kind == "pages":
        raw = c.get_all(f"/courses/{cid}/pages", sort="updated_at", order="desc")
        return jdump(
            [
                {
                    "title": p.get("title"),
                    "url_slug": p.get("url"),
                    "updated": to_local(p.get("updated_at")),
                }
                for p in raw[:60]
            ]
        )

    if kind == "syllabus":
        info = c.get(f"/courses/{cid}", **{"include[]": ["syllabus_body"]})
        return html_to_text(info.get("syllabus_body")) or "这门课没有填 syllabus。"

    return f"未知的 kind: {kind}(可选 modules / files / pages / syllabus)"


def t_get(args):
    """逃生舱:任意只读 GET。有了它,遇到上面没覆盖的接口也不会卡住。"""
    path = args["path"]
    params = args.get("params") or {}
    data = client().get(path, **params)
    text = jdump(data)
    if len(text) > 30000:
        text = text[:30000] + "\n...[响应过大,已截断]"
    return text


TOOLS = [
    {
        "name": "canvas_whoami",
        "description": "确认 Canvas 连接和当前登录身份(姓名、邮箱、学号、时区)。排查 token 问题时先用这个。",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_whoami,
    },
    {
        "name": "canvas_courses",
        "description": "列出这学期在读的课程,含课程 ID、课号、当前总评分数。其他工具需要的 course_id 从这里拿。往期学期的课(老师忘了结课的那种)不在里面。",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_courses,
    },
    {
        "name": "canvas_upcoming",
        "description": "跨所有课程的待办清单(作业/讨论/quiz/日程),按截止时间排序,含剩余天数和是否已提交。回答『我该做什么』首选这个。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "往后看多少天,默认 21",
                    "default": 21,
                }
            },
        },
        "fn": t_upcoming,
    },
    {
        "name": "canvas_assignments",
        "description": "某门课的作业列表,含提交状态、得分、是否迟交/缺交。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "description": "课程 ID"},
                "include_past": {
                    "type": "boolean",
                    "description": "是否包含 14 天前就过期的作业,默认 false",
                    "default": False,
                },
            },
            "required": ["course_id"],
        },
        "fn": t_assignments,
    },
    {
        "name": "canvas_assignment_detail",
        "description": "单个作业的完整信息:题目要求全文、分值、提交方式、rubric 评分标准、我的提交状态。动手做作业前用这个读要求。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "course_id": {"type": "integer"},
                "assignment_id": {"type": "integer"},
            },
            "required": ["course_id", "assignment_id"],
        },
        "fn": t_assignment_detail,
    },
    {
        "name": "canvas_grades",
        "description": "成绩:每门课的当前总评 + 已评分作业的逐项得分。不传 course_id 就返回全部课程。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "description": "可选,只看一门课"}
            },
        },
        "fn": t_grades,
    },
    {
        "name": "canvas_announcements",
        "description": "所有课程最近的公告全文(教授改 DDL、调课、考试通知通常在这里)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "往前看多少天,默认 14",
                    "default": 14,
                }
            },
        },
        "fn": t_announcements,
    },
    {
        "name": "canvas_course_content",
        "description": "某门课的教学内容:kind=modules 看周次结构/进度,files 看课件下载链接,pages 看页面,syllabus 看教学大纲。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "course_id": {"type": "integer"},
                "kind": {
                    "type": "string",
                    "enum": ["modules", "files", "pages", "syllabus"],
                    "default": "modules",
                },
            },
            "required": ["course_id"],
        },
        "fn": t_course_content,
    },
    {
        "name": "canvas_get",
        "description": "逃生舱:对任意 Canvas API 路径发只读 GET,例如 path='/courses/200001/discussion_topics'。上面的工具覆盖不到时用它。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "如 /users/self/todo,可省略 /api/v1 前缀",
                },
                "params": {"type": "object", "description": "查询参数键值对"},
            },
            "required": ["path"],
        },
        "fn": t_get,
    },
]

TOOL_MAP = {t["name"]: t for t in TOOLS}
PUBLIC_TOOLS = [{k: v for k, v in t.items() if k != "fn"} for t in TOOLS]


# ---------------------------------------------------------------- JSON-RPC


def handle(msg: dict) -> dict | None:
    """返回要写回 stdout 的响应;通知类消息返回 None。"""
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        # 回显客户端请求的协议版本,兼容性最好
        version = params.get("protocolVersion") or FALLBACK_PROTOCOL
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method and method.startswith("notifications/"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": PUBLIC_TOOLS}}

    # 没声明 resources/prompts 能力,但有客户端还是会问 —— 给空列表比报错省事
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}

    if method == "tools/call":
        name = params.get("name")
        tool = TOOL_MAP.get(name)
        if tool is None:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [{"type": "text", "text": f"没有这个工具: {name}"}],
                    "isError": True,
                },
            }
        try:
            text = tool["fn"](params.get("arguments") or {})
            is_error = False
        except Exception as exc:
            log(f"tool {name} failed: {exc}\n{traceback.format_exc()}")
            text = f"{type(exc).__name__}: {exc}"
            is_error = True
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            },
        }

    if mid is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    # Windows 上不设这个,中文工具描述会在管道里编码报错
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    log("server started, waiting for initialize")
    for line in sys.stdin:
        # 去掉 BOM。有些客户端(PowerShell 的管道就是一个)会在 UTF-8 里带
        # 上 BOM,而 json.loads 不认它 —— 整条请求会被当成坏 JSON 丢掉。
        # Claude Code 本身不带,但装机脚本的自检是用管道喂进来的,
        # 不容忍这一个字符就会每次都误报"MCP 没响应"。
        line = line.lstrip("﻿").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            log(f"bad json: {exc}")
            continue
        try:
            resp = handle(msg)
        except Exception as exc:
            log(f"handler crashed: {exc}\n{traceback.format_exc()}")
            resp = {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "error": {"code": -32603, "message": str(exc)},
            }
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    log("stdin closed, exiting")


if __name__ == "__main__":
    main()
