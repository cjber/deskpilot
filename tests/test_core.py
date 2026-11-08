import json

import pytest

from deskpilot import keys, screen
from deskpilot.desk import Desk
from deskpilot.hypr import AmbiguousWindow, Hyprland, Window, WindowNotFound, find_window


def win(address="0x1", cls="wow.exe", title="World of Warcraft", x=1440, y=560, w=3440, h=1440):
    return Window(address, cls, title, x, y, w, h, 1, "1", True, 10)


class FakeHyprctl:
    """Answers hyprctl like Hyprland 0.57: Lua dispatchers only, string ones are a Lua syntax error."""

    def __init__(self, clients, lua=True):
        self.clients, self.lua, self.calls, self.showing = clients, lua, [], "1"

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["-j", "clients"]:
            return json.dumps(self.clients)
        if args[:2] == ["-j", "monitors"]:
            return json.dumps(
                [{"name": "DP-1", "x": 0, "y": 0, "width": 10, "height": 10, "activeWorkspace": {"name": self.showing}}]
            )
        if args[:2] == ["-j", "activewindow"]:
            return json.dumps(self.clients[0] if self.clients else {})
        if args[0] == "dispatch":
            is_lua = args[1].startswith("hl.")
            return "ok" if is_lua == self.lua else "error: bad dispatcher"
        return ""


CLIENT = {
    "address": "0xabc",
    "class": "battle.net.exe",
    "title": "Battle.net",
    "at": [2527, 966],
    "size": [1266, 684],
    "monitor": 1,
    "workspace": {"name": "1"},
    "xwayland": True,
    "pid": 7,
    "mapped": True,
}


def test_find_prefers_exact_class_over_substring():
    wins = [win("0x1", "wow.exe"), win("0x2", "wow.exe-helper", "x")]
    assert find_window(wins, "WOW.EXE").address == "0x1"


def test_find_by_title_substring_and_address():
    wins = [win("0x1", "google-chrome", "CurseForge"), win("0x2", "wow.exe")]
    assert find_window(wins, "curseforge").address == "0x1"
    assert find_window(wins, "0x2").cls == "wow.exe"


def test_find_ambiguous_names_both_and_missing_is_an_error():
    wins = [win("0x1", "kitty", "a"), win("0x2", "kitty", "b")]
    with pytest.raises(AmbiguousWindow, match="0x1.*0x2"):
        find_window(wins, "kitty")
    with pytest.raises(WindowNotFound):
        find_window(wins, "firefox")


def test_screenshot_point_maps_to_global():
    w = win(x=1440, y=560, w=3440, h=1440)
    assert screen.to_global(w, 688, 538, 0.4) == (1440 + 1720, 560 + 1345)
    assert screen.to_global(w, 10, 10, 1.0) == (1450, 570)


def test_point_outside_window_is_refused():
    with pytest.raises(ValueError, match="outside"):
        screen.to_global(win(w=100, h=100), 150, 10, 1.0)


def test_scale_only_shrinks_wide_windows():
    assert screen.scale_for(1266, 1400) == 1.0
    assert screen.scale_for(3440, 1376) == 0.4


def test_last_scale_round_trips(tmp_path):
    screen.remember_scale("0xabc", 0.4, tmp_path)
    assert screen.last_scale("0xabc", tmp_path) == 0.4
    assert screen.last_scale("0xother", tmp_path) == 1.0


def test_chords_and_text():
    assert keys.parse_chord("ctrl+shift+t") == [keys.CTRL, keys.SHIFT, 20]
    assert keys.parse_chord("Return") == [28]
    assert keys.text_strokes("/Ab?") == [(53, False), (30, True), (48, False), (53, True)]
    with pytest.raises(ValueError, match="£"):
        keys.text_strokes("£5")


def test_hyprland_parses_clients_and_uses_lua_dispatch():
    fake = FakeHyprctl([CLIENT])
    hypr = Hyprland(fake)
    (w,) = hypr.windows()
    assert (w.cls, w.x, w.y, w.width, w.height, w.focused) == ("battle.net.exe", 2527, 966, 1266, 684, True)
    hypr.move_cursor(3503, 1081)
    assert fake.calls[-1] == ["dispatch", "hl.dsp.cursor.move({x=3503,y=1081})"]


def test_hyprland_falls_back_to_string_dispatch_on_older_versions():
    fake = FakeHyprctl([CLIENT], lua=False)
    Hyprland(fake).move_cursor(1, 2)
    assert fake.calls[-1] == ["dispatch", "movecursor", "1 2"]


class FakeInput:
    def __init__(self):
        self.events = []

    def nudge(self):
        self.events.append("nudge")

    def click(self, button, count):
        self.events.append(("click", button, count))

    def close(self):
        pass


def test_click_moves_to_the_screenshot_point_then_clicks(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake, inp = FakeHyprctl([CLIENT]), FakeInput()
    desk = Desk(Hyprland(fake), lambda: inp)
    screen.remember_scale("0xabc", 0.5)
    assert desk.click("battle", 100, 50) == (2527 + 200, 966 + 100)
    assert ["dispatch", "hl.dsp.cursor.move({x=2727,y=1066})"] in fake.calls
    assert inp.events == ["nudge", ("click", "left", 1)]


def test_visible_compares_the_window_workspace_with_the_monitors():
    fake = FakeHyprctl([CLIENT])
    hypr = Hyprland(fake)
    (w,) = hypr.windows()
    assert hypr.visible(w)
    fake.showing = "2"
    assert not hypr.visible(w)
