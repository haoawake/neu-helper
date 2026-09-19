# -*- coding: utf-8 -*-
"""一封邮件里除了主题以外的东西:正文全文、内附链接、附件。

**为什么单独一个模块。** `mailbox.py` 管的是"拉下来、存索引",那份索引
(`data/mail.json`)是个滚动缓存,每次合并都整个重写 —— 正文和附件塞进去
会让每次轮询都在硬盘上搬几 MB。所以:

  data/mail.json              索引(头 + 摘要 + 链接 + 附件清单),会滚动
  data/mail_bodies/<hash>.txt 正文全文,一封一个文件
  data/mail_files/<日期>/     附件,按邮件日期分文件夹

**什么时候抓全文。** 只给**新到的**邮件抓 —— 轮询每 3 分钟一轮,要是每轮都
把最近 40 封的 RFC822 重下一遍,那是几十 MB 的白流量。已经见过的 UID 只取
FLAGS(读没读过会变),这一步几乎不花钱。旧邮件的全文是**点开那一下**
才按需去抓的。

**链接**为什么要单独抽出来:正文里一串 `https://…&utm_source=…` 是没法读的,
但那个链接本身往往就是这封信要你做的事(交表、看职位、确认预约)。
抽出来单独列一行,比让人在正文里找强。
"""
from __future__ import annotations

import email
import email.header
import email.utils
import hashlib
import html as _html
import re
from pathlib import Path

# 正文全文留多少 —— 再长的基本是引用历史和页脚,读也读不动
BODY_CHARS = 20000
MAX_LINKS = 20
# 单个附件超过这个就不自动下,只记名字
DEFAULT_MAX_FILE_MB = 20
# 整封信超过这个就不去下全文了(基本是巨型附件),退回只取头 + 摘要
DEFAULT_MAX_MSG_MB = 25

# 这些一看就是追踪像素/退订/分享按钮,列出来只会淹没真正有用的那几条
_JUNK_LINK = re.compile(
    r"(?i)(unsubscribe|/track|/open\.aspx|utm_medium=email_pixel|"
    r"list-manage\.com/track|/wf/open|\.gif(\?|$)|/pixel|beacon|"
    r"twitter\.com/intent|facebook\.com/sharer|linkedin\.com/sharing)")
_URL_RE = re.compile(r"""(?i)\bhttps?://[^\s<>"'）)\]】,,;;]+""")

# 纯追踪用的 query 参数。去掉之后链接短得多、也更容易看出是不是同一个;
# **只删已知的这些**,别的一律留着 —— 有的链接的 token 就在 query 里,
# 删错了那条链接就打不开了
_TRACK_PARAMS = {
    "lipi", "lici", "trk", "trkemail", "midtoken", "midsig", "eid", "ek",
    "otptoken", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "mkt_tok", "igshid",
    "fbclid", "gclid", "ncid", "spm", "sourceid", "recommendedflavor",
}
# 邮件顶部/底部那排站点导航。它们从来不是这封信要说的事
_CHROME_PATH = re.compile(
    r"(?i)^/?(comm/)?(feed|messaging|mynetwork|notifications|jobs|settings|"
    r"preferences|privacy|terms|help|support|home|login|signin)/?$")


def _clean_url(u: str) -> str:
    """砍掉追踪参数。砍不动就原样返回 —— 宁可长一点,也不能弄坏链接。"""
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        sp = urlsplit(u)
        if not sp.query:
            return u
        keep = [(k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True)
                if k.lower() not in _TRACK_PARAMS
                and not k.lower().startswith("utm_")]
        return urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(keep), ""))
    except Exception:
        return u


def safe_name(s: str, fallback: str = "file") -> str:
    """把附件名变成能落盘的样子。

    邮件里的文件名是攻击面:`../../autoexec` 这种必须挡掉,控制字符和
    Windows 保留字符也得清掉。
    """
    s = (s or "").strip().replace("\\", "/").split("/")[-1]
    s = re.sub(r"[\x00-\x1f<>:\"|?*]", "", s)
    s = s.strip(" .")
    if s in ("", ".", ".."):
        s = fallback
    return s[:120]


def _decode_filename(part) -> str:
    raw = part.get_filename()
    if not raw:
        return ""
    try:
        bits = email.header.decode_header(raw)
        out = ""
        for chunk, enc in bits:
            if isinstance(chunk, bytes):
                out += chunk.decode(enc or "utf-8", "replace")
            else:
                out += chunk
        return out
    except Exception:
        return str(raw)


def _payload_text(part) -> str:
    try:
        raw = part.get_payload(decode=True)
    except Exception:
        return ""
    if raw is None:
        return ""
    cs = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(cs, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def _links_from_html(s: str) -> list[dict]:
    out = []
    for m in re.finditer(
            r"""(?is)<a\b[^>]*href\s*=\s*(["'])(.*?)\1[^>]*>(.*?)</a>""", s):
        url = _html.unescape(m.group(2)).strip()
        # 锚文本里常夹着换行和缩进(整块 <td> 都是链接),压成一行再截
        text = re.sub(r"\s+", " ", _strip_html(m.group(3))).strip()[:80]
        out.append({"url": url, "text": text})
    return out


def extract(msg) -> dict:
    """从一封已解析的邮件里刨出正文、链接、附件描述(**不落盘**)。

    返回 {text, links, files}。files 里的 data 是字节,由调用方决定存不存 ——
    这个函数不碰硬盘,好测。
    """
    plain, htmls, files = [], [], []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        fname = _decode_filename(part)
        disp = (part.get("Content-Disposition") or "").lower()
        ctype = part.get_content_type()
        if fname or "attachment" in disp:
            try:
                data = part.get_payload(decode=True) or b""
            except Exception:
                data = b""
            # 签名里的小图标不算附件 —— 每封信都挂两三个,列出来全是噪音。
            # 明写了 attachment 的一律留下,不管多小。
            inline_junk = ("attachment" not in disp
                           and ctype.startswith("image/")
                           and len(data) < 100 * 1024)
            if not inline_junk:
                files.append({
                    "name": safe_name(fname, "attachment"),
                    "size": len(data),
                    "ctype": ctype,
                    "data": data,
                })
            continue
        if ctype == "text/plain":
            plain.append(_payload_text(part))
        elif ctype == "text/html":
            htmls.append(_payload_text(part))

    text = "\n".join(t for t in plain if t.strip()).strip()
    if not text:
        text = "\n\n".join(_strip_html(h) for h in htmls).strip()
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()[:BODY_CHARS]

    # 链接:先要 HTML 里的 <a>(有锚文本,读得懂),再补纯文本里裸露的
    links: list[dict] = []
    for h in htmls:
        links += _links_from_html(h)
    # 再扫一遍**最终正文**里裸露的 URL。不能只扫 plain 分段:纯 HTML 的信里
    # 经常有没做成 <a> 的裸链接(实测漏过一条),而 text 这时候已经是
    # 去过标签的正文,两种来源一次盖全。重复的下面会去掉。
    links += [{"url": u, "text": ""} for u in _URL_RE.findall(text)]

    seen, clean = set(), []
    for l in links:
        u = (l.get("url") or "").strip()
        if not u.lower().startswith(("http://", "https://")):
            continue
        if _JUNK_LINK.search(u):
            continue
        u = _clean_url(u)
        label = (l.get("text") or "").strip()
        # 没有说明文字、指向的又是站点导航 —— 那是邮件顶栏,不是这封信的内容
        try:
            from urllib.parse import urlsplit
            if not label and _CHROME_PATH.match(urlsplit(u).path or "/"):
                continue
        except Exception:
            pass
        key = u.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append({"url": u[:500], "text": label[:80]})
    # 有说明文字的排前面 —— 那些是正文里真的写了"点这里干什么"的。
    # sorted 是稳定的,同一档里保持原来的出现顺序
    clean.sort(key=lambda x: 0 if x["text"] else 1)
    return {"text": text, "links": clean[:MAX_LINKS], "files": files}


# ─────────────────────────── 落盘 ───────────────────────────

def body_path(root: Path, mid: str) -> Path:
    """正文放哪儿。邮件 id 里有 `@` `#` 这些字符,直接当文件名不合适,
    取哈希 —— 反正这个文件只给程序读。"""
    h = hashlib.sha1(mid.encode("utf-8")).hexdigest()[:16]
    return Path(root) / "mail_bodies" / f"{h}.txt"


def save_body(root: Path, mid: str, text: str) -> str:
    p = body_path(root, mid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text or "", encoding="utf-8")
    return str(p)


def load_body(root: Path, mid: str) -> str | None:
    p = body_path(root, mid)
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return None


def save_files(root: Path, day: str, files: list[dict],
               max_mb: int = DEFAULT_MAX_FILE_MB) -> list[dict]:
    """附件按邮件日期分文件夹存。返回给界面用的清单(不含字节)。

    重名不覆盖 —— 同一天两封信都有 `resume.pdf` 是很正常的事。
    """
    out = []
    folder = Path(root) / "mail_files" / (day or "未知日期")
    cap = max_mb * 1024 * 1024
    for f in files:
        rec = {"name": f["name"], "size": f["size"], "ctype": f.get("ctype", ""),
               "path": "", "too_big": False}
        if f["size"] > cap:
            rec["too_big"] = True
            out.append(rec)
            continue
        try:
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f["name"]
            stem, suf, n = dest.stem, dest.suffix, 1
            while dest.exists() and dest.stat().st_size != f["size"]:
                dest = folder / f"{stem} ({n}){suf}"
                n += 1
            if not dest.exists():
                dest.write_bytes(f["data"])
            rec["path"] = str(dest)
        except Exception:
            pass
        out.append(rec)
    return out
