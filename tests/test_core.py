import json
import pathlib
import subprocess
from dataclasses import asdict, replace

import pytest
from PIL import Image

from deskpilot import a11y, clipboard, keys, screen, session
from deskpilot import desk as desk_module
from deskpilot import plugin as plugin_module
from deskpilot.desk import Desk, NotVisible, error_code
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
from deskpilot.plugin import (
    REQUEST_BYTES,
    Plugin,
    PluginError,
    PluginNotLoaded,
    parse_reply,
    request_size,
    source_hash,
)


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
                        "id": 1,
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


def test_a_capture_that_gets_no_frame_is_a_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(screen.subprocess, "run", run)
    with pytest.raises(TimeoutError, match="no frame") as error:
        screen.capture(replace(win(), stable_id="18000377"))
    assert error_code(error.value) == "timeout"


def test_a_hidden_shot_of_a_window_outside_its_monitor_is_refused_before_grim(tmp_path, monkeypatch):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    monkeypatch.setattr(desk_module, "launched_by", lambda pid, token: True)
    shot = []
    monkeypatch.setattr(screen, "capture", lambda win, out, max_width: shot.append(win.address))
    fake.clients = [AGENT_CLIENT]
    desk.shot("0xdef", hidden=True)
    assert shot == ["0xdef"], "a window on a hidden workspace still lies in its monitor's area"
    # A scrolling layout puts a later column of the agent's workspace off its monitor.
    fake.clients = [{**AGENT_CLIENT, "at": [-1500, 966]}]
    with pytest.raises(NotVisible, match="outside its monitor's area") as error:
        desk.shot("0xdef", hidden=True)
    assert error_code(error.value) == "not_visible" and shot == ["0xdef"]


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


class FakePlugin:
    agent = "tester"

    def __init__(self):
        self.calls = []
        self.mapped: list[dict] = []  # the window's popups

    def register(self, owner=None):
        self.calls.append(("register", owner))
        return {"ok": True, "workspace": "special:agent-tester", "token": "deskpilot-agent-tester"}

    def stop(self):
        self.calls.append(("stop",))

    def keys(self, *args, **timing):
        self.calls.append(("keys", *args, timing))

    def path(self, *args):
        self.calls.append(("path", *args))
        return {"ok": True, "ticket": 9}

    def cursor(self, *args):
        self.calls.append(("cursor", *args))

    def end(self, ticket):
        self.calls.append(("end", ticket))

    def popups(self, address):
        self.calls.append(("popups", address))
        return self.mapped


def make_desk(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.delenv("DESKPILOT_CURSOR", raising=False)
    fake, inp, plugin = FakeHyprctl([CLIENT]), FakeInput(), FakePlugin()
    desk = Desk(Hyprland(fake), lambda: inp, plugin=plugin)
    remember_shot(CLIENT, 0.5)
    return desk, fake, inp, plugin


def remember_shot(client, scale):
    """A shot of `client` at `scale`, as `deskpilot shot` would have left it."""
    folder = screen.shots_dir()
    folder.mkdir(parents=True, exist_ok=True)
    w, h = client["size"]
    shot = screen.Shot(
        "abc-1",
        "",
        client["address"],
        round(w * scale),
        round(h * scale),
        scale,
        w,
        h,
        pid=client["pid"],
        cls=client["class"],
    )
    (folder / "abc-1.json").write_text(json.dumps(asdict(shot)))


def test_click_goes_through_the_plugin_and_leaves_the_pointer_alone(tmp_path, monkeypatch):
    desk, fake, inp, plugin = make_desk(tmp_path, monkeypatch)
    assert desk.click("battle", 100, 50, "right", 2) == (2527 + 200, 966 + 100)
    assert plugin.calls == [("path", "0xabc", "shown", ["m2727,1066", "d52", "p273", "r273", "p273", "r273"])]
    assert inp.events == []
    assert not any(c[0] == "dispatch" and "cursor.move" in c[1] for c in fake.calls)
    desk.scroll("battle", 100, 50, -3)
    assert plugin.calls[-1] == ("path", "0xabc", "shown", ["m2727,1066", "d52", "w-3"])
    with pytest.raises(ValueError):
        desk.scroll("battle", 100, 50, 51)


AGENT_CLIENT = {**CLIENT, "address": "0xdef", "workspace": {"name": "special:agent-tester"}}


def launched_desk(tmp_path, monkeypatch):
    """A desk whose one window the agent launched, for hidden actions."""
    desk, fake, inp, plugin = make_desk(tmp_path, monkeypatch)
    fake.clients = [AGENT_CLIENT]
    monkeypatch.setattr(desk_module, "launched_by", lambda pid, token: True)
    return desk, fake, inp, plugin


def test_hidden_actions_drive_only_the_agent_s_launched_windows(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    # A window the user moved onto the agent's workspace has no launch token.
    moved = {**CLIENT, "address": "0x123", "pid": 8, "workspace": {"name": "special:agent-tester"}}
    fake.clients += [AGENT_CLIENT, moved]
    monkeypatch.setattr(desk_module, "launched_by", lambda pid, token: pid == 7 and token == "deskpilot-agent-tester")
    with pytest.raises(WindowNotFound, match="deskpilot launch"):
        desk.click("0x123", 100, 50, hidden=True)
    plugin.calls.clear()
    desk.click("0xdef", 100, 50, scale=0.5, hidden=True)
    assert plugin.calls == [("register", None), ("path", "0xdef", "hidden", ["m2727,1066", "d52", "p272", "r272"])]
    assert not any(c[0] == "dispatch" for c in fake.calls), "hidden input must not focus or show anything"
    with pytest.raises(WindowNotFound, match="deskpilot launch"):
        desk.click("0xabc", 100, 50, hidden=True)
    with pytest.raises(ValueError):
        desk.click("0xdef", 100, 50, hidden=True, show=True)
    with pytest.raises(ValueError):
        desk.click("0xdef", 100, 50, hidden=True, take_pointer=True)
    with pytest.raises(ValueError):
        desk.click("0xdef", 100, 50, hidden=True, take_focus=True)


def test_launch_tags_the_app_for_the_agent_s_workspace_and_returns_its_window(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    desk.owner = 4242
    fake.clients.append({**AGENT_CLIENT, "stableId": "1"})
    monkeypatch.setenv("HL_INITIAL_WORKSPACE_TOKEN", "the-terminal-s")
    started = []

    class FakePopen:
        def __init__(self, command, env, **kwargs):
            started.append((command, env, kwargs))
            # Hyprland gives the new window the address of one closed before, with a new stable id.
            fake.clients[:] = [CLIENT, {**AGENT_CLIENT, "stableId": "2"}]

        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    assert desk.launch(["gnome-calculator"]).address == "0xdef"
    ((command, env, kwargs),) = started
    assert command == ["gnome-calculator"] and kwargs["start_new_session"]
    assert env["HL_EXEC_RULE_TOKEN"] == "deskpilot-agent-tester" and "HL_INITIAL_WORKSPACE_TOKEN" not in env
    assert plugin.calls == [("register", 4242)]
    desk.stop()
    assert plugin.calls[-1] == ("stop",)


def test_cursor_off_sends_plain_paths(tmp_path, monkeypatch):
    desk, _, _, plugin = make_desk(tmp_path, monkeypatch)
    monkeypatch.setenv("DESKPILOT_CURSOR", "off")
    desk.click("battle", 100, 50)
    assert plugin.calls[-1][2] == "plain"


def test_take_pointer_moves_the_user_pointer_then_clicks(tmp_path, monkeypatch):
    desk, fake, inp, plugin = make_desk(tmp_path, monkeypatch)
    desk.click("battle", 100, 50, take_pointer=True)
    assert ["dispatch", "hl.dsp.cursor.move({x=2727,y=1066})"] in fake.calls
    assert inp.events == ["nudge", ("click", "left", 1)]
    assert plugin.calls == []


def test_move_only_glides_the_agent_cursor(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    desk.move("battle", 100, 50)
    assert plugin.calls == [("cursor", 2727, 1066)]
    assert not any(c[0] == "dispatch" and "cursor.move" in c[1] for c in fake.calls)


def test_keys_go_to_the_window_without_focusing_it(tmp_path, monkeypatch):
    desk, fake, inp, plugin = make_desk(tmp_path, monkeypatch)
    desk.key("ctrl+a", "battle")
    desk.type("Hi", "battle", delay=0.05)
    assert plugin.calls == [
        ("keys", "0xabc", ["+29", "p61", "r61", "-29"], {"hold": 0.0, "mode": "plain"}),
        # The whole text is one action, timed inside the plugin, so no other caller's keys land between characters.
        ("keys", "0xabc", ["t48", "t69"], {"hold": 0.05, "gap": 0.05, "mode": "plain"}),
    ]
    assert not any(c[0] == "dispatch" and "focus" in c[1] for c in fake.calls)
    assert inp.events == []


def test_click_with_take_focus_focuses_the_window_before_the_plugin_clicks(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    desk.click("battle", 10, 10, take_focus=True)
    focus = ["dispatch", 'hl.dsp.focus({window="address:0xabc"})']
    assert focus in fake.calls and plugin.calls[-1][0] == "path"
    desk.click("battle", 10, 10)
    assert fake.calls.count(focus) == 1


def test_keys_without_a_window_go_to_the_focused_one_and_take_focus_focuses_first(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    desk.key("Return")
    assert plugin.calls[-1] == ("keys", "0xabc", ["+28", "-28"], {"hold": 0.0, "mode": "plain"})
    desk.type("x", "battle", take_focus=True)
    assert ["dispatch", 'hl.dsp.focus({window="address:0xabc"})'] in fake.calls
    assert plugin.calls[-1][:3] == ("keys", "0xabc", ["t78"])


def test_plugin_formats_key_events():
    calls = []
    Plugin(lambda args: calls.append(args) or '{"ok":true}').keys("0xabc", ["+29", "t61"], hold=0.03, gap=0.1)
    assert calls == [["deskpilot", "keys", "0xabc", "plain", "30", "100", "none", "+29", "t61"]]


def test_plugin_puts_the_key_mode_after_the_window():
    calls = []
    Plugin(lambda args: calls.append(args) or '{"ok":true}').keys("0xabc", ["+28", "-28"], mode="hidden")
    assert calls == [["deskpilot", "keys", "0xabc", "hidden", "0", "0", "none", "+28", "-28"]]


def test_hidden_keys_name_the_mode_and_never_focus(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    fake.clients.append(AGENT_CLIENT)
    monkeypatch.setattr(desk_module, "launched_by", lambda pid, token: True)
    desk.key("Return", "0xdef", hidden=True)
    desk.type("x", "0xdef", hidden=True)
    desk.paste("hi", "0xdef", hidden=True)
    sent = [c for c in plugin.calls if c[0] == "keys"]
    assert [(c[1], c[-1]["mode"]) for c in sent] == [("0xdef", "hidden")] * 3
    assert not any(c[0] == "dispatch" for c in fake.calls), "hidden keys must not focus or show anything"


def test_hidden_keys_to_a_window_the_agent_did_not_launch_are_not_found(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    fake.showing = "2"
    for send in (desk.key, desk.type, desk.paste):
        with pytest.raises(WindowNotFound, match="deskpilot launch") as error:
            send("a", "battle", hidden=True)
        assert error_code(error.value) == "not_found"
    assert not any(c[0] == "keys" for c in plugin.calls)


def test_keys_to_an_off_screen_window_are_refused_unless_shown(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    fake.showing = "2"
    for send in (desk.key, desk.type, desk.paste):
        with pytest.raises(NotVisible):
            send("a", "battle")
    assert plugin.calls == [] and not any(c[0] == "dispatch" for c in fake.calls)
    desk.key("a", "battle", show=True)
    assert ["dispatch", 'hl.dsp.focus({window="address:0xabc"})'] in fake.calls
    assert plugin.calls[-1][-1]["mode"] == "plain"


def test_hidden_or_shown_keys_need_a_window_and_no_take_focus(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        desk.key("Return", hidden=True)
    with pytest.raises(ValueError):
        desk.type("x", "battle", take_focus=True, hidden=True)
    with pytest.raises(ValueError, match="show"):
        desk.key("Return", show=True)
    with pytest.raises(ValueError, match="show"):
        desk.paste("x", "battle", take_focus=True, show=True)
    assert plugin.calls == [] and not any(c[0] == "dispatch" for c in fake.calls)


def test_paste_sends_its_text_in_a_file_the_request_names_and_removes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    seen = []

    def run(args):
        seen.append((args, pathlib.Path(args[4]).read_text()))
        return '{"ok":true}'

    Plugin(run).keys("0xabc", ["p76"], paste="naïve 日本 ✓")
    ((args, text),) = seen
    assert args[:4] == ["deskpilot", "paste", "0xabc", "plain"] and args[5:] == ["0", "0", "none", "p76"]
    assert text == "naïve 日本 ✓" and not pathlib.Path(args[4]).exists()


def test_a_paste_into_the_app_that_owns_the_clipboard_is_refused_with_its_own_code(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    files = []

    def run(args):
        if args[1] == "paste":
            files.append(pathlib.Path(args[4]))
            return '{"ok":true,"pending":3}'
        return '{"ok":false,"error":"this window\'s app holds the user\'s clipboard","code":"owns_clipboard"}'

    with pytest.raises(PluginError, match="holds the user's clipboard") as error:
        Plugin(run).keys("0xabc", ["p76"], paste="text")
    assert error_code(error.value) == "owns_clipboard"
    assert not files[0].exists()


def test_plugin_stages_a_long_key_sequence_and_queues_it_whole():
    calls = []
    events = [word for _ in range(5000) for word in ("+30", "-30")]
    Plugin(lambda args: calls.append(args) or '{"ok":true}').keys("0xabc", events, mode="hidden")
    *stages, last = calls
    assert stages and all(c[:2] == ["deskpilot", "stage"] for c in stages)
    txn = stages[0][2]
    assert all(c[2] == txn for c in stages) and last[:7] == ["deskpilot", "keys", "0xabc", "hidden", "0", "0", txn]
    sent = [w for c in stages for w in c[3:]] + last[7:]
    assert sent == events
    assert all(request_size(c[1:]) <= REQUEST_BYTES for c in calls)
    # The fewest requests: every one but the first staged is full, and the keys request takes all that fit after it.
    assert len(calls) == -(-request_size(["keys", "0xabc", "hidden", "0", "0", txn, *sent]) // REQUEST_BYTES)


def test_plugin_waits_on_a_queued_action_until_it_finishes():
    replies = iter(['{"ok":true,"pending":7}', '{"ok":true,"pending":7}', '{"ok":true,"x":1,"y":2}'])
    calls = []
    reply = Plugin(lambda args: calls.append(args) or next(replies)).path("0xabc", "shown", ["m1,2"])
    assert reply == {"ok": True, "x": 1, "y": 2}
    assert calls[1:] == [["deskpilot", "wait", "7"], ["deskpilot", "wait", "7"]]
    with pytest.raises(PluginError, match="queue is full"):
        Plugin(lambda args: '{"ok":false,"error":"the deskpilot queue is full"}').cursor(1, 2)


def test_each_agent_names_its_own_cursor(monkeypatch):
    calls = []
    run = lambda args: calls.append(args) or '{"ok":true}'  # noqa: E731
    monkeypatch.delenv("DESKPILOT_AGENT", raising=False)
    Plugin(run).cursor(1, 2)
    monkeypatch.setenv("DESKPILOT_AGENT", "CLAUDE-1")
    Plugin(run).cursor(1, 2)
    Plugin(run).path("0xabc", "shown", ["m1,2"])
    assert calls == [
        ["deskpilot", "cursor", "1", "2"],
        ["deskpilot", "cursor", "agent=CLAUDE-1", "1", "2"],
        ["deskpilot", "path", "agent=CLAUDE-1", "0xabc", "shown", "m1,2"],
    ]
    monkeypatch.setenv("DESKPILOT_AGENT", "two words")
    with pytest.raises(ValueError, match="DESKPILOT_AGENT"):
        Plugin(run)


def test_a_refusal_from_a_stale_plugin_names_the_build_and_the_fix():
    def run(args, version):
        if args[1] == "status":
            return f'{{"ok":true,"version":"{version}"}}'
        return '{"ok":false,"error":"hold and gap must be 0 to 10000 milliseconds"}'

    with pytest.raises(PluginError, match=r"build 000000000000, not this CLI's .*deskpilot plugin load.*hold and gap"):
        Plugin(lambda args: run(args, "000000000000")).keys("0xabc", ["+28", "-28"])
    with pytest.raises(PluginError, match=r"^hold and gap must be"):
        Plugin(lambda args: run(args, source_hash())).keys("0xabc", ["+28", "-28"])


def test_load_after_an_upgrade_refuses_and_keeps_the_loaded_build(monkeypatch):
    calls = []

    def run(args):
        calls.append(args)
        if args[:2] == ["-j", "version"]:
            return '{"commit":"aaaaaaaaaaaaaaaa"}'
        return '{"ok":true,"version":"000000000000","path":"/old.so"}'

    monkeypatch.setattr(plugin_module, "headers_commit", lambda: "bbbbbbbbbbbbbbbb")
    with pytest.raises(PluginError, match=r"headers are from commit bbbbbbbbbbbb.*running Hyprland is aaaaaaaaaaaa"):
        Plugin(run).load()
    assert not any(args[0] == "plugin" for args in calls)


def test_a_plugin_change_keeps_the_seat_library_and_a_seat_change_replaces_both(monkeypatch, tmp_path):
    for name in ("deskpilot.cpp", "seat.cpp", "seat.hpp", "Makefile"):
        (tmp_path / name).write_text(name)
    monkeypatch.setattr(plugin_module, "SOURCE", tmp_path)
    plugin_build, seat = plugin_module.build_path("c" * 16), plugin_module.seat_path("c" * 16)

    (tmp_path / "deskpilot.cpp").write_text("changed")
    assert plugin_module.build_path("c" * 16) != plugin_build
    assert plugin_module.seat_path("c" * 16) == seat

    (tmp_path / "seat.cpp").write_text("changed")
    assert plugin_module.seat_path("c" * 16) != seat


COMMIT = "aaaaaaaaaaaaaaaa"


def fake_hyprland(tmp_path, monkeypatch, plugin_load="ok"):
    """A Hyprland whose headers match it, with this source's build already on disk so nothing is compiled. Each status
    answers the next of `fake.statuses` (the last repeats), and `plugin load` answers `plugin_load` and loads it."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(plugin_module, "headers_commit", lambda: COMMIT)
    path = plugin_module.build_path(COMMIT)
    path.parent.mkdir(parents=True)
    path.touch()
    mine = json.dumps({"ok": True, "version": source_hash(), "path": str(path)})

    class Fake:
        def __init__(self):
            self.calls, self.statuses = [], ["unknown request"]

        def __call__(self, args):
            self.calls.append(args)
            if args[:2] == ["-j", "version"]:
                return json.dumps({"commit": COMMIT})
            if args[:2] == ["plugin", "load"]:
                if plugin_load in ("ok", "Cannot load a plugin twice!"):
                    self.statuses = [mine]
                return plugin_load
            if args == ["deskpilot", "status"]:
                return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            if args[:2] == ["deskpilot", "keys"]:
                return '{"ok":true}' if self.statuses == [mine] else "unknown request"
            raise AssertionError(args)

    return Fake(), path, mine


def test_a_load_another_process_won_is_accepted(tmp_path, monkeypatch):
    fake, _, _ = fake_hyprland(tmp_path, monkeypatch, "Cannot load a plugin twice!")
    assert Plugin(fake).keys("0xabc", ["+28", "-28"]) == {"ok": True}
    assert [args[1] for args in fake.calls if args[0] == "deskpilot" and args[1] == "keys"] == ["keys", "keys"]


def test_a_refused_load_with_nothing_loaded_still_fails(tmp_path, monkeypatch):
    fake, _, _ = fake_hyprland(tmp_path, monkeypatch, "permission is disabled")
    with pytest.raises(PluginError, match="permission is disabled") as error:
        Plugin(fake).load()
    assert error.value.code == "unavailable"


def test_a_status_hyprctl_cannot_get_is_not_read_as_not_loaded(tmp_path, monkeypatch):
    fake, _, _ = fake_hyprland(tmp_path, monkeypatch)

    def run(args):
        if args == ["deskpilot", "status"]:
            raise HyprctlError("hyprctl deskpilot status exited 6: Hyprland IPC didn't respond in time")
        return fake(args)

    with pytest.raises(PluginError, match="exited 6") as error:
        Plugin(run).load()
    assert error.value.code == "unavailable"
    assert not any(args[0] == "plugin" for args in fake.calls)


def test_load_rechecks_once_it_holds_the_lock(tmp_path, monkeypatch):
    fake, path, mine = fake_hyprland(tmp_path, monkeypatch)
    fake.statuses = ["unknown request", mine]
    assert Plugin(fake).load() == path
    assert not any(args[0] == "plugin" for args in fake.calls)


def test_an_empty_reply_is_unavailable_not_a_missing_plugin():
    with pytest.raises(PluginError, match="without answering") as error:
        parse_reply("")
    assert not isinstance(error.value, PluginNotLoaded) and error.value.code == "unavailable"


def test_plugin_sends_button_codes_and_reads_replies():
    calls = []

    def run(args):
        calls.append(args)
        return '{"ok":true,"x":1,"y":2}'

    Plugin(run).path("0xabc", "hidden", ["m10,20", "p273", "r273"])
    assert calls[-1] == ["deskpilot", "path", "0xabc", "hidden", "m10,20", "p273", "r273"]
    Plugin(run).cursor(10, 20)
    assert calls[-1] == ["deskpilot", "cursor", "10", "20"]
    with pytest.raises(PluginError, match="not loaded"):
        parse_reply("unknown request")
    with pytest.raises(PluginError, match="is on top") as error:
        parse_reply('{"ok":false,"error":"window 0x1 (x) is on top at 1,2","code":"covered"}')
    assert error.value.code == "covered"
    with pytest.raises(PluginError) as error:
        parse_reply('{"ok":false,"error":"an older build answers without a code"}')
    assert error.value.code == "refused"
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
    assert not off.on_monitor and partly.on_monitor


def test_a_window_on_another_monitors_area_is_not_on_its_own():
    """Hyprland copies a window's frame only on a commit of its own monitor, so lying on another one is not enough."""
    fake = FakeHyprctl([{**CLIENT, "monitor": 2}])
    assert not Hyprland(fake).windows()[0].on_monitor


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
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    fake.showing = "2"
    with pytest.raises(NotVisible, match="workspace 1 is not showing"):
        desk.click("battle", 100, 50)
    assert plugin.calls == []
    assert not any(c[0] == "dispatch" for c in fake.calls), "refusing must not focus anything"
    desk.click("battle", 100, 50, show=True)
    assert ["dispatch", 'hl.dsp.focus({window="address:0xabc"})'] in fake.calls
    assert plugin.calls == [("path", "0xabc", "shown", ["m2727,1066", "d52", "p272", "r272"])]


def test_a_click_against_a_resized_window_is_refused(tmp_path, monkeypatch):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    fake.clients[0] = {**CLIENT, "size": [1000, 684]}
    with pytest.raises(screen.StaleShot, match="take a new shot"):
        desk.click("battle", 100, 50)
    assert desk.click("battle", 100, 50, scale=1) == (2627, 1016)


def test_a_missing_plugin_is_loaded_and_the_command_sent_again(monkeypatch):
    replies = iter(["unknown request", '{"ok":true,"x":1,"y":2}'])
    plugin = Plugin(run=lambda args: next(replies))
    loads = []
    monkeypatch.setattr(Plugin, "load", lambda self: loads.append(1))
    assert plugin.cursor(1, 2) == {"ok": True, "x": 1, "y": 2}
    assert loads == [1]


def test_mcp_tools_report_why_an_action_was_refused(tmp_path, monkeypatch):
    """An expected failure is an error result carrying its code, both in the text and as structured content."""
    import asyncio

    from mcp.types import CallToolResult, TextContent

    from deskpilot import mcp_server

    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    fake.showing = "2"
    monkeypatch.setattr(mcp_server, "desk", desk)

    def refusal(tool, arguments):
        result = asyncio.run(mcp_server.mcp.call_tool(tool, arguments))
        assert isinstance(result, CallToolResult) and result.is_error
        text, refused = result.content[0], result.structured_content
        assert isinstance(text, TextContent) and refused is not None
        assert text.text == f"{refused['code']}: {refused['message']}"
        return refused

    assert refusal("click", {"window": "battle", "x": 1, "y": 1})["code"] == "not_visible"
    assert "is not on screen" in refusal("click", {"window": "battle", "x": 1, "y": 1})["message"]
    assert refusal("click", {"window": "nosuchwindow", "x": 1, "y": 1})["code"] == "not_found"
    assert refusal("scroll", {"window": "battle", "dy": 0, "x": 1, "y": 1, "hidden": True})["code"] == "invalid"
    assert refusal("key", {"combo": "Return", "window": "battle"})["code"] == "not_visible"
    assert refusal("type_text", {"text": "x", "hidden": True})["code"] == "invalid"


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


def test_drag_is_one_plugin_path_that_presses_moves_and_releases(tmp_path, monkeypatch):
    desk, _, inp, plugin = make_desk(tmp_path, monkeypatch)
    assert desk.drag("battle", (100, 50), (300, 50), duration=0.1) == (2527 + 600, 1066)
    ((name, address, mode, steps),) = plugin.calls
    assert (name, address, mode) == ("path", "0xabc", "shown")
    assert steps[:4] == ["m2727,1066", "d60", "p272", "d60"] and steps[-2:] == ["d60", "r272"]
    moves = [s for s in steps if s.startswith("m")]
    assert moves[-1] == "m3127,1066" and len(moves) == 1 + 6
    assert inp.events == []
    with pytest.raises(ValueError):
        desk.drag("battle", (100, 50), (300, 50), duration=6)


def test_hover_answers_once_in_place_shoots_then_ends_the_path(tmp_path, monkeypatch):
    desk, _, _, plugin = make_desk(tmp_path, monkeypatch)
    monkeypatch.setattr("deskpilot.desk.time.sleep", lambda _: None)
    taken = []
    monkeypatch.setattr(screen, "capture", lambda win, max_width: taken.append(plugin.calls[:]) or "shot")
    assert desk.hover("battle", 100, 50, wait=0.5) == "shot"
    assert plugin.calls == [("path", "0xabc", "shown", ["m2727,1066", "n", "d5500"]), ("end", 9)]
    # The screenshot is taken while the pointer rests there, before the path ends.
    assert taken == [[("path", "0xabc", "shown", ["m2727,1066", "n", "d5500"])]]


def test_hover_then_click_clicks_before_the_pointer_leaves(tmp_path, monkeypatch):
    desk, _, _, plugin = make_desk(tmp_path, monkeypatch)
    assert desk.hover("battle", 100, 50, wait=0.2, then_click=(100, 80)) == (2727, 1126)
    assert plugin.calls == [("path", "0xabc", "shown", ["m2727,1066", "d200", "m2727,1126", "p272", "r272"])]


def test_an_element_is_targeted_at_its_live_centre(tmp_path, monkeypatch):
    desk, _, _, plugin = make_desk(tmp_path, monkeypatch)
    monkeypatch.setattr("deskpilot.desk.a11y.locate", lambda win, element: a11y.Located(10, 20, 30, 40))
    assert desk.click("battle", None, None, element="e3") == (2527 + 25, 966 + 40)
    assert plugin.calls[-1][-1][0] == "m2552,1006"
    with pytest.raises(ValueError, match="not both"):
        desk.click("battle", 1, 2, element="e3")
    with pytest.raises(ValueError, match="neither"):
        desk.click("battle", None, None)


def test_key_hold_is_bounded(tmp_path, monkeypatch):
    desk, _, _, plugin = make_desk(tmp_path, monkeypatch)
    desk.key("Right", "battle", hold=2)
    assert plugin.calls[-1] == ("keys", "0xabc", ["+106", "-106"], {"hold": 2, "mode": "plain"})
    with pytest.raises(ValueError):
        desk.key("Right", "battle", hold=11)


def popup(id, parent, x, y, sx, sy, width=100, height=200):
    return {"id": id, "parent": parent, "x": x, "y": y, "sx": sx, "sy": sy, "width": width, "height": height}


def test_a_gtk3_element_is_clicked_on_the_surface_it_is_on(tmp_path, monkeypatch):
    desk, _, _, plugin = launched_desk(tmp_path, monkeypatch)
    located = a11y.Located(10, 20, 30, 40, known=True)
    monkeypatch.setattr("deskpilot.desk.a11y.locate", lambda win, element: located)
    desk.click("0xdef", None, None, element="e3", hidden=True)
    assert plugin.calls[-1] == ("path", "0xdef", "hidden", ["s", "m2552,1006", "d52", "p272", "r272"])

    # A menu item: its centre (46,19) on the menu's surface at 14,183.
    plugin.mapped = [popup(0, -1, 20, 188, 14, 183)]
    located = a11y.Located(6, 9, 80, 20, ((20, 188),), True)
    assert desk.click("0xdef", None, None, element="e4", hidden=True) == (2527 + 60, 966 + 202)
    assert plugin.calls[-2:] == [
        ("popups", "0xdef"),
        ("path", "0xdef", "hidden", ["s14,183", "m2587,1168", "d52", "p272", "r272"]),
    ]

    # A submenu item is on the menu's single child, wherever GTK places that, even past the window's edge.
    plugin.mapped = [popup(0, -1, 20, 188, 14, 183), popup(1, 0, 162, 640, 156, 635)]
    located = a11y.Located(6, 30, 80, 20, ((20, 188), (162, 124)), True)
    desk.scroll("0xdef", None, None, 2, element="e6", hidden=True)
    assert plugin.calls[-1][-1][:2] == ["s156,635", f"m{2527 + 156 + 46},{966 + 635 + 40}"]


def test_a_window_element_is_refused_while_a_popup_is_at_the_window_origin(tmp_path, monkeypatch):
    desk, _, _, plugin = launched_desk(tmp_path, monkeypatch)
    monkeypatch.setattr("deskpilot.desk.a11y.locate", lambda win, element: a11y.Located(10, 20, 30, 40, known=True))
    plugin.mapped = [popup(0, -1, 0, 0, -6, -5)]
    with pytest.raises(a11y.A11yError) as error:
        desk.click("0xdef", None, None, element="e3", hidden=True)
    assert error_code(error.value) == "ambiguous"
    assert not any(call[0] == "path" for call in plugin.calls)


def test_a_popup_element_that_cannot_be_placed_is_refused(tmp_path, monkeypatch):
    desk, _, _, plugin = launched_desk(tmp_path, monkeypatch)
    located = a11y.Located(6, 9, 80, 20, ((20, 188),), True)
    monkeypatch.setattr("deskpilot.desk.a11y.locate", lambda win, element: located)
    for mapped, code in (
        ([], "stale"),
        ([popup(0, -1, 20, 188, 14, 183), popup(1, -1, 20, 188, 14, 183)], "ambiguous"),
        ([popup(0, -1, 20, 188, 14, 183, height=10)], "offscreen"),
    ):
        plugin.mapped = mapped
        with pytest.raises(a11y.A11yError) as error:
            desk.click("0xdef", None, None, element="e4", hidden=True)
        assert error_code(error.value) == code
    assert not any(call[0] == "path" for call in plugin.calls)


def test_the_mcp_server_reports_the_package_version():
    from importlib.metadata import version

    from deskpilot import mcp_server

    assert mcp_server.mcp.version == version("deskpilot") != ""


def test_capture_uses_the_targets_compositor(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-user")
    envs = []

    def run(cmd, **kwargs):
        envs.append(kwargs["env"])
        Image.new("RGB", (100, 50)).save(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(screen.subprocess, "run", run)
    screen.capture(replace(win(w=100, h=50), wayland="wayland-nested", instance="nested"))
    assert envs[0]["WAYLAND_DISPLAY"] == "wayland-nested"


@pytest.mark.parametrize("changed", [{"pid": 99}, {"cls": "other"}, {"stable_id": "new"}, {"instance": "other"}])
def test_shot_refuses_reused_addresses(tmp_path, monkeypatch, changed):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake_grim(monkeypatch, (100, 50))
    window = win(w=100, h=50)
    shot = screen.capture(window)
    with pytest.raises(screen.StaleShot, match="has changed"):
        shot.check(replace(window, **changed))
    shot.check(replace(window, title="A title changed by navigation"))


def test_keys_and_explicit_scale_refuse_reused_address(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    fake.clients[0] = {**CLIENT, "pid": 999}
    for action in (
        lambda: desk.key("Return", "battle"),
        lambda: desk.type("text", "battle"),
        lambda: desk.paste("text", "battle"),
        lambda: desk.click("battle", 1, 1, scale=1),
    ):
        with pytest.raises(screen.StaleShot, match="has changed"):
            action()
    assert plugin.calls == []


def test_isolated_paste_uses_only_private_xwayland_clipboard(tmp_path, monkeypatch):
    desk, _, _, plugin = make_desk(tmp_path, monkeypatch)
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "private")
    monkeypatch.setenv("DESKPILOT_ISOLATED", "private")
    monkeypatch.setattr(session, "verify_current", lambda: "private")
    written = []
    monkeypatch.setattr(clipboard, "write", written.append)
    desk.paste("private text", "0xabc", hidden=True)
    assert written == ["private text"]
    assert plugin.calls == [("keys", "0xabc", keys.chord_words("ctrl+v"), {"mode": "plain"})]


def test_isolation_marker_cannot_enable_private_clipboard_on_another_compositor(tmp_path, monkeypatch):
    desk, _, _, plugin = make_desk(tmp_path, monkeypatch)
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "user")
    monkeypatch.setenv("DESKPILOT_ISOLATED", "private")
    monkeypatch.setattr(clipboard, "write", lambda text: pytest.fail("user clipboard changed"))
    with pytest.raises(HyprctlError):
        desk.paste("text", "0xabc")
    assert plugin.calls == []


@pytest.mark.parametrize("lua", [True, False])
def test_isolated_launch_forwards_private_bus_and_profile_without_parent_tokens(tmp_path, monkeypatch, lua):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    fake.lua = lua
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "private")
    monkeypatch.setenv("DESKPILOT_ISOLATED", "private")
    monkeypatch.setattr(session, "verify_current", lambda: "private")
    monkeypatch.setenv("AT_SPI_BUS_ADDRESS", "unix:path=/private/a11y")
    monkeypatch.setenv("CHROME_CONFIG_HOME", "/private/config")
    monkeypatch.setenv("HL_INITIAL_WORKSPACE_TOKEN", "user-token")
    original = desk.hypr._dispatch

    def opened(*args):
        original(*args)
        fake.clients.append({**AGENT_CLIENT, "stableId": "new"})

    monkeypatch.setattr(desk.hypr, "_dispatch", opened)
    assert desk.launch(["browser", "a b"]).address == AGENT_CLIENT["address"]
    command = next(c[-1] for c in reversed(fake.calls) if c[0] == "dispatch")
    assert "AT_SPI_BUS_ADDRESS=unix:path=/private/a11y" in command
    assert "CHROME_CONFIG_HOME=/private/config" in command
    assert "user-token" not in command
    assert plugin.calls == []


def test_latest_shot_is_scoped_to_compositor(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    window = replace(win(), stable_id="same", instance="first")
    fake_grim(monkeypatch, (window.width, window.height))
    first = screen.capture(window)
    other = replace(first, id="1-newer", instance="second")
    (screen.shots_dir() / f"{other.id}.json").write_text(json.dumps(asdict(other)))
    assert screen.latest(window.address, instance="first") == first
    assert screen.latest(window.address, instance="second") == other
    assert screen.latest(window.address, instance="third") is None


@pytest.mark.parametrize("hidden,expected", [(False, "agent"), (True, "agent-hidden")])
def test_independent_keyboard_is_explicit_and_preserves_hidden_ownership(tmp_path, monkeypatch, hidden, expected):
    desk, fake, _, plugin = launched_desk(tmp_path, monkeypatch)
    if not hidden:
        fake.special = "special:agent-tester"
    desk.key("ctrl+a", "0xdef", hidden=hidden, agent_keyboard=True)
    desk.type("Text()", "0xdef", hidden=hidden, agent_keyboard=True)
    assert [call[-1]["mode"] for call in plugin.calls if call[0] == "keys"] == [expected, expected]
    with pytest.raises(WindowNotFound):
        desk.type("Text", "user-window", hidden=True, agent_keyboard=True)


def test_independent_keyboard_requires_an_explicit_target(tmp_path, monkeypatch):
    desk, _, _, plugin = make_desk(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="named window"):
        desk.key("a", agent_keyboard=True)
    with pytest.raises(ValueError, match="named window"):
        desk.type("a", agent_keyboard=True)
    assert plugin.calls == []


@pytest.mark.parametrize("command,value", [("key", "ctrl+a"), ("type", "Text()")])
def test_cli_exposes_independent_keyboard(command, value):
    from deskpilot.cli import build_parser

    args = build_parser().parse_args([command, value, "--window", "0x1", "--agent-keyboard"])
    assert args.agent_keyboard is True


def test_window_snapshot_pins_one_compositor_across_queries(monkeypatch):
    from deskpilot import hypr

    selected = []
    monkeypatch.setattr(hypr, "current_instance", lambda: "chosen")

    def run(args, *, instance=None):
        selected.append(instance)
        return json.dumps([{"instance": "chosen", "wl_socket": "wayland-chosen"}]) if args[-1] == "instances" else "[]"

    monkeypatch.setattr(hypr, "run_hyprctl", run)
    assert hypr.Hyprland(run).windows() == []
    assert selected == ["chosen"] * 4
