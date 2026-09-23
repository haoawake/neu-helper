# -*- coding: utf-8 -*-
"""菜单栏图标 —— macOS 实现。

**位置和 Windows 不一样,这是对的。** Windows 的通知区在任务栏右下角;
macOS 没有那个东西,对应的位置是**菜单栏右侧**(NSStatusItem)。同一个
功能、同一套回调,摆在各自系统里该摆的地方。

和 Windows 那份宿主的三个差别:

1. **没有隐藏窗口。** Shell_NotifyIcon 要一个窗口来收鼠标消息;
   NSStatusItem 自带一个按钮,菜单直接挂上去,系统负责弹
2. **菜单左键也会弹。** 这是 macOS 的惯例 —— 菜单栏图标点一下就出菜单,
   没有"左键一个动作、右键另一个菜单"那种分法。所以"打开"是菜单的第一项,
   而不是绑在左键上
3. **必须在主线程上碰 AppKit。** 所有动作都过 native_cocoa._main
   (和 orb_cocoa / toast_cocoa 同一个办法)

图标用 `setTemplate_(True)` 交给系统去染色:菜单栏在浅色下是深图标、
深色下是浅图标,自己判断深浅只会和系统打架(而且还有"强调色菜单栏"这种档)。
"""
from __future__ import annotations

import threading

import objc
from AppKit import (NSApplication, NSImage, NSMenu, NSMenuItem,
                    NSSquareStatusItemLength, NSStatusBar, NSVariableStatusItemLength)
from Foundation import NSObject

import native_cocoa

# 图标在菜单栏里的高度。18pt 是 Apple 给模板图标的建议值 —— 再大就顶到
# 菜单栏边缘,再小看不清
ICON_PT = 18.0


class _Target(NSObject):
    """菜单项的动作靶子。

    **必须是一个 NSObject 子类。** AppKit 的 target/action 是 Objective-C
    的消息派发,普通 Python 对象接不到 —— 菜单点了什么也不会发生。
    """

    def initWithTray_(self, tray):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self._tray = tray
        return self

    @objc.IBAction
    def hit_(self, sender):
        fn = {0: self._tray.on_open, 1: self._tray.on_expand,
              2: self._tray.on_orb, 3: self._tray.on_quit}.get(sender.tag())
        # 回调丢到别的线程:它们要切形态、等窗口动画 —— 在主线程上同步做完
        # 的话,这段时间整个界面(菜单栏也包括)是不响应的
        if fn is not None:
            threading.Thread(target=fn, daemon=True, name="tray-cb").start()


class Tray:
    """菜单栏图标。外部接口和 tray_win32.Tray 完全一致。"""

    def __init__(self, on_open=None, on_expand=None, on_orb=None,
                 on_quit=None, icon_path: str = "",
                 tip: str = "NEU Helper", labels: dict | None = None):
        self.on_open = on_open
        self.on_expand = on_expand
        self.on_orb = on_orb
        self.on_quit = on_quit
        self.icon_path = str(icon_path or "")
        self.tip = tip
        self.labels = dict(labels or {})
        self.hwnd = 0                 # 这边没有 HWND 这回事,占位保持签名一致
        self._item = None
        self._target = None
        self.ready = threading.Event()
        native_cocoa._main(self._build)
        self.ready.set()

    # ─────────── 外部接口 ───────────

    def set_labels(self, labels: dict) -> None:
        self.labels = dict(labels or {})
        native_cocoa._main(self._rebuild_menu)

    def set_tip(self, tip: str) -> None:
        self.tip = tip or ""
        native_cocoa._main(self._apply_tip)

    def visible(self) -> bool:
        return self._item is not None

    def close(self) -> None:
        native_cocoa._main(self._teardown)

    # ─────────── 主线程上的活 ───────────

    def _build(self):
        NSApplication.sharedApplication()
        bar = NSStatusBar.systemStatusBar()
        # 定长(方图标)而不是变长:变长是给"图标 + 一段文字"用的,
        # 只有图标的时候会在两边留出多余的空白
        self._item = bar.statusItemWithLength_(NSSquareStatusItemLength)
        self._target = _Target.alloc().initWithTray_(self)
        img = None
        if self.icon_path:
            img = NSImage.alloc().initWithContentsOfFile_(self.icon_path)
        if img is not None:
            img.setSize_((ICON_PT, ICON_PT))
            # 交给系统染色 —— 浅色菜单栏出深图标、深色出浅图标
            img.setTemplate_(True)
            btn = self._item.button()
            if btn is not None:
                btn.setImage_(img)
        elif self._item.button() is not None:
            # 没图标也得有个能点的东西,别让入口消失
            self._item.button().setTitle_("NEU")
        self._apply_tip()
        self._rebuild_menu()

    def _apply_tip(self):
        if self._item is None:
            return
        btn = self._item.button()
        if btn is not None:
            btn.setToolTip_(self.tip or "")

    def _rebuild_menu(self):
        if self._item is None:
            return
        L = self.labels
        menu = NSMenu.alloc().init()
        rows = [(0, L.get("open") or "打开"),
                (1, L.get("expand") or "展开完整面板"),
                (2, L.get("orb") or "收成悬浮球"),
                (None, None),
                (3, L.get("quit") or "退出 NEU Helper")]
        for tag, title in rows:
            if tag is None:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, objc.selector(self._target.hit_, signature=b"v@:@"), "")
            it.setTag_(tag)
            it.setTarget_(self._target)
            menu.addItem_(it)
        self._item.setMenu_(menu)

    def _teardown(self):
        if self._item is None:
            return
        NSStatusBar.systemStatusBar().removeStatusItem_(self._item)
        self._item = None
