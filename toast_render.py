# -*- coding: utf-8 -*-
"""右下角信息弹窗长什么样 —— **纯 Python 逐像素算,两个平台共用这一份。**

和 orb_render.py 同一个思路:输出预乘 alpha 的 BGRA,Windows 的
`UpdateLayeredWindow` 和 macOS 的 CGImage 都直接吃这个格式。

**这里不画文字。** 文字要量宽高、要断行、要省略号 —— 那是各自系统的排版引擎
(GDI 的 DrawTextW / Core Text)。所以分工是:

  宿主   量出正文要几行高 -> 算出卡片总高 -> 开画布
  这里   把卡片底和外阴影画进去,并**交回一份 alpha 遮罩**
  宿主   在上面写字,然后照着遮罩把 alpha 补回来

那份遮罩不是可选的:两个平台的文字引擎都会把字形像素的 alpha 弄坏
(GDI 直接清零),不补回去字就是一片透明的洞 —— 这个坑踩过一次。
"""
from __future__ import annotations

WIDTH = 340                # 逻辑像素。横条:宽而不高
PAD = 14
SHADOW_MARGIN = 20         # 外阴影占的外圈(逻辑像素)。要够宽,阴影才有地方
#                            衰减干净 —— 位图边界上还剩不透明度的话,那一刀
#                            切出来就是个方角
SHOW_MS = 9000             # 停留多久
FADE_MS = 180

BODY_MAX = 72              # 正文最多占多高(逻辑像素),超了省略号
TITLE_H = 20
GAP = 4


def _rgba(r, g, b, a):
    """预乘 alpha —— 两个平台的合成器要的都是预乘过的 BGRA。"""
    f = a / 255.0
    return bytes((int(b * f), int(g * f), int(r * f), a))


# 当前配色名。空 = 按深浅在明暗两套里选;"neu" = 校徽的黑白红。
# 和 orb_render 一个路子:宿主只知道"现在是不是深色",不知道主题叫什么,
# 而弹窗是自绘的读不到 CSS —— 所以用一个模块级开关,由 server 在偏好
# 变化时拨过来。
THEME = ""


def set_theme(name: str | None) -> None:
    """拨到某一档配色。认不出的名字一律回到"按深浅选"。"""
    global THEME
    THEME = name if name == "neu" else ""


def colors(dark: bool):
    """(卡片底色, 标题色, 正文色, 阴影不透明度)。"""
    if THEME == "neu":
        # 中性灰而不是深色主题那个带蓝的 (32,39,51) —— 黑白红里没有蓝的位置
        return (28, 28, 32), (0xF6, 0xF6, 0xF7), (0xBD, 0xBD, 0xC2), 0.58
    if dark:
        return (32, 39, 51), (0xF2, 0xF5, 0xFA), (0xC6, 0xCE, 0xDC), 0.55
    return (252, 253, 255), (0x10, 0x14, 0x1C), (0x4A, 0x50, 0x60), 0.30


def accent(dark: bool):
    """左边那道竖条的颜色。

    **原来这里是写死的 `0x2A78D6`**,所以弹窗在深色下也用浅色主题那支蓝,
    换成 NEU 主题更是纹丝不动 —— 界面全红了,右下角还弹出一道蓝杠。
    现在跟着界面的 --accent 走,三档各自对齐。
    """
    if THEME == "neu":
        return (0xEF, 0x2D, 0x47)
    return (0x4F, 0x97, 0xEE) if dark else (0x2A, 0x78, 0xD6)


def metrics(scale: float) -> dict:
    """一处算全部尺寸,宿主别自己再乘一遍 scale。"""
    return {
        "pad": int(PAD * scale),
        "mar": int(SHADOW_MARGIN * scale),
        "w": int(WIDTH * scale) + int(SHADOW_MARGIN * scale) * 2,
        "r": int(12 * scale),            # 圆角
        "bar": int(4 * scale),           # 左边那道强调色竖条
        "title_h": int(TITLE_H * scale),
        "body_max": int(BODY_MAX * scale),
        "gap": int(GAP * scale),
        "inset": int(16 * scale),        # 离屏幕边缘的距离
        "text_x": int(6 * scale),        # 文字再往右让一点,别贴着竖条
    }


def card_height(body_h: int, scale: float) -> int:
    """卡片本体的高度 —— 跟着正文行数走,所以要宿主先量。"""
    m = metrics(scale)
    return m["pad"] * 2 + m["title_h"] + body_h + m["gap"]


def paint_card(buf, w: int, h: int, scale: float, dark: bool) -> bytearray:
    """把圆角卡片和外阴影画进 buf(每像素 4 字节,BGRA 预乘),返回 alpha 遮罩。

    buf 只需要支持 `buf[o:o+4] = b"...."` —— Windows 上是 DIB 的内存视图
    (零拷贝),macOS 上是 bytearray。
    """
    s = scale
    pad = int(PAD * s)
    mar = int(SHADOW_MARGIN * s)
    base, _tcol, _bcol, sh_a = colors(dark)   # 文字色是宿主的事,这里只要底色
    acc = accent(dark)
    r = int(12 * s)
    bar = int(4 * s)
    x0, y0 = mar, mar
    x1, y1 = w - mar, h - mar
    spread = max(2.0, mar * 0.38)
    drop = mar * 0.22                     # 阴影往下偏一点,卡片才像"浮"着
    # alpha 遮罩留一份:宿主写完字要靠它把 alpha 补回来
    mask = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            # 圆角矩形的符号距离:负数在里面
            dx = max(x0 + r - x, 0, x - (x1 - 1 - r))
            dy = max(y0 + r - y, 0, y - (y1 - 1 - r))
            d = (dx * dx + dy * dy) ** 0.5 - r
            o = (y * w + x) * 4
            if d <= -1.0:                          # 卡片内部:完全不透明
                a = 255
                px = _rgba(*base, 255) if x >= x0 + bar else _rgba(*acc, 255)
            elif d <= 0.0:                         # 圆角上的抗锯齿
                a = int(255 * (-d))
                px = _rgba(*base, a)
            else:                                  # 外面:阴影
                dys = max(y0 + r - (y - drop), 0,
                          (y - drop) - (y1 - 1 - r))
                ds = (dx * dx + dys * dys) ** 0.5 - r
                # 指数衰减到位图边界时还剩 6% 左右,直接截断就是一圈方角
                # (这一坑和悬浮球的投影是同一个)。再乘一个到边界正好归零
                # 的平滑窗,外圈就真的化掉了
                t = min(1.0, max(0.0, ds / max(1.0, mar - 1.0)))
                win = (1.0 - t) ** 2
                a = int(255 * sh_a * win
                        * (2.718281828 ** (-max(ds, 0.0) / spread)))
                if a < 2:
                    buf[o:o + 4] = b"\x00\x00\x00\x00"
                    mask[y * w + x] = 0
                    continue
                px = _rgba(0, 0, 0, a)
            buf[o:o + 4] = px
            mask[y * w + x] = a

    return mask
