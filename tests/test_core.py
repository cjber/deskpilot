import json
import pathlib
import subprocess
from dataclasses import asdict, replace

import pytest
from PIL import Image

from deskpilot import clipboard, keys, screen
from deskpilot.desk import Desk, NotVisible
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
from deskpilot.plugin import REQUEST_BYTES, Plugin, PluginError, parse_reply, request_size, source_hash


def win(address="0x1", cls="wow.exe", title="World of Warcraft", x=1440, y=560, w=3440, h=1440):
    return Window(address, cls, title, x, y, w, h, 1, "1", True, 10)


class FakeHyprctl:
    """Answers hyprctl like Hyprland 0.57: Lua dispatchers only, string ones are a Lua syntax error."""

    def __init__(self, clients, lua=True):
        self.clients, self.lua, self.calls, self.showing, self.special = clients, lua, [], "1", ""

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["-j", "clients"]:
            return json.dumps(self.clients)
        if args[:2] == ["-j", "monitors"]:
            return json.dumps(
                [
                    {
                        "name": "DP-1",
                        "x": 1440,
                        "y": 560,
                        "width": 3440,
                        "height": 1440,
                        "activeWorkspace": {"name": self.showing},
                        "specialWorkspace": {"name": self.special},
                    }
                ]
            )
        if args[:2] == ["-j", "activewindow"]:
            return json.dumps(self.clients[0] if self.clients else {})
        if args[0] == "dispatch":
            is_lua = args[1].startswith("hl.")
            if "focus" in args[1]:
                self.showing = "1"
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


def fake_grim(monkeypatch, size):
    """grim writing a PNG of `size`, as a toplevel capture of a window's buffer does."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        Image.new("RGB", size).save(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(screen.subprocess, "run", run)
    return calls


def test_shot_reads_its_scale_from_the_image_and_shrinks_wide_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    window = win(w=1720, h=720)
    # A scale 2 output: the buffer is twice the window's logical size.
    calls = fake_grim(monkeypatch, (3440, 1440))
    shot = screen.capture(replace(window, stable_id="18000377"), max_width=1376)
    assert calls[0][:3] == ["grim", "-T", "18000377"]
    assert (shot.width, shot.height, shot.scale) == (1376, 576, 0.8)
    assert Image.open(shot.path).size == (1376, 576)
    assert screen.latest("0x1") == shot == screen.load(shot.id)


def test_shot_without_a_toplevel_id_captures_the_area_at_an_explicit_scale(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    calls = fake_grim(monkeypatch, (100, 50))
    shot = screen.capture(win(w=100, h=50))
    assert calls[0][:5] == ["grim", "-s", "1", "-g", "1440,560 100x50"]
    assert shot.scale == 1.0


def test_a_shot_is_refused_for_another_window_or_a_resized_one(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake_grim(monkeypatch, (100, 50))
    shot = screen.capture(win(w=100, h=50))
    shot.check(win(w=100, h=50))
    with pytest.raises(screen.StaleShot, match="100x60 now, 100x50 in shot"):
        shot.check(win(w=100, h=60))
    with pytest.raises(screen.StaleShot, match="not 0x2"):
        shot.check(win(address="0x2", w=100, h=50))
    with pytest.raises(screen.StaleShot, match="no shot nope"):
        screen.load("nope")


def test_old_shots_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake_grim(monkeypatch, (10, 10))
    ids = []
    for t in range(screen.KEEP + 2):
        monkeypatch.setattr(screen.time, "time_ns", lambda t=t: (1_000_000 + t) * 1_000_000)
        ids.append(screen.capture(win(w=10, h=10)).id)
    with pytest.raises(screen.StaleShot):
        screen.load(ids[0])
    assert screen.load(ids[-1]).id == ids[-1]
    assert len(list(screen.shots_dir().glob("*.png"))) == screen.KEEP


def test_chords_name_keys_and_text_names_characters():
    """Characters go to the plugin as code points, resolved there on the user's keymap, so any layout types them."""
    assert keys.chord_words("ctrl+shift+T") == ["+29", "+42", "p74", "r74", "-42", "-29"]
    assert keys.chord_words("Return") == ["+28", "-28"]
    assert keys.chord_words("A") == ["t41"] and keys.chord_words("+") == ["t2b"]
    assert keys.text_words("é\n") == ["te9", "ta"]
    # hyprctl takes any word that starts with '-' and is not a number as one of its own flags.
    words = keys.chord_words("ctrl+alt+shift+super+a+F4") + keys.text_words("-x")
    assert all(not w.startswith("-") or w[1:].isdigit() for w in words)
    with pytest.raises(ValueError, match="unknown key 'hyper'"):
        keys.chord_words("ctrl+hyper")


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
    remember_shot(CLIENT, 0.5)
    return desk, fake, inp, plugin, cursor


def remember_shot(client, scale):
    """A shot of `client` at `scale`, as `deskpilot shot` would have left it."""
    folder = screen.shots_dir()
    folder.mkdir(parents=True, exist_ok=True)
    w, h = client["size"]
    shot = screen.Shot("abc-1", "", client["address"], round(w * scale), round(h * scale), scale, w, h)
    (folder / "abc-1.json").write_text(json.dumps(asdict(shot)))


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
    assert plugin.calls == [
        ("keys", "0xabc", ["+29", "p61", "r61", "-29"], {}),
        # The whole text is one action, timed inside the plugin, so no other caller's keys land between characters.
        ("keys", "0xabc", ["t48", "t69"], {"hold": 0.05, "gap": 0.05}),
    ]
    assert not any(c[0] == "dispatch" and "focus" in c[1] for c in fake.calls)
    assert inp.events == []


def test_keys_without_a_window_go_to_the_focused_one_and_take_focus_focuses_first(tmp_path, monkeypatch):
    desk, fake, _, plugin, _ = make_desk(tmp_path, monkeypatch)
    desk.key("Return")
    assert plugin.calls[-1] == ("keys", "0xabc", ["+28", "-28"], {})
    desk.type("x", "battle", take_focus=True)
    assert ["dispatch", 'hl.dsp.focus({window="address:0xabc"})'] in fake.calls
    assert plugin.calls[-1][:3] == ("keys", "0xabc", ["t78"])


def test_plugin_formats_key_events():
    calls = []
    Plugin(lambda args: calls.append(args) or '{"ok":true}').keys("0xabc", ["+29", "t61"], hold=0.03, gap=0.1)
    assert calls == [["deskpilot", "keys", "0xabc", "30", "100", "none", "+29", "t61"]]


def test_paste_sends_its_text_in_a_file_the_request_names_and_removes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    seen = []

    def run(args):
        seen.append((args, pathlib.Path(args[3]).read_text()))
        return '{"ok":true}'

    Plugin(run).keys("0xabc", ["p76"], paste="naïve 日本 ✓")
    ((args, text),) = seen
    assert args[:3] == ["deskpilot", "paste", "0xabc"] and args[4:] == ["0", "0", "none", "p76"]
    assert text == "naïve 日本 ✓" and not pathlib.Path(args[3]).exists()


def test_plugin_stages_a_long_key_sequence_and_queues_it_whole():
    calls = []
    events = [word for _ in range(5000) for word in ("+30", "-30")]
    Plugin(lambda args: calls.append(args) or '{"ok":true}').keys("0xabc", events)
    *stages, last = calls
    assert stages and all(c[:2] == ["deskpilot", "stage"] for c in stages)
    txn = stages[0][2]
    assert all(c[2] == txn for c in stages) and last[:6] == ["deskpilot", "keys", "0xabc", "0", "0", txn]
    sent = [w for c in stages for w in c[3:]] + last[6:]
    assert sent == events
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


def test_a_refusal_from_a_stale_plugin_names_the_build_and_the_fix():
    def run(args, version):
        if args[1] == "status":
            return f'{{"ok":true,"version":"{version}"}}'
        return '{"ok":false,"error":"hold and gap must be 0 to 10000 milliseconds"}'

    with pytest.raises(PluginError, match=r"build 000000000000, not this CLI's .*deskpilot plugin load.*hold and gap"):
        Plugin(lambda args: run(args, "000000000000")).keys("0xabc", ["+28", "-28"])
    with pytest.raises(PluginError, match=r"^hold and gap must be"):
        Plugin(lambda args: run(args, source_hash())).keys("0xabc", ["+28", "-28"])


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


def test_a_window_is_visible_only_on_a_monitor_showing_its_workspace():
    fake = FakeHyprctl([CLIENT])
    hypr = Hyprland(fake)
    assert hypr.windows()[0].visible
    fake.showing = "2"
    assert not hypr.windows()[0].visible
    fake.showing, fake.special = "1", "special:term"
    assert not hypr.windows()[0].visible, "an open special workspace covers the normal one"


def test_a_column_scrolled_off_every_monitor_is_not_visible():
    fake = FakeHyprctl([{**CLIENT, "at": [-766, 572]}, {**CLIENT, "address": "0xdef", "at": [1000, 572]}])
    off, partly = Hyprland(fake).windows()
    assert not off.visible
    assert partly.visible, "1000 + 1266 reaches past the monitor's left edge at 1440"


def test_a_rotated_or_scaled_monitor_covers_its_logical_area():
    from deskpilot.hypr import Monitor

    rotated = Monitor.from_json(
        {"x": 0, "y": 0, "width": 2560, "height": 1440, "transform": 1, "activeWorkspace": {"name": "4"}}
    )
    assert (rotated.width, rotated.height) == (1440, 2560)
    scaled = Monitor.from_json(
        {"x": 0, "y": 0, "width": 3840, "height": 2160, "scale": 2, "activeWorkspace": {"name": "1"}}
    )
    assert (scaled.width, scaled.height) == (1920, 1080)


def test_a_hidden_window_is_refused_unless_shown(tmp_path, monkeypatch):
    desk, fake, _, plugin, _ = make_desk(tmp_path, monkeypatch)
    fake.showing = "2"
    with pytest.raises(NotVisible, match="workspace 1 is not showing"):
        desk.click("battle", 100, 50)
    assert plugin.calls == []
    assert not any(c[0] == "dispatch" for c in fake.calls), "refusing must not focus anything"
    desk.click("battle", 100, 50, show=True)
    assert ["dispatch", 'hl.dsp.focus({window="address:0xabc"})'] in fake.calls
    assert plugin.calls == [("click", "0xabc", 2727, 1066, "left", 1)]


def test_a_click_against_a_resized_window_is_refused(tmp_path, monkeypatch):
    desk, fake, _, _, _ = make_desk(tmp_path, monkeypatch)
    fake.clients[0] = {**CLIENT, "size": [1000, 684]}
    with pytest.raises(screen.StaleShot, match="take a new shot"):
        desk.click("battle", 100, 50)
    assert desk.click("battle", 100, 50, scale=1) == (2627, 1016)


def test_a_missing_plugin_is_loaded_and_the_command_sent_again(monkeypatch):
    replies = iter(["unknown request", '{"ok":true,"x":1,"y":2}'])
    plugin = Plugin(run=lambda args: next(replies))
    loads = []
    monkeypatch.setattr(Plugin, "load", lambda self: loads.append(1))
    assert plugin.click("0xabc", 1, 2) == {"ok": True, "x": 1, "y": 2}
    assert loads == [1]


def test_mcp_tools_report_why_an_action_was_refused(tmp_path, monkeypatch):
    """The SDK hides any exception other than ToolError behind `Error executing tool <name>`."""
    import asyncio

    from mcp.server.mcpserver.exceptions import ToolError

    from deskpilot import mcp_server

    desk, fake, _, _, _ = make_desk(tmp_path, monkeypatch)
    fake.showing = "2"
    monkeypatch.setattr(mcp_server, "desk", desk)
    with pytest.raises(ToolError, match="is not on screen"):
        asyncio.run(mcp_server.mcp.call_tool("click", {"window": "battle", "x": 1, "y": 1}))


def test_clipboard_reads_plain_text_and_not_another_text_type(monkeypatch):
    offered, asked = ["text/x-moz-url", "text/html"], []

    def wl_paste(cmd, **kwargs):
        if cmd[1] == "--list-types":
            return subprocess.CompletedProcess(cmd, 0, "\n".join(offered), "")
        asked.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "café".encode(), b"")

    monkeypatch.setattr(clipboard.subprocess, "run", wl_paste)
    with pytest.raises(clipboard.ClipboardError, match="no plain text; it offers text/x-moz-url, text/html"):
        clipboard.read()
    offered.append("text/plain")
    assert clipboard.read() == "café" and asked == ["text/plain"]


def test_wait_for_returns_once_the_window_has_the_title_and_size_asked(tmp_path, monkeypatch):
    desk, fake, *_ = make_desk(tmp_path, monkeypatch)
    changes = iter([{"title": "Battle.net - Login"}, {"size": [800, 600]}])
    monkeypatch.setattr("deskpilot.desk.time.sleep", lambda _: fake.clients[0].update(next(changes)))
    win = desk.wait_for("battle", 5, title="Login", size=(800, 600))
    assert (win.title, win.width, win.height) == ("Battle.net - Login", 800, 600)
    with pytest.raises(TimeoutError, match="is not 1x1 after 0s"):
        desk.wait_for("battle", 0, title="Login", size=(1, 1))


def test_wait_changed_returns_a_shot_of_the_change_and_where_it_is(tmp_path, monkeypatch):
    desk, *_ = make_desk(tmp_path, monkeypatch)
    frames = iter([None, None, (10, 20), (10, 20)])

    def grim(cmd, **kwargs):
        image = Image.new("RGB", (1266, 684))
        if (dot := next(frames)) is not None:
            image.putpixel(dot, (255, 255, 255))
        image.save(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(screen.subprocess, "run", grim)
    monkeypatch.setattr("deskpilot.desk.time.sleep", lambda _: None)
    base = desk.shot("battle")
    shot, box = desk.wait_changed("battle", base.id, 5)
    assert box == (10, 20, 11, 21) and Image.open(shot.path).getpixel((10, 20)) == (255, 255, 255)
    assert [r.stem for r in sorted(screen.shots_dir().glob("*.json"))] == sorted(["abc-1", base.id, shot.id])
    with pytest.raises(ValueError, match="not inside shot"):
        desk.wait_changed("battle", base.id, 5, (1200, 0, 100, 10))
    with pytest.raises(TimeoutError, match="did not change from shot"):
        desk.wait_changed("battle", shot.id, 0)
