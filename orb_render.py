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

# 海边:浅水的绿松石配一点珊瑚橘,玻璃本身偏暖白 —— 像阳光下的水珠。
# 球里那半球水是另外画的(见 render_ball 里的 water 那一段),这几个值管的
# 是玻璃壳本身;两团流转的光在水面**之上**,转起来像海上的日头。
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
# 像素:这一档**根本不画球**,画的是一个草方块(render_block)。
# 所以下面这几个值平时用不到 —— 它留在这儿只为一件事:PALETTES 是 set_theme
# 认名字的那张表,"pixel" 不在里面就会被当成无效名字、退回默认档,方块也就
# 永远画不出来。值本身按"实心红球"配着,万一哪天分流没走到也不至于是空白。
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

# 按名字挑的配色。空字符串 = 按 dark 在 LIGHT / DARK 之间选。
# **用模块级开关而不是加参数**:两个宿主(orb_win32 / orb_cocoa)只知道
# "现在是不是深色",不知道主题叫什么名字,而它们各有一条画图的路径 ——
# 与其在三处签名上都挂一个参数,不如让 server 在偏好变化时把这儿拨一下。
#
# 这张表同时是**合法主题名的清单**(见 set_theme),所以 pixel 也得在里面,
# 哪怕它走的是另一条画法。
PALETTES = {"neu": NEU, "beach": BEACH, "pixel": PIXEL}
THEME = ""


def set_theme(name: str | None) -> bool:
    """拨到某一档配色。认不出的名字一律回到"按深浅选"。

    **返回"这一下有没有真的换"** —— 宿主那边的 set_look 只在直径/深浅/缩放/
    动画变了的时候才重画,换主题这几样一样都不变。不把"换了"这件事告诉它,
    球就一直用着上次那张位图:窗口开着的时候球是隐藏的、动画定时器也不转,
    等你收成球,看到的还是换主题之前那颗。
    """
    global THEME
    want = name if name in PALETTES else ""
    if want == THEME:
        return False
    THEME = want
    return True


def palette(dark: bool) -> dict:
    return PALETTES.get(THEME) or (DARK if dark else LIGHT)


def canvas_size(diameter: int, pad: int) -> int:
    """画布是正方形,边长 = 球径 + 两边的留白。

    留白不是装饰:投影要在**位图边界之前**衰减到 0,不然边界那一刀切出来是
    个方块(踩过,见 render_ball 里的注释)。宿主按这个尺寸开画布。
    """
    return diameter + pad * 2


# 水面起伏的幅度(球半径的比例)。再大就拍到球壁上了
WAVE_AMP = 0.085


def _hash01(i: int, j: int) -> float:
    """按格子给一个稳定的 0~1。

    **必须稳定** —— 方块的杂色要是每帧都重摇,那就不是泥土纹理,是雪花屏。
    所以不用 random,用坐标哈希。
    """
    n = (i * 73856093) ^ (j * 19349663)
    n = (n ^ (n >> 13)) & 0x7FFFFFFF
    return ((n * 1274126177) & 0x7FFFFFFF) / 2147483647.0


# 我的世界草方块的三个面。每个面两档色,靠格子哈希随机挑,凑出泥土的颗粒感。
# 左右两面亮度不同 —— 立体感全靠这个,不是靠描边
_TOP = ((0x7C, 0xB3, 0x42), (0x6A, 0xA0, 0x35))          # 草(顶面)
_LEFT = ((0x7A, 0x5A, 0x3A), (0x6B, 0x4E, 0x31))         # 泥土(左面,背光)
_RIGHT = ((0x96, 0x70, 0x48), (0x86, 0x63, 0x3E))        # 泥土(右面,受光)
_GRASS_L = ((0x5F, 0x8F, 0x33), (0x54, 0x7F, 0x2C))      # 侧面顶上那圈草沿
_GRASS_R = ((0x74, 0xA8, 0x3E), (0x67, 0x97, 0x36))


def render_block(f, diameter: int, pad: int, scale: float = 1.0,
                 bright: float = 1.0):
    """画一个等距的草方块(我的世界那个)。

    **和玻璃球是两套东西。** 球那套的灵魂是透明、柔光、抗锯齿;方块要的正相反 ——
    不透明、硬边、看得见像素。所以不复用 render_ball,单开一个。

    只有外轮廓做抗锯齿(否则贴在桌面上边缘是锯齿状的毛刺,那不叫像素风叫没画完),
    内部一律按格子取色,一个渐变都没有。

    投影也换成方的:像素游戏里的影子就是底下垫一块深色,不是柔光。
    """
    w, h = f.w, f.h
    # 留一点余量给投影
    a = diameter * 0.5 * scale * 0.94
    if a < 3:
        return f
    cx, cy = w * 0.5, h * 0.5
    hh = a * 0.5                       # 顶面菱形的半高(2:1 等距)
    # 侧面高度。0.66 那版看着是块砖不是方块 —— 等距立方体的侧面应该和顶面
    # 菱形的**全高**差不多。总高 2*hh + sh = 1.88a,还在画布里
    sh = a * 0.88
    # 一个"大像素"多少个真实像素。16 格是方块贴图的原始分辨率
    cell = max(1.0, (a * 2.0) / 16.0)

    top_c = cy - sh * 0.5              # 顶面菱形的中心
    grass_lip = sh * 0.22              # 侧面顶上那圈草沿有多厚

    for y in range(h):
        fy = y + 0.5
        row = y * w
        for x in range(w):
            fx = x + 0.5
            px, py = fx - cx, fy - cy
            # 格子坐标:同一格里的所有像素取同一个色,这就是"像素感"的来源
            gi = int(math.floor((px + a) / cell))
            gj = int(math.floor((py + a) / cell))

            col = None
            # ── 顶面:以 top_c 为中心的菱形
            td = abs(px) / a + abs(py - (top_c - cy)) / hh
            if td <= 1.0:
                pair = _TOP
                # 菱形边缘一圈压暗,草皮才有厚度
                if td > 0.88:
                    pair = (_TOP[1], _TOP[1])
                col = pair[0] if _hash01(gi, gj) < 0.55 else pair[1]
            else:
                # ── 侧面:两块平行四边形。给定 x,顶边斜着走
                if px <= 0:
                    edge = (top_c - cy) + hh * (px + a) / a
                    inside = -a <= px <= 0
                    pair, gp = _LEFT, _GRASS_L
                else:
                    edge = (top_c - cy) + hh * (a - px) / a
                    inside = 0 <= px <= a
                    pair, gp = _RIGHT, _GRASS_R
                if inside and edge <= py <= edge + sh:
                    # 顶上那圈是草沿,下面是泥土。草沿的下边缘故意做成锯齿 ——
                    # 我的世界的草土交界本来就是一格一格参差的
                    lip = grass_lip * (0.75 + 0.5 * _hash01(gi, 777))
                    if py - edge <= lip:
                        col = gp[0] if _hash01(gi, gj) < 0.6 else gp[1]
                    else:
                        col = pair[0] if _hash01(gi, gj) < 0.5 else pair[1]

            if col is None:
                # ── 方块外面:一块方投影,垫在右下
                sx, syy = px - a * 0.14, py - a * 0.16
                if (abs(sx) / a + abs(syy - (top_c - cy)) / hh <= 1.0
                        or (abs(sx) <= a and
                            (top_c - cy) + hh * (a - abs(sx)) / a <= syy
                            <= (top_c - cy) + hh * (a - abs(sx)) / a + sh)):
                    f.px[row + x] = (0x66 << 24)      # 预乘:纯黑 40% 不透明
                else:
                    f.px[row + x] = 0
                continue

            r8, g8, b8 = col
            if bright != 1.0:
                r8 = int(_clamp(r8 * bright, 0, 255))
                g8 = int(_clamp(g8 * bright, 0, 255))
                b8 = int(_clamp(b8 * bright, 0, 255))
            f.px[row + x] = (0xFF << 24) | (r8 << 16) | (g8 << 8) | b8
    return f


def render_ball(f, diameter: int, pad: int, dark: bool, scale: float = 1.0,
                phase: float = 0.0, bright: float = 1.0):
    """把一颗透明玻璃泡画进画布 f(预乘 alpha 的 BGRA)。

    scale  球相对画布缩放(弹出动画用:只换位图、不动窗口尺寸,不会有错位帧)
    phase  内部光晕的相位 0~1,转一圈
    bright hover 时整体提亮的系数

    alpha 是逐像素算出来的:球心低(能透到桌面)、边缘高(轮廓立得住)、
    光晕走过的地方再加一点(看起来像里面有东西在流)。
    """
    # 像素那一档根本不是球,是个方块 —— 单独一套画法,提前分流
    if THEME == "pixel":
        return render_block(f, diameter, pad, scale, bright)

    pal = palette(dark)
    w, h = f.w, f.h
    r = diameter * 0.5 * scale
    if r < 2:
        return f
    # 海边:球里那半球水。wave_base 略高于球心(-0.06)= 装到六成满,
    # 空一点才看得出是个"球里有水",装满就只是一颗蓝球了
    water = THEME == "beach"
    wave_base = -0.06
    wave_t = phase * TAU
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

                # ── 海边那一档:球里装着半球水,水面是会晃的浪
                #
                # 这一层**加在玻璃里、镜面高光下面** —— 顺序错了就成了"水面
                # 浮着一块白斑",而不是"隔着玻璃看见里面的水"。
                # 两条正弦叠加(频率 3.1 和 6.7)而不是一条:单条正弦晃起来像
                # 钟摆,叠一条快的才有"水在荡"的乱劲。
                if water:
                    surf = (wave_base
                            + WAVE_AMP * math.sin(nx * 3.1 + wave_t)
                            + WAVE_AMP * 0.45 * math.sin(nx * 6.7 - wave_t * 1.7))
                    if ny > surf:
                        depth = _clamp((ny - surf) / 1.25)
                        # 越深越暗越蓝,浅处偏绿松石
                        tr = 30 + (8 - 30) * depth
                        tg = 200 + (92 - 200) * depth
                        tb = 212 + (156 - 212) * depth
                        m = 0.88
                        cr += (tr - cr) * m
                        cg += (tg - cg) * m
                        cb += (tb - cb) * m
                        a = max(a, 0.58 + 0.34 * depth)
                    # 水面那道白沫。上沿比下沿薄,看着才像光打在浪尖上
                    d_surf = ny - surf
                    if -0.030 < d_surf < 0.055:
                        fk = 1.0 - abs(d_surf) / 0.055
                        cr += (255 - cr) * fk * 0.85
                        cg += (255 - cg) * fk * 0.85
                        cb += (255 - cb) * fk * 0.85
                        a = max(a, 0.52 + 0.40 * fk)

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

