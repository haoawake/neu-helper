# -*- coding: utf-8 -*-
"""悬浮球长什么样 —— **纯 Python 逐像素算,两个平台共用这一份。**

这里一行平台专有的代码都没有。输出是**预乘 alpha 的 32 位像素**,打包成
`(A << 24) | (R << 16) | (G << 8) | B` 写进一个 uint32 数组 —— 小端机器上
内存里的字节序就是 B, G, R, A。

这个格式两边都是原生的,不用转换:

  Windows   `UpdateLayeredWindow` + `ULW_ALPHA` 要的就是预乘 BGRA 的 DIB
  macOS     CGImage 的 `PremultipliedFirst | ByteOrder32Little` 同样是它

所以球在两个系统上是**同一颗球**,不是各画一遍长得像。

调用方要提供一个"画布"对象,只需要三样东西:

  f.w, f.h   尺寸
  f.px[i]    一个能按下标写 uint32 的序列(Windows 上是 DIB 的内存,零拷贝;
             macOS 上是 array('I'))

画布由各自的宿主模块提供(orb_win32.py / orb_cocoa.py),因为它还要承担
"怎么把这块内存交给系统去合成"这件平台专有的事。
"""
from __future__ import annotations

import math

TAU = math.pi * 2


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else (hi if v > hi else v)


# 玻璃泡的参数。核心是 core_a / rim_a 这一对:中心的 alpha 压得很低,
# 桌面才透得出来;边缘拉高,球的轮廓才立得住。
LIGHT = {
    "glass": (0xF4, 0xF8, 0xFF),     # 玻璃本身的底色(冷白)
    "core_a": 0.18,                  # 球心不透明度 —— 这就是"透明球"的关键
    "rim_a": 0.80,                   # 边缘那圈亮环
    "spec": 0.92,                    # 左上强高光强度
    "glow_a": 0.52,                  # 内部光晕能加多少 alpha
    "glow_1": (0x3E, 0x8E, 0xF7),    # 流转的光 A:品牌蓝
    "glow_2": (0x9A, 0x6F, 0xF0),    # 流转的光 B:紫
    "shadow": (0x17, 0x20, 0x33),
    "shadow_a": 0.26,
}
DARK = {
    "glass": (0xC8, 0xD6, 0xF0),
    "core_a": 0.16,
    "rim_a": 0.62,
    "spec": 0.70,
    "glow_a": 0.62,
    "glow_1": (0x5A, 0xA6, 0xFF),
    "glow_2": (0xAE, 0x86, 0xFF),
    "shadow": (0x00, 0x00, 0x00),
    "shadow_a": 0.44,
}
# 「NEU」主题那一档:校徽的黑白红。玻璃底色换成中性的暖白(蓝调白压在
# 红光上会发脏),流转的两束光一深一浅都取红 —— 只有一支红的话球是死的,
# 两支同色不同亮度才转得起来。
NEU = {
    "glass": (0xEC, 0xE6, 0xE8),
    "core_a": 0.16,
    "rim_a": 0.66,
    "spec": 0.74,
    "glow_a": 0.64,
    "glow_1": (0xE0, 0x22, 0x3F),
    "glow_2": (0xFF, 0x7A, 0x8C),
    "shadow": (0x00, 0x00, 0x00),
    "shadow_a": 0.46,
}

# 按名字挑的配色。空字符串 = 按 dark 在 LIGHT / DARK 之间选。
# **用模块级开关而不是加参数**:两个宿主(orb_win32 / orb_cocoa)只知道
# "现在是不是深色",不知道主题叫什么名字,而它们各有一条画图的路径 ——
# 与其在三处签名上都挂一个参数,不如让 server 在偏好变化时把这儿拨一下。
# 海边:浅水的绿松石配一点珊瑚橘,玻璃本身偏暖白 —— 像阳光下的水珠
BEACH = {
    "glass": (0xFF, 0xFA, 0xF0),
    "core_a": 0.17,
    "rim_a": 0.78,
    "spec": 0.95,
    "glow_a": 0.56,
    "glow_1": (0x2E, 0xC4, 0xCE),
    "glow_2": (0xFF, 0xA8, 0x70),
    "shadow": (0x0D, 0x5A, 0x64),
    "shadow_a": 0.24,
}
# 像素:这一档要的不是"玻璃球",是一颗**实心的彩球**。
# core_a 拉到接近 1、rim_a 拉满、高光压到很低 —— 透明感和柔和高光正是
# 像素画风的反面。真正的方块感做不到(球的形状是 render_ball 算出来的圆),
# 但"不透明 + 硬边 + 单色"已经足够和玻璃那套区分开。
PIXEL = {
    "glass": (0xD1, 0x3B, 0x3B),
    "core_a": 0.94,
    "rim_a": 1.0,
    "spec": 0.16,
    "glow_a": 0.30,
    "glow_1": (0xE0, 0xA5, 0x00),
    "glow_2": (0x3F, 0x9E, 0x3F),
    "shadow": (0x20, 0x20, 0x3A),
    "shadow_a": 0.55,
}

PALETTES = {"neu": NEU, "beach": BEACH, "pixel": PIXEL}
THEME = ""


def set_theme(name: str | None) -> None:
    """拨到某一档配色。认不出的名字一律回到"按深浅选"。"""
    global THEME
    THEME = name if name in PALETTES else ""


def palette(dark: bool) -> dict:
    return PALETTES.get(THEME) or (DARK if dark else LIGHT)


def canvas_size(diameter: int, pad: int) -> int:
    """画布是正方形,边长 = 球径 + 两边的留白。

    留白不是装饰:投影要在**位图边界之前**衰减到 0,不然边界那一刀切出来是
    个方块(踩过,见 render_ball 里的注释)。宿主按这个尺寸开画布。
    """
    return diameter + pad * 2


def render_ball(f, diameter: int, pad: int, dark: bool, scale: float = 1.0,
                phase: float = 0.0, bright: float = 1.0):
    """把一颗透明玻璃泡画进画布 f(预乘 alpha 的 BGRA)。

    scale  球相对画布缩放(弹出动画用:只换位图、不动窗口尺寸,不会有错位帧)
    phase  内部光晕的相位 0~1,转一圈
    bright hover 时整体提亮的系数

    alpha 是逐像素算出来的:球心低(能透到桌面)、边缘高(轮廓立得住)、
    光晕走过的地方再加一点(看起来像里面有东西在流)。
    """
    pal = palette(dark)
    w, h = f.w, f.h
    r = diameter * 0.5 * scale
    if r < 2:
        return f
    cx = cy = w * 0.5
    sy = cy + diameter * 0.06            # 投影往下偏一点,球才像"浮"着
    spread = max(1.0, pad * 0.40)
    # 阴影必须在位图边界**之前**就归零,否则边界那一刀切出来的是个方块。
    # 用到内切圆的距离做收尾 —— 圆形的收尾才看不出边
    fade_r = w * 0.5 - 0.5
    fade_w = max(1.0, pad * 0.55)
    gr, gg, gb = pal["glass"]
    sr, sg, sb = pal["shadow"]
    g1 = pal["glow_1"]
    g2 = pal["glow_2"]
    core_a, rim_a = pal["core_a"], pal["rim_a"]
    spec_k = pal["spec"] * bright
    glow_k = pal["glow_a"] * bright
    speak = pal["shadow_a"] * (0.35 + 0.65 * scale)

    # 两团光的位置:相位差 2.3 弧度,绕着球心转;半径也随相位轻微呼吸,
    # 这样看起来是"流动"而不是"齿轮匀速转"
    a1 = phase * TAU
    a2 = a1 + 2.3
    d1 = 0.40 + 0.10 * math.sin(a1 * 1.5)
    d2 = 0.44 + 0.09 * math.cos(a1 * 1.2)
    gx1, gy1 = math.cos(a1) * d1, math.sin(a1) * d1
    gx2, gy2 = math.cos(a2) * d2, math.sin(a2) * d2

    for y in range(h):
        fy = y + 0.5
        row = y * w
        for x in range(w):
            fx = x + 0.5
            dx, dy = fx - cx, fy - cy
            dist = math.sqrt(dx * dx + dy * dy)

            # ── 投影
            sd = math.sqrt(dx * dx + (fy - sy) ** 2) - r * 1.02
            sa = speak if sd <= 0 else speak * math.exp(-sd / spread)
            sa *= _clamp((fade_r - dist) / fade_w)      # 圆形收尾
            if sa < 0.004 and dist > r + 1.5:
                f.px[row + x] = 0
                continue

            cover = _clamp((r - dist) / 1.25 + 0.5)      # 球的覆盖率(抗锯齿)
            ba = 0.0
            cr = cg = cb = 0.0
            if cover > 0:
                nx, ny = dx / r, dy / r
                nd = dist / r

                # 底色 = 玻璃本身
                cr, cg, cb = float(gr), float(gg), float(gb)
                # alpha:球心 core_a -> 边缘 rim_a,四次方让亮环收得很窄
                a = core_a + (rim_a - core_a) * (nd ** 4)

                # 内部光晕:两团高斯光,既加色也加 alpha
                for (ox, oy), (lr, lg, lb), wide in (
                    ((gx1, gy1), g1, 0.30), ((gx2, gy2), g2, 0.26)
                ):
                    e = math.exp(-((nx - ox) ** 2 + (ny - oy) ** 2) / wide)
                    if e > 0.01:
                        k = e * glow_k
                        cr += (lr - cr) * min(1.0, k * 1.5)
                        cg += (lg - cg) * min(1.0, k * 1.5)
                        cb += (lb - cb) * min(1.0, k * 1.5)
                        a += k * 0.55

                # 左上那块强高光:玻璃的镜面反射,小而亮
                hx, hy = nx + 0.40, ny + 0.46
                sp = math.exp(-(hx * hx + hy * hy) / 0.055) * spec_k
                if sp > 0.004:
                    cr += (255 - cr) * sp
                    cg += (255 - cg) * sp
                    cb += (255 - cb) * sp
                    a += sp * 0.75

                # 下缘内反光:光从桌面反上来,球才有厚度
                if nd > 0.72:
                    rim = ((nd - 0.72) / 0.28) ** 2 * _clamp(0.25 + 0.75 * (ny + 1) * 0.5)
                    cr += (255 - cr) * rim * 0.45
                    cg += (255 - cg) * rim * 0.45
                    cb += (255 - cb) * rim * 0.45

                ba = _clamp(a) * cover

            # ── 球叠在投影上(预乘 alpha 的 over)
            oa = ba + sa * (1 - ba)
            if oa <= 0.002:
                f.px[row + x] = 0
                continue
            k = sa * (1 - ba)
            pr = cr * ba + sr * k
            pg = cg * ba + sg * k
            pb = cb * ba + sb * k
            f.px[row + x] = (
                (int(oa * 255) << 24) | (int(_clamp(pr, 0, 255)) << 16)
                | (int(_clamp(pg, 0, 255)) << 8) | int(_clamp(pb, 0, 255))
            )
    return f


# ─────────────────────────── 逾期角标 ───────────────────────────
#
# 圆盘是纯 Python 画的(见下面的注释:这样两边一致),**只有数字需要平台的
# 文字引擎** —— 画布得提供一个 `draw_text_centered` 方法。整个平台差异就这一个
# 方法,不值得为它把角标写两遍。


def badge_geometry(diameter: int, pad: int) -> tuple[int, int, int]:
    """角标的圆心和半径 (cx, cy, r)。宿主排版和命中判定都要用,所以单独提出来。"""
    r = max(9, int(diameter * 0.28))
    return pad + diameter - int(r * 0.72), pad + int(r * 0.72), r


def badge_text(count: int) -> str:
    return "" if count <= 0 else (str(count) if count < 100 else "99+")


def draw_badge(f, diameter: int, pad: int, count: int) -> None:
    """右上角的逾期角标:先填圆盘,再让宿主把数字写上去。

    **圆盘刻意用纯 Python 填,而且是硬边。** 原来这里是 GDI 的 `Ellipse`,
    但 GDI 不认预乘 alpha —— 画完那块 alpha 是 0,所以事后要按
    `(x-cx)^2 + (y-cy)^2 <= (r-0.5)^2` 把圆内的 alpha 补满。也就是说
    **真正看得见的一直就是这一组像素**,GDI 多画在外面的部分 alpha=0、根本不显示。
    那不如直接填这一组:结果在 Windows 上和以前一模一样,而且不用写第二遍。

    数字同理会被文字引擎清掉 alpha(GDI)或者需要另一套 API(Core Text),
    所以写完之后再把圆盘的 alpha 整体拍成 255。
    """
    text = badge_text(count)
    if not text:
        return
    bcx, bcy, br = badge_geometry(diameter, pad)

    # BGRA 预乘。角标是实心的,alpha=255,所以预乘等于原色
    disc = 0xFFD03B3B          # A=FF R=D0 G=3B B=3B —— 醒目的红
    rr = (br - 0.5) ** 2
    y0, y1 = max(0, bcy - br - 1), min(f.h, bcy + br + 2)
    x0, x1 = max(0, bcx - br - 1), min(f.w, bcx + br + 2)
    for y in range(y0, y1):
        row = y * f.w
        dy2 = (y + 0.5 - bcy) ** 2
        for x in range(x0, x1):
            if (x + 0.5 - bcx) ** 2 + dy2 <= rr:
                f.px[row + x] = disc

    f.draw_text_centered(text, (bcx - br, bcy - br, bcx + br, bcy + br),
                         px=int(br * 1.2), bold=True, color=(255, 255, 255))

    # 文字引擎把字形的 alpha 清成 0 了(GDI 在 32 位 DIB 上一定会;Core Text
    # 那边宿主自己保证)。圆盘本来就该是实心的,整块补回 255 就行。
    for y in range(y0, y1):
        row = y * f.w
        dy2 = (y + 0.5 - bcy) ** 2
        for x in range(x0, x1):
            if (x + 0.5 - bcx) ** 2 + dy2 <= rr:
                f.px[row + x] |= 0xFF000000

