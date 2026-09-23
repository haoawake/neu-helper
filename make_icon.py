# -*- coding: utf-8 -*-
"""用画悬浮球那套代码生成应用图标。

    python make_icon.py            按当前平台生成需要的那个
    python make_icon.py --all      两个都生成
    python make_icon.py --ico      只要 gui/icon.ico   (Windows)
    python make_icon.py --icns     只要 gui/icon.icns  (macOS)

两种容器都允许"每个尺寸直接塞一张 PNG",所以写起来都很简单:

  ICO   目录项 + 若干个 PNG 块(Vista 以后都认)
  ICNS  `icns` 头 + 若干个 (类型, 长度, PNG) 块

**刻意不依赖任何窗口宿主。** 以前这里 `import orb_window`,但那现在只是个
按平台分发的壳(在 Windows 上会拉起 GDI、在 macOS 上会拉起 AppKit),
而生成图标根本不需要窗口。所以这里自带一个最小画布 —— 装机脚本在
两个平台上都能跑它,不用先把 GUI 那一套依赖装齐。
"""
from __future__ import annotations

import array
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 进度是中文打的,而终端不一定编得出来。英文版 Windows 的控制台是 cp1252,
# 一个 print 就是 UnicodeEncodeError、退出码 1 —— 而 install.ps1 和
# packaging/build_win.ps1 都拿退出码判断成败,于是**装不上、也打不了包**,
# 报错还完全不指向真正的原因(GitHub Actions 上就是这么挂的)。
# 进度文字编不出来就退化成问号,不该拖垮整件事。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except (AttributeError, ValueError):            # 被重定向成非文本流
    pass

import orb_render  # noqa: E402

# ICO 里放这些尺寸。再大没意义:Windows 最大用到 256
ICO_SIZES = (256, 128, 64, 48, 32, 16)

# ICNS 的块类型 -> 边长。这几个类型现代 macOS 全认,而且都接受 PNG 负载
ICNS_TYPES = (
    (b"icp4", 16),
    (b"icp5", 32),
    (b"icp6", 64),
    (b"ic07", 128),
    (b"ic08", 256),
    (b"ic09", 512),
)


class Canvas:
    """orb_render 要的最小画布:能按下标写 uint32 就够了。

    `draw_text_centered` 是角标的数字用的,而图标不画角标(count=0 时
    `draw_badge` 直接返回),所以这里是个空实现。
    """

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.px = array.array("I", bytes(w * h * 4))

    def draw_text_centered(self, text, rect, px, bold, color):
        pass


def _chunk(tag: bytes, data: bytes) -> bytes:
    """PNG 的块:长度 + 标签 + 数据 + CRC。ICO/ICNS 两条路都要用,
    所以从 png_bytes 里提到模块级。"""
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_bytes(f) -> bytes:
    """预乘 alpha 的 BGRA -> 直通 alpha 的 RGBA PNG。

    去预乘这一步不能省:两种容器里的 PNG 都是直通 alpha 的,直接塞预乘的
    数据会让半透明的地方(整颗球的边缘和投影)发黑。
    """
    w, h = f.w, f.h
    raw = bytearray()
    for y in range(h):
        raw += b"\x00"                         # PNG 每行的滤波器字节
        row = y * w
        for x in range(w):
            v = f.px[row + x]
            a = (v >> 24) & 255
            b, g, r = v & 255, (v >> 8) & 255, (v >> 16) & 255
            if a and a < 255:                  # 去预乘
                r = min(255, r * 255 // a)
                g = min(255, g * 255 // a)
                b = min(255, b * 255 // a)
            raw += bytes((r, g, b, a))

    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


# ---------------------------------------------------------------- 源图

# 图标的源图。有它就用它,没有就退回"画一颗悬浮球"那套(第一版就是那么来的)。
SRC_PNG = HERE / "icon.png"

# 比这亮就算背景。源图是白底上一块黑色圆角方块,白底得挖掉 —— 不挖的话
# 任务栏和程序坞里是一个白方块,圆角完全看不出来。
# 实测底色是 253~255,黑块边缘一个像素之内就掉到 71 以下,没有阴影过渡,
# 所以这个阈值有很大的余量
WHITE_CUT = 236


def read_png(path: Path) -> tuple[int, int, bytearray]:
    """读一张 PNG,返回 (宽, 高, 直通 alpha 的 RGBA)。

    **只认 8 位、非交错、RGB / RGBA**。这不是通用解码器,是为了读项目自己那
    一张图标源图 —— 认不了的格式直接报错说清楚,比悄悄画错强。
    (为什么不用 Pillow:整个项目零第三方依赖,连 PNG 编码都是手写的。
    为一个一次性的图标生成脚本引进一个装机依赖不划算。)
    """
    d = path.read_bytes()
    if d[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} 不是 PNG")
    w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", d[16:29])
    if depth != 8 or interlace or ctype not in (2, 6):
        raise ValueError(f"{path}:只认 8 位非交错的 RGB/RGBA,"
                         f"这张是 位深={depth} 颜色类型={ctype} 交错={interlace}")
    idat = bytearray()
    i = 8
    while i < len(d):
        ln = struct.unpack(">I", d[i:i + 4])[0]
        tag = d[i + 4:i + 8]
        if tag == b"IDAT":
            idat += d[i + 8:i + 8 + ln]
        elif tag == b"IEND":
            break
        i += 12 + ln
    raw = zlib.decompress(bytes(idat))

    bpp = 3 if ctype == 2 else 4
    stride = w * bpp
    out = bytearray(w * h * 4)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        # 逐行反滤波。五种滤波器都得认 —— 编码器挑哪种是它的自由
        if ft == 1:                                   # Sub
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 255
        elif ft == 2:                                 # Up
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif ft == 3:                                 # Average
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 255
        elif ft == 4:                                 # Paeth
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                c = prev[x - bpp] if x >= bpp else 0
                b = prev[x]
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        elif ft:
            raise ValueError(f"{path}:第 {y} 行的滤波器类型 {ft} 不认识")
        o = y * w * 4
        if bpp == 4:
            out[o:o + w * 4] = line
        else:
            for x in range(w):
                s, t = x * 3, o + x * 4
                out[t] = line[s]
                out[t + 1] = line[s + 1]
                out[t + 2] = line[s + 2]
                out[t + 3] = 255
        prev = line
    return w, h, out


def cut_background(w: int, h: int, buf: bytearray) -> None:
    """从四个角漫过去,把连通的白底挖成透明。**就地改。**

    为什么是漫延而不是"所有白像素一律透明":图标里面也有白 —— 那个 N、
    "NEU" 几个字、机器人的脸。它们被黑块围着,和外面的白不连通,所以漫不到。
    """
    seen = bytearray(w * h)
    stack = [0, w - 1, (h - 1) * w, h * w - 1]
    for s in stack:
        seen[s] = 1
    while stack:
        i = stack.pop()
        o = i * 4
        if min(buf[o], buf[o + 1], buf[o + 2]) < WHITE_CUT:
            continue                     # 撞到图本身,停在这儿
        buf[o + 3] = 0
        x, y = i % w, i // w
        if x and not seen[i - 1]:
            seen[i - 1] = 1
            stack.append(i - 1)
        if x + 1 < w and not seen[i + 1]:
            seen[i + 1] = 1
            stack.append(i + 1)
        if y and not seen[i - w]:
            seen[i - w] = 1
            stack.append(i - w)
        if y + 1 < h and not seen[i + w]:
            seen[i + w] = 1
            stack.append(i + w)


def crop_opaque(w: int, h: int, buf: bytearray) -> tuple[int, int, bytearray]:
    """裁到还剩下的那块内容的外接正方形。

    裁成**正方形**而不是紧贴的矩形:ICO / ICNS 每一档都是正方形,给个非方的
    进去就得拉伸,圆角会被压扁。
    """
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            if buf[row + x * 4 + 3]:
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                if y > y1:
                    y1 = y
    if x1 < 0:
        raise ValueError("整张图都被当成背景挖掉了 —— 阈值不对?")
    side = max(x1 - x0 + 1, y1 - y0 + 1)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    sx, sy = cx - side // 2, cy - side // 2
    out = bytearray(side * side * 4)
    for y in range(side):
        ys = sy + y
        if 0 <= ys < h:
            for x in range(side):
                xs = sx + x
                if 0 <= xs < w:
                    s = (ys * w + xs) * 4
                    t = (y * side + x) * 4
                    out[t:t + 4] = buf[s:s + 4]
    return side, side, out


def box_resize(w: int, h: int, buf: bytearray, size: int) -> bytearray:
    """盒式滤波缩到 size×size。

    **按 alpha 加权平均颜色**(也就是在预乘域里平均,再除回去)—— 不加权的话
    透明区那些"白色但 alpha=0"的像素会把边缘洗白,圆角外面糊一圈白边。
    源图比目标大好几倍,所以盒式就够了,边缘的锯齿正好被这一步磨平。
    """
    out = bytearray(size * size * 4)
    for ty in range(size):
        y0, y1 = ty * h // size, max(ty * h // size + 1, (ty + 1) * h // size)
        for tx in range(size):
            x0, x1 = tx * w // size, max(tx * w // size + 1, (tx + 1) * w // size)
            r = g = b = a = n = 0
            for y in range(y0, y1):
                row = y * w * 4
                for x in range(x0, x1):
                    o = row + x * 4
                    al = buf[o + 3]
                    r += buf[o] * al
                    g += buf[o + 1] * al
                    b += buf[o + 2] * al
                    a += al
                    n += 1
            t = (ty * size + tx) * 4
            if a:
                out[t] = min(255, r // a)
                out[t + 1] = min(255, g // a)
                out[t + 2] = min(255, b // a)
            out[t + 3] = a // n
    return out


def png_from_rgba(w: int, h: int, rgba: bytearray) -> bytes:
    """直通 alpha 的 RGBA -> PNG。"""
    raw = bytearray()
    for y in range(h):
        raw += b"\x00"
        raw += rgba[y * w * 4:(y + 1) * w * 4]
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def render_orb(size: int) -> bytes:
    """画一颗球,返回 PNG。没有源图时的退路。

    留一点边给投影;相位 0.18 时光晕在左上,和高光呼应。
    bright=1.9:图标要在小尺寸下认得出来,光晕得比界面上那颗浓。
    """
    pad = max(1, round(size * 0.07))
    d = size - pad * 2
    f = orb_render.render_ball(Canvas(size, size), d, pad, False, 1.0,
                               phase=0.18, bright=1.9)
    assert f.w == size, (f.w, size)
    return png_bytes(f)


def write_ico(path: Path, cache: dict) -> None:
    imgs = [(s, cache[s]) for s in ICO_SIZES]
    out = bytearray(struct.pack("<HHH", 0, 1, len(imgs)))
    offset = 6 + 16 * len(imgs)
    for size, data in imgs:
        # 目录项里 256 要写成 0 —— 那个字段只有一个字节
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                           len(data), offset)
        offset += len(data)
    for _, data in imgs:
        out += data
    path.write_bytes(bytes(out))
    print(f"写出 {path} —— {len(out)} 字节,{len(imgs)} 个尺寸 {ICO_SIZES}")


def write_icns(path: Path, cache: dict) -> None:
    blocks = bytearray()
    for tag, size in ICNS_TYPES:
        data = cache[size]
        # 每块的长度**包含这 8 字节的头**,少算就会被认成损坏文件
        blocks += tag + struct.pack(">I", len(data) + 8) + data
    out = b"icns" + struct.pack(">I", len(blocks) + 8) + bytes(blocks)
    path.write_bytes(out)
    print(f"写出 {path} —— {len(out)} 字节,"
          f"{len(ICNS_TYPES)} 个尺寸 {tuple(s for _, s in ICNS_TYPES)}")


def main(argv: list[str]) -> int:
    want_ico = "--ico" in argv or "--all" in argv
    want_icns = "--icns" in argv or "--all" in argv
    if not (want_ico or want_icns):
        import platform_id
        want_icns = platform_id.IS_MAC
        want_ico = not want_icns

    sizes = set()
    if want_ico:
        sizes |= set(ICO_SIZES)
    if want_icns:
        sizes |= {s for _, s in ICNS_TYPES}

    gui = HERE / "gui"
    gui.mkdir(exist_ok=True)

    # 有 icon.png 就以它为准。没有才退回画球 —— 仓库第一版是那么来的,
    # 那条路留着,换图标的人不该被逼着先准备一张源图
    master = None
    if SRC_PNG.exists():
        print(f"源图 {SRC_PNG.name} …", flush=True)
        mw, mh, mbuf = read_png(SRC_PNG)
        cut_background(mw, mh, mbuf)
        mw, mh, mbuf = crop_opaque(mw, mh, mbuf)
        master = (mw, mh, mbuf)
        print(f"  挖掉白底、裁成 {mw}x{mh}", flush=True)
    else:
        print("没有 icon.png,退回画悬浮球", flush=True)

    # 逐像素是纯 Python 的,512 那张要一秒多 —— 所以同尺寸只画一次
    cache = {}
    for s in sorted(sizes, reverse=True):
        print(f"  出 {s}x{s} …", flush=True)
        if master:
            cache[s] = png_from_rgba(s, s, box_resize(*master, s))
        else:
            cache[s] = render_orb(s)

    # 界面自己也要用这个图标(标题栏那个小标),所以顺手出一张放进 gui/。
    # **不直接引用根目录那张源图**:它 1254px、1.1MB,而界面上只显示 18px;
    # 而且 gui/ 才是打进包里的那个目录(见 packaging/neu-helper.spec)
    if master:
        mark = cache.get(128) or png_from_rgba(128, 128, box_resize(*master, 128))
        (gui / "icon-128.png").write_bytes(mark)
        print(f"写出 {gui / 'icon-128.png'} —— {len(mark)} 字节(界面用)")

    if want_ico:
        write_ico(gui / "icon.ico", cache)
    if want_icns:
        write_icns(gui / "icon.icns", cache)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
