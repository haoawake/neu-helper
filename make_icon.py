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

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def render(size: int) -> bytes:
    """画一颗球,返回 PNG。

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
    # 逐像素是纯 Python 的,512 那张要一秒多 —— 所以同尺寸只画一次
    cache = {}
    for s in sorted(sizes, reverse=True):
        print(f"  画 {s}x{s} …", flush=True)
        cache[s] = render(s)

    if want_ico:
        write_ico(gui / "icon.ico", cache)
    if want_icns:
        write_icns(gui / "icon.icns", cache)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
