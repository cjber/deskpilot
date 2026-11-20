"""The macOS twin of scripts/smoke_app.py, in Cocoa: every input it receives is written to a JSON state file.

Aqua Tk cannot stand in on macOS: it places a mouse event at the hardware pointer (`[NSEvent mouseLocation]` in
tkMacOSXMouseEvent.c), never at the event's own location, and takes keys only once it holds a key window. Input
posted to its process, which leaves the user's pointer alone, never reaches a widget. Cocoa routes a posted event by
its window and location, as TextEdit and most Mac apps do, so this window has the same controls and state as the Tk
one, as AppKit views.

`python smoke_app_macos.py STATE` opens the test window; `python smoke_app_macos.py STATE --cover TARGET` opens a
floating window over the pad of the window whose state is TARGET, for the covered-point check.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import AppKit  # ty: ignore[import-not-found]  # pyobjc, installed on macOS only
import objc  # ty: ignore[import-not-found]
import Quartz  # ty: ignore[import-not-found]  # noqa: F401  # bridges the CGColor a layer takes
from PyObjCTools import AppHelper  # ty: ignore[import-not-found]

path = Path(sys.argv[1])
cover = "--cover" in sys.argv
state: dict = {"clicks": [], "scroll": [], "drag": [], "hover": 0, "keys": [], "entry": "", "pressed": 0}
widgets: dict = {}
# Quartz and the state file put the origin at the top left of the main display, AppKit at its bottom left.
SCREEN_HEIGHT = AppKit.NSScreen.screens()[0].frame().size.height
# The modifier that makes a shortcut: Command on macOS, Control as Tk records it elsewhere.
SHORTCUT = AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagControl


def top_left(rect) -> list[int]:
    return [round(rect.origin.x), round(SCREEN_HEIGHT - rect.origin.y - rect.size.height), round(rect.size.width),
            round(rect.size.height)]  # fmt: skip


def save() -> None:
    state["root"] = top_left(window.convertRectToScreen_(window.contentView().frame()))[:2]
    state["widgets"] = {
        name: top_left(window.convertRectToScreen_(view.convertRect_toView_(view.bounds(), None)))
        for name, view in widgets.items()
    }
    state["entry"] = "" if cover else str(widgets["entry"].stringValue())
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    os.replace(temporary, path)


def inside(view, event) -> bool:
    return bool(AppKit.NSPointInRect(view.convertPoint_fromView_(event.locationInWindow(), None), view.bounds()))


class SmokeApplication(AppKit.NSApplication):
    """Records pointer motion over the hover spot and every key, as they reach the app, then handles them as usual."""

    def sendEvent_(self, event):
        kind = event.type()
        mine = not cover and event.windowNumber() == window.windowNumber()
        if mine and kind == AppKit.NSEventTypeMouseMoved and inside(widgets["spot"], event):
            state["hover"] += 1
            save()
        if not cover and kind == AppKit.NSEventTypeKeyDown:
            state["keys"].append([str(event.charactersIgnoringModifiers()), bool(event.modifierFlags() & SHORTCUT)])
            # Command-A selects all here, before AppKit, as Tk's entry binds Control-A itself on the other
            # platforms. AppKit's own path is an Edit menu key equivalent, which runs only for the active app; the
            # smoke sends the chord to the app in the background, as it does on Windows. With the app active, that
            # menu path also selected nothing on the runner, so it would test this fixture's menu, not deskpilot.
            command = event.modifierFlags() & AppKit.NSEventModifierFlagCommand
            if mine and command and event.charactersIgnoringModifiers() == "a":
                window.firstResponder().selectAll_(None)
                save()
                return
        objc.super(SmokeApplication, self).sendEvent_(event)
        if not cover and kind in (AppKit.NSEventTypeKeyDown, AppKit.NSEventTypeKeyUp):
            save()


class Flipped(AppKit.NSView):
    def isFlipped(self):
        return True


class Pad(Flipped):
    """Records left, double and right clicks and the wheel; the first click turns it red, so a shot changes."""

    def acceptsFirstMouse_(self, _event):
        return True

    @objc.python_method
    def click(self, kind: str) -> None:
        state["clicks"].append({"kind": kind, "num": 1})
        self.layer().setBackgroundColor_(AppKit.NSColor.systemRedColor().CGColor())
        save()

    def mouseDown_(self, event):
        self.click("double" if event.clickCount() == 2 else "left")

    def rightMouseDown_(self, _event):
        self.click("right")

    def scrollWheel_(self, event):
        if event.deltaY():
            state["scroll"].append(1 if event.deltaY() > 0 else -1)
            save()


class Track(Flipped):
    """Records a drag's press, motion and release, in the track's own coordinates."""

    def acceptsFirstMouse_(self, _event):
        return True

    @objc.python_method
    def record(self, kind: str, event) -> None:
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        state["drag"].append([kind, round(point.x), round(point.y)])
        save()

    def mouseDown_(self, event):
        self.record("down", event)

    def mouseDragged_(self, event):
        self.record("move", event)

    def mouseUp_(self, event):
        self.record("up", event)


class FirstMouseButton(AppKit.NSButton):
    def acceptsFirstMouse_(self, _event):
        return True


class FirstMouseField(AppKit.NSTextField):
    def acceptsFirstMouse_(self, _event):
        return True


class Handler(AppKit.NSObject):
    def press_(self, _sender):
        state["pressed"] += 1
        window.setTitle_(f"deskpilot smoke pressed {state['pressed']}")
        save()

    def controlTextDidChange_(self, _notification):
        save()


def colored(cls, frame: tuple[int, int, int, int], color):
    view = cls.alloc().initWithFrame_(AppKit.NSMakeRect(*frame))
    view.setWantsLayer_(True)
    view.layer().setBackgroundColor_(color.CGColor())
    return view


app = SmokeApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
handler = Handler.alloc().init()

if cover:
    pad = json.loads(Path(sys.argv[3]).read_text())["widgets"]["pad"]
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(0, 0, 160, 120), style, AppKit.NSBackingStoreBuffered, False
    )
    window.setTitle_("deskpilot cover")
    window.setLevel_(AppKit.NSFloatingWindowLevel)
    window.setFrameTopLeftPoint_((pad[0] + 10, SCREEN_HEIGHT - (pad[1] + 10)))
else:
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(120, SCREEN_HEIGHT - 120 - 420, 520, 420), style, AppKit.NSBackingStoreBuffered, False
    )
    window.setTitle_("deskpilot smoke")
    window.setAcceptsMouseMovedEvents_(True)
    content = Flipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 520, 420))
    window.setContentView_(content)
    widgets["pad"] = colored(Pad, (140, 10, 240, 120), AppKit.NSColor.systemBlueColor())
    button = FirstMouseButton.alloc().initWithFrame_(AppKit.NSMakeRect(225, 140, 70, 28))
    button.setTitle_("Press")
    button.setTarget_(handler)
    button.setAction_("press:")
    widgets["button"] = button
    widgets["track"] = colored(Track, (140, 178, 240, 60), AppKit.NSColor.systemGreenColor())
    widgets["spot"] = colored(Flipped, (167, 248, 186, 22), AppKit.NSColor.systemOrangeColor())
    entry = FirstMouseField.alloc().initWithFrame_(AppKit.NSMakeRect(119, 280, 282, 28))
    entry.setDelegate_(handler)
    widgets["entry"] = entry
    for view in widgets.values():
        content.addSubview_(view)

window.makeKeyAndOrderFront_(None)
AppHelper.callLater(0.2, save)
app.run()
