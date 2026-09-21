# -*- coding: utf-8 -*-
"""邮箱:只走 IMAP。

**QQ / 163 / Gmail** —— IMAP + 授权码。QQ 不让用登录密码走 IMAP,要在邮箱设置里
单独生成一个「授权码」。这一步只能用户自己点(设置 → 账号 → IMAP/SMTP 服务 →
开启 → 发短信 → 拿到 16 位授权码)。拿到之后填进设置面板就行。

**Outlook / 学校邮箱** —— 在 Outlook 里转发到 QQ。微软 2022 年起关掉了 IMAP 的
基本认证,只剩 OAuth,而 OAuth 要求先去 Azure 注册一个应用拿 client_id ——
为了收几封信让人跑一趟 Azure,不值得。设备码那条路做过,又删了。

**转发来的信按原始日期排。** 转发不改 `Date:` 头,所以一封旧信今天被转过来,
在按日期排的列表里仍然落在它原来的位置,不会冒到最上面。这不是漏收 ——
定级和"只看未读"是找到它的更好办法。

凭据存在 `~/.canvas-helper/mail.json`,和 Canvas token 同一个
目录(那个目录的 ACL 在装机时已经收紧到只有本人可读写)。**不放在项目里**,
项目目录是可能被整个打包发出去的。
"""
from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import select
import threading
import time

import mailparts
from datetime import datetime, timedelta, timezone
from pathlib import Path

CRED_DIR = Path(os.path.expanduser("~")) / ".canvas-helper"
CRED_PATH = CRED_DIR / "mail.json"

KEEP_MESSAGES = 300            # 每个账号在本地留多少封(只留头 + 摘要)
SNIPPET_CHARS = 700            # 正文摘要留多少字 —— 够简报判断,又不至于占爆上下文
FETCH_BATCH = 40               # 一次拉多少封
POLL_SECONDS = 120             # 后台轮询间隔(IDLE 之外的兜底)
WATCH_SECONDS = 10             # 长连接多久重 SELECT 一次(实测一次 0.22 秒)
IDLE_RENEW = 24 * 60           # IDLE 每隔多久重开一次(RFC 说 < 29 分钟)

IMAP_PRESETS = {
    "qq": {"host": "imap.qq.com", "port": 993, "label": "QQ 邮箱"},
    "163": {"host": "imap.163.com", "port": 993, "label": "网易 163"},
    "gmail": {"host": "imap.gmail.com", "port": 993, "label": "Gmail"},
    "other": {"host": "", "port": 993, "label": "其他 IMAP"},
}


# ───────────────────────────── 凭据 ─────────────────────────────

def _read_creds() -> dict:
    try:
        d = json.loads(CRED_PATH.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_creds(d: dict) -> None:
    CRED_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CRED_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CRED_PATH)


def accounts_public() -> list[dict]:
    """给界面看的账号列表 —— **不带密码和 token**。"""
    out = []
    for a in _read_creds().get("accounts", []):
        out.append({
            "id": a.get("id"),
            "kind": a.get("kind"),
            "label": a.get("label") or a.get("email") or a.get("id"),
            "email": a.get("email"),
            "host": a.get("host"),
            "ready": bool(a.get("password")),
            "last_error": a.get("last_error"),
            "last_sync": a.get("last_sync"),
        })
    return out


def _save_account(acc: dict) -> None:
    d = _read_creds()
    arr = d.setdefault("accounts", [])
    for i, a in enumerate(arr):
        if a.get("id") == acc.get("id"):
            arr[i] = {**a, **acc}
            break
    else:
        arr.append(acc)
    _write_creds(d)


def _find_account(aid: str) -> dict | None:
    for a in _read_creds().get("accounts", []):
        if a.get("id") == aid:
            return a
    return None


def add_imap_account(email_addr: str, password: str, preset: str = "qq",
                     host: str = "", port: int = 993, label: str = "") -> dict:
    """加一个 IMAP 账号。password 对 QQ 来说是**授权码**,不是登录密码。"""
    p = IMAP_PRESETS.get(preset) or IMAP_PRESETS["other"]
    acc = {
        "id": f"imap:{email_addr}",
        "kind": "imap",
        "email": email_addr,
        "password": password,
        "host": host or p["host"],
        "port": int(port or p["port"]),
        "label": label or p["label"],
    }
    if not acc["host"]:
        return {"ok": False, "error": "要填 IMAP 服务器地址"}
    ok, err = imap_probe(acc)
    acc["last_error"] = None if ok else err
    _save_account(acc)
    return {"ok": ok, "error": err, "id": acc["id"]}


def remove_account(aid: str) -> bool:
    d = _read_creds()
    arr = d.get("accounts", [])
    n = len(arr)
    d["accounts"] = [a for a in arr if a.get("id") != aid]
    _write_creds(d)
    return len(d["accounts"]) < n


# ───────────────────────────── IMAP ─────────────────────────────

def _decode_header(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
    except Exception:
        return str(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", "replace"))
            except LookupError:
                out.append(text.decode("utf-8", "replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def _body_snippet(msg: email.message.Message) -> str:
    """取一段纯文本摘要。HTML 的话粗暴去标签 —— 简报只要大意。"""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_type() == "text/plain":
                text = _payload_text(part)
                if text.strip():
                    break
        if not text.strip():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    text = _strip_html(_payload_text(part))
                    break
    else:
        text = _payload_text(msg)
        if msg.get_content_type() == "text/html":
            text = _strip_html(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(chr(10) + r"\s*" + chr(10) + "+", chr(10), text).strip()
    return text[:SNIPPET_CHARS]


def _payload_text(part: email.message.Message) -> str:
    try:
        raw = part.get_payload(decode=True)
    except Exception:
        return ""
    if raw is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _strip_html(html_text: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_text)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", chr(10), s)
    s = re.sub(r"<[^>]+>", " ", s)
    import html as _h
    return _h.unescape(s)


def imap_set_seen(acc: dict, uid: str, seen: bool = True
                  ) -> tuple[bool, str | None]:
    """把服务器上那封信标成已读 / 未读。

    **全项目唯一会写邮箱的地方。** 别处都是 `select("INBOX", readonly=True)`;
    这里必须可写,否则 STORE 会被服务器拒掉。范围也刻意收到最小:
    只动 `\\Seen` 这一个标记,不删信、不移动、不碰别的标记。
    """
    try:
        with imaplib.IMAP4_SSL(acc["host"], int(acc.get("port") or 993)) as M:
            M.login(acc["email"], acc["password"])
            typ, _ = M.select("INBOX")          # 注意:这里不能 readonly
            if typ != "OK":
                return False, "打不开收件箱"
            typ, _ = M.uid("store", str(uid),
                           "+FLAGS" if seen else "-FLAGS", "(\\Seen)")
            if typ != "OK":
                return False, "服务器不接受这次标记"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, None


def imap_probe(acc: dict) -> tuple[bool, str | None]:
    """能不能登上去。返回 (ok, 错误说明)。"""
    try:
        with imaplib.IMAP4_SSL(acc["host"], int(acc.get("port") or 993)) as M:
            M.login(acc["email"], acc["password"])
            M.select("INBOX", readonly=True)
        return True, None
    except imaplib.IMAP4.error as exc:
        msg = str(exc)
        if "LOGIN" in msg.upper() or "AUTH" in msg.upper():
            if acc.get("host", "").endswith("qq.com"):
                msg += "(QQ 要的是**授权码**,不是登录密码:邮箱设置 → 账号 → "
                msg += "IMAP/SMTP 服务 → 开启 → 发短信 → 拿 16 位授权码)"
        return False, msg
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def imap_fetch(acc: dict, limit: int = FETCH_BATCH,
               known: set | None = None, data_root: Path | None = None,
               max_file_mb: int = mailparts.DEFAULT_MAX_FILE_MB,
               only_uids: list | None = None) -> tuple[list[dict], str | None]:
    """拉最近 limit 封。

    **没见过的**才下整封(RFC822)—— 要正文、链接和附件就得整封下来。
    见过的只取 FLAGS,因为唯一会变的就是读没读过,那一步几乎不花钱。
    不这么分的话,每 3 分钟把最近 40 封重下一遍是几十 MB 的白流量。

    known      已经在本地的邮件 id,用来判断哪些是新的
    data_root  附件和正文往哪儿存;不给就只解析不落盘
    only_uids  只要这几个 UID(按需补抓旧邮件的全文时用)
    """
    known = known or set()
    out: list[dict] = []
    try:
        with imaplib.IMAP4_SSL(acc["host"], int(acc.get("port") or 993)) as M:
            M.login(acc["email"], acc["password"])
            M.select("INBOX", readonly=True)
            if only_uids:
                uids = [str(u).encode() for u in only_uids]
            else:
                typ, data = M.uid("search", None, "ALL")
                if typ != "OK":
                    return [], "IMAP search 失败"
                uids = (data[0] or b"").split()[-limit:]
            if not uids:
                return [], None

            # ── 第一趟:FLAGS + 整封多大。便宜,一次搞定所有 UID
            sizes, flags_by_uid = {}, {}
            typ, chunks = M.uid("fetch", b",".join(uids), "(FLAGS RFC822.SIZE)")
            if typ == "OK":
                for item in chunks:
                    line = (item[0] if isinstance(item, tuple) else item) or b""
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", "replace")
                    mu = re.search(r"UID (\d+)", line)
                    if not mu:
                        continue
                    u = mu.group(1)
                    mf = re.search(r"FLAGS \(([^)]*)\)", line)
                    flags_by_uid[u] = mf.group(1) if mf else ""
                    ms = re.search(r"RFC822.SIZE (\d+)", line)
                    sizes[u] = int(ms.group(1)) if ms else 0

            fresh = [u for u in uids
                     if f"{acc['id']}#{u.decode()}" not in known]
            # 巨型邮件(基本是超大附件)不下全文,退回只取头 + 摘要
            cap = mailparts.DEFAULT_MAX_MSG_MB * 1024 * 1024
            big = [u for u in fresh if sizes.get(u.decode(), 0) > cap]
            full = [u for u in fresh if u not in big]

            for u in full:
                uid = u.decode()
                typ, d = M.uid("fetch", u, "(RFC822)")
                if typ != "OK" or not d:
                    continue
                raw = next((x[1] for x in d if isinstance(x, tuple) and x[1]), None)
                if not raw:
                    continue
                out.append(_build_full(acc, uid, flags_by_uid.get(uid, ""), raw,
                                       data_root, max_file_mb))

            # 太大的那几封:老办法,头 + 4KB
            for u in big:
                uid = u.decode()
                typ, d = M.uid("fetch", u,
                               "(RFC822.HEADER BODY.PEEK[TEXT]<0.4000>)")
                if typ != "OK" or not d:
                    continue
                blobs = [x[1] for x in d if isinstance(x, tuple) and x[1]]
                if not blobs:
                    continue
                m = _build_msg(acc, uid, flags_by_uid.get(uid, ""), blobs[0],
                               blobs[1] if len(blobs) > 1 else b"")
                m["too_big"] = True
                # has_body 必须显式写上。补抓是按"有没有这个键"判断要不要取的,
                # 不写的话这封每一轮都会被重试,进度条永远差最后这一封
                m["has_body"] = False
                out.append(m)

            # 见过的:只回报读没读过,让 MailStore 原地更新
            for u in uids:
                uid = u.decode()
                mid = f"{acc['id']}#{uid}"
                if mid in known:
                    out.append({"id": mid, "account": acc["id"],
                                "unread": "\\Seen" not in flags_by_uid.get(uid, ""),
                                "_flags_only": True})
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    out.sort(key=lambda m: m.get("ts") or "", reverse=True)
    return out, None


def _build_full(acc: dict, uid: str, flags: str, raw: bytes,
                data_root, max_file_mb: int) -> dict:
    """整封信 -> 索引记录 + 正文落盘 + 附件落盘。"""
    msg = email.message_from_bytes(raw)
    rec = _build_msg(acc, uid, flags, raw, b"")
    try:
        got = mailparts.extract(msg)
    except Exception:
        return rec
    rec["links"] = got["links"]
    day = (rec.get("ts") or "")[:10]
    if data_root is not None:
        try:
            mailparts.save_body(data_root, rec["id"], got["text"])
            rec["has_body"] = bool(got["text"].strip())
        except Exception:
            rec["has_body"] = False
        rec["files"] = mailparts.save_files(data_root, day, got["files"],
                                            max_file_mb)
    else:
        rec["has_body"] = bool(got["text"].strip())
        rec["files"] = [{"name": f["name"], "size": f["size"],
                         "ctype": f.get("ctype", ""), "path": "",
                         "too_big": False} for f in got["files"]]
    # 摘要优先用解析出来的正文 —— 比 _build_msg 那条路准
    if got["text"].strip():
        rec["snippet"] = got["text"][:SNIPPET_CHARS]
    return rec


def _build_msg(acc: dict, uid: str, flags: str, header: bytes, body: bytes) -> dict:
    msg = email.message_from_bytes(header)
    ts = None
    dt = msg.get("Date")
    if dt:
        try:
            ts = email.utils.parsedate_to_datetime(dt)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            ts = None
    snippet = ""
    if body:
        try:
            # 头和正文之间**必须**空一行。IMAP 的 RFC822.HEADER 通常自带
            # 结尾空行,但不保证 —— 少了那一行,email 解析器会把整个正文
            # 当成头的续行,摘要就是空的(实测踩过)。所以先剥干净,
            # 再固定拼两个 CRLF。
            full = email.message_from_bytes(
                header.rstrip(b"\r\n") + b"\r\n\r\n" + body)
            snippet = _body_snippet(full)
        except Exception:
            snippet = body.decode("utf-8", "replace")[:SNIPPET_CHARS]
    if not snippet.strip():
        # 有的服务器把整封信塞在一个 item 里,body 就是空的 ——
        # 那时候正文其实在 header 这块里,再解一次
        try:
            snippet = _body_snippet(email.message_from_bytes(header))
        except Exception:
            pass
    return {
        "id": f"{acc['id']}#{uid}",
        "account": acc["id"],
        "account_label": acc.get("label") or acc.get("email"),
        "from": _decode_header(msg.get("From")),
        "to": _decode_header(msg.get("To")),
        "subject": _decode_header(msg.get("Subject")) or "(无主题)",
        "ts": ts.astimezone().isoformat(timespec="seconds") if ts else "",
        "date_local": ts.astimezone().strftime("%m-%d %H:%M") if ts else "",
        "unread": "\\Seen" not in flags,
        "snippet": snippet,
        "web_url": None,
    }


# ───────────────────────────── 存档 + 轮询 ─────────────────────────────

class MailStore:
    """本地邮件索引。只存头和摘要 —— 正文要全文就去网页版看。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = {"messages": [], "updated": None}
        try:
            d = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(d, dict) and isinstance(d.get("messages"), list):
                self._data = d
        except Exception:
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def ids(self) -> set:
        with self._lock:
            return {m["id"] for m in self._data["messages"]}

    def update(self, mid: str, patch: dict) -> bool:
        """原地改一封(补抓到正文之后回写 links/files/has_body)。"""
        with self._lock:
            for m in self._data["messages"]:
                if m["id"] == mid:
                    m.update(patch)
                    self._save()
                    return True
        return False

    def merge(self, msgs: list[dict]) -> int:
        """合并进来,返回新增了几封。

        进来的可能混着两种:完整记录(新邮件)和只带读没读过的瘦记录
        (见过的那些,IMAP 那边只取了 FLAGS)。两种都要处理 ——
        以前只在"一封新的都没有"的时候才刷标记,于是有新邮件的那一轮里,
        旧邮件的已读状态是不更新的。
        """
        with self._lock:
            have = {m["id"] for m in self._data["messages"]}
            # 先刷标记,新旧两种记录都算数
            by_id = {m["id"]: m for m in msgs}
            for m in self._data["messages"]:
                other = by_id.get(m["id"])
                if other is not None and "unread" in other:
                    m["unread"] = other["unread"]
            fresh = [m for m in msgs
                     if m["id"] not in have and not m.get("_flags_only")]
            if fresh:
                self._data["messages"] = (fresh + self._data["messages"])[:KEEP_MESSAGES]
            self._data["messages"].sort(key=lambda m: m.get("ts") or "", reverse=True)
            self._data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save()
            return len(fresh)

    def all(self, limit: int = 120, account: str | None = None,
            unread_only: bool = False, day: str | None = None) -> list[dict]:
        """按 ts 倒序(最新在前)。

        day 是 `YYYY-MM-DD`,筛某一天的。`ts` 存的已经是带偏移的本地时间
        (`2026-09-18T16:23:28-07:00`),所以切前 10 个字符就是本地日期 ——
        不用再换算时区。
        """
        with self._lock:
            out = list(self._data["messages"])
        if account:
            out = [m for m in out if m.get("account") == account]
        if unread_only:
            out = [m for m in out if m.get("unread")]
        if day:
            out = [m for m in out if (m.get("ts") or "")[:10] == day]
        return out[:limit]

    def days(self) -> list[str]:
        """本地有邮件的那些日期,新的在前 —— 界面上的日期筛选用它。"""
        with self._lock:
            seen = {(m.get("ts") or "")[:10] for m in self._data["messages"]}
        return sorted((d for d in seen if len(d) == 10), reverse=True)

    def get(self, mid: str) -> dict | None:
        with self._lock:
            return next((m for m in self._data["messages"] if m["id"] == mid), None)

    def since(self, iso: str) -> list[dict]:
        with self._lock:
            return [m for m in self._data["messages"] if (m.get("ts") or "") >= iso]

    def updated(self) -> str | None:
        return self._data.get("updated")

    def unread_count(self) -> int:
        with self._lock:
            return sum(1 for m in self._data["messages"] if m.get("unread"))


class MailFetcher:
    """后台把所有账号的收件箱拉下来。默认 3 分钟一轮 —— 够"实时"了,
    又不至于把 IMAP 连接打爆(QQ 对频繁登录会限流)。"""

    def __init__(self, store: MailStore, on_event=None, data_root=None,
                 max_file_mb_getter=None):
        self.store = store
        self.on_event = on_event
        # 正文和附件往哪儿落。不给就只解析不落盘(测试用)
        self.data_root = data_root
        self.max_file_mb_getter = max_file_mb_getter
        self._lock = threading.Lock()
        self.state = {"running": False, "last": None, "added": 0, "errors": []}

    def snapshot(self) -> dict:
        d = dict(self.state)
        d["errors"] = list(d["errors"])[-4:]
        d["unread"] = self.store.unread_count()
        d["updated"] = self.store.updated()
        return d

    def fetch_now(self) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        # 同上:调用方要能立刻从 snapshot() 里看到"在跑"
        self.state["running"] = True
        threading.Thread(target=self._run, daemon=True, name="mailfetch").start()
        return True

    def _emit(self, **kw) -> None:
        self.state.update(kw)
        if self.on_event:
            try:
                self.on_event(self.snapshot())
            except Exception:
                pass

    def _run(self) -> None:
        try:
            self._emit(running=True, added=0, errors=[])
            total = 0
            for acc in _read_creds().get("accounts", []):
                if not acc.get("password"):
                    continue
                msgs, err = imap_fetch(
                    acc, known=self.store.ids(), data_root=self.data_root,
                    max_file_mb=(self.max_file_mb_getter()
                                 if self.max_file_mb_getter else
                                 mailparts.DEFAULT_MAX_FILE_MB))
                if err:
                    self.state["errors"].append(f"{acc.get('label') or acc.get('id')}: {err}")
                    acc["last_error"] = err
                else:
                    acc["last_error"] = None
                    acc["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    total += self.store.merge(msgs)
                _save_account({k: acc[k] for k in
                               ("id", "last_error", "last_sync") if k in acc})
            self._emit(running=False, added=total,
                       last=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as exc:
            self.state["errors"].append(f"{type(exc).__name__}: {exc}")
            self._emit(running=False)
        finally:
            try:
                self._lock.release()
            except RuntimeError:
                pass

    def start_scheduler(self, enabled_getter=None) -> None:
        def loop():
            time.sleep(8)
            while True:
                try:
                    if enabled_getter is None or enabled_getter():
                        self.fetch_now()
                except Exception:
                    pass
                time.sleep(POLL_SECONDS)

        threading.Thread(target=loop, daemon=True, name="mailfetch-sched").start()


class IdleWatcher:
    """盯着收件箱有没有新信。一个账号一条长连接、一条线程。

    只负责"有变化"这个信号,**不自己取邮件** —— 取邮件那套逻辑(增量、
    只对新 UID 下全文)已经在 MailFetcher 里了,这边收到信号就喊它一声。

    **为什么是重 SELECT 而不是 IDLE。** 实测 QQ 的 IDLE 是个空头承诺:
    CAPABILITY 里有、命令也接受、"+ idling" 也回,但从不推送 —— 另一条连接
    APPEND 成功后等 40 秒一条 EXISTS 都没有,NOOP 同样什么都不说。
    而同一条连接上重新 SELECT 一次,EXISTS 立刻是新的,只花 0.22 秒。
    所以主循环靠重 SELECT;IDLE 只在支持的服务器上当"提前唤醒"用。
    """

    def __init__(self, on_change, enabled_getter=None):
        self.on_change = on_change
        self.enabled = enabled_getter or (lambda: True)
        self.state: dict[str, str] = {}      # 账号 -> 现在什么情况
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()

    def snapshot(self) -> dict:
        return dict(self.state)

    def stop(self) -> None:
        self._stop.set()

    def start(self) -> None:
        for acc in _read_creds().get("accounts", []):
            if not acc.get("password") or acc["id"] in self._threads:
                continue
            t = threading.Thread(target=self._loop, args=(acc["id"],),
                                 daemon=True, name="watch-" + acc["id"][:12])
            self._threads[acc["id"]] = t
            t.start()

    def _loop(self, acc_id: str) -> None:
        backoff = 5
        while not self._stop.is_set():
            if not self.enabled():
                self.state[acc_id] = "关着"
                time.sleep(10)
                continue
            acc = get_account(acc_id)
            if not acc or not acc.get("password"):
                self.state[acc_id] = "账号没了"
                return
            try:
                self._session(acc)
                backoff = 5                  # 正常收场:退避重置
            except Exception as exc:
                self.state[acc["id"] if acc else acc_id] = (
                    f"断了,{backoff} 秒后重连:{type(exc).__name__}")
                time.sleep(backoff)
                backoff = min(300, backoff * 2)

    def _session(self, acc: dict) -> None:
        with imaplib.IMAP4_SSL(acc["host"], int(acc.get("port") or 993)) as M:
            M.login(acc["email"], acc["password"])
            n = int(M.select("INBOX", readonly=True)[1][0])
            can_idle = "IDLE" in M.capabilities
            sock = M.socket()
            sock.settimeout(None)            # 等多久由 select 决定
            self.state[acc["id"]] = f"盯着({n} 封)"
            while not self._stop.is_set() and self.enabled():
                self._wait(M, sock, can_idle)
                now = int(M.select("INBOX", readonly=True)[1][0])
                if now > n:
                    self.state[acc["id"]] = (
                        f"{time.strftime('%H:%M:%S')} 发现 {now - n} 封新的")
                    n = now
                    try:
                        self.on_change()
                    except Exception:
                        pass
                else:
                    n = now                  # 删了信也要跟上,否则再也不会触发
                    self.state[acc["id"]] = f"盯着({n} 封)"

    def _wait(self, M, sock, can_idle: bool) -> None:
        """等 WATCH_SECONDS,或者被 IDLE 的推送提前叫醒。

        **不能在 socket 上设超时靠 readline 抛异常来计时** —— readline 读的是
        带缓冲的文件对象,超时一次之后状态就不对了,后面再也读不到东西
        (踩过:状态一直停在"在等推送")。用 select 等可读。
        """
        if not can_idle:
            self._stop.wait(WATCH_SECONDS)
            return
        tag = M._new_tag()
        try:
            M.send(tag + b" IDLE\r\n")
            if not M.readline().startswith(b"+"):
                self._stop.wait(WATCH_SECONDS)
                return
            t0 = time.time()
            while time.time() - t0 < WATCH_SECONDS:
                if getattr(sock, "pending", lambda: 0)() or select.select(
                        [sock], [], [], min(2.0, WATCH_SECONDS))[0]:
                    line = M.readline()
                    if not line:
                        raise RuntimeError("连接被对方关了")
                    if b"EXISTS" in line or b"RECENT" in line:
                        break                # 真有服务器推了,提前收工
                if self._stop.is_set():
                    break
        finally:
            try:
                M.send(b"DONE\r\n")
                for _ in range(8):
                    if not (getattr(sock, "pending", lambda: 0)()
                            or select.select([sock], [], [], 3)[0]):
                        break
                    if M.readline().startswith(tag):
                        break
            except Exception:
                pass


def get_account(acc_id: str) -> dict | None:
    """按 id 拿**带密码**的账号记录。补抓正文要重新登一次 IMAP。

    公开这个口子是为了让 server 不去碰  —— 凭据是从项目外面
    那个 ACL 收紧过的目录读的,入口越少越好管。"""
    for a in _read_creds().get("accounts", []):
        if a.get("id") == acc_id:
            return a
    return None


def fetch_one_body(acc: dict, uid: str, data_root, max_file_mb: int
                   = mailparts.DEFAULT_MAX_FILE_MB) -> tuple[dict | None, str | None]:
    """按需补抓一封的全文和附件。

    存量邮件是老版本抓的,只有头和 4KB 摘要 —— 点开的时候现去拿一次。
    拿完写进正文文件和附件目录,调用方再把 links/files 回写进索引。
    """
    msgs, err = imap_fetch(acc, known=set(), data_root=data_root,
                           max_file_mb=max_file_mb, only_uids=[uid])
    if err:
        return None, err
    real = [m for m in msgs if not m.get("_flags_only")]
    if not real:
        return None, "这封信在服务器上找不到了"
    return real[0], None


def build_mail_prompt(store: MailStore, history, today: str,
                      facts=None, watching: str = "",
                      rated: list[dict] | None = None,
                      day: str | None = None) -> str:
    """邮件简报的 prompt。

    **只播前一天的邮件。** 简报是"昨天发生了什么"的播报,不是收件箱的复述;
    把 60 封历史邮件每天重念一遍,既占上下文又说不出新东西。

    **而且只放已经分析好的那几行**(标签 / 级别 / 一句话摘要),不放正文。
    正文在 AI 过目那一步已经读过一次了(见 mailai.py),结果存在
    `data/mail_ai.json` 里 —— 同一封信的正文不该进第二次 prompt。
    还没过目的那些才带上摘要,不然简报会对它们两眼一黑。

    facts    结构化的个人信息 [{k, v}] —— 判断"哪封对我重要"的依据
    watching 还在盯的重点邮件那一段(见 mailflags.briefing_block)
    rated    带级别/标签/标注的邮件;没给就退回原始列表
    day      要播报哪一天;默认是 today 的前一天
    """
    import mailai

    target = day or _prev_day(today)
    pool = rated if rated is not None else store.all(limit=300)
    msgs = [m for m in pool if (m.get("ts") or "")[:10] == target]

    lines = ["mail-briefing", ""]
    rows = mailai._facts_block(facts)
    if rows:
        lines += ["<我的情况>",
                  "(判断哪封邮件对我重要时以这个为准,不要拿通用标准套)"]
        lines += rows
        lines += ["</我的情况>", ""]
    if watching:
        lines.append(watching)
    lines += [
        f"今天是 {today}。下面是 **{target}(前一天)** 收到的邮件,"
        "请按 CLAUDE.md 里「邮件简报」那一节的规矩播报。",
        "每封前面的标签和级别是已经过目过的结论,正文不再附上 —— "
        "要点都在「摘要」那一行里。觉得级别判错了就按摘要的内容来。",
        "",
    ]
    if not msgs:
        lines.append(f"({target} 一封邮件都没有。直接说这一天没有新邮件,"
                     "然后如果有还在盯的就只报那些。)")
    for m in msgs:
        flag = "未读" if m.get("unread") else "已读"
        rank = m.get("rank") or {}
        tags = "/".join(rank.get("tags") or []) or "未分类"
        mark = " ★重点" if m.get("star") and not m.get("done") else ""
        who = m.get("from")
        # 分了组的发件人要标出来 —— 简报判"要不要说这封"时,"这是我导师"
        # 比任何标签都管用
        if m.get("person"):
            who = f"{who}(我的{m['person']})"
        lines.append(f"- [{rank.get('label') or '普通'}][{tags}][{flag}]{mark} "
                     f"{m.get('date_local')} · 来自 {who}")
        lines.append(f"  主题:{m.get('subject')}")
        if rank.get("summary"):
            lines.append(f"  摘要:{rank['summary']}")
        elif rank.get("pending"):
            # 还没过目 —— 这一封只能带原文摘要,否则简报会漏掉它
            snip = (m.get("snippet") or "").strip().replace(chr(10), " ")
            if snip:
                lines.append(f"  (还没过目)正文开头:{snip[:200]}")
        if m.get("note"):
            lines.append(f"  我的备注:{m['note']}")
        # 已经进日程表的,标一句。**简报和日程表别两头各说一遍** ——
        # 这一句是给模型的提示:那件事有着落了,不用再当成"要你安排"来播
        if m.get("dated"):
            lines.append("  (这封里的日期已经进日程表了)")
    if history:
        lines += ["", "<以往邮件简报>",
                  "(按时间从早到晚。先跟这些对比,说变化,别每天重念一样的清单)", ""]
        # BriefingStore.recent_texts 给的是 [(日期, 正文)] 元组,而且是倒序的
        for date_str, text in reversed(list(history)):
            lines.append(f"── {date_str} ──")
            lines.append(text)
            lines.append("")
        lines.append("</以往邮件简报>")
    return chr(10).join(lines)


def _prev_day(today: str) -> str:
    try:
        d = datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return today
