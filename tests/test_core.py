import json

import pytest

from deskpilot import keys, screen
from deskpilot.desk import Desk
from deskpilot.hypr import (
    AmbiguousWindow,
    HyprctlError,
    Hyprland,
    Window,
    WindowNotFound,
    find_window,
    pick_instance,
    run_hyprctl,
)
from deskpilot.plugin import REQUEST_BYTES, Plugin, PluginError, parse_reply, request_size


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
    with pytest.raises(AmbiguousWindow, match=r"0x1.*0x2"):
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


class FakeCursor:
    def __init__(self):
        self.glides = []

    def glide(self, x, y):
        self.glides.append((x, y))

    def close(self):
        pass


class FakePlugin:
    def __init__(self):
        self.calls = []

    def click(self, *args):
        self.calls.append(("click", *args))

    def scroll(self, *args):
        self.calls.append(("scroll", *args))

    def keys(self, *args, **timing):
        self.calls.append(("keys", *args, timing))


def make_desk(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake, inp, plugin, cursor = FakeHyprctl([CLIENT]), FakeInput(), FakePlugin(), FakeCursor()
    desk = Desk(Hyprland(fake), lambda: inp, plugin=plugin, cursor=cursor)
    screen.remember_scale("0xabc", 0.5)
    return desk, fake, inp, plugin, cursor


def test_click_goes_through_the_plugin_and_leaves_the_pointer_alone(tmp_path, monkeypatch):
    desk, fake, inp, plugin, cursor = make_desk(tmp_path, monkeypatch)
    assert desk.click("battle", 100, 50, "right", 2) == (2527 + 200, 966 + 100)
    assert plugin.calls == [("click", "0xabc", 2727, 1066, "right", 2)]
    assert cursor.glides == [(2727, 1066)]
    assert inp.events == []
    assert not any(c[0] == "dispatch" and "cursor.move" in c[1] for c in fake.calls)
    desk.scroll("battle", 100, 50, -3)
    assert plugin.calls[-1] == ("scroll", "0xabc", 2727, 1066, -3)


def test_take_pointer_moves_the_user_pointer_then_clicks(tmp_path, monkeypatch):
    desk, fake, inp, plugin, _ = make_desk(tmp_path, monkeypatch)
    desk.click("battle", 100, 50, take_pointer=True)
    assert ["dispatch", "hl.dsp.cursor.move({x=2727,y=1066})"] in fake.calls
    assert inp.events == ["nudge", ("click", "left", 1)]
    assert plugin.calls == []


def test_move_only_glides_the_agent_cursor(tmp_path, monkeypatch):
    desk, fake, _, _, cursor = make_desk(tmp_path, monkeypatch)
    desk.move("battle", 100, 50)
    assert cursor.glides == [(2727, 1066)]
    assert not any(c[0] == "dispatch" and "cursor.move" in c[1] for c in fake.calls)


def test_keys_go_to_the_window_without_focusing_it(tmp_path, monkeypatch):
    desk, fake, inp, plugin, _ = make_desk(tmp_path, monkeypatch)
    desk.key("ctrl+a", "battle")
    desk.type("Hi", "battle", delay=0.05)
    a, h, i = keys.key_code("a"), keys.key_code("h"), keys.key_code("i")
    assert plugin.calls == [
        ("keys", "0xabc", [(keys.CTRL, True), (a, True), (a, False), (keys.CTRL, False)], {}),
        # The whole text is one action, timed inside the plugin, so no other caller's keys land between characters.
        (
            "keys",
            "0xabc",
            [(keys.SHIFT, True), (h, True), (h, False), (keys.SHIFT, False), (i, True), (i, False)],
            {"hold": 0.05, "gap": 0.05},
        ),
    ]
    assert not any(c[0] == "dispatch" and "focus" in c[1] for c in fake.calls)
    assert inp.events == []


def test_plugin_formats_key_events():
    calls = []
    Plugin(lambda args: calls.append(args) or '{"ok":true}').keys("0xabc", [(29, True), (30, True)], hold=0.03, gap=0.1)
    assert calls == [["deskpilot", "keys", "0xabc", "30", "100", "none", "+29", "+30"]]


def test_plugin_stages_a_long_key_sequence_and_queues_it_whole():
    calls = []
    events = [(30, down) for _ in range(5000) for down in (True, False)]
    Plugin(lambda args: calls.append(args) or '{"ok":true}').keys("0xabc", events)
    *stages, last = calls
    assert stages and all(c[:2] == ["deskpilot", "stage"] for c in stages)
    txn = stages[0][2]
    assert all(c[2] == txn for c in stages) and last[:6] == ["deskpilot", "keys", "0xabc", "0", "0", txn]
    sent = [w for c in stages for w in c[3:]] + last[6:]
    assert sent == [f"{'+' if down else '-'}{code}" for code, down in events]
    assert all(request_size(c[1:]) <= REQUEST_BYTES for c in calls)
    # The fewest requests: every one but the first staged is full, and the keys request takes all that fit after it.
    assert len(calls) == -(-request_size(["keys", "0xabc", "0", "0", txn, *sent]) // REQUEST_BYTES)


def test_plugin_waits_on_a_queued_action_until_it_finishes():
    replies = iter(['{"ok":true,"pending":7}', '{"ok":true,"pending":7}', '{"ok":true,"x":1,"y":2}'])
    calls = []
    reply = Plugin(lambda args: calls.append(args) or next(replies)).click("0xabc", 1, 2)
    assert reply == {"ok": True, "x": 1, "y": 2}
    assert calls[1:] == [["deskpilot", "wait", "7"], ["deskpilot", "wait", "7"]]
    with pytest.raises(PluginError, match="queue is full"):
        Plugin(lambda args: '{"ok":false,"error":"the deskpilot queue is full"}').scroll("0xabc", 1, 2, 3)


def test_plugin_sends_button_codes_and_reads_replies():
    calls = []

    def run(args):
        calls.append(args)
        return '{"ok":true,"x":1,"y":2}'

    Plugin(run).click("0xabc", 10, 20, "right", 2)
    assert calls[-1] == ["deskpilot", "click", "0xabc", "10", "20", "273", "2"]
    with pytest.raises(PluginError, match="not loaded"):
        parse_reply("unknown request")
    with pytest.raises(PluginError, match="is on top"):
        parse_reply('{"ok":false,"error":"window 0x1 (x) is on top at 1,2"}')
    assert not Plugin(lambda args: "unknown request").loaded()


def sockets(tmp_path, folders, live):
    """Instance folders with socket files; only those in `live` accept a connection."""
    for name in folders:
        (tmp_path / name).mkdir(exist_ok=True)
        (tmp_path / name / ".socket.sock").touch()
    return lambda path: path.parent.name in live


def test_pick_instance_keeps_a_live_signature(tmp_path):
    assert pick_instance("a", tmp_path, sockets(tmp_path, ["a", "b"], {"a", "b"})) == "a"


def test_pick_instance_falls_back_to_the_one_live_socket(tmp_path):
    alive = sockets(tmp_path, ["stale", "live"], {"live"})
    assert pick_instance("stale", tmp_path, alive) == "live"
    assert pick_instance(None, tmp_path, alive) == "live"


def test_pick_instance_names_every_live_socket_or_none(tmp_path):
    with pytest.raises(HyprctlError, match=r"stale has no live socket.*one: x, y"):
        pick_instance("stale", tmp_path, sockets(tmp_path, ["stale", "x", "y"], {"x", "y"}))
    with pytest.raises(HyprctlError, match="No running Hyprland"):
        pick_instance("stale", tmp_path, sockets(tmp_path, ["stale"], set()))


def test_run_hyprctl_reports_hyprctls_own_error(monkeypatch):
    import subprocess

    from deskpilot import hypr

    monkeypatch.setattr(hypr, "current_instance", lambda: "sig")

    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["hyprctl", "-i", "sig"]
        return subprocess.CompletedProcess(cmd, 3, "", "Couldn't connect to socket\nmore\n")

    monkeypatch.setattr(hypr.subprocess, "run", fake_run)
    with pytest.raises(HyprctlError, match=r"exited 3: Couldn't connect to socket$"):
        run_hyprctl(["-j", "clients"])


def test_visible_compares_the_window_workspace_with_the_monitors():
    fake = FakeHyprctl([CLIENT])
    hypr = Hyprland(fake)
    (w,) = hypr.windows()
    assert hypr.visible(w)
    fake.showing = "2"
    assert not hypr.visible(w)
