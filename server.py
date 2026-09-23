# -*- coding: utf-8 -*-
"""NEU Helper 的本地 HTTP 后端。

为什么不用 pywebview 的 js_api 桥:那个桥每次调用返回值都要跨线程 marshal 回
WebView2 的 UI 线程,而 WebView2 的 COM 对象只允许 UI 线程访问。低频调用没事,
但聊天流式增量需要高频往返,桥会被打满,窗口直接卡成「未响应」。

改成本地 HTTP 之后,前端用 fetch / EventSource 走浏览器自己的网络栈,
聊天流是一条长驻 SSE 连接 —— 热路径上一次 marshal 都没有。

只监听 127.0.0.1,并且用一次性 token 校验 —— 这些接口会吐出个人学业数据,
不该让本机其他进程随便读。
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from canvas_api import (
    SETUP_SCRIPT,
    CanvasClient,
    CanvasConfigError,
    days_left,
    html_to_text,
    parse_ts,
    to_local,
)
import desktop
import native_window
import platform_id
import setupfix
import updater
import version as appver
from briefings import (BRIEF_HOUR, BriefingRunner, BriefingStore, build_prompt,
                       today_str)
from chat_bridge import ChatSession
from chats import CONTEXT_TURNS, ChatStore
from dismissals import DismissStore
from filesync import FileSync
import mailbox as mailmod
import mailai
import mailflags
import mailparts
import mailevents
import mailpeople
import memos
import timetable as tt
import applang
import orb_render
import toast as toastmod
import toast_render

# 两个根目录。**没打包的时候它们是同一个**,所以平时读起来和以前一样。
#
# 打包成 exe 之后必须分开:
#   GUI   前端三件套是**打包进去的只读资源**,在临时解包目录里
#   HERE  data/ 是**运行时要写**的(邮件、对话、课件、偏好),得在 exe 旁边
# 混用的后果是:要么界面加载不出来,要么每次启动数据都没了
# (onefile 模式下解包目录是临时的,退出就删)。
if getattr(sys, "frozen", False):
    RES = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    HERE = Path(sys.executable).resolve().parent
else:
    RES = HERE = Path(__file__).resolve().parent
GUI = RES / "gui"

WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

TOKEN = secrets.token_urlsafe(16)
# 邮件列表一页多少封。放在这儿而不是靠近窗口那几个常量:list_messages 的默认
# 参数在类定义时就求值,那会儿文件后半段还没执行到
PAGE_SIZE = 50
# 「补抽日程」一次最多重判多少封。半年前那封信里的截止日期早过去了,
# 为它花钱没意义 —— 这个上限就是这个意思
REDATE_LIMIT = 80
# 悬浮球停一秒弹出来的那条备忘录窄栏(逻辑像素)
PEEK_SIZE = (300, 460)
HTTP_LOG = os.environ.get("CANVAS_HELPER_HTTPLOG") == "1"


def _safe_name(name: str) -> str:
    """文件名落盘前清一遍。

    Canvas 上的文件名什么都有(冒号、斜杠、引号),直接拿去建文件会炸;
    而且必须挡住 .. 和绝对路径,否则下载接口就成了任意写。
    """
    keep = []
    for ch in (name or "file"):
        keep.append("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch)
    out = "".join(keep).strip(" .") or "file"
    return out[:120]


# 弹窗里会出现的那几个固定词的英文。
#
# **只给右下角那条原生弹窗用。** 界面上的那份在 gui/i18n.js 的 EN 表里 ——
# 那张表是权威,这里是它的一小块副本。副本存在的唯一理由是弹窗不是网页
# (自己画的一张位图,DOM 观察者够不着),别为了别的用途往这儿加词。
# 改了 i18n.js 里这几条记得改这儿。
_TOAST_EN = {
    # 作业紧急度(urgency_bucket)
    "无期限": "no deadline", "已过期": "overdue", "今天到期": "due today",
    "紧急": "urgent", "临近": "coming up", "充裕": "plenty of time",
    # 邮件标签(mailai.TAG_DEFS)。标签是**枚举值**,模型那边永远输出中文
    # (json_note 明说了不许翻枚举值),所以这儿必须自己映一道
    "诈骗": "Scam", "学业": "Study", "求职": "Job hunt", "工作": "Work",
    "会议": "Meeting", "行政": "Admin", "财务": "Money", "住房": "Housing",
    "出行": "Travel", "健康": "Health", "订阅": "Subscriptions",
    "广告": "Ads", "社交": "Social", "娱乐": "Entertainment",
    "验证码": "Codes", "系统通知": "System",
}


def _toast_en(zh: str) -> str:
    """中文档原样返回;英文档认得的换掉,认不出的(模型自己造的标签)也原样留着。"""
    return _TOAST_EN.get(zh, zh) if applang.is_en() else zh


def urgency_bucket(d: float | None, soon: float = 3.0) -> tuple[str, str, str]:
    """剩余天数 -> (状态 slug, 形状图标, 文字标签)。

    颜色只是三个通道之一:形状和文字各自也能独立区分状态,
    所以灰度打印或色觉障碍下信息不丢失。

    soon 是设置里那根「几天内算紧急」,默认 3。两档都从它派生 ——
    紧急 = soon 天内,临近 = 2×soon 天内。硬写死的话那个设置就是个摆设。
    """
    if d is None:
        return ("none", "·", "无期限")
    if d < 0:
        return ("critical", "■", "已过期")
    if d < 1:
        return ("critical", "■", "今天到期")
    if d < soon:
        return ("serious", "▲", "紧急")
    if d < soon * 2:
        return ("warning", "◆", "临近")
    return ("good", "○", "充裕")


class Backend:
    def __init__(self):
        self._client: CanvasClient | None = None
        self._cache: dict | None = None
        self._files_cache: dict[int, tuple] = {}
        self._course_cache: dict[int, tuple] = {}
        self._lock = threading.Lock()

        # 两条独立通道:用户问问题不会打断简报生成,反之亦然
        self.chats = ChatStore(HERE / "data" / "chats.json")
        self.chat = ChatSession(HERE, channel="chat",
                                on_done=lambda s: self._chat_done(s))
        self.dismissed = DismissStore(HERE / "data" / "dismissed.json")
        self.memos = memos.MemoStore(HERE / "data" / "memos.json")
        self.briefing_store = BriefingStore(HERE / "data" / "briefings.json")
        self.brief_session = ChatSession(
            HERE,
            channel="briefing",
            on_done=lambda s: self.briefings.on_session_done(s),
        )
        self.briefings = BriefingRunner(
            self.briefing_store,
            self.brief_session,
            # 自动播报时也要打热标记,否则第一段增量会慢 400ms
            ensure_fresh_data=lambda: (self.mark_hot(), self.dashboard(force=True))[1],
            hour_provider=lambda: int(read_prefs().get("briefHour", BRIEF_HOUR)),
            history_provider=lambda: int(read_prefs().get("briefHistory", 3)),
            prompt_builder=lambda store, date: build_prompt(
                store, date,
                int(read_prefs().get("briefHistory", 3)),
                read_prefs().get("focusCourses", ""),
            ),
        )

        # ── 邮箱:一整套和课业平行的东西(存档 / 简报 / 对话都分开)
        self.mail = mailmod.MailStore(HERE / "data" / "mail.json")
        # 重点标注单独存:mail.json 是滚动缓存,标注得比它活得久
        self.mail_flags = mailflags.FlagStore(HERE / "data" / "mail_flags.json")
        # 发件人分组。跟着地址走,不跟着邮件走 —— 所以也不能塞进滚动缓存
        self.people = mailpeople.PeopleStore(HERE / "data" / "mail_people.json")
        # 邮件日程的删改记录。日程本体在 mail_ai.json 每封信那条里,
        # 这里只有覆盖层 —— 理由见 mailevents 的模块注释
        self.mail_events = mailevents.EventStore(
            HERE / "data" / "mail_events.json")
        # AI 过目的结果。单独一份,和 mail.json 的滚动缓存解耦 ——
        # 一封信只过一次模型,这份存档就是"过过了"的凭据
        self.mail_ai = mailai.TagStore(HERE / "data" / "mail_ai.json")
        self.analyzer = mailai.Analyzer(
            self.mail, self.mail_ai, HERE,
            facts_getter=lambda: read_prefs().get("mailFacts") or [],
            catalog_getter=lambda: read_prefs().get("mailTags") or mailai.DEFAULT_TAGS,
            model_getter=lambda: read_prefs().get("mailModel") or "sonnet",
            on_event=lambda st: self.push_mail(self.mail_fetcher.snapshot()),
            on_new_tags=self._learn_tags,
            # 发件人分组是日程那道闸要用的:分了组的人(必看/好友/熟人)
            # 说周四见就是真要见,渠道信的"活动预告"则一律不收
            group_getter=lambda who: self.people.group_of(who or ""),
        )

        # 补抓正文的进度。和收邮件分开:收邮件是"有没有新的",
        # 补抓是"存量里还有多少封没取过全文"
        self.fill_state = {"running": False, "done": 0, "total": 0,
                           "last": None, "errors": []}
        self._fill_lock = threading.Lock()
        self.mail_fetcher = mailmod.MailFetcher(
            self.mail, on_event=lambda st: self._on_mail_event(st),
            data_root=HERE / "data",
            max_file_mb_getter=lambda: int(read_prefs().get("mailFileMB", 20)))
        # IMAP IDLE:服务器一有新信就推。轮询留着当兜底(长连接会被掐)
        self.mail_idle = mailmod.IdleWatcher(
            on_change=lambda: self.mail_fetcher.fetch_now(),
            enabled_getter=lambda: bool(read_prefs().get("mailOn", True)))
        self.mail_chats = ChatStore(HERE / "data" / "mail_chats.json")
        self.mail_chat = ChatSession(HERE, channel="mailchat",
                                     on_done=lambda s: self._mail_chat_done(s))
        self.mail_brief_store = BriefingStore(HERE / "data" / "mail_briefings.json")
        self.mail_brief_session = ChatSession(
            HERE, channel="mailbrief",
            on_done=lambda s: self.mail_briefings.on_session_done(s))
        self.mail_briefings = BriefingRunner(
            self.mail_brief_store,
            self.mail_brief_session,
            # 生成前先抓一轮邮件,别拿半小时前的数据播报
            # 先收一轮邮件,再等过目跑完 —— 简报要的是标签和摘要,
            # 分析没跑完就播报等于对昨天的邮件视而不见
            ensure_fresh_data=lambda: (self.mark_hot(),
                                       self.mail_fetcher.fetch_now(),
                                       self._await_analysis())[1],
            hour_provider=lambda: int(read_prefs().get("mailHour", 9)),
            prompt_builder=lambda store, date: mailmod.build_mail_prompt(
                self.mail,
                store.recent_texts(n=int(read_prefs().get("briefHistory", 3)),
                                   before=date),
                date,
                facts=read_prefs().get("mailFacts") or [],
                watching=mailflags.briefing_block(self.mail_flags),
                # 只给前一天那些;rate_messages 会把存好的标签和摘要挂上来,
                # 所以这一步不碰任何正文
                rated=self.rate_messages(self.mail.all(limit=300)),
            ),
            label="邮件简报",
            # 一个账号都没配就别去问模型 —— 那次调用只会得到"没有邮件"
            precondition=lambda: (
                (True, "")
                if any(a["ready"] for a in mailmod.accounts_public())
                else (False, "还没配邮箱账号(设置 → 邮箱)")
            ),
        )

        # 课表:上课时间和 office hour **不是查出来的,是从课程正文里抽的** ——
        # Canvas 没有这两样的结构化接口,理由和实测记在 timetable.py 开头。
        # 抽要跑模型,所以走后台线程 + 一份存档,界面渲染只读存档。
        self.schedule = tt.ScheduleStore(HERE / "data" / "schedule.json")
        self.sched_state = {"running": False, "done": 0, "total": 0,
                            "course": "", "last": None, "errors": [],
                            # 这一轮放回来几条被删的、几门课因为源文没变跳过了。
                            # 手动点「重新解析」却什么都没发生时,得说得出原因
                            "restored": 0, "skipped": 0}
        self._sched_lock = threading.Lock()

        # 课件同步:把 Canvas 上的文件分门别类下到本地,增量更新
        self.sync = FileSync(
            HERE / "data" / "downloads",
            client_getter=self.client,
            course_getter=lambda cid, force=False: self.course(cid, force),
            on_event=lambda st: self.push_sync(st),
            limit_getter=lambda: int(read_prefs().get("maxSyncMB", 80)) * 1024 * 1024,
        )

        # 第三条通道:窗口形态。悬浮球是原生窗口,点它的时候得有办法告诉页面
        # 「你现在是对话框形态了」—— 不然 CSS 还停在球那一态。
        self._win_events: list[dict] = []
        self._win_lock = threading.Lock()
        # 悬浮球悬停展开的那条备忘录窄栏
        self._peek = False
        self._peek_lock = threading.Lock()
        # 页面在填表时把浮窗"钉住":系统的日期选择器是另一个弹出窗口,
        # 指针会离开浮窗的矩形,不钉住的话选个时间窗口就没了
        self._peek_pin = False
        # 右下角那个信息弹窗。由 app.py 在窗口出来之后装上(要先问到 DPI)
        self.toast = None
        # 已经报过的 Canvas 条目。第一次启动只记账不弹 ——
        # 不然一装上就把 20 条存量作业全弹一遍
        self._seen_items: set[str] | None = None

        # ── 更新
        self.tray = None                     # 托盘/菜单栏图标,app.py 装上来
        self.update_info: dict = {}          # 上一次 check() 的结果
        # 上一次点的更新有没有落地。**启动时立刻核对一次** —— 换文件的是
        # 另一个进程,它成功与否发起方永远看不到(那时候它已经退了),
        # 只有装好的这一份起来之后拿自己的版本号去比才知道
        self.failed_update: dict = updater.take_pending(HERE)
        if self.failed_update:
            print("[update] 上次更新没落地:想装 v"
                  + str(self.failed_update.get("to"))
                  + ",现在还是 v" + str(self.failed_update.get("now")),
                  file=sys.stderr, flush=True)
        self.updater = updater.Updater(
            HERE, on_event=lambda st: self.push_update(self.update_info, st))
        # 这个版本已经提醒过了吗 —— 弹窗一个版本只弹一次,不是每次检查都弹
        self._told_version = ""
        # 点右下角那条弹窗之后界面该跳到哪儿。show 的时候写上、点的时候取走
        self._toast_go = ""

    def mark_hot(self) -> None:
        """刚发过消息 —— 接下来这段时间 SSE 用高频轮询。"""
        self._hot_until = time.time() + 90

    def is_hot(self) -> bool:
        return (time.time() < getattr(self, "_hot_until", 0.0)
                or self.chat.is_busy() or self.brief_session.is_busy()
                or self.mail_chat.is_busy() or self.mail_brief_session.is_busy())

    def push_window(self, mode: str) -> None:
        with self._win_lock:
            self._win_events.append({"channel": "window", "kind": "mode", "mode": mode})

    def push_sync(self, st: dict) -> None:
        """同步进度推给前端。只留最后一条 —— 进度是"当前状态",不是流水。"""
        with self._win_lock:
            self._win_events = [e for e in self._win_events if e.get("kind") != "sync"]
            self._win_events.append({"channel": "window", "kind": "sync", "sync": st})

    def push_schedule(self) -> None:
        """课表解析进度。和 push_sync 一样只留最后一条 —— 这是"当前状态"。"""
        with self._win_lock:
            self._win_events = [e for e in self._win_events
                                if e.get("kind") != "sched"]
            self._win_events.append({"channel": "window", "kind": "sched",
                                     "sched": dict(self.sched_state)})

    def push_update(self, info: dict, job: dict) -> None:
        """更新的状态推给前端。和 push_sync 一样只留最后一条 ——
        下载进度是"当前是什么样",不是一条条流水。"""
        with self._win_lock:
            self._win_events = [e for e in self._win_events
                                if e.get("kind") != "update"]
            self._win_events.append({"channel": "window", "kind": "update",
                                     "info": info, "job": job})

    def sync_courses(self) -> list[dict]:
        d = self.dashboard()
        return [] if d.get("error") else (d.get("courses") or [])

    def push_prefs(self, prefs: dict) -> None:
        """偏好变了也推一条:手改 prefs.json、或者从别的地方改,页面都能立刻跟上。"""
        with self._win_lock:
            self._win_events.append(
                {"channel": "window", "kind": "prefs", "prefs": prefs})

    def drain_window(self) -> list[dict]:
        with self._win_lock:
            out, self._win_events[:] = list(self._win_events), []
        return out

    # ------------------------------------------------------------ 文件

    def files(self, course_id: int, force: bool = False) -> dict:
        """某门课的文件列表。缓存 5 分钟 —— 文件区不像 DDL 那样一直变。"""
        now = time.time()
        hit = self._files_cache.get(course_id)
        if hit and not force and now - hit[0] < 300:
            return hit[1]
        try:
            c = self.client()
            files = c.files(course_id)
            folders = c.folders(course_id) if files else {}
        except CanvasConfigError as exc:
            return {"error": "config", "message": str(exc), "files": []}
        except Exception as exc:
            return {"error": "network", "message": f"{type(exc).__name__}: {exc}",
                    "files": []}
        short = self.course_short(course_id)
        idx = self.local_index()
        for f in files:
            f["folder"] = folders.get(f.get("folder_id"), "")
            f["local"] = idx.get((_safe_name(short), f["name"]))
        out = {"error": None, "course_id": course_id, "course_short": short,
               "files": files}
        self._files_cache[course_id] = (now, out)
        return out

    def course_short(self, course_id: int) -> str:
        d = self._cache or {}
        for c in (d.get("courses") or []):
            if c.get("id") == course_id:
                return c.get("short") or c.get("code") or str(course_id)
        return str(course_id)

    def download_dir(self, course_short: str) -> Path:
        return HERE / "data" / "downloads" / _safe_name(course_short)

    def local_index(self) -> dict:
        """(课程, 文件名) -> 本地路径。来自同步器写的 index.json。

        同步之后文件是按 作业/课件/其他 分类放的,不能再用"平铺路径"去猜
        本地在哪 —— 那样刚同步好的文件会全部显示成"没下载"。
        """
        path = HERE / "data" / "downloads" / "index.json"
        try:
            st = path.stat().st_mtime
        except OSError:
            self._local_idx = ({}, 0.0)
            return {}
        cached, when = getattr(self, "_local_idx", ({}, -1.0))
        if when == st:
            return cached
        out = {}
        try:
            d = json.loads(path.read_text(encoding="utf-8-sig"))
            for f in d.get("files", []):
                out[(f.get("course"), f.get("name"))] = f.get("path")
        except Exception:
            out = {}
        self._local_idx = (out, st)
        return out

    def download(self, course_id: int, file_id: int) -> dict:
        info = self.files(course_id)
        if info.get("error"):
            return {"ok": False, "error": info.get("message")}
        f = next((x for x in info["files"] if x["id"] == file_id), None)
        if f is None:
            return {"ok": False, "error": "这门课的文件列表里没有这个 id"}
        if not f.get("url"):
            return {"ok": False, "error": "Canvas 没给这个文件的下载地址"}
        # 落点要和同步器一致:同步索引里有就用那个路径,别在课程根目录下再
        # 铺一份平的副本(那样同一个文件会有两处,"已同步"的判断也会乱)
        known = self.local_index().get((_safe_name(info["course_short"]), f["name"]))
        dest = Path(known) if known else (
            self.download_dir(info["course_short"]) / "其他" / _safe_name(f["name"]))
        if dest.exists():
            return {"ok": True, "path": str(dest), "name": f["name"],
                    "size": dest.stat().st_size, "cached": True}
        try:
            size = self.client().download(f["url"], dest)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        f["local"] = str(dest)
        return {"ok": True, "path": str(dest), "name": f["name"], "size": size,
                "cached": False}

    # ------------------------------------------------------------ 课程视图

    def course(self, course_id: int, force: bool = False) -> dict:
        """一门课的全部:作业 / 模块目录 / 散装文件 / 公告 / 成绩。

        缓存 5 分钟。一次视图要 4~5 个 Canvas 请求,来回切课程不该每次都重拉。
        """
        now = time.time()
        hit = self._course_cache.get(course_id)
        if hit and not force and now - hit[0] < 300:
            return hit[1]

        try:
            c = self.client()
            courses = (self._cache or {}).get("courses") or c.courses()
            meta = next((x for x in courses if x.get("id") == course_id), None)
            assigns = c.assignments(course_id)
            mods = c.modules(course_id)
            files = c.files(course_id)
            folders = c.folders(course_id) if files else {}
            try:
                anns = self._announcements(c, [{"id": course_id,
                                                "code": (meta or {}).get("code", "")}], 21)
            except Exception:
                anns = []
        except CanvasConfigError as exc:
            return {"error": "config", "message": str(exc)}
        except Exception as exc:
            return {"error": "network", "message": f"{type(exc).__name__}: {exc}"}

        short = (meta or {}).get("short") or self.course_short(course_id)
        by_id = {f["id"]: f for f in files}
        idx = self.local_index()
        for f in files:
            f["folder"] = folders.get(f.get("folder_id"), "")
            # 本地副本从同步索引里查(文件是分门别类放的,路径算不出来)
            f["local"] = idx.get((_safe_name(short), f["name"]))

        # 作业的附件:描述 HTML 里挂的那些文件。
        # **不能要求它出现在 /courses/:id/files 里** —— 老师直接上传到作业里的
        # 附件不在那个列表(实测 DS5110 的 HW1.ipynb 就不在),而且有的课整个
        # 文件区是关的(CS5800 返回 403)。列表里没有就按 id 单查。
        used: set[int] = set()
        for a in assigns:
            atts = []
            for fid in a.pop("file_ids", []):
                f = by_id.get(fid) or c.file_meta(fid)
                if not f:
                    continue
                used.add(fid)
                atts.append({
                    "id": f["id"], "name": f["name"], "size": f.get("size"),
                    "local": f.get("local") or idx.get((_safe_name(short), f["name"])),
                    "url": f.get("url"), "updated_raw": f.get("updated_raw"),
                })
            a["attachments"] = atts

        # 模块条目里的文件也补上本地状态,顺便记下哪些文件已经"归过类"
        for m in mods:
            for it in m["items"]:
                if it.get("type") == "File" and it.get("content_id") in by_id:
                    f = by_id[it["content_id"]]
                    used.add(f["id"])
                    it["file"] = {"id": f["id"], "name": f["name"],
                                  "size": f["size"], "local": f["local"],
                                  "url": f.get("url"),
                                  "updated_raw": f.get("updated_raw")}

        # 剩下的才叫"其他文件" —— 已经出现在作业附件或模块里的不重复列一遍
        loose = [f for f in files if f["id"] not in used]

        out = {
            "error": None,
            "id": course_id,
            "short": short,
            "name": (meta or {}).get("name") or short,
            "url": (meta or {}).get("url"),
            "score": {
                "has": (meta or {}).get("current_score") is not None,
                "current": (meta or {}).get("current_score"),
                "letter": (meta or {}).get("current_grade"),
            },
            "assignments": assigns,
            "modules": mods,
            "files": loose,
            "file_count": len(files),
            "announcements": anns,
            # 前端要靠它区分"还没轮到"和"太大所以没下"
            "max_sync_bytes": int(read_prefs().get("maxSyncMB", 80)) * 1024 * 1024,
            # 本地目录:前端的「打开文件夹」按钮要用
            "local_dir": str(self.download_dir(short)),
            "fetched_at": datetime.now().strftime("%H:%M:%S"),
        }
        self._course_cache[course_id] = (now, out)
        return out

    # 一轮最多补多少封。整封下载是有流量的(一般一封几十到几百 KB),
    # 分批跑既能看到进度,也不至于一口气把连接占满
    FILL_CHUNK = 20
    FILL_MAX_PER_RUN = 300

    def box_counts(self) -> dict:
        """收件箱 / 垃圾箱各有多少封,以及各分组的来信数。

        单独算一遍全量 —— 界面上那两个数字要的是"总共",不是"当前这一页"。
        """
        trash_tags = set(read_prefs().get("mailTrashTags") or [])
        rated = self.rate_messages(self.mail.all(limit=3000))
        trash = sum(1 for m in rated if self.is_trash(m, trash_tags))
        by_person: dict[str, int] = {}
        for m in rated:
            g = m.get("person")
            if g:
                by_person[g] = by_person.get(g, 0) + 1
        return {"inbox": len(rated) - trash, "trash": trash,
                "person": by_person}

    def fill_pending(self) -> list[dict]:
        """还没取过全文的。取过的 has_body 是 True/False,没取过的压根没这个键 ——
        所以判断用"键在不在",不是真假值。nobody 是服务器上已经没有的那些,
        标了就不再反复去要。"""
        return [m for m in self.mail.all(limit=3000)
                if "has_body" not in m and not m.get("nobody")]

    def fill_snapshot(self) -> dict:
        d = dict(self.fill_state)
        d["errors"] = list(d["errors"])[-3:]
        d["pending"] = len(self.fill_pending())
        return d

    def backfill_now(self) -> bool:
        if not self.fill_pending():
            return False
        if not self._fill_lock.acquire(blocking=False):
            return False
        self.fill_state.update({"running": True, "done": 0, "total": 0})
        threading.Thread(target=self._backfill, daemon=True,
                         name="mailfill").start()
        return True

    def _backfill(self) -> None:
        try:
            todo = self.fill_pending()[:self.FILL_MAX_PER_RUN]
            self.fill_state["total"] = len(todo)
            self.push_mail(self.mail_fetcher.snapshot())
            root = HERE / "data"
            cap = int(read_prefs().get("mailFileMB", 20))
            # 按账号分组:一个连接连着取一批,别一封一登录
            groups: dict[str, list[str]] = {}
            for m in todo:
                groups.setdefault(m["account"], []).append(
                    m["id"].rsplit("#", 1)[1])
            for acc_id, uids in groups.items():
                acc = mailmod.get_account(acc_id)
                if not acc or not acc.get("password"):
                    continue
                for i in range(0, len(uids), self.FILL_CHUNK):
                    batch = uids[i:i + self.FILL_CHUNK]
                    msgs, err = mailmod.imap_fetch(
                        acc, known=set(), data_root=root,
                        max_file_mb=cap, only_uids=batch)
                    if err:
                        self.fill_state["errors"].append(err[:200])
                        break
                    got = set()
                    for r in msgs:
                        if r.get("_flags_only"):
                            continue
                        got.add(r["id"])
                        patch = {k: r[k] for k in
                                 ("links", "files", "has_body", "snippet",
                                  "too_big") if k in r}
                        patch.setdefault("has_body", False)
                        self.mail.update(r["id"], patch)
                    # 服务器上已经没有的那几封:标一下,别每轮都去要
                    for u in batch:
                        mid = f"{acc_id}#{u}"
                        if mid not in got:
                            self.mail.update(mid, {"nobody": True})
                    self.fill_state["done"] += len(batch)
                    self.push_mail(self.mail_fetcher.snapshot())
            self.fill_state["last"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception as exc:
            self.fill_state["errors"].append(f"{type(exc).__name__}: {exc}"[:200])
        finally:
            self.fill_state["running"] = False
            self._fill_lock.release()
            self.push_mail(self.mail_fetcher.snapshot())

    def _await_analysis(self, timeout: float = 240.0) -> bool:
        """把还没过目的邮件跑完再回来。最多等 timeout 秒 ——
        等不完也得让简报出去,总比不播报好。"""
        if not read_prefs().get("mailAiOn", True):
            return False
        self.analyzer.analyze_now()
        t0 = time.time()
        while self.analyzer.state.get("running") and time.time() - t0 < timeout:
            time.sleep(1.0)
        return True

    def _on_mail_event(self, st: dict) -> None:
        """收邮件那一轮结束后,顺手把没过目的送去分析。

        Analyzer 自己会判断"没什么可分析的就别跑",所以这里无脑调一次就行。
        """
        self.push_mail(st)
        if st.get("added"):
            self.notify_new_mail(int(st["added"]))
        if st.get("running"):
            return
        # 先补正文再过目:过目要读正文摘要,补抓会把摘要换成解析出来的那一份
        self.backfill_now()
        if read_prefs().get("mailAiOn", True):
            self.analyzer.analyze_now()

    def _migrate_mail_prefs(self) -> None:
        """把新增的标签和「我的情况」栏位补进已有的存档。

        **不能只改 DEFAULT_PREFS。** 那份默认值只在存档里**没有**这个键时
        才生效,而 mailTags / mailFacts 一开始就被写进 prefs.json 了 ——
        于是老用户永远看不到新加的「诈骗」「订阅」标签,也没有「我在用的
        服务」那一栏可填。所以补一次:只增不减,用户删掉过的不硬塞回来
        (靠"这个键从来没出现过"来区分,见下面的 seen)。
        """
        prefs = read_prefs()
        patch = {}
        tags = list(prefs.get("mailTags") or [])
        seen = set(prefs.get("mailTagsSeen") or []) | set(tags)
        fresh = [t for t in mailai.DEFAULT_TAGS if t not in seen]
        if fresh:
            patch["mailTags"] = tags + fresh
            patch["mailTagsSeen"] = sorted(seen | set(fresh))
        facts = list(prefs.get("mailFacts") or [])
        keys = {str(f.get("k") or "") for f in facts if isinstance(f, dict)}
        seen_k = set(prefs.get("mailFactsSeen") or []) | keys
        add = [f for f in DEFAULT_PREFS["mailFacts"]
               if f["k"] not in seen_k]
        if add:
            patch["mailFacts"] = facts + [dict(f) for f in add]
            patch["mailFactsSeen"] = sorted(seen_k | {f["k"] for f in add})
        if patch:
            write_prefs(patch)
            print(f"[mail] 补了 {len(fresh)} 个标签、{len(add)} 条个人信息栏位",
                  file=sys.stderr, flush=True)

    def _learn_tags(self, added: list[str]) -> None:
        """模型新造的标签收进目录 —— 下次它自己看得到,用户也能删。"""
        cur = list(read_prefs().get("mailTags") or mailai.DEFAULT_TAGS)
        for t in added:
            if t not in cur:
                cur.append(t)
        write_prefs({"mailTags": cur[:60]})

    def rate_messages(self, msgs: list[dict]) -> list[dict]:
        """给每封信挂上标注和级别。

        级别的来源按优先级:自己标的重点 > AI 过目的结论 > 内置粗判。
        粗判会带 pending 标记 —— 界面上要看得出"这还不是定论"。
        """
        # 哪些信带日程 —— 算一次给整页用(每封单算要把几百条日程拍平几十遍)
        try:
            dated = self.mail_event_ids()
        except Exception:
            dated = set()
        out = []
        for m in msgs:
            f = self.mail_flags.get(m["id"])
            rank = mailai.rank_of(m["id"], self.mail_ai, f,
                                  fallback=mailflags.classify(m, f))
            out.append({**m, "star": bool(f.get("star")), "done": bool(f.get("done")),
                        "note": f.get("note") or "",
                        "star_days": self.mail_flags.days_since(f),
                        "person": self.people.group_of(m.get("from", "")),
                        # 这封信里的事进了日程表。**删掉那条日程之后就不挂了**
                        # —— 标识说的是"日程表上有它",不是"抽过"
                        "dated": m["id"] in dated,
                        "rank": rank})
        return out

    # 列表的排序 / 筛选都在这里做,不在前端 —— 前端只拿到 120 条,
    # "按重要程度排"要是只排这 120 条,第 121 条那封要紧的就永远看不到
    def is_trash(self, m: dict, trash_tags: set) -> bool:
        """这封信该不该只出现在垃圾箱里。

        **分了组的人是豁免的。** 这就是"分组比标签高一级"的具体含义:
        规则可能判错,你对人的认定不会 —— 导师用 Gmail 群发一封带「广告」
        标签的信,不能因为标签就给藏了。
        """
        if not trash_tags or m.get("person"):
            return False
        return bool(set((m.get("rank") or {}).get("tags") or []) & trash_tags)

    @staticmethod
    def pinned(m: dict) -> int:
        """这封信"盯着"吗 —— 标了重点、还没标完成。

        盯住一封信的意思就是"这件事我还没处理完,别让它沉下去"。所以它是
        **所有排序的第一顺位**:按时间排、按重要程度排,盯着的都在最上面。
        标完成之后就退出置顶,回到普通的排序里。
        """
        return 1 if (m.get("star") and not m.get("done")) else 0

    def list_messages(self, per: int = PAGE_SIZE, page: int = 1,
                      account: str | None = None,
                      unread_only: bool = False, day: str | None = None,
                      tag: str = "", min_level: int = 0,
                      sort: str = "date_desc", box: str = "in",
                      person: str = "",
                      star_only: bool = False) -> tuple[list[dict], int]:
        """筛完排完再切页。返回 (这一页, 筛选后总数)。

        切页放在最后一步:要是先切再排,"按重要程度排"就只在这 50 封里排,
        第 51 封那件要紧事永远翻不到前面。
        """
        msgs = self.rate_messages(self.mail.all(
            limit=3000, account=account, unread_only=unread_only, day=day))
        trash_tags = set(read_prefs().get("mailTrashTags") or [])
        want_trash = box == "trash"
        msgs = [m for m in msgs if self.is_trash(m, trash_tags) == want_trash]
        if person:
            msgs = [m for m in msgs if m.get("person") == person]
        if tag:
            msgs = [m for m in msgs if tag in ((m["rank"] or {}).get("tags") or [])]
        if min_level:
            msgs = [m for m in msgs if (m["rank"] or {}).get("level", 1) >= min_level]
        if star_only:
            msgs = [m for m in msgs if self.pinned(m)]
        if sort == "date_asc":
            # 盯着的照样置顶;-pinned 配升序 = 1 在前
            msgs.sort(key=lambda m: (-self.pinned(m), m.get("ts") or ""))
        elif sort == "level":
            # 三层:盯着的 > 未读/已读 > 级别 > 时间。
            #
            # **"未读还是已读"压过级别**:已读的「要紧」比不过未读的「普通」——
            # 看过了就等于知道了,不该再把没看过的挤下去。
            #
            # **但已读那一堆里面,级别照样管用。** 原来那一版把已读一律折成
            # 0 级,于是"按重要程度排"对已读的部分完全失效 —— 翻到已读那一段
            # 就变成纯时间序了,而那恰恰是回头找一封要紧信的时候。
            msgs.sort(key=lambda m: (
                self.pinned(m),
                1 if m.get("unread") else 0,
                (m["rank"] or {}).get("level", 1),
                m.get("ts") or ""), reverse=True)
        else:
            msgs.sort(key=lambda m: (self.pinned(m), m.get("ts") or ""),
                      reverse=True)
        total = len(msgs)
        per = max(1, min(int(per), 500))
        page = max(1, int(page))
        start = (page - 1) * per
        if start >= total and total:          # 翻过头了(比如筛完只剩一页)
            page = (total + per - 1) // per
            start = (page - 1) * per
        return msgs[start:start + per], total

    def _mail_chat_done(self, session) -> None:
        """邮件对话的一轮结束:落进**邮件那套**存档,别和课业混。"""
        cid = self.mail_chats.active_id()
        self.mail_chats.add(cid, "assistant", session.last_text, session.last_cost)
        self.mail_chats.set_session(cid, session.session_id)

    def open_mail_chat(self, cid: str) -> bool:
        c = self.mail_chats.get(cid)
        if c is None:
            return False
        self.mail_chats.set_active(cid)
        self.mail_chat.cancel()
        self.mail_chat.session_id = c.get("session_id")
        self.mail_chat.total_cost = float(c.get("cost") or 0.0)
        return True

    # ------------------------------------------------------- 悬停展开备忘录

    def peek_on(self) -> None:
        """球上停了 1 秒:把主窗口摆成一条窄栏,只显示备忘录,贴着球弹出来。

        只在"收成球"的时候有意义 —— 窗口本来就开着的话没必要再弹一个。
        """
        h = _hwnd()
        if not h or orb is None or not orb.visible():
            return
        with self._peek_lock:
            if self._peek:
                return
            self._peek = True
        prefs = read_prefs()
        self.push_window_peek(True)
        w, ht = PEEK_SIZE
        scale = native_window.dpi_scale(h)
        pw, ph = int(w * scale), int(ht * scale)
        bx, by, bd = orb.ball_rect()
        wx, wy, ww, wh = native_window.work_area(h)
        # 贴着球放:默认在球的左上方,贴不下就往回收进工作区
        x = bx + bd - pw
        y = by - ph - 8
        x = max(wx, min(x, wx + ww - pw))
        y = max(wy, min(y, wy + wh - ph))
        native_window.set_rect(h, x, y, pw, ph)
        native_window.set_window_alpha(h, float(prefs["opacity"]))
        native_window.show(h, foreground=False)
        native_window.set_topmost(h, True)
        threading.Thread(target=self._peek_watch, daemon=True,
                         name="peekwatch").start()

    def peek_pin(self, on: bool) -> None:
        self._peek_pin = bool(on)

    def peek_off(self, force: bool = False) -> None:
        with self._peek_lock:
            if not self._peek:
                return
            self._peek = False
            self._peek_pin = False
        h = _hwnd()
        self.push_window_peek(False)
        if h and (force or (orb is not None and orb.visible())):
            native_window.hide(h)
            native_window.set_topmost(h, False)

    def peeking(self) -> bool:
        return self._peek

    def _peek_watch(self) -> None:
        """指针离开"球 + 浮窗"这两块区域连续 600ms 就收起来。

        不能只听球的 mouse-leave:指针往浮窗上走的时候正好会触发它,
        那样刚弹出来就被收掉了。
        """
        out_since = 0.0
        while True:
            time.sleep(0.12)
            if not self._peek:
                return
            h = _hwnd()
            if not h or orb is None:
                return
            try:
                px, py = native_window.cursor_pos()
                rects = [native_window.get_rect(h)]
                bx, by, bd = orb.ball_rect()
                rects.append((bx - 6, by - 6, bx + bd + 6, by + bd + 6))
            except Exception:
                return
            inside = any(l <= px <= r and t <= py <= b for l, t, r, b in rects)
            # 钉住了、或者浮窗本身是前台窗口(正在里面点东西)—— 都不收
            if self._peek_pin or native_window.is_foreground(h):
                inside = True
            if inside:
                out_since = 0.0
                continue
            now = time.time()
            if not out_since:
                out_since = now
            elif now - out_since > 0.6:
                self.peek_off()
                return

    def push_window_peek(self, on: bool) -> None:
        with self._win_lock:
            self._win_events = [e for e in self._win_events
                                if e.get("kind") != "peek"]
            self._win_events.append({"channel": "window", "kind": "peek",
                                     "on": bool(on)})

    # ------------------------------------------------------------ 信息弹窗

    def check_update(self, force: bool = False) -> dict:
        """问一次 GitHub。**结果存下来给界面用,不要每次刷新都去问。**

        设置里能关(updateCheck)—— 这个请求会把你的 IP 告诉 GitHub,
        有人不想要这个,得给个开关。
        """
        prefs = read_prefs()
        if not force and not prefs.get("updateCheck", True):
            return {}
        info = updater.check()
        # **源码版还要看一眼 origin。** check() 问的是 releases/latest,
        # 而源码版的更新根本不经过 Release —— 代码一推上 main 就能快进拿到。
        # 不看的话,推上去的修复对源码版用户同样是静默的
        if updater.install_kind(HERE) == "git":
            try:
                g = updater.check_git(HERE)
            except Exception as exc:               # noqa: BLE001
                g = {}
                print(f"[update] 源码检查跳过:{type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
            if g.get("behind"):
                info["ok"] = True
                info.setdefault("current", appver.VERSION)
                info.update({"git_behind": g["behind"],
                             "git_upstream": g.get("upstream") or "",
                             "git_subjects": g.get("subjects") or []})
        # **代码已经换新、但这个进程还是旧的。** 判据是「磁盘上的 version.py
        # vs 内存里的 VERSION」—— 不联网、不猜。原来是拿 GitHub Release 和内存
        # 比,那个比法在工作区已经追平的时候会说"有新版本",点下去 git 却无事
        # 可做,两句话自相矛盾。这种情况要做的不是更新,是**重启**
        disk = updater.disk_version(HERE)
        if disk and appver.is_newer(disk, appver.VERSION):
            info["ok"] = True
            info.setdefault("current", appver.VERSION)
            info["stale_process"] = True
            info["disk_version"] = disk
            info.setdefault("latest", disk)
        if info.get("ok"):
            self.update_info = info
            self.push_update(info, self.updater.snapshot())
            self._tell_update(info, prefs)
        elif info.get("error"):
            # 查不到不是错误:断网、GitHub 抽风都很正常,别拿它烦人
            print(f"[update] 查不到:{info['error']}", file=sys.stderr, flush=True)
        return info

    def _tell_update(self, info: dict, prefs: dict) -> None:
        """有新版本就在右下角报一句。三道闸,缺一不可:

          · **只对发了 Release 的版本报。** 源码版的新提交交给横幅 ——
            为几个提交每 6 小时弹一次窗是骚扰
          · 一个版本只报一次(_told_version 在内存里:重启算一次新的提醒
            时机,而横幅一直挂着,想更新随时点得到)
          · 点过「跳过这个版本」的,那个版本再也不提

        **不受 toastOn 管。** 那个开关说的是"新作业、新邮件报不报",和
        "你装的这份代码旧了"是两回事 —— 不想被内容打扰的人,不等于不想知道
        有更新。所以更新提醒自己一个 updateToast。
        """
        latest = str(info.get("latest") or "")
        if not (info.get("newer") and latest):
            return
        if latest in (self._told_version, str(prefs.get("skipVersion") or "")):
            return
        if not prefs.get("updateToast", True):
            return
        self._told_version = latest
        # 弹窗里带一句"改了什么"。**弹窗只有两行**,所以只念最新那一版第一条;
        # 落后好几版的时候说清一共几版,全文在横幅里点「改了什么」展开
        hist = info.get("history") or []
        head = ""
        for line in (hist[0].get("notes") if hist else info.get("notes") or "").splitlines():
            t = line.strip().lstrip("-*#0123456789. ").strip()
            if t:
                head = t[:40]
                break
        body = applang.tr("点这里去更新。", "Click here to update.")
        if head:
            body = (applang.tr(f"共 {len(hist)} 个版本:{head}…",
                               f"{len(hist)} versions behind: {head}...")
                    if len(hist) > 1
                    else applang.tr(f"更新内容:{head}", f"What changed: {head}"))
        self.notify(applang.tr(f"有新版本 v{latest}", f"v{latest} is out"),
                    body, go="update", force=True)

    def start_update_watch(self) -> None:
        """启动时查一次,之后每天 09:00 查一次。

        原来是"启动 + 每 6 小时"。改成钉在每天固定一个点上,理由有两条:

        · **提醒的时机应该可预期。** 每 6 小时一次意味着提醒会落在一天里的
          任意时刻(包括你正在写作业的时候),而且开机时间不同、每台机器
          还不一样。固定 09:00 和每日简报、邮件简报是同一个作息
        · 发版频率远低于一天四次,多查的那几次除了给 GitHub 送 IP 没有别的
          用处(未认证请求每小时 60 次的额度也是共用的)

        启动那次留 20 秒 —— 让窗口、悬浮球、本地服务先起来,别和它们抢。
        """
        def loop():
            time.sleep(20)
            while True:
                try:
                    self.check_update()
                except Exception as exc:           # noqa: BLE001
                    print(f"[update] {type(exc).__name__}: {exc}",
                          file=sys.stderr, flush=True)
                time.sleep(updater.until_daily(updater.DAILY_HOUR))
        threading.Thread(target=loop, daemon=True, name="updatewatch").start()

    def notify(self, title: str, body: str, go: str = "",
               force: bool = False) -> bool:
        """右下角弹一条。设置里关掉就什么都不做。

        `go` 是点它之后界面该跳到哪儿(见 toast_clicked);`force` 的调用方
        自己管开关 —— 现在只有更新提醒是这样,它有自己的 updateToast。
        """
        if (not force and not read_prefs().get("toastOn", True)) or self.toast is None:
            return False
        self._toast_go = go
        try:
            return bool(self.toast.show(title, body,
                                        float(read_prefs().get("toastSecs", 9))))
        except Exception:
            print("[toast] 弹窗失败:" + traceback.format_exc(limit=2),
                  file=sys.stderr)
            return False

    def toast_clicked(self) -> None:
        """点了右下角那条弹窗。

        两件事:让窗口看得见,外加**跳到该看的地方** —— 更新提醒弹出来、
        点一下却只是把窗口叫回来、横幅还在另一个子页面上,那这条弹窗就白弹了。

        **"看得见"不等于"换形态"。** 原来这儿无条件 `apply_mode(restore_mode())`,
        而 restoreMode 记的是"点悬浮球该还原成哪个形态"(默认 chat)。窗口本来
        开着完整面板的时候点一条弹窗,它会被硬切成小对话框 —— 用户报的就是
        "点了右下角的弹窗,立刻给我缩小了"。
        那个还原只在**收成球**(窗口是隐藏的)时才有意义;窗口已经在眼前,
        该做的只是把它抬到前面,尺寸一个像素都不该动。
        """
        try:
            collapsed = orb is not None and orb.visible()
            if collapsed:
                apply_mode(restore_mode())
            else:
                h = _hwnd()
                if h:
                    native_window.show(h)
                    # **show() 之后必须把置顶再定一次。** 它为了保证窗口能露头
                    # 会蹭一下 HWND_TOPMOST,而那一下是不撤的 —— 不补这句,
                    # 完整面板从此一直压在所有窗口上面,要手动缩一次才恢复
                    # (用户报的就是这个)。apply_mode 那条路早有同样的补丁,
                    # 注释就写在 grow() 里,我这条新路忘了抄。
                    prefs = read_prefs()
                    mode = prefs.get("mode")
                    native_window.set_topmost(
                        h, want_topmost(mode if mode in WINDOW_MODES else "full",
                                        prefs))
        except Exception:                          # noqa: BLE001
            pass
        go, self._toast_go = self._toast_go, ""
        if go:
            with self._win_lock:
                self._win_events.append({"channel": "window", "kind": "goto",
                                         "where": go})

    def notify_new_mail(self, added: int) -> None:
        """有新邮件:等 AI 过目完,把最要紧那封的摘要弹出来。

        **等过目**是有意的:摘要是过目那一步写的,不等的话只能弹主题 ——
        那和系统通知没区别。等不到(关了过目 / 超时)就退回主题。
        """
        def run():
            t0 = time.time()
            while (self.analyzer.state.get("running")
                   and time.time() - t0 < 90):
                time.sleep(1.0)
            msgs = self.list_messages(per=max(1, min(added, 5)), page=1)[0]
            if not msgs:
                return
            # 挑级别最高的那封报;同级里挑最新的(列表本来就是时间序)
            m = max(msgs, key=lambda x: (x.get("rank") or {}).get("level", 1))
            rank = m.get("rank") or {}
            who = (m.get("from") or "").split("<")[0].strip().strip('"')
            tag = "/".join(_toast_en(x) for x in (rank.get("tags") or []))
            new_mail = applang.tr("新邮件", "New mail")
            head = f"{new_mail} · {who}" if who else new_mail
            if added > 1:
                head += applang.tr(f"(共 {added} 封)", f" ({added} total)")
            body = rank.get("summary") or m.get("subject") or ""
            if tag:
                body = f"[{tag}] {body}"
            # 点这条弹窗 = 直接打开这封信。**只在报单封时给** —— 一次进来
            # 好几封的话,点开某一封不一定是他想看的那封,那就还是回列表
            go = f"mail:{m.get('id')}" if (added == 1 and m.get("id")) else "mail"
            self.notify(head, body, go=go)

        threading.Thread(target=run, daemon=True, name="toast-mail").start()

    def notify_canvas(self, data: dict) -> None:
        """Canvas 有新东西(新作业 / 新公告)就报一句。

        比对的是"见过的 id",不是时间戳 —— Canvas 改一次截止时间不会换 id,
        那种变化不该当成新东西反复弹。
        """
        if data.get("error"):
            return
        now = set()
        fresh = []
        for it in data.get("todo") or []:
            key = f"a{it.get('plannable_id')}"
            now.add(key)
            fresh.append((key, f"{it.get('course_short') or ''} · "
                               f"{(it.get('title') or '').strip()}",
                          applang.tr(
                              f"{it.get('due_short') or '无期限'} 截止 · "
                              f"{it.get('status_label') or ''}",
                              f"due {_toast_en(it.get('due_short') or '')} · "
                              f"{_toast_en(it.get('status_label') or '')}")))
        for an in data.get("announcements") or []:
            key = "n" + str(an.get("id") or an.get("url") or an.get("title"))
            now.add(key)
            fresh.append((key, applang.tr(f"{an.get('course') or ''} 新公告",
                                          f"{an.get('course') or ''} announcement"),
                          (an.get("title") or "").strip()))
        if self._seen_items is None:
            self._seen_items = now          # 第一次只记账
            return
        newly = [f for f in fresh if f[0] not in self._seen_items]
        self._seen_items = now
        if not newly:
            return
        head, body = newly[0][1], newly[0][2]
        if len(newly) > 1:
            head += applang.tr(f"(还有 {len(newly) - 1} 条)",
                               f" (+{len(newly) - 1} more)")
        self.notify(head, body)

    def push_memos(self) -> None:
        """备忘录变了就推一条。只留最后一条 —— 这是"当前有几条",不是流水。"""
        with self._win_lock:
            self._win_events = [e for e in self._win_events
                                if e.get("kind") != "memos"]
            self._win_events.append({"channel": "window", "kind": "memos",
                                     "pending": self.memos.pending_count()})

    def push_mail(self, st: dict) -> None:
        """邮件拉取的状态推给前端(和窗口事件同一条 SSE,按 kind 分)。"""
        with self._win_lock:
            self._win_events = [e for e in self._win_events if e.get("kind") != "mail"]
            self._win_events.append({"channel": "window", "kind": "mail", "mail": st})

    def _chat_done(self, session) -> None:
        """一轮回答结束:正文和 session_id 落进当前那段对话。

        session_id 每轮都记一次而不是只记第一次 —— CLI 有时会在续接时给出新的
        id(压缩上下文之类),记住最新的那个下次才续得上。
        """
        cid = self.chats.active_id()
        self.chats.add(cid, "assistant", session.last_text, session.last_cost)
        self.chats.set_session(cid, session.session_id)

    def open_chat(self, cid: str) -> bool:
        """切到某段历史对话:把 CLI 的 session_id 也换过去。"""
        c = self.chats.get(cid)
        if c is None:
            return False
        self.chats.set_active(cid)
        self.chat.cancel()
        self.chat.session_id = c.get("session_id")
        self.chat.total_cost = float(c.get("cost") or 0.0)
        return True

    def client(self) -> CanvasClient:
        with self._lock:
            if self._client is None:
                self._client = CanvasClient()
            return self._client

    # ------------------------------------------------------------ 仪表盘

    def dashboard(self, force: bool = False) -> dict:
        if self._cache is not None and not force:
            return self._cache
        try:
            c = self.client()
            me = c.whoami()
            courses = c.courses()
            prefs0 = read_prefs()
            items = c.upcoming(int(prefs0.get("upcomingDays", 28)),
                               {x["id"] for x in courses})
            try:
                anns = self._announcements(c, courses, int(prefs0.get("annDays", 10)))
            except Exception:
                anns = []   # 公告挂了不该让整个仪表盘空白
        except CanvasConfigError as exc:
            return {"error": "config", "message": str(exc)}
        except Exception as exc:
            return {"error": "network", "message": f"{type(exc).__name__}: {exc}"}

        short = {x["id"]: (x["code"].split(".")[0] or x["code"])[:10] for x in courses}

        dismissed_keys = self.dismissed.keys()
        todo = []
        for it in items:
            slug, icon, label = urgency_bucket(
                it["days_left"], float(prefs0.get("soonDays", 3)))
            due_dt = parse_ts(it.get("due_utc"))
            todo.append(
                {
                    **it,
                    "dismissed": it.get("key") in dismissed_keys,
                    "course_short": short.get(it["course_id"], "—"),
                    "status": slug,
                    "status_icon": icon,
                    "status_label": label,
                    # 卡片放短格式;完整带时区的时间留在 due_local
                    "due_short": (
                        due_dt.astimezone().strftime("%m-%d %H:%M") if due_dt else "无期限"
                    ),
                }
            )

        now = datetime.now()
        # 逾期项单独告警。混进倒计时会让 hero 显示「距离下一个截止 0 天」,
        # 那是在说谎 —— 截止时间已经过去了。
        # 划掉的条目退出所有统计:不进倒计时、不算待办、不报逾期,也不写进
        # 快照 —— 所以以后的每日简报也不会再提它。
        unsubmitted = [
            t for t in todo if not t.get("submitted") and not t.get("dismissed")
        ]
        overdue = [t for t in unsubmitted if t["days_left"] < 0]
        nearest = next((t for t in unsubmitted if t["days_left"] >= 0), None)

        credit = [x for x in courses if x["code"].startswith(("CS", "DS"))]
        pending_points = sum((t.get("points") or 0) for t in unsubmitted)

        self._cache = {
            "error": None,
            "profile": {"name": me.get("name"), "email": me.get("primary_email")},
            "today": f"{now:%m月%d日} {WEEK[now.weekday()]}",
            "fetched_at": f"{now:%H:%M}",
            "hero": (
                {
                    "days": round(nearest["days_left"], 1),
                    "hours": round(nearest["days_left"] * 24),
                    "title": nearest["title"].strip(),
                    "course": nearest["course_short"],
                    "status": nearest["status"],
                    "status_icon": nearest["status_icon"],
                    "status_label": nearest["status_label"],
                    "due": nearest["due_local"],
                }
                if nearest
                else None
            ),
            "overdue": overdue,
            "dismissed_count": sum(1 for t in todo if t.get("dismissed")),
            "stats": [
                {"label": "未提交待办", "value": len(unsubmitted)},
                {"label": "学分课", "value": len(credit)},
                {"label": "待拿分值", "value": int(pending_points)},
            ],
            "todo": todo,
            "courses": [
                {
                    **x,
                    "short": short.get(x["id"], x["code"]),
                    "has_score": x["current_score"] is not None,
                }
                for x in courses
            ],
            "announcements": anns,
        }
        self._write_snapshot(self._cache)
        # 刚重建完就对一遍:有新作业/新公告弹一条。放在这儿(而不是各个调用方)
        # 是因为仪表盘有好几个入口:手动刷新、简报前刷、定时同步
        try:
            self.notify_canvas(self._cache)
        except Exception:
            pass
        return self._cache

    def _write_snapshot(self, d: dict) -> None:
        """把当前数据落成 markdown,供聊天会话直接读。

        这样简报不用再调一遍 canvas_upcoming(省 token),而且因为是每次加载
        仪表盘时重写的,读到的必定是新鲜的 —— 陈旧快照会让它说错话。
        """
        lines = [f'# Canvas 快照 · {d["fetched_at"]}', "", "## 在读课程", ""]
        for x in d["courses"]:
            lines.append(f'- **{x["code"]}** (id `{x["id"]}`) — {x["name"]}')
            lines.append(
                f'  - 当前总评: {x["current_score"]} / 等级 {x["current_grade"]}'
            )
        if d["overdue"]:
            lines += ["", "## 已过期且未提交", ""]
            for t in d["overdue"]:
                lines.append(
                    f'- **{t["title"].strip()}** — {t["course_short"]} · '
                    f'原截止 {t["due_local"]} · {t["points"]} 分'
                )
        lines += ["", "## 未来四周待办", ""]
        pending = [
            t
            for t in d["todo"]
            if t["days_left"] >= 0 and not t.get("dismissed")
        ]
        if not pending:
            lines.append("(无)")
        for t in pending:
            lines.append(f'- **{t["title"].strip()}** — {t["course_short"]} [{t["type"]}]')
            lines.append(
                f'  - 截止 {t["due_local"]} · 剩 {t["days_left"]} 天 · '
                f'{t["points"]} 分 · 已提交={t.get("submitted")}'
            )
            lines.append(f'  - {t.get("url")}')
        lines += ["", "## 最近公告(标题)", ""]
        for a in d["announcements"]:
            lines.append(f'- {a["course"]} · {a["title"]} ({a["days_ago"]} 天前)')
        lines += [
            "",
            "---",
            "",
            "这份快照由桌面应用在加载仪表盘时写入,时间戳见开头。",
            "要更细的信息(作业要求全文、rubric、公告正文)用 canvas_* MCP 工具现查。",
            "",
        ]
        try:
            out = HERE / "data" / "snapshot.md"
            out.parent.mkdir(exist_ok=True)
            out.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass   # 快照写不进去不该让仪表盘失败

    def _announcements(self, c: CanvasClient, courses: list, days: int) -> list:
        now = datetime.now(timezone.utc)
        raw = c.get_all(
            "/announcements",
            start_date=(now - timedelta(days=days)).date().isoformat(),
            end_date=(now + timedelta(days=1)).date().isoformat(),
            **{"context_codes[]": [f"course_{x['id']}" for x in courses]},
        )
        by_code = {f"course_{x['id']}": (x["code"].split(".")[0])[:10] for x in courses}
        return [
            {
                "title": a.get("title"),
                "course": by_code.get(a.get("context_code"), ""),
                "posted": to_local(a.get("posted_at")),
                "days_ago": (
                    abs(round(days_left(a.get("posted_at")) or 0, 1))
                    if a.get("posted_at")
                    else None
                ),
                "excerpt": html_to_text(a.get("message"), 240),
                "url": a.get("html_url"),
            }
            for a in raw[:12]
        ]

    def assignment(self, course_id: int, assignment_id: int) -> dict:
        try:
            a = self.client().get(
                f"/courses/{course_id}/assignments/{assignment_id}",
                **{"include[]": ["submission"]},
            )
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        sub = a.get("submission") or {}
        return {
            "error": None,
            "name": a.get("name"),
            "due": to_local(a.get("due_at")),
            "points": a.get("points_possible"),
            "state": sub.get("workflow_state", "unsubmitted"),
            "score": sub.get("score"),
            "description": html_to_text(a.get("description"), 6000),
            "url": a.get("html_url"),
        }

    # ------------------------------------------------------------ 邮件日程

    def mail_event_list(self) -> list[dict]:
        """所有还在的邮件日程(拍平、去重、套过删改),新的日期在前。

        **以 mail_ai.json 为准去遍历,不是以 mail.json。** 本地邮件索引是
        滚动的(每个账号留 300 封),而过目结果留 2000 条 —— 顺着邮件列表走
        的话,一封滚出去的信里那件十月的事就凭空消失了。主题和收信日能查到
        就查(给块上那行说明用),查不到就退回过目时间。
        """
        rows = []
        for mid, evs in self.mail_ai.events().items():
            m = self.mail.get(mid) or {}
            ts = str(m.get("ts") or "")
            rows.append({
                "mid": mid,
                "subject": m.get("subject") or "",
                "day": ts[:10] or (self.mail_ai.get(mid).get("at") or "")[:10],
                "ts": ts or (self.mail_ai.get(mid).get("at") or ""),
                "events": evs,
            })
        # 去重留的是先遇到的那条,所以必须**新的在前** —— 一件事被提醒三遍,
        # 最后那封信的时间才是改过之后的
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return self.mail_events.apply(mailevents.flatten(rows))

    def mail_event_ids(self) -> set:
        """哪些邮件带日程 —— 列表上那个小标识要用。

        只看**还没被删掉**的:用户把那条日程删了,卡片上就不该还挂着标。

        合并过的日程要把**每一封来源信**都算上 —— 三封信说同一件事,课表上
        是一条,但那三封信卡片上都该挂着标,不然点进去的人会以为漏抽了。
        """
        keep = set()
        for e in self.mail_event_list():
            for src in e.get("sources") or []:
                if src.get("mid"):
                    keep.add(src["mid"])
            if e.get("mid"):
                keep.add(e["mid"])
        return keep

    # ------------------------------------------------------------ 课表

    def schedule_view(self, week_start: str = "") -> dict:
        """界面要的那份课表:解析出来的条目 + 手改 + 手加 + 备忘录。

        week_start 是所显示那一周的周日(YYYY-MM-DD)。课表条目本身只认星期
        几,不需要日期;但备忘录里"某一天"那种得靠日期才知道落不落在这一周。
        """
        courses = (self._cache or {}).get("courses") or []
        out = self.schedule.view(courses)
        week = []
        try:
            d0 = datetime.strptime(week_start, "%Y-%m-%d")
            week = [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        except ValueError:
            pass
        try:
            out["items"] += tt.memo_items(self.memos.all(include_done=False), week)
        except Exception:
            pass
        # 邮件日程。有时刻的进格子,只知道哪天的走表头下面那条全天条 ——
        # 全天画成 00:00–23:59 会把纵轴撑成 0–24,两门真课挤成一条缝
        out["allday"] = []
        try:
            timed, allday = mailevents.week_items(self.mail_event_list(), week)
            out["items"] += timed
            out["allday"] = allday
        except Exception as exc:                   # noqa: BLE001
            print(f"[schedule] 邮件日程没排上:{type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
        out["items"].sort(key=lambda x: (x["weekday"], x["start"]))
        out["state"] = dict(self.sched_state)
        # 没有课程列表(仪表盘还没加载)时前端要能区分"还没抓"和"真的没有"
        out["courses"] = [{"id": c["id"], "short": c.get("short") or c.get("code")}
                          for c in courses]
        return out

    def parse_schedule(self, force: bool = False, manual: bool = False) -> bool:
        """后台把每门课的正文送去解析。已经在跑就不重复起。

        `manual` = 用户自己点的「重新解析」,和每小时那次自动保鲜区别对待:
        手动那次会把删掉的条目放回来(见 _parse_schedule)。
        """
        with self._sched_lock:
            if self.sched_state["running"]:
                return False
            self.sched_state.update({"running": True, "done": 0, "total": 0,
                                     "course": "", "errors": [],
                                     "restored": 0, "skipped": 0})
        self.push_schedule()
        threading.Thread(target=self._parse_schedule, args=(force, manual),
                         daemon=True).start()
        return True

    # 自动保鲜的间隔。和课件同步一样是一小时 —— 老师改课时间、补发 TA
    # office hour 都是"今天某个时候"的事,一小时的延迟够用了
    SCHED_EVERY = 3600

    def start_schedule_watch(self) -> None:
        """每小时把课表重抽一遍 —— 但**只在你已经手动解析过至少一门课之后**。

        两件事分开:第一次解析永远是你点出来的(它要花钱,不该在你不知道的
        时候发生);之后的保鲜是白捡的,因为源文没变就不会调模型,只多几个
        HTTP 请求。老师在公告里补了 TA office hour,下一个整点就进格子了。
        """

        def loop():
            time.sleep(90)          # 让首屏和课件同步先过去
            while True:
                try:
                    if self.schedule.view().get("parsed"):
                        self.parse_schedule(force=False)
                except Exception:
                    pass
                time.sleep(self.SCHED_EVERY)

        threading.Thread(target=loop, daemon=True, name="sched-watch").start()

    def _parse_schedule(self, force: bool, manual: bool = False) -> None:
        errors: list[str] = []
        restored = skipped = 0
        try:
            c = self.client()
            courses = self.sync_courses()
            # **一门课都没有就得说出来。** 原来这里直接往下走:循环零次、
            # errors 空、状态回到 idle —— 界面上什么都不显示,点「重新解析」
            # 像是没反应。而这恰恰是新装的人最容易碰到的状态(Canvas 还没连上
            # 或者 token 没写),沉默地什么都不做是最糟的回答
            if not courses:
                d = self.dashboard()
                if d.get("error") == "config":
                    raise RuntimeError(
                        f"还没配好 Canvas token,抽不了课表 —— 跑一次 "
                        f"{SETUP_SCRIPT} 写入 token")
                if d.get("error"):
                    raise RuntimeError(f"Canvas 连不上,抽不了课表:"
                                       f"{d.get('message') or d['error']}")
                raise RuntimeError("这学期一门在读的课都没有,没有课表可抽")
            try:
                mine = c.my_sections()
            except Exception:
                mine = {}
            # **手动点「重新解析」= 真的重建一次。** 删掉的条目在这儿放回来:
            # 按钮上写的是"重新解析",人点它就是想把课表重新拿回来,结果
            # 一条 hidden 在底下压着,抽出来的东西照样画不进格子 —— 从他那头
            # 看就是"点了没反应"。手改(改名、挪时间)不动,那些才是该留的。
            # 每小时那次自动保鲜**不**走这条路:背着人把删掉的请回来更糟。
            if manual:
                restored = self.schedule.unhide(c2["id"] for c2 in courses)
            model = read_prefs().get("schedModel") or "sonnet"
            self.sched_state["total"] = len(courses)
            self.push_schedule()
            for i, course in enumerate(courses):
                self.sched_state["course"] = course.get("short") or ""
                self.push_schedule()
                try:
                    try:
                        anns = self._announcements(c, [course], 45)
                    except Exception:
                        anns = []
                    src = tt.gather(c, course, mine.get(course["id"], []), anns)
                    # 正文短到这个份上的课(纯培训模块之类)根本没有课时表,
                    # 送进去只是白花钱
                    if len(src["text"]) < 300:
                        continue
                    if not force and src["fp"] == self.schedule.fingerprint(course["id"]):
                        skipped += 1
                        continue      # 源文没变,上次抽的还算数
                    got, cost = tt.run_one(HERE, course, src["text"], model)
                    self.schedule.put_course(course["id"], src["fp"], got, model, cost)
                except Exception as exc:
                    errors.append(f'{course.get("short")}:{exc}')
                finally:
                    self.sched_state["done"] = i + 1
                    self.push_schedule()
        except Exception as exc:
            errors.append(str(exc))
        finally:
            self.sched_state.update({
                "running": False, "course": "", "errors": errors[:5],
                "restored": restored, "skipped": skipped,
                "last": datetime.now().strftime("%H:%M:%S"),
            })
            self.push_schedule()


backend = Backend()
app = Flask(__name__, static_folder=None)


@app.before_request
def _check_token():
    """只守 /api/*。

    静态资源不能守:index.html 里的 <link href="app.css"> 是相对路径,
    解析出来不带 ?k=,一守就 403,整个前端会退化成无样式的裸 HTML。
    这些文件里也没有任何个人数据 —— 要保护的是接口。
    """
    if request.path.startswith("/api/") and request.args.get("k") != TOKEN:
        abort(403)
    # 排障开关:前端在空闲时到底有没有在反复打接口(闪屏/耗电的常见原因)。
    # 默认关,因为它每条请求写一行日志。
    if HTTP_LOG:
        print(f"[http] {time.strftime('%H:%M:%S')} {request.method} {request.full_path[:80]}",
              file=sys.stderr, flush=True)


@app.get("/")
def index():
    return send_from_directory(GUI, "index.html")


@app.get("/<path:name>")
def static_file(name: str):
    return send_from_directory(GUI, name)


@app.get("/api/dashboard")
def api_dashboard():
    force = request.args.get("force") == "1"
    d = backend.dashboard(force)
    # 悬浮球上的角标 = 逾期未提交的条数。收成球以后这是唯一还看得见的信息。
    if orb is not None and not d.get("error"):
        orb.set_badge(len(d.get("overdue") or []))
    return jsonify(d)


@app.get("/api/assignment/<int:course_id>/<int:assignment_id>")
def api_assignment(course_id: int, assignment_id: int):
    return jsonify(backend.assignment(course_id, assignment_id))


# ─────────────────────────── 课表 ───────────────────────────


@app.get("/api/schedule")
def api_schedule():
    return jsonify(backend.schedule_view(request.args.get("week", "")))


@app.post("/api/schedule/parse")
def api_schedule_parse():
    """重新从课程正文里抽一遍。force=1 连"源文没变"的课也重抽。"""
    body = request.get_json(silent=True) or {}
    started = backend.parse_schedule(bool(body.get("force")),
                                     manual=bool(body.get("manual")))
    return jsonify({"started": started, "state": backend.sched_state})


@app.post("/api/schedule/item")
def api_schedule_item():
    """加 / 改 / 删一条。

    自动抽出来的条目改不进存档本身(下次解析就冲掉了),改的是 edits 里的
    覆盖层;手加的条目则是真改真删。前端不用关心这个区别 —— 看 id 前缀就行,
    u 开头是手加的,`mail:` 开头是从邮件里抽的。

    邮件日程走的是另一份存档(mail_events.json)。**不能混进课表那份** ——
    「清空课表」会把它整个重置,那样被删掉的邮件日程会全回来。
    """
    d = request.get_json(silent=True) or {}
    act, iid = d.get("action"), (d.get("id") or "")
    mail = iid.startswith("mail:")
    try:
        if act == "add":
            return jsonify({"ok": True, "item": backend.schedule.add_manual(d)})
        if act == "delete":
            if mail:
                # 邮件日程「删」= 藏起来。那封信不会再过第二次模型,所以
                # 它不会被请回来;记下来是为了重装/换机之后也还是删掉的
                backend.mail_events.hide(iid)
                return jsonify({"ok": True})
            return jsonify({"ok": backend.schedule.delete(iid)})
        if act == "edit":
            patch = {k: v for k, v in d.items() if k not in ("action", "id")}
            if mail:
                backend.mail_events.edit(iid, patch)
                return jsonify({"ok": True})
            if iid.startswith("u"):
                return jsonify({"ok": backend.schedule.edit_manual(iid, patch)})
            backend.schedule.edit(iid, patch)
            return jsonify({"ok": True})
        if act == "revert":          # 撤销手改,回到解析出来的样子
            if mail:
                backend.mail_events.edit(iid, {})
                return jsonify({"ok": True})
            backend.schedule.edit(iid, {})
            return jsonify({"ok": True})
        if act == "reset":
            backend.schedule.reset()
            return jsonify({"ok": True})
        if act == "reset-mail":      # 只把邮件日程的删改清掉
            return jsonify({"ok": True, "n": backend.mail_events.reset()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": False, "error": f"未知操作 {act}"}), 400


def context_prefix(items: list) -> str:
    """把拖进对话的对象拼成一段前缀。

    **id 必须给**:只给标题的话模型还得先猜是哪门课的哪个作业,给了
    course_id / assignment_id 它可以直接调 canvas_assignment_detail 拉全文。
    """
    if not items:
        return ""
    lines = ["<关联对象>",
             "用户下面这句话问的就是这些东西,不用再猜是哪个。"]
    for i, it in enumerate(items[:6], 1):
        kind = {"assignment": "作业", "announcement": "公告",
                "file": "文件"}.get(it.get("kind"), it.get("kind") or "条目")
        head = f"{i}. {kind} · "
        if it.get("course"):
            head += f"{it['course']} · "
        head += str(it.get("title") or "(无标题)")
        lines.append(head)
        bits = []
        if it.get("due"):
            bits.append(f"截止 {it['due']}")
        if it.get("points") is not None:
            bits.append(f"{it['points']} 分")
        if it.get("submitted") is not None:
            bits.append("已提交" if it["submitted"] else "未提交")
        if it.get("dismissed"):
            bits.append("(已被我划掉,但这次特意拿出来问)")
        if bits:
            lines.append("   " + " · ".join(bits))
        ids = []
        if it.get("course_id"):
            ids.append(f"course_id={it['course_id']}")
        if it.get("assignment_id"):
            ids.append(f"assignment_id={it['assignment_id']}")
        if ids:
            lines.append("   " + " ".join(ids)
                         + " —— 要细节就用 canvas_assignment_detail")
        if it.get("path"):
            lines.append(f"   本地文件 {it['path']} —— 直接用 Read 工具读它")
        if it.get("url"):
            lines.append(f"   链接 {it['url']}")
        if it.get("excerpt"):
            lines.append(f"   摘要:{it['excerpt'][:200]}")
    lines.append("</关联对象>")
    lines.append("")
    return "\n".join(lines)


@app.post("/api/chat")
def api_chat():
    d = request.get_json(silent=True) or {}
    msg = d.get("message", "")
    ctx = d.get("context") or []
    if not msg.strip():
        return jsonify({"ok": False, "message": "空消息"})
    if backend.chat.is_busy():
        return jsonify({"ok": False, "message": "上一个问题还在回答"})
    cid = backend.chats.active_id()
    backend.chats.add(cid, "user", msg, ctx=ctx)
    # 上下文优先靠 --resume;续不上时用存档里的最近 10 轮重建(见 chat_bridge._run)
    backend.mark_hot()
    backend.chat.send_async(context_prefix(ctx) + msg + applang.reply_note(),
                            fallback_context=backend.chats.context_block(cid))
    return jsonify({"ok": True, "chat_id": cid})


def resend_edited(chat_session, store, d: dict) -> dict:
    """「改一句重发」:砍掉那条及之后的历史,重开会话,把前文补回去。

    CLI 的会话没法回退 —— 里头还留着被砍掉的那一问一答,再 `--resume` 上去
    模型看到的是原来那句。所以这里换一条时间线:丢掉 session_id,把截断之后
    的存档拼成一个 `<以往对话>` 块跟新消息一起发过去。

    因为前文已经在消息里了,`fallback_context` 就留空 —— 重试时原样再发一遍
    就是对的,拼两遍反而重复。
    """
    msg = str(d.get("message") or "")
    ctx = d.get("context") or []
    try:
        index = int(d.get("index", -1))
    except (TypeError, ValueError):
        index = -1
    if not msg.strip():
        return {"ok": False, "message": "空消息"}
    if chat_session.is_busy():
        return {"ok": False, "message": "上一个问题还在回答"}
    cid = store.active_id()
    if index >= 0:
        store.truncate(cid, index)
    store.clear_session(cid)
    chat_session.fork()
    head = store.context_block(cid)        # 砍完之后、加新消息之前的那段前文
    store.add(cid, "user", msg, ctx=ctx)
    prompt = context_prefix(ctx) + msg
    if head:
        prompt = head + chr(10) * 2 + prompt
    prompt += applang.reply_note()
    backend.mark_hot()
    chat_session.send_async(prompt)
    return {"ok": True, "chat_id": cid}


@app.post("/api/chat/edit")
def api_chat_edit():
    d = request.get_json(silent=True) or {}
    return jsonify(resend_edited(backend.chat, backend.chats, d))


@app.post("/api/chat/reset")
def api_chat_reset():
    """新对话:旧的那段留在存档里,不是清空。"""
    backend.chat.reset()
    cid = backend.chats.new()
    return jsonify({"ok": True, "chat_id": cid})


# ─────────────────────────── 对话存档 ───────────────────────────
#
# 存在 data/chats.json。上下文本身是 CLI 那边的 session,这里存消息原文是为了
# 两件事:能回看,以及 session 没了之后还能把最近 10 轮拼回去。

@app.get("/api/chats")
def api_chats():
    return jsonify({
        "active": backend.chats.active_id(),
        "context_turns": CONTEXT_TURNS,
        "index": backend.chats.index(),
    })


@app.get("/api/chats/<cid>")
def api_chat_one(cid: str):
    c = backend.chats.get(cid)
    if c is None:
        return jsonify({"error": "没有这段对话"}), 404
    return jsonify(c)


@app.post("/api/chats/open")
def api_chat_open():
    cid = str((request.get_json(silent=True) or {}).get("id", ""))
    if not backend.open_chat(cid):
        return jsonify({"ok": False, "error": "没有这段对话"}), 404
    return jsonify({"ok": True, "chat": backend.chats.get(cid)})


@app.post("/api/chats/delete")
def api_chat_delete():
    cid = str((request.get_json(silent=True) or {}).get("id", ""))
    ok = backend.chats.delete(cid)
    # 删掉的正好是当前这段:顺手开一段新的,免得前端拿着个空 id
    if ok:
        new_id = backend.chats.active_id()
        if new_id != cid:
            backend.open_chat(new_id)
    return jsonify({"ok": ok, "active": backend.chats.active_id()})


@app.post("/api/chats/clear")
def api_chats_clear():
    n = backend.chats.clear_all()
    backend.chat.reset()
    return jsonify({"ok": True, "deleted": n, "active": backend.chats.active_id()})


@app.post("/api/chat/cancel")
def api_chat_cancel():
    backend.chat.cancel()
    return jsonify({"ok": True})


@app.post("/api/dismiss")
def api_dismiss():
    # 划掉 / 撤回一条待办。key 来自 canvas_api.upcoming 的 key 字段。
    d = request.get_json(silent=True) or {}
    key = str(d.get("key", ""))
    on = bool(d.get("on", True))
    state = backend.dismissed.set(key, on, str(d.get("title", "")))
    # 仪表盘缓存里带着旧的 dismissed 标记,强制重算一次
    backend.dashboard(force=True)
    return jsonify({"ok": True, "key": key, "dismissed": state})


@app.get("/api/dismiss")
def api_dismiss_list():
    return jsonify(backend.dismissed.all())


@app.post("/api/dismiss/clear")
def api_dismiss_clear():
    n = backend.dismissed.clear()
    backend.dashboard(force=True)
    return jsonify({"ok": True, "restored": n})


@app.get("/api/briefings")
def api_briefings():
    """简报目录 + 今天的状态。"""
    today = today_str()
    return jsonify(
        {
            "today": today,
            "brief_hour": int(read_prefs().get("briefHour", BRIEF_HOUR)),
            "has_today": backend.briefing_store.has(today),
            "generating": backend.brief_session.is_busy(),
            "index": backend.briefing_store.index(),
        }
    )


@app.get("/api/briefings/<date>")
def api_briefing_one(date: str):
    entry = backend.briefing_store.get(date)
    if entry is None:
        return jsonify({"error": "没有这一天的简报"}), 404
    return jsonify(entry)


@app.post("/api/briefings/generate")
def api_briefing_generate():
    force = bool((request.get_json(silent=True) or {}).get("force"))
    backend.mark_hot()
    return jsonify(backend.briefings.trigger(force=force))


@app.get("/api/chat/stream")
def api_chat_stream():
    """一条长驻 SSE 连接,整个应用生命周期只开一次。

    两条通道复用这一条连接,事件自带 channel 字段,前端按它分流。
    """

    def gen():
        idle_since = time.time()
        last_ping = time.time()
        while True:
            events = (backend.chat.drain() + backend.brief_session.drain()
                      + backend.mail_chat.drain() + backend.mail_brief_session.drain()
                      + backend.drain_window())
            for ev in events:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            now = time.time()
            if events:
                idle_since = now
            # 自适应:有人在说话就 80ms(流式增量要跟得上),安静 2 秒以上退到
            # 400ms。固定 80ms 空转一天要醒一百多万次,纯白烧电。
            hot = backend.is_hot() or (now - idle_since) < 2.0
            if now - last_ping >= 15:
                last_ping = now
                yield ": ping\n\n"          # 心跳,别让中间层把连接掐了
            time.sleep(0.08 if hot else 0.40)

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────── 偏好设置 ───────────────────────────
#
# 存在后端而不是 localStorage:端口每次启动都是随机的,而 localStorage 按
# origin 隔离 —— 存在浏览器里的话,主题和窗口形态一重启就全丢了。

PREFS_PATH = HERE / "data" / "prefs.json"
_prefs_lock = threading.Lock()

# 设置面板里能调的每一项都在这儿有个默认值。前端拿到的永远是「默认值 + 存档」
# 合并后的完整对象,所以前端不用到处写 fallback。
DEFAULT_PREFS = {
    # 每 6 小时问一次 GitHub 有没有新版本。**默认开**,但能关 ——
    # 这个请求会把你的 IP 告诉 GitHub,有人不想要
    "updateCheck": True,
    # 有新版本时右下角报一句。**和 toastOn 分开** —— 不想被"新邮件"打扰
    # 不等于不想知道有更新,理由见 Backend._tell_update
    "updateToast": True,
    # 点过「跳过这个版本」的那个版本号 —— 它再也不会提醒
    "skipVersion": "",
    # 居中那个更新对话框**已经为哪个版本弹过**了。
    # 记进偏好而不是内存:模态比横幅打扰得多,一个版本只该拦你一次,
    # 重开应用也不再拦(横幅一直挂着,想更新随时点得到)
    "updSheetSeen": "",
    # auto | light | dark | neu | beach | pixel。
    # 后三个不只是换色:neu 是深色皮,beach/pixel 连圆角、投影、缓动、
    # 字体一起换(见 gui/app.css 里那两段)
    "theme": "auto",
    "lang": "zh",           # 界面语言 zh | en。也决定模型用哪门语言回答
    "mode": "full",         # orb | chat | full
    "blur": 26,             # 玻璃模糊半径(px),0 = 关掉 backdrop-filter
    "glass": 0.55,          # 玻璃表面不透明度 0.30~1.00(页面内的,CSS 管)
    "opacity": 1.0,         # 整窗不透明度 0.55~1.00(DWM 管,能透到桌面)
    "anim": True,           # 过渡动画
    "orbSize": 36,          # 悬浮球直径(逻辑像素)
    "orbX": None,           # 悬浮球位置(物理像素),None = 没拖过
    "orbY": None,
    "briefHour": BRIEF_HOUR,   # 每天几点播报
    "topmost": True,        # 对话框形态是否置顶
    "pinned": False,        # 大头针:钉住之后所有形态都置顶
    "restoreMode": "chat",  # 收成球之前是哪个形态 —— 点球就还原成它
    "restoreRect": [],      # 收成球之前那个矩形 [x, y, w, h],点球原样还原
    "restorePreExpand": [], # 再上一层:展开之前那个矩形(双击还原要回到它)
    "showDismissed": True,  # 划掉的条目还列不列出来
    "autoSync": True,       # 自动把课件同步到本地(启动 + 每小时)
    "mailOn": True,         # 后台拉邮件(3 分钟一轮)
    "mailHour": 9,          # 邮件简报每天几点播报
    # 结构化的个人信息 [{k, v}] —— AI 过目和邮件简报都拿它当判断依据。
    # 刻意是键值对而不是一大段自述:填一行就多一条依据,写起来没负担
    "mailFacts": [
        {"k": "我的专业", "v": ""},
        {"k": "我的住址", "v": ""},
        {"k": "我常用的软件", "v": ""},
        # 填了这一栏,运营商/宽带/保险的续费和账单提醒才会被当成正经事务 ——
        # 不填的话模型只能按通用标准判,催你交钱的信很容易被当成营销或者诈骗
        {"k": "我在用的服务", "v": ""},
        {"k": "我在找什么", "v": ""},
    ],
    "mailTags": list(mailai.DEFAULT_TAGS),   # 标签目录,可增可删
    # 「这些标签/栏位曾经出现过」。**只用来判断该不该自动补** ——
    # 你手动删掉的不会被下次启动硬塞回来,见 Backend._migrate_mail_prefs
    # 默认必须是**空的**:read_prefs 会用默认值补上缺失的键,要是这里写成
    # DEFAULT_TAGS,老存档一读出来就"见过"全部新标签,迁移就成了空操作
    "mailTagsSeen": [],
    "mailFactsSeen": [],
    # 坏标签:打了这些标签的信不进收件箱,只在垃圾箱里
    "mailTrashTags": [],
    "mailGroups": list(mailpeople.DEFAULT_GROUPS),   # 发件人分组的名字
    "mailAiOn": True,       # 新邮件自动送去 AI 过目
    "mailModel": "sonnet",  # 过目用哪个模型(haiku 省钱 / sonnet 均衡)
    "mailSort": "date_desc",  # 列表排序:date_desc | date_asc | level
    # 右下角的信息弹窗:Canvas 有新作业/新公告、或者来了新邮件时报一句
    "toastOn": True,
    "toastSecs": 9,         # 停留几秒
    "memoOpen": False,      # 第三列(备忘录)是展开还是收成一条竖边
    "mailFileMB": 20,       # 单个附件多大以上不自动下,只记名字
    # 在这儿点开读过之后,顺手把服务器上那封也标成已读。
    # **这是唯一一个会改动邮箱的开关**,所以给得出来、也关得掉
    "mailMarkRead": True,
    "page": "study",        # 上次在哪个子页面:study | mail
    # ── Canvas 那一栏
    "upcomingDays": 28,     # 待办往后看多少天
    "annDays": 10,          # 公告往前看多少天
    "soonDays": 3,          # 几天内算"紧急";"临近"是它的两倍
    "briefHistory": 3,      # 简报 prompt 里带几天的历史
    "focusCourses": "",     # 重点课程(课程简称,一行一个;空 = 全都一样看)
    "prefsTab": "general",  # 设置面板上次停在哪一栏
    "maxSyncMB": 80,        # 单个文件多大以上不自动下(课程录像动辄 200MB)
    # ── 课表
    "schedModel": "sonnet",  # 从课程正文里抽课时表用哪个模型
    "schedFull": False,      # 纵轴画满 0–24,还是只画有内容的时段
    "schedMemos": True,      # 备忘录里的每周/某天条目也画进格子
    "schedMail": True,       # 邮件里抽出来的日程也画进格子
}
PREF_KEYS = set(DEFAULT_PREFS)


def read_prefs() -> dict:
    """默认值打底,存档覆盖。"""
    d = dict(DEFAULT_PREFS)
    try:
        saved = json.loads(PREFS_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(saved, dict):
            d.update({k: v for k, v in saved.items() if k in PREF_KEYS})
    except Exception:
        pass
    return d


def write_prefs(patch: dict) -> dict:
    with _prefs_lock:
        d = read_prefs()
        d.update({k: v for k, v in patch.items() if k in PREF_KEYS})
        try:
            PREFS_PATH.parent.mkdir(exist_ok=True)
            tmp = PREFS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(PREFS_PATH)
        except Exception:
            pass          # 存不下去不该让界面上的操作失败
    return d


# 补新增的标签和「我的情况」栏位(见 Backend._migrate_mail_prefs)。
# **必须放在 read_prefs / write_prefs 定义之后** —— 模块是从上往下执行的,
# 写在 Backend.__init__ 里会在那两个函数还不存在时就跑,NameError。
try:
    backend._migrate_mail_prefs()
except Exception as _exc:                          # noqa: BLE001
    print(f"[mail] 偏好迁移跳过:{type(_exc).__name__}: {_exc}",
          file=sys.stderr, flush=True)


def dark_mode() -> bool:
    """当前该用深色吗 —— 给悬浮球和弹窗配色用(它们是自绘的,读不到 CSS)。

    偏好里明确选了就按那个走;`auto` 才去问系统。两个平台问法不同,
    那一段在 desktop.system_dark 里。
    """
    theme = read_prefs().get("theme")
    if theme in ("dark", "neu"):
        return True                     # neu 是深色的一张皮,不是第三种明暗
    if theme in ("light", "beach", "pixel"):
        return False                    # 这两档都是浅底的
    return desktop.system_dark()


# ─────────────────────────── 备忘录 ───────────────────────────

@app.post("/api/window/peek/pin")
def api_peek_pin():
    """钉住 / 放开悬浮球那条浮窗。

    页面在打开新增表单时钉住 —— 系统的日期选择器会把指针带到浮窗外面,
    不钉住的话选个时间窗口就关了。
    """
    on = bool((request.get_json(silent=True) or {}).get("on"))
    backend.peek_pin(on)
    return jsonify({"ok": True, "pinned": on})


@app.post("/api/window/peek/off")
def api_peek_off():
    """手动收掉那条浮窗。

    **必须有这一条。** 自动收起看的是"指针离开了没",而指针一旦在浮窗里
    点过一下,浮窗就成了前台窗口 —— 那时候 `_peek_watch` 认定你还在用它,
    永远不收。没有这个接口的话,点过一下的浮窗就再也关不掉了。
    """
    backend.peek_off(force=True)
    return jsonify({"ok": True})


@app.get("/api/setup")
def api_setup_state():
    """安装完整吗。只读、不改东西。

    存在的理由:装机脚本可能跑到一半失败(真发生过),那时候应用能跑但缺
    半套配置 —— 让用户"再装一遍"是个很差的答复。
    """
    st = setupfix.state(HERE)
    return jsonify({"state": st, "missing": setupfix.missing(st)})


@app.post("/api/setup/fix")
def api_setup_fix():
    """把缺的补上:MCP 注册、CLAUDE.md、权限、快捷方式。"""
    return jsonify(setupfix.fix(HERE))


@app.get("/api/update")
def api_update_get():
    """当前版本 + 上次检查的结果。**不联网** —— 界面刷新一次查一次
    GitHub 是没必要的,也容易撞限流。"""
    return jsonify({"version": appver.VERSION,
                    "kind": updater.install_kind(HERE),
                    # 界面上凡是要念脚本名的地方都得看它 —— 装机脚本
                    # Windows 是 install.ps1、macOS 是 install.sh
                    "os": platform_id.NAME,
                    "setup_script": SETUP_SCRIPT,
                    "where": str(HERE),
                    # 上次点的更新到底成没成。**只有这一处说得出真话** ——
                    # 换文件的是另一个进程,它干完就退了
                    "failed_update": backend.failed_update,
                    "info": backend.update_info,
                    "job": backend.updater.snapshot()})


@app.post("/api/update/check")
def api_update_check():
    """现在去问一次 GitHub。"""
    backend.check_update(force=True)
    return jsonify({"ok": True, "info": backend.update_info,
                    "version": appver.VERSION,
                    "kind": updater.install_kind(HERE)})


@app.post("/api/update/restart")
def api_update_restart():
    """重启应用。

    给"代码已经换新、但跑着的进程还是旧的"那种情况用(源码版特有,
    见 check_update 里的 stale_process)。那时候 git 快进无事可做,
    真正要做的就是重起一份。
    """
    if not updater.restart(HERE):
        return jsonify({"ok": False, "error": "找不到 app.py,重启不了"})
    threading.Timer(0.8, lambda: os._exit(0)).start()
    return jsonify({"ok": True})


@app.post("/api/update/apply")
def api_update_apply():
    """下载并装上。两种安装方式两条路,都是自动的。

    packaged  下载新 zip -> 换 exe -> 重启
    git       fetch + merge --ff-only -> 重启(不碰 data/ 和 CLAUDE.md,
              它们在 .gitignore 里)

    只有"源码但没有 .git"和 macOS 打包版还得手动 —— 前者无从更新起,
    后者的 .app 替换没在真机上验证过。
    """
    info = backend.update_info or {}
    kind = updater.install_kind(HERE)
    # 代码已经换新、只是进程旧了 —— 这时候"更新"该做的就是重启。
    # git 那条路自己会认出来(_run_git 里 behind==0 那一支),别的安装方式
    # 在这儿处理
    if kind != "git" and info.get("stale_process"):
        if updater.restart(HERE):
            threading.Timer(0.8, lambda: os._exit(0)).start()
            return jsonify({"ok": True, "kind": kind, "restarted": True})
    if kind == "git":
        started = backend.updater.start_git()
        return jsonify({"ok": started, "kind": kind,
                        "job": backend.updater.snapshot()})
    # 剩下只有"源码但没有 .git"还得手动 —— 那种情况无从更新起。
    # **macOS 的打包版不再走这条路**:换 .app 的流程写好了
    # (updater._apply_update_mac),两个平台现在都是自动的。
    if kind != "packaged":
        if info.get("page"):
            webbrowser.open(info["page"])
        return jsonify({"ok": False, "opened": True, "kind": kind,
                        "error": applang.tr(
                            "这种安装方式要手动更新,已经打开下载页",
                            "This install has to be updated by hand — "
                            "the download page is open")})
    started = backend.updater.start(info.get("asset") or "",
                                    info.get("latest") or "")
    return jsonify({"ok": started, "kind": kind,
                    "job": backend.updater.snapshot()})


@app.post("/api/toast/test")
def api_toast_test():
    """设置里那个「试一条」—— 不改任何数据,只是看看长什么样、位置对不对。"""
    ok = backend.notify("NEU Helper · 试一条",
                        "以后 Canvas 有新作业、或者来了新邮件,就是这个样子。")
    return jsonify({"ok": ok,
                    "error": "" if ok else "弹窗没就绪(或者设置里关掉了)"})


@app.get("/api/memos")
def api_memos():
    return jsonify({"items": backend.memos.all(),
                    "pending": backend.memos.pending_count()})


@app.post("/api/memos")
def api_memos_add():
    d = request.get_json(silent=True) or {}
    try:
        m = backend.memos.add(
            d.get("text", ""), d.get("kind", "plain"),
            at=d.get("at"), weekday=d.get("weekday"), day=d.get("day"),
            hour=d.get("hour"), minute=d.get("minute"), link=d.get("link"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    backend.push_memos()
    return jsonify({"ok": True, "memo": m,
                    "items": backend.memos.all(),
                    "pending": backend.memos.pending_count()})


@app.post("/api/memos/update")
def api_memos_update():
    d = request.get_json(silent=True) or {}
    m = backend.memos.update(str(d.get("id") or ""),
                             {k: v for k, v in d.items() if k != "id"})
    if m is None:
        return jsonify({"ok": False, "error": "没有这条备忘"}), 404
    backend.push_memos()
    return jsonify({"ok": True, "items": backend.memos.all(),
                    "pending": backend.memos.pending_count()})


@app.post("/api/memos/delete")
def api_memos_delete():
    ok = backend.memos.delete(str((request.get_json(silent=True) or {}).get("id", "")))
    backend.push_memos()
    return jsonify({"ok": ok, "items": backend.memos.all(),
                    "pending": backend.memos.pending_count()})


@app.post("/api/memos/from-item")
def api_memos_from_item():
    """把拖过来的东西(作业 / 公告 / 邮件 / 文件)变成一条备忘。

    作业带截止时间的话直接做成"单次"备忘 —— 那个时间点本来就是它的意义。
    其余的做成纯文字,时间让用户自己补。
    """
    it = (request.get_json(silent=True) or {}).get("item") or {}
    kind = it.get("kind") or ""
    title = (it.get("title") or "").strip()
    course = (it.get("course") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "这个东西没有标题"}), 400

    text, mkind, at = title, "plain", None
    if kind == "assignment":
        text = f"{course} · {title}" if course else title
        at = _iso_from_due(it.get("due") or "")
        mkind = "once" if at else "plain"
    elif kind == "announcement":
        text = f"{course} 公告:{title}" if course else f"公告:{title}"
    elif kind == "mail":
        who = (it.get("from") or "").split("<")[0].strip().strip('"')
        text = f"回 {who}:{title}" if who else f"邮件:{title}"
    elif kind == "file":
        text = f"看 {title}"

    m = backend.memos.add(text, mkind, at=at, link={
        k: it.get(k) for k in
        ("kind", "title", "course", "url", "course_id", "assignment_id",
         "path", "id", "date", "from") if it.get(k) is not None})
    backend.push_memos()
    return jsonify({"ok": True, "memo": m, "items": backend.memos.all(),
                    "pending": backend.memos.pending_count()})


def _iso_from_due(due: str) -> str | None:
    """从 `2026-09-23 23:55 太平洋夏令时` 这种显示串里取出时间。

    拖过来的是**给人看的**那一版(带时区名字),所以只认前面那段;
    `09-23 23:55` 这种没年份的补上今年。
    """
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", due or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}"
    m = re.match(r"(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", due or "")
    if m:
        y = datetime.now().year
        return f"{y}-{m.group(1)}-{m.group(2)}T{m.group(3)}:{m.group(4)}"
    return None


@app.get("/api/prefs")
def api_prefs_get():
    return jsonify(read_prefs())


@app.post("/api/prefs")
def api_prefs_set():
    d = write_prefs(request.get_json(silent=True) or {})
    # 悬浮球的外观/大小、整窗透明度都归设置管,改完立刻生效
    # 球和右下角那个弹窗都是自绘的,读不到 CSS —— 主题得单独告诉它们一声
    theme_changed = orb_render.set_theme(d.get("theme"))
    toast_render.set_theme(d.get("theme"))
    applang.set_lang(d.get("lang"))
    # 托盘那个菜单是原生的,切了语言得自己换一遍文案
    if backend.tray is not None:
        try:
            backend.tray.set_labels(tray_labels())
        except Exception:                      # noqa: BLE001
            pass                               # 菜单没换成不该挡住保存设置
    if backend.toast is not None:
        # dark 是个普通属性,下一条弹窗就跟着走(toast.py 的接口说明里写着)
        backend.toast.dark = dark_mode()
    if orb:
        # force=换过主题:那四个参数一个都不会变,不强制的话球不会重画
        orb.set_look(diameter=int(d["orbSize"]), dark=dark_mode(),
                     animate=bool(d.get("anim", True)), force=theme_changed)
    h = _hwnd()
    if h:
        native_window.set_window_alpha(h, float(d["opacity"]))
    backend.push_prefs(d)
    return jsonify({"ok": True, "prefs": d})


# ─────────────────────────── 窗口三态 ───────────────────────────
#
#   orb   悬浮球    —— **不是 WebView 窗口**,是 orb_window.py 自己画的分层窗口。
#                      收成球的时候整个 WebView 窗口直接隐藏,渲染进程跟着歇。
#   chat  400x620   只有对话框,置顶
#   full  1180x780  完整面板,不置顶
#
# 走 HTTP + 纯 Win32,不用 pywebview 的 js_api 桥 —— 原因见 native_window.py。

WINDOW_TITLE = "NEU Helper"
WINDOW_MODES = {"chat": (400, 620), "full": (1180, 780)}
MODES = ("orb", "chat", "full")

# 由 app.py 在窗口出来之后装上(球要知道 DPI,而 DPI 要从主窗口问)
orb = None

_hwnd_cache = 0


def tray_labels() -> dict:
    """托盘右键菜单的文案。

    **必须在 Python 这边分中英。** 那是个原生菜单,不是网页 ——
    gui/i18n.js 的 MutationObserver 够不着它。同 applang.tr 的另外两处
    (右下角弹窗、更新器里带路径的报错)。
    """
    return {
        "open": applang.tr("打开 NEU Helper", "Open NEU Helper"),
        "expand": applang.tr("展开完整面板", "Expand the full panel"),
        "orb": applang.tr("收成悬浮球", "Collapse to the orb"),
        "quit": applang.tr("退出 NEU Helper", "Quit NEU Helper"),
    }


def sync_art_theme() -> bool:
    """把**两个自绘件**(悬浮球、右下角弹窗)的配色拨到当前主题。
    返回球的配色有没有真的变。

    球和弹窗是自己画位图的,读不到 CSS —— 主题得单独告诉它们一声。

    **必须在建球之前调。** 球是在构造函数里就把所有帧画好的(弹出/收起的
    缩放帧、光晕的相位帧,默认尺寸下四十来张),而 `set_look` 只在
    直径/深浅/缩放/动画开关变了的时候才重画 —— 那四样一个都不会因为换主题
    而变。所以顺序错一步,结果就是:**开机永远是默认那颗蓝球,直到你去动一下
    直径**。这正是用户报的现象。

    弹窗没这个问题:它是出图那一刻才读模块级的 THEME,先后无所谓。
    """
    theme = read_prefs().get("theme")
    changed = orb_render.set_theme(theme)
    toast_render.set_theme(theme)
    return changed


def attach_orb(o) -> None:
    """app.py 在窗口出来之后把球装上来。"""
    global orb
    orb = o
    # 兜底:正常路径上 app.py 已经在建球之前调过 sync_art_theme(),
    # 这里的 set_theme 返回 False、不会白画一遍。漏调了的话这一下把它救回来
    # —— 代价只是多渲染一次(约 350ms,在球自己的线程上)
    if sync_art_theme():
        o.set_look(force=True)


def _hwnd() -> int:
    global _hwnd_cache
    if not native_window.is_window(_hwnd_cache):
        _hwnd_cache = native_window.find_own_window(WINDOW_TITLE)
    return _hwnd_cache


def want_topmost(mode: str, prefs: dict) -> bool:
    """这个形态该不该置顶。

    大头针钉住 = 所有形态都置顶;没钉的话只有对话框形态置顶(那一态本来就是
    "浮在别的窗口上问两句"),完整面板从不置顶 —— 它那么大,压着别人没法干活。
    """
    if prefs.get("pinned"):
        return True
    return mode == "chat" and bool(prefs.get("topmost", True))


def _cross_fade(hwnd: int, to_orb: bool, floor: float = 0.62,
                ms: int = 130, steps: int = 9) -> None:
    """主窗口和悬浮球之间交叉淡化。两者此刻必须同位置、同大小。

    to_orb=True:主窗口 floor -> 0,球 0 -> 255
    to_orb=False:反过来

    球那边的重贴走 PostMessage 交给球自己的线程做(CMD_FADE),不跨线程碰 GDI。
    """
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)                 # smoothstep,两头收一点
        if to_orb:
            native_window.set_window_alpha(hwnd, floor * (1 - t))
            if orb:
                orb.set_fade(int(255 * t))
        else:
            native_window.set_window_alpha(hwnd, floor * t)
            if orb:
                orb.set_fade(int(255 * (1 - t)))
        time.sleep(ms / 1000.0 / steps)


def restore_mode() -> str:
    """点悬浮球该还原成哪个形态。"""
    m = read_prefs().get("restoreMode")
    return m if m in WINDOW_MODES else "chat"


def apply_mode(mode: str) -> dict:
    """三态切换的唯一入口:HTTP 接口和悬浮球的回调都走这里。

    切到 orb:主窗口先收一下再隐藏,球在原来右下角那个位置弹出来 ——
    看起来是一个连续动作,而不是"窗口消失、球凭空出现"。
    """
    if mode not in MODES:
        return {"ok": False, "error": f"未知模式 {mode}"}
    h = _hwnd()
    if not h:
        return {"ok": False, "error": "找不到窗口句柄"}
    prefs = read_prefs()
    # 浮窗还开着的话先收掉 —— 接下来要真的换形态了,两者不能并存
    backend.peek_off(force=True)
    # 形态事件**先**推给页面:点原生悬浮球时页面还不知道要展开,得先让它把内容
    # 藏起来,窗口才好从球那儿长出来。按钮点的那条路页面已经自己改过了,
    # 这条事件会被同态判断吃掉,不会重复动画。
    backend.push_window(mode)
    # 最小化状态下 SetWindowPos 只会改"还原后的尺寸",窗口仍然缩在任务栏里 ——
    # 表现就是点了按钮什么也没发生。先恢复出来。
    if native_window.is_iconic(h) and mode != "orb":
        native_window.restore(h)
        time.sleep(0.08)
    # 处于系统最大化状态时 SetWindowPos 只改"还原后的尺寸",屏幕上纹丝不动。
    # unzoom 脱掉这个状态但矩形不动,接下来的动画才看得见。
    native_window.unzoom(h)

    if mode == "orb":
        # 球是窗口出来之后在后台线程里装的(要先问到 DPI),而前端 boot 可能
        # 比它快一步就来要 orb 形态 —— 等一下,别直接报错
        for _ in range(40):
            if orb is not None:
                break
            time.sleep(0.1)
        if orb is None:
            return {"ok": False, "error": "悬浮球还没就绪"}
        left, top, right, bottom = native_window.get_rect(h)
        # 记下收起来之前**长什么样**:形态、整个矩形、以及"展开之前"那个矩形。
        # 只记形态是不够的 —— 收之前铺满全屏的话,点球回来得还是铺满全屏,
        # 拖过的位置和改过的尺寸也一样要还原。
        if prefs.get("mode") in WINDOW_MODES:
            write_prefs({
                "restoreMode": prefs["mode"],
                "restoreRect": [left, top, right - left, bottom - top],
                "restorePreExpand": list(native_window.get_pre_expand(h) or []),
            })
        ox, oy = prefs.get("orbX"), prefs.get("orbY")
        if ox is None or oy is None:
            # 没拖过就落在窗口右下角那一块
            ox, oy = right - orb.size, bottom - orb.size
        tch = orb_render.set_theme(prefs.get("theme"))
        toast_render.set_theme(prefs.get("theme"))
        orb.set_look(force=tch, diameter=int(prefs["orbSize"]), dark=dark_mode(),
                     # render_scale 而不是 dpi_scale:球要的是"一个窗口单位
                     # 画几个位图像素"。Windows 上两者相等,macOS 上不等
                     scale=native_window.render_scale(h),
                     animate=bool(prefs.get("anim", True)))

        def collapse():
            # 1. 等页面把内容淡掉(CSS 45ms)。不等的话缩的过程里那串中间布局
            #    全看在眼里,像抽了一下而不是一个动作。
            #
            # **整套预算从 580ms 压到 280ms**(45 + 165 + 70)。原来那一版
            # 110/340/130 是照着"看清每一段"调的,可这个动作一天要做几十次 ——
            # 慢半秒就变成"点了没反应"。三段等比压缩,顺序和观感不变。
            time.sleep(0.045)
            # 2. 一路收到**球本体**那么大、那么位置。收到 170px 就停、然后让
            #    46px 的球凭空弹出来 —— 那个 6 倍的尺寸断层就是"不丝滑"的来源
            bx, by, bd = orb.ball_rect(int(ox), int(oy))
            # frames 跟着时长一起减 —— 165ms 里打 26 帧是 6ms 一帧,
            # SetWindowPos 根本跟不上,白发一半
            native_window.animate_rect(h, bx, by, bd, bd,
                                       duration=0.165, frames=14,
                                       alpha_from=float(prefs["opacity"]),
                                       alpha_to=0.50)
            # 3. 交接:两个窗口此刻完全重合,130ms 交叉淡化。圆角方和圆在这个
            #    尺寸上只差四个角几个像素,淡化里看不出来
            orb.show(int(ox), int(oy), alpha=0)
            _cross_fade(h, to_orb=True, floor=0.50, ms=70, steps=6)
            # 4. 收尾:藏掉主窗口、把整窗 alpha 还原成用户设的值
            native_window.hide(h)
            native_window.set_window_alpha(h, float(prefs["opacity"]))
            native_window.set_topmost(h, False)
            orb.settle()

        threading.Thread(target=collapse, daemon=True).start()
    else:
        w, ht = WINDOW_MODES[mode]
        coming_back = not native_window.is_visible(h)
        # 从球回来的那条路**不能**在这儿把球藏掉:交叉淡化正需要它还在,
        # 由 grow() 淡完再 hide_now()。其他情况(窗口本来就开着)才顺手收掉。
        if orb is not None and orb.visible() and not coming_back:
            orb.hide()
        if coming_back:
            # 从球回来:先把窗口摆成"球那么大、就在球那儿",显示出来(此刻页面
            # 内容还是淡掉的),再逐帧长到目标尺寸 —— 看起来是从球里长出来的
            pos = (orb.position() if orb else None) or (None, None)
            saved = prefs.get("restoreRect") or []
            if mode == prefs.get("restoreMode") and len(saved) == 4:
                # 收之前什么样就还原成什么样(全屏 / 拖过的位置 / 改过的尺寸)。
                # 挪一下:这中间可能换过分辨率、拔过副屏。只保证够得着,不改尺寸 ——
                # 窗口故意伸出屏幕底边是很常见的用法
                tx, ty, tw, th_ = native_window.nudge_onscreen(h, *saved)
                target = (tx, ty, tx + tw, ty + th_)
                native_window.set_pre_expand(h, prefs.get("restorePreExpand"))
            else:
                # 换了形态(比如收之前是完整面板,现在点的是"对话框"),
                # 那就用这个形态的标准尺寸,贴着球的右下角长出来
                anchor = ((pos[0] + orb.size, pos[1] + orb.size)
                          if pos[0] is not None else None)
                native_window.set_geometry(h, w, ht, anchor_br=anchor)
                target = native_window.get_rect(h)
                tw, th_ = target[2] - target[0], target[3] - target[1]
            if pos[0] is not None:
                seed = max(60, int(orb.size * 0.9))
                cx = pos[0] + orb.size // 2
                cy = pos[1] + orb.size // 2
                native_window.set_rect(h, cx - seed // 2, cy - seed // 2, seed, seed)

            def grow():
                # 1. 等页面收到上面那条事件、把内容藏好
                time.sleep(0.04)
                # 2. 主窗口先和球完全重合(球本体那么大、那么位置),全透明地显示
                bx, by, bd = orb.ball_rect()
                native_window.set_rect(h, bx, by, bd, bd)
                native_window.set_window_alpha(h, 0.0)
                native_window.show(h)
                # 3. 交叉淡化:球淡出、窗口淡入。两者同位置同大小,看起来是
                #    同一个东西在变
                _cross_fade(h, to_orb=False, floor=0.50, ms=70, steps=6)
                orb.hide_now()
                # 4. 从球那么大长到目标尺寸,同时把整窗 alpha 提到用户设的值
                native_window.animate_rect(h, target[0], target[1], tw, th_,
                                           duration=0.165, frames=14,
                                           alpha_from=0.50,
                                           alpha_to=float(prefs["opacity"]))
                native_window.set_window_alpha(h, float(prefs["opacity"]))
                # 置顶必须**在这里**再定一次:show() 为了保证窗口露头会蹭一次
                # HWND_TOPMOST,而 apply_mode 里那次 set_topmost 早在 90ms 前就
                # 跑完了 —— 不补这一下,完整面板会一直压在所有窗口上面。
                native_window.set_topmost(h, want_topmost(mode, prefs))

            threading.Thread(target=grow, daemon=True).start()
        else:
            def resize():
                # 和收球那条路一样先等页面把内容淡掉(CSS 45ms)。不等的话
                # 三栏重排和窗口形变同时发生,看着就是"整个屏幕先变一次
                # 再缩小",而不是一个连续的动作
                time.sleep(0.045)
                native_window.animate_to(h, w, ht, duration=0.165, frames=14)

            # 形变放后台线程,HTTP 立刻返回 —— 否则前端要等 260ms 才能换 CSS,
            # 原生窗口和页面内容就对不上了
            threading.Thread(target=resize, daemon=True).start()
        # 整窗透明度要重贴:从隐藏状态 show() 回来时,分层属性还在,
        # 但尺寸变过,重设一次最省心
        native_window.set_window_alpha(h, float(prefs["opacity"]))
        # 置顶必须放在 show() 之后:show() 为了保证窗口真的露出来会先蹭一下
        # 置顶(后台进程调 SetForegroundWindow 经常被系统拒掉),不在这之后
        # 明确改回来,完整面板就会一直压在所有窗口上面。
        native_window.set_topmost(h, want_topmost(mode, prefs))

    write_prefs({"mode": mode})
    return {"ok": True, "mode": mode, "topmost": native_window.is_topmost(h)}


@app.post("/api/window/mode")
def api_window_mode():
    mode = (request.get_json(silent=True) or {}).get("mode", "")
    r = apply_mode(mode)
    return jsonify(r), (200 if r.get("ok") else 400)


@app.get("/api/window/state")
def api_window_state():
    """页面重新可见时用来对账。

    形态变化平时靠 SSE 的 window 通道推过去,但那条队列是"取走即清"的:
    万一 EventSource 正好在重连,那条事件就丢了,页面的 CSS 会和原生窗口
    对不上(比如窗口已经是 400 宽的对话框,页面还画着完整面板)。
    所以页面一恢复可见就来问一次。
    """
    h = _hwnd()
    return jsonify({
        "mode": read_prefs().get("mode", "full"),
        # 展开状态看的是"矩形有没有铺满工作区",不是 IsZoomed ——
        # 我们刻意不用系统的最大化(那个没法做动画)
        "maximized": bool(h and native_window.is_expanded(h)),
        "visible": bool(h and native_window.is_visible(h)),
    })


@app.post("/api/window/pin")
def api_window_pin():
    """大头针:钉住之后所有形态都置顶。"""
    d = request.get_json(silent=True) or {}
    prefs = write_prefs({"pinned": bool(d.get("on"))})
    h = _hwnd()
    if h:
        native_window.set_topmost(h, want_topmost(prefs.get("mode", "full"), prefs))
    backend.push_prefs(prefs)
    return jsonify({"ok": True, "pinned": prefs["pinned"],
                    "topmost": bool(h and native_window.is_topmost(h))})


@app.post("/api/window/maximize")
def api_window_maximize():
    """双击标题栏:已经是完整面板就最大化 / 还原。"""
    h = _hwnd()
    if not h:
        return jsonify({"ok": False})
    return jsonify({"ok": True, "maximized": native_window.toggle_maximize(h)})


@app.post("/api/window/minimize")
def api_window_minimize():
    h = _hwnd()
    if h:
        native_window.minimize(h)
    return jsonify({"ok": bool(h)})


@app.post("/api/window/close")
def api_window_close():
    h = _hwnd()
    if h:
        native_window.close(h)
    return jsonify({"ok": bool(h)})


# 拖窗口:pointerdown 调 start,pointermove 每帧调 move,松手调 end。
# 位移全由后端自己 GetCursorPos 算,前端不传坐标(理由见 native_window.py)。

@app.post("/api/window/drag/start")
def api_drag_start():
    """开始拖窗。

    `follow` 告诉前端"后端自己会跟"——那种情况下前端**不要**每帧再发
    drag/move:那条路每帧要新建一条 loopback TCP 连接,而这台机器上新建连接
    的 p90 是 500ms(werkzeug 是 HTTP/1.0,每个响应都 Connection: close),
    窗口会一顿一顿地追手。理由和实测数据在 native_win32 的拖窗那一节。
    """
    h = _hwnd()
    if h:
        native_window.drag_start(h)
    return jsonify({"ok": bool(h),
                    "follow": bool(getattr(native_window, "DRAG_FOLLOWS", False))})


@app.post("/api/window/drag/move")
def api_drag_move():
    return jsonify({"ok": native_window.drag_move()})


@app.post("/api/window/drag/end")
def api_drag_end():
    native_window.drag_end()
    return jsonify({"ok": True})


@app.post("/api/clientlog")
def api_clientlog():
    """前端把异常送到这里,最终落进 data/app.log。

    开机是用 pythonw 启动的,没有控制台也没有 devtools —— 不把前端报错捞回来,
    界面出问题时会完全无迹可寻(比如某个 id 拼错导致 boot 中途抛出)。
    """
    d = request.get_json(silent=True) or {}
    print(
        f"[client] {d.get('kind', 'error')}: {d.get('message')} "
        f"@ {d.get('source')}:{d.get('line')}\n{d.get('stack', '')}",
        file=sys.stderr,
        flush=True,
    )
    return jsonify({"ok": True})


# ─────────────────────────── 课程文件 ───────────────────────────
#
# 下载到 data/downloads/<课程简称>/。打开接口只认这个目录下的文件 ——
# 那个接口只挡着一个本机 token,不该变成"任意文件打开器"。

# ─────────────────────────── 邮箱 ───────────────────────────
#
# 账号凭据在 ~/.canvas-helper/mail.json(和 Canvas token 同目录,
# 那个目录装机时就收紧过 ACL)。**接口一律不回传密码和 token。**

@app.get("/api/mail")
def api_mail():
    per = int(request.args.get("per") or PAGE_SIZE)
    page = int(request.args.get("page") or 1)
    msgs, total = backend.list_messages(
        per=per, page=page,
        box=request.args.get("box") or "in",
        person=request.args.get("person") or "",
        account=request.args.get("account") or None,
        unread_only=request.args.get("unread") == "1",
        day=request.args.get("day") or None,
        tag=request.args.get("tag") or "",
        min_level=int(request.args.get("level") or 0),
        sort=request.args.get("sort") or "date_desc",
        star_only=request.args.get("star") == "1",
    )
    pages = max(1, (total + per - 1) // per)
    page = min(max(1, page), pages)
    return jsonify({
        "accounts": mailmod.accounts_public(),
        "state": backend.mail_fetcher.snapshot(),
        "messages": msgs,
        "total": total,
        "page": page,
        "pages": pages,
        "per": per,
        "watching": len(backend.mail_flags.watching()),
        "ai": backend.analyzer.snapshot(),
        "idle": backend.mail_idle.snapshot(),
        "fill": backend.fill_snapshot(),
        "boxes": backend.box_counts(),
        "trash_tags": read_prefs().get("mailTrashTags") or [],
        "groups": read_prefs().get("mailGroups") or mailpeople.DEFAULT_GROUPS,
        # 标签这一栏只统计"当前筛选出来的这些",不是全库 —— 界面上那排
        # 标签按钮要跟看到的列表对得上
        "tag_counts": backend.mail_ai.tag_counts({m["id"] for m in msgs}),
        "tags": read_prefs().get("mailTags") or mailai.DEFAULT_TAGS,
        # 每个标签是什么意思。**这份定义就是进 prompt 的那一份** ——
        # 界面上悬停看到的和模型判断时读到的是同一句话,不会两头对不上
        "tag_defs": dict(mailai.TAG_DEFS),
        "days": backend.mail.days()[:60],
    })


@app.get("/api/mail/one")
def api_mail_one():
    """按 id 取一封信(带评级、标注、分组)。

    日程表上点「看这封邮件」用它 —— 那儿只有邮件 id,而列表是分页的,
    要找的那封可能根本不在当前这页里。
    """
    mid = request.args.get("id") or ""
    m = backend.mail.get(mid)
    if not m:
        return jsonify({"ok": False,
                        "error": "这封信已经滚出本地索引了(只留最近 300 封)"}), 404
    return jsonify({"ok": True, "message": backend.rate_messages([m])[0]})


@app.get("/api/mail/body")
def api_mail_body():
    """一封信的全文。

    存量邮件是老版本抓的(只有头 + 4KB 摘要),点开的时候才现去 IMAP 补一次,
    补完连链接和附件一起回写进索引 —— 下次点开就是本地的了。
    """
    mid = request.args.get("id") or ""
    m = backend.mail.get(mid)
    if not m:
        return jsonify({"ok": False, "error": "这封信不在本地索引里"}), 404
    text = mailparts.load_body(HERE / "data", mid)
    if text is None and "#" in mid:
        acc_id, uid = mid.rsplit("#", 1)
        acc = mailmod.get_account(acc_id)
        if not acc or not acc.get("password"):
            return jsonify({"ok": False, "error": "这个账号已经不在了"}), 404
        got, err = mailmod.fetch_one_body(
            acc, uid, HERE / "data", int(read_prefs().get("mailFileMB", 20)))
        if err:
            return jsonify({"ok": False, "error": err}), 502
        backend.mail.update(mid, {k: got[k] for k in
                                  ("links", "files", "has_body", "snippet")
                                  if k in got})
        m = backend.mail.get(mid) or m
        text = mailparts.load_body(HERE / "data", mid) or ""
    return jsonify({"ok": True, "id": mid, "text": text or "",
                    "links": m.get("links") or [],
                    "files": m.get("files") or []})


@app.post("/api/mail/file")
def api_mail_file():
    """打开一个已经下到本地的附件,或者打开它所在的文件夹。

    **只认 `data/mail_files` 底下的路径。** 这个路径是从邮件里来的
    (附件名由发信人决定),不锁死等于让别人指挥我们打开任意文件。
    """
    d = request.get_json(silent=True) or {}
    root = (HERE / "data" / "mail_files").resolve()
    try:
        p = Path(d.get("path") or "").resolve()
        p.relative_to(root)
    except Exception:
        return jsonify({"ok": False, "error": "路径不在附件目录里"}), 400
    if not p.exists():
        return jsonify({"ok": False, "error": "文件不在了"}), 404
    try:
        if d.get("reveal"):
            desktop.reveal_path(p)
        else:
            desktop.open_path(p)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})
    return jsonify({"ok": True})


@app.post("/api/mail/backfill")
def api_mail_backfill():
    """手动催一轮补抓(取存量邮件的正文、链接、附件)。"""
    started = backend.backfill_now()
    return jsonify({"ok": True, "started": started,
                    "fill": backend.fill_snapshot()})


@app.post("/api/mail/analyze")
def api_mail_analyze():
    """手动催一轮过目。

    redo=1    把已有结论全扔了重判(改完个人信息想让它重新看一遍的时候用)
    relink=1  只补**链接挑选** —— 带链接、但结论里还没有挑选结果的那些信
    redate=1  只补**日程** —— 结论里还没有 events 的那些信,而且只补最近
              REDATE_LIMIT 封

    三个都要花钱,所以都得显式点。relink / redate 存在的意义是省钱:这两个
    功能上线之前过目的信没有对应的字段,而为了补上它们去把全部存量重付一次
    不划算。redate 还额外限了封数 —— 半年前那封信里的截止日期早过去了,
    为它花钱没有意义。

    **补日程会把这几封的标签和摘要一起重判**(它们是同一次调用的产出),
    这不是浪费:标签体系也刚改过,顺带就新了。
    """
    d = request.get_json(silent=True) or {}
    redo = bool(d.get("redo"))
    dropped = 0
    if d.get("relink") and not redo:
        stale = [m["id"] for m in backend.mail.all(limit=10000)
                 if (m.get("links") or [])
                 and backend.mail_ai.has(m["id"])
                 and "links" not in backend.mail_ai.get(m["id"])]
        dropped = backend.mail_ai.drop(stale)
    if d.get("redate") and not redo:
        stale = [m["id"] for m in backend.mail.all(limit=REDATE_LIMIT)
                 if backend.mail_ai.has(m["id"])
                 and "events" not in backend.mail_ai.get(m["id"])]
        dropped = backend.mail_ai.drop(stale)
    started = backend.analyzer.analyze_now(force_all=redo)
    return jsonify({"ok": True, "started": started, "dropped": dropped,
                    "ai": backend.analyzer.snapshot()})


@app.post("/api/mail/tags")
def api_mail_tags():
    """标签目录的增删。AI 过目时会把这份目录放进 prompt,所以改完之后
    新邮件就会往新标签上靠。"""
    d = request.get_json(silent=True) or {}
    cur = list(read_prefs().get("mailTags") or mailai.DEFAULT_TAGS)
    add = str(d.get("add") or "").strip()
    drop = str(d.get("drop") or "").strip()
    if add and add not in cur:
        cur.append(add)
    if drop:
        cur = [t for t in cur if t != drop]
    if d.get("reset"):
        cur = list(mailai.DEFAULT_TAGS)
    p = write_prefs({"mailTags": cur[:60]})
    return jsonify({"ok": True, "tags": p.get("mailTags")})


@app.post("/api/mail/fetch")
def api_mail_fetch():
    return jsonify({"ok": True, "started": backend.mail_fetcher.fetch_now(),
                    "state": backend.mail_fetcher.snapshot()})


@app.post("/api/mail/accounts/imap")
def api_mail_add_imap():
    d = request.get_json(silent=True) or {}
    r = mailmod.add_imap_account(
        (d.get("email") or "").strip(),
        (d.get("password") or "").strip(),
        preset=(d.get("preset") or "qq"),
        host=(d.get("host") or "").strip(),
        port=int(d.get("port") or 993),
    )
    if r.get("ok"):
        backend.mail_fetcher.fetch_now()
    return jsonify(r)


@app.post("/api/mail/accounts/remove")
def api_mail_remove():
    aid = str((request.get_json(silent=True) or {}).get("id", ""))
    return jsonify({"ok": mailmod.remove_account(aid)})


@app.get("/api/mail/index")
def api_mail_index():
    """搜索用的索引:**一次拿走全部邮件的可搜字段**,之后搜索不再联网。

    为什么是这个形状,而不是把关键词发给后端筛:
    输入一个字就要有结果,那就意味着每敲一下键盘发一次请求。本机 loopback
    一来一回量下来 p50 1.7ms、p90 14ms —— 听着不慢,但那是**空载**;真按住
    一串字打下去,请求会互相排队,而且 werkzeug 是 HTTP/1.0(每个响应
    Connection: close),每次都得新建一条 TCP。把索引一次性拿到本地,
    搜索就变成纯内存过滤:零请求、零等待。

    3000 封的量级下这份 JSON 大约几百 KB,localhost 上一次传完的事。
    `ver` 变了才需要重新拿(界面自己判断)。

    字段名刻意压成一两个字母 —— 键名在 JSON 里会重复三千遍,
    `"unread"` 换成 `"u"` 能省掉百来 KB。
    """
    prefs = read_prefs()
    trash_tags = set(prefs.get("mailTrashTags") or [])
    msgs = backend.rate_messages(backend.mail.all(limit=4000))
    rows = []
    for m in msgs:
        r = m.get("rank") or {}
        rows.append({
            "i": m.get("id"),
            "u": 1 if m.get("unread") else 0,
            "l": int(r.get("level", 1) or 0),
            "lb": r.get("label") or "",
            "ic": r.get("icon") or "",
            "t": m.get("date_local") or "",
            "f": m.get("from") or "",
            "s": m.get("subject") or "",
            # 摘要没有(还没过目)就拿正文开头顶上 —— 搜"验证码"这种
            # 词的时候,没过目的那几封同样得搜得到
            # 截到 180:整份索引的体积主要就是这一项(3000 封的话
            # 240 和 180 差着两三百 KB)。摘要本来也就一两句话,
            # 截掉的是长摘要的尾巴,搜得到搜不到几乎不受影响
            "m": (r.get("summary") or m.get("snippet") or "")[:180],
            "g": " ".join(r.get("tags") or []),
            "b": 1 if backend.is_trash(m, trash_tags) else 0,
            # 盯着的要置顶,所以搜索结果也得知道 star / done 这一对
            # (盯着 = 标了重点、还没标完成)
            "st": 1 if m.get("star") else 0,
            "dn": 1 if m.get("done") else 0,
            "p": m.get("person") or "",
        })
    # 列表是 ts 倒序的(mail.all 保证),所以下标就是时间序 ——
    # 前端排序拿它当同级的 tie-break,不用再传一份 ts
    return jsonify({"ver": f"{backend.mail.updated()}|{len(rows)}",
                    "rows": rows})


@app.post("/api/mail/seen/all")
def api_mail_seen_all():
    """一键已读:把**现在这个箱子里**所有未读的信标成已读。

    两层都要动:服务器上的 `\Seen`,和本地索引。顺序是先服务器再本地 ——
    反过来的话服务器写失败就成了"界面说已读、邮箱里还是未读"。

    按账号分组批量发 STORE(见 mailbox.imap_set_seen_many):一次登录一条
    命令搞定一个账号,而不是一封一次。某个账号失败不影响别的账号 ——
    结果里把失败的原因一起带回去。
    """
    d = request.get_json(silent=True) or {}
    box = d.get("box") or "in"
    account = d.get("account") or None
    prefs = read_prefs()
    trash_tags = set(prefs.get("mailTrashTags") or [])
    want_trash = box == "trash"
    msgs = [m for m in backend.rate_messages(
                backend.mail.all(limit=4000, account=account))
            if m.get("unread")
            and backend.is_trash(m, trash_tags) == want_trash]
    if not msgs:
        return jsonify({"ok": True, "done": 0, "errors": []})

    by_acc: dict[str, list[str]] = {}
    for m in msgs:
        mid = str(m.get("id") or "")
        if "#" not in mid:
            continue
        acc_id, uid = mid.rsplit("#", 1)
        by_acc.setdefault(acc_id, []).append(uid)

    done_ids, errors = [], []
    for acc_id, uids in by_acc.items():
        acc = mailmod.get_account(acc_id)
        if not acc or not acc.get("password"):
            errors.append(f"{acc_id}:这个账号已经不在了")
            continue
        n, err = mailmod.imap_set_seen_many(acc, uids, True)
        if err:
            errors.append(f"{acc.get('label') or acc_id}:{err}")
            continue
        done_ids += [f"{acc_id}#{u}" for u in uids[:n]]
    changed = backend.mail.patch_many(done_ids, {"unread": False})
    st = backend.mail_fetcher.snapshot()
    backend.push_mail(st)
    return jsonify({"ok": not errors, "done": len(changed),
                    "errors": errors, "state": st})


@app.post("/api/mail/seen")
def api_mail_seen():
    """把一封信在**服务器上**标成已读/未读。

    本地索引同步改掉,不然要等下一轮轮询(3 分钟)界面才跟上。
    服务器写失败就原样返回错误 —— 不能本地标了已读、邮箱里还是未读。
    """
    d = request.get_json(silent=True) or {}
    mid = str(d.get("id") or "")
    seen = d.get("seen", True)
    m = backend.mail.get(mid)
    if not m or "#" not in mid:
        return jsonify({"ok": False, "error": "这封信不在本地索引里"}), 404
    acc_id, uid = mid.rsplit("#", 1)
    acc = mailmod.get_account(acc_id)
    if not acc or not acc.get("password"):
        return jsonify({"ok": False, "error": "这个账号已经不在了"}), 404
    ok, err = mailmod.imap_set_seen(acc, uid, bool(seen))
    if not ok:
        return jsonify({"ok": False, "error": err}), 502
    backend.mail.update(mid, {"unread": not seen})
    st = backend.mail_fetcher.snapshot()
    backend.push_mail(st)
    return jsonify({"ok": True, "unread": not seen, "state": st})


@app.post("/api/mail/flag")
def api_mail_flag():
    """重点标注 / 完成 / 备注。

    meta 一起存下来:缓存滚动之后这封信可能已经不在 mail.json 里了,
    但简报还要提醒"这封还没完成",那时候只能靠这份快照。
    """
    d = request.get_json(silent=True) or {}
    mid = str(d.get("id") or "")
    if not mid:
        return jsonify({"ok": False, "error": "要 id"}), 400
    meta = backend.mail.get(mid)
    f = backend.mail_flags.set(
        mid,
        star=d.get("star") if "star" in d else None,
        done=d.get("done") if "done" in d else None,
        note=d.get("note") if "note" in d else None,
        meta=meta or d.get("meta"),
    )
    return jsonify({"ok": True, "flags": f,
                    "watching": len(backend.mail_flags.watching())})


@app.post("/api/mail/trashtag")
def api_mail_trashtag():
    """把一个标签标成 / 取消"坏标签"。打了坏标签的信只在垃圾箱里出现。"""
    d = request.get_json(silent=True) or {}
    tag = str(d.get("tag") or "").strip()
    if not tag:
        return jsonify({"ok": False, "error": "要给 tag"}), 400
    cur = list(read_prefs().get("mailTrashTags") or [])
    if d.get("on", True):
        if tag not in cur:
            cur.append(tag)
    else:
        cur = [t for t in cur if t != tag]
    p = write_prefs({"mailTrashTags": cur[:40]})
    return jsonify({"ok": True, "trash_tags": p.get("mailTrashTags"),
                    "boxes": backend.box_counts()})


@app.post("/api/mail/person")
def api_mail_person():
    """把某封信的发件人归到一个分组(group 传空 = 取消分组)。

    也可以直接给 addr —— 设置里管理成员用得上。
    """
    d = request.get_json(silent=True) or {}
    group = str(d.get("group") or "").strip()
    who = str(d.get("addr") or "")
    name = ""
    if not who and d.get("id"):
        m = backend.mail.get(str(d["id"]))
        if not m:
            return jsonify({"ok": False, "error": "这封信不在本地索引里"}), 404
        who, name = m.get("from", ""), m.get("from", "")
    if not who:
        return jsonify({"ok": False, "error": "要给 id 或 addr"}), 400
    groups = read_prefs().get("mailGroups") or mailpeople.DEFAULT_GROUPS
    if group and group not in groups:
        return jsonify({"ok": False, "error": f"没有「{group}」这个分组"}), 400
    backend.people.set(who, group, name)
    return jsonify({"ok": True, "group": group,
                    "addr": mailpeople.addr_of(who),
                    "boxes": backend.box_counts()})


@app.get("/api/mail/people")
def api_mail_people():
    return jsonify({"groups": read_prefs().get("mailGroups")
                    or mailpeople.DEFAULT_GROUPS,
                    "members": backend.people.members(
                        request.args.get("group") or ""),
                    "counts": backend.people.counts()})


@app.post("/api/mail/groups")
def api_mail_groups():
    """分组名字的增删改。删组的时候把成员一起移出去,不留孤儿。"""
    d = request.get_json(silent=True) or {}
    cur = list(read_prefs().get("mailGroups") or mailpeople.DEFAULT_GROUPS)
    add = str(d.get("add") or "").strip()
    drop = str(d.get("drop") or "").strip()
    if add and add not in cur:
        cur.append(add)
    if drop:
        cur = [g for g in cur if g != drop]
        backend.people.rename_group(drop, "")
    p = write_prefs({"mailGroups": cur[:20]})
    return jsonify({"ok": True, "groups": p.get("mailGroups"),
                    "counts": backend.people.counts()})


@app.get("/api/mail/watching")
def api_mail_watching():
    """还在盯的那些(简报每天会点名,直到标完成)。"""
    out = []
    for w in backend.mail_flags.watching():
        out.append({**w, "days": backend.mail_flags.days_since(w)})
    return jsonify({"watching": out})


@app.get("/api/mail/briefings")
def api_mail_briefings():
    today = today_str()
    return jsonify({
        "today": today,
        "brief_hour": int(read_prefs().get("mailHour", 9)),
        "has_today": backend.mail_brief_store.has(today),
        "generating": backend.mail_brief_session.is_busy(),
        "index": backend.mail_brief_store.index(),
    })


@app.get("/api/mail/briefings/<date>")
def api_mail_briefing_one(date: str):
    entry = backend.mail_brief_store.get(date)
    if entry is None:
        return jsonify({"date": date, "text": None})
    return jsonify(entry)


@app.post("/api/mail/briefings/generate")
def api_mail_briefing_generate():
    force = bool((request.get_json(silent=True) or {}).get("force"))
    backend.mark_hot()
    return jsonify(backend.mail_briefings.trigger(force=force))


# ── 邮件对话(和课业对话完全分开的一套)

@app.post("/api/mailchat")
def api_mailchat():
    d = request.get_json(silent=True) or {}
    msg = d.get("message", "")
    ctx = d.get("context") or []
    if not msg.strip():
        return jsonify({"ok": False, "message": "空消息"})
    if backend.mail_chat.is_busy():
        return jsonify({"ok": False, "message": "上一个问题还在回答"})
    cid = backend.mail_chats.active_id()
    backend.mail_chats.add(cid, "user", msg, ctx=ctx)
    backend.mark_hot()
    backend.mail_chat.send_async(
        context_prefix(ctx) + msg + applang.reply_note(),
        fallback_context=backend.mail_chats.context_block(cid))
    return jsonify({"ok": True, "chat_id": cid})


@app.post("/api/mailchat/edit")
def api_mailchat_edit():
    d = request.get_json(silent=True) or {}
    return jsonify(resend_edited(backend.mail_chat, backend.mail_chats, d))


@app.post("/api/mailchat/reset")
def api_mailchat_reset():
    backend.mail_chat.reset()
    return jsonify({"ok": True, "chat_id": backend.mail_chats.new()})


@app.get("/api/mailchats")
def api_mailchats():
    return jsonify({
        "active": backend.mail_chats.active_id(),
        "context_turns": CONTEXT_TURNS,
        "index": backend.mail_chats.index(),
    })


@app.get("/api/mailchats/<cid>")
def api_mailchat_one(cid: str):
    c = backend.mail_chats.get(cid)
    if c is None:
        return jsonify({"error": "没有这段对话"}), 404
    return jsonify(c)


@app.post("/api/mailchats/open")
def api_mailchat_open():
    cid = str((request.get_json(silent=True) or {}).get("id", ""))
    if not backend.open_mail_chat(cid):
        return jsonify({"ok": False, "error": "没有这段对话"}), 404
    return jsonify({"ok": True, "chat": backend.mail_chats.get(cid)})


@app.post("/api/mailchats/delete")
def api_mailchat_delete():
    cid = str((request.get_json(silent=True) or {}).get("id", ""))
    ok = backend.mail_chats.delete(cid)
    if ok:
        new_id = backend.mail_chats.active_id()
        if new_id != cid:
            backend.open_mail_chat(new_id)
    return jsonify({"ok": ok, "active": backend.mail_chats.active_id()})


@app.get("/api/sync")
def api_sync_state():
    return jsonify(backend.sync.snapshot())


@app.post("/api/sync")
def api_sync_run():
    """手动同步。force=1 连课程数据一起重拉(默认用 5 分钟内的缓存)。"""
    force = bool((request.get_json(silent=True) or {}).get("force"))
    started = backend.sync.sync_async(backend.sync_courses(), force=force)
    return jsonify({"ok": True, "started": started,
                    "state": backend.sync.snapshot()})


@app.get("/api/course/<int:course_id>")
def api_course(course_id: int):
    """课程单页:作业 / 模块目录 / 文件 / 公告 一次给全。"""
    return jsonify(backend.course(course_id, force=request.args.get("force") == "1"))


@app.get("/api/files")
def api_files():
    try:
        cid = int(request.args.get("course_id", "0"))
    except ValueError:
        cid = 0
    if not cid:
        return jsonify({"error": "bad", "message": "要 course_id", "files": []}), 400
    return jsonify(backend.files(cid, force=request.args.get("force") == "1"))


@app.post("/api/files/download")
def api_file_download():
    d = request.get_json(silent=True) or {}
    try:
        cid, fid = int(d.get("course_id", 0)), int(d.get("file_id", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "course_id / file_id 不对"}), 400
    return jsonify(backend.download(cid, fid))


@app.post("/api/files/open")
def api_file_open():
    """打开一个已下载的文件。路径必须在 data/downloads 下面。"""
    raw = str((request.get_json(silent=True) or {}).get("path", ""))
    root = (HERE / "data" / "downloads").resolve()
    try:
        path = Path(raw).resolve()
        path.relative_to(root)           # 不在下载目录里就抛 ValueError
    except Exception:
        return jsonify({"ok": False, "error": "只能打开 data/downloads 下的文件"}), 400
    if not path.exists():
        return jsonify({"ok": False, "error": "文件不在了,重新下一次"}), 404
    try:
        desktop.open_path(path)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})
    return jsonify({"ok": True})


@app.post("/api/files/reveal")
def api_file_reveal():
    """在资源管理器里打开某个文件/目录所在的位置。

    和 /api/files/open 一样只认 data/downloads 下面的路径 —— 这个接口只挡着
    一个本机 token,不该变成"任意目录浏览器"。
    """
    raw = str((request.get_json(silent=True) or {}).get("path", ""))
    root = (HERE / "data" / "downloads").resolve()
    try:
        path = Path(raw).resolve()
        path.relative_to(root)
    except Exception:
        return jsonify({"ok": False, "error": "只能打开 data/downloads 下的位置"}), 400
    # 目录不存在就往上退到最近一个存在的父目录(还没同步的作业目录就是这种)
    target = path
    while not target.exists() and target != root:
        target = target.parent
    if not target.exists():
        return jsonify({"ok": False, "error": "这个位置还不存在,先同步一次"}), 404
    try:
        # 是文件就打开父目录并选中它;是目录就直接打开
        desktop.reveal_path(target)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})
    return jsonify({"ok": True, "opened": str(target)})


@app.get("/api/autostart")
def api_autostart_get():
    return jsonify({"on": desktop.autostart_on(),
                    "path": str(desktop.autostart_path())})


@app.post("/api/autostart")
def api_autostart_set():
    """开 / 关开机自启。

    **不进 prefs.json。** 这件事的真相在操作系统那边(启动文件夹里的快捷方式、
    LaunchAgent 的 plist)—— 用户可能直接去那儿删掉,也可能装机脚本刚建好。
    在偏好里再存一份布尔值只会和现实打架,所以每次都现问。
    """
    want = bool((request.get_json(silent=True) or {}).get("on"))
    got = desktop.set_autostart(HERE, want)
    return jsonify({"ok": got == want, "on": got,
                    "path": str(desktop.autostart_path())})


@app.post("/api/reveal")
def api_reveal():
    """在资源管理器里打开某个目录。**只认白名单**,不接受前端传路径 ——
    这个接口只挡着一个本机 token,不该变成"任意目录打开器"。"""
    what = (request.get_json(silent=True) or {}).get("what", "")
    targets = {
        "data": HERE / "data",
        "startup": desktop.autostart_dir(),
        "project": HERE,
        "downloads": HERE / "data" / "downloads",
        "mailfiles": HERE / "data" / "mail_files",
    }
    path = targets.get(what)
    if path is None:
        return jsonify({"ok": False, "error": "未知目标"}), 400
    try:
        path.mkdir(parents=True, exist_ok=True)
        desktop.open_path(path)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})
    return jsonify({"ok": True, "path": str(path)})


@app.post("/api/open")
def api_open():
    url = (request.get_json(silent=True) or {}).get("url", "")
    if not url.startswith(("http://", "https://")):
        return jsonify({"ok": False})
    webbrowser.open(url)
    return jsonify({"ok": True})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int) -> None:
    # 语言拨在这儿而不是 attach_orb 里:那个只在悬浮球起得来的时候才走,
    # 而"模型用哪门语言回答"和有没有球无关
    applang.set_lang(read_prefs().get("lang"))
    # 每条请求都打一行日志会把启动器捕获的 stderr 刷爆,只留错误
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    # threaded=True 是必须的:SSE 是长连接,单线程会把其他请求全堵死
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)
