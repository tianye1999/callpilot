"""在 pywebview/WebKit 导入前显示轻量原生启动窗口，只由窗口进程调用。"""

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _delegate_class():
    import objc
    from Foundation import NSObject

    # 与正式窗口保持相同的 NSUInteger 返回类型。
    objc.registerMetaDataForSelector(b"NSObject", b"applicationShouldTerminate:", {"retval": {"type": b"Q"}})

    class CallPilotStartupDelegate(NSObject):
        def windowWillClose_(self, notification):
            self.on_close()

        def applicationShouldTerminate_(self, application):
            self.on_quit()
            return 0

    return CallPilotStartupDelegate


class StartupWindow:
    def __init__(self, native, delegate):
        self.native = native
        self.delegate = delegate
        self.closed = False

    def close(self):
        self.native.setDelegate_(None)
        self.native.orderOut_(None)
        self.native.close()


def show_startup_window(support: Path) -> StartupWindow:
    from AppKit import (
        NSApplication,
        NSBackingStoreBuffered,
        NSFont,
        NSMakeRect,
        NSMenu,
        NSMenuItem,
        NSScreen,
        NSTextField,
        NSWindow,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskResizable,
        NSWindowStyleMaskTitled,
    )
    from Foundation import NSDate, NSRunLoop

    from agentcall import macos_shell

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(0)
    screens = [screen.visibleFrame() for screen in NSScreen.screens()]
    geometry = macos_shell.restore_geometry(support / "window.json", [
        (int(r.origin.x), int(r.origin.y), int(r.size.width), int(r.size.height)) for r in screens
    ])
    native = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, geometry[2], geometry[3]),
        NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        | NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable,
        NSBackingStoreBuffered, False,
    )
    native.setReleasedWhenClosed_(False)
    native.setTitle_("CallPilot")
    native.setFrame_display_(NSMakeRect(*geometry), True)
    label = NSTextField.labelWithString_(macos_shell.ui_text("正在启动…", "Starting…"))
    label.setFont_(NSFont.systemFontOfSize_(24))
    label.setFrame_(NSMakeRect(40, max(40, geometry[3] - 150), geometry[2] - 80, 50))
    native.contentView().addSubview_(label)
    delegate = _delegate_class().alloc().init()
    splash = StartupWindow(native, delegate)

    def closing():
        rect = native.frame()
        macos_shell.save_geometry(support / "window.json", tuple(int(v) for v in (
            rect.origin.x, rect.origin.y, rect.size.width, rect.size.height,
        )))
        splash.closed = True

    delegate.on_close = closing
    delegate.on_quit = lambda: macos_shell.send_command("quit", support)
    native.setDelegate_(delegate)
    app.setDelegate_(delegate)
    menu = NSMenu.alloc().init()
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("CallPilot", None, "")
    submenu = NSMenu.alloc().init()
    submenu.addItemWithTitle_action_keyEquivalent_(
        macos_shell.ui_text("退出 CallPilot", "Quit CallPilot"), "terminate:", "q",
    )
    item.setSubmenu_(submenu)
    menu.addItem_(item)
    app.setMainMenu_(menu)
    native.makeKeyAndOrderFront_(None)
    native.displayIfNeeded()
    app.activateIgnoringOtherApps_(True)
    # 先交给 WindowServer 显示，再做可能耗时的 WebKit 导入。
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.01))
    return splash
