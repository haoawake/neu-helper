# -*- coding: utf-8 -*-
"""界面语言 —— 只管**给模型的那句话**,不管界面文案。

界面文案在 `gui/i18n.js`(按中文原文索引的一张表);这里管的是另一半:
模型该用哪门语言回答。

**为什么单独一个模块。** 送去给模型的 prompt 散在四个互不相识的地方 ——
对话(server)、每日简报(briefings)、邮件过目(mailai)、课表抽取(timetable)。
它们都不该反过来 import server 去读偏好,那会绕成一个环。所以把"现在是哪门
语言"放在这儿,由 server 在启动时和改设置时拨一次,和 orb_render.set_theme /
toast_render.set_theme 同一个路子。

**为什么是"追加一句"而不是换一套 prompt。** 那四份 prompt 都是中文写的、
而且塞满了业务规则(标签定义、日程抽取的边界、简报的结构)。整份翻译一遍
等于同一套规则维护两份,迟早跑偏。语言只是**输出要求**,追加一句就够了 ——
模型读中文指令、用英文作答,这件事它做得很稳。
"""
from __future__ import annotations

LANG = "zh"


def set_lang(name: str | None) -> None:
    """拨到 zh 或 en。认不出的一律回到中文。"""
    global LANG
    LANG = "en" if name == "en" else "zh"


def is_en() -> bool:
    return LANG == "en"


def tr(zh: str, en: str) -> str:
    """两句里挑一句。

    界面上的中文原则上**全部**由 `gui/i18n.js` 在 DOM 边界翻,Python 这边
    一个字都不用管。只有两类够不着,才用这个函数:

    - **右下角那条原生弹窗。** 它不是网页,是自己画的一张位图
      (toast_render),文字在进位图之前就定死了,观察者够不着
    - **更新器里那几条带路径的报错。** 句子中间夹着一个绝对路径,
      i18n 那张按原文索引的表永远对不上

    别把它用到别处 —— 一旦开始在 Python 里成片地拼英文文案,同一套话就有了
    两个出处,i18n.js 那张表会慢慢变得不可信。
    """
    return en if LANG == "en" else zh


def reply_note() -> str:
    """追加在用户消息末尾的语言要求。

    **放在最后而不是最前。** CLAUDE.md 里那句「全程用中文回答」是写死在项目
    指令里的,位置比用户消息靠前;要压过它,这句得出现在更近的地方,而且得
    明说"覆盖前面的语言设定",不然模型会在两条互相矛盾的指令之间摇摆。

    中文这一档也照样明写。CLAUDE.md 里虽然有,但不是每个人的都有 ——
    这个文件是各人自己填的(仓库里放的是 CLAUDE.example.md)。
    """
    if LANG == "en":
        return ("\n\n---\n**Answer in English.** This overrides any language "
                "instruction given earlier (including CLAUDE.md) — the user has "
                "set the app language to English.")
    return "\n\n---\n**全程用中文回答。**"


def json_note() -> str:
    """给那些"只输出 JSON"的调用(邮件过目、课表抽取)用的那句。

    和 reply_note 分开是因为要求不一样:**键名不能翻**,翻了程序就读不到了;
    要翻的只是给人看的那几个字段。
    """
    if LANG == "en":
        return ("\n\nWrite every human-readable field (summary, title, why, "
                "notes, labels) in English. **Do not translate the JSON keys "
                "or any enumerated values** — those are read by code and must "
                "stay exactly as specified above.")
    return "\n\n给人看的字段(摘要、标题、why、说明)一律用中文写。"
