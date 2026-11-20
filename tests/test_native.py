"""NativeDesk, the Windows and macOS backend, over a fake driver: no real windows, input or capture."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from PIL import Image

from deskpilot import win32
from deskpilot.errors import Covered, NotVisible, Unavailable, error_code
from deskpilot.hypr import Window
from deskpilot.native import NativeDesk, Node, chord, descendants


def window(
    address: str = "0x10", pid: int = 100, title: str = "notes", visible: bool = True, focused: bool = True
) -> Window:
    return Window(address, "Notepad", title, 100, 50, 400, 300, 0, "", False, pid, instance="test",
                  stable_id=f"{address}-1", visible=visible, focused=focused)  # fmt: skip


class FakeDriver:
    instance = "test"

    def __init__(self, windows: list[Window]):
        self.list = windows
        self.events: list[tuple] = []
        self.top: str | None = None
        self.tree: dict = {"node": Node("window", "notes", (), None, 100, 50, 400, 300), "children": []}
        self.spawned: list[Window] = []
        self.fail_on: str | None = None
        self.shown_on_focus: Window | None = None

    def windows(self):
        return list(self.list)

    def locked(self):
        return False

    def grab(self, window, path: Path):
        Image.new("RGB", (window.width * 2, window.height * 2)).save(path)

    def focus(self, window):
        self.events.append(("focus", window.address))
        if self.shown_on_focus:
            self.list = [self.shown_on_focus]

    def cursor(self):
        return (5, 5)

    def top_at(self, x, y):
        if self.top is not None:
            return self.top
        for w in self.list:
            if w.x <= x < w.x + w.width and w.y <= y < w.y + w.height:
                return w.address
        return None

    def mouse(self, window, event, x, y, *, button="left", held=frozenset(), clicks=1, dy=0, seat=False):
        if event == self.fail_on and any(e[1] == "down" for e in self.events):
            raise RuntimeError("boom")
        self.events.append(("mouse", event, x, y, button, clicks, dy, seat))

    def key(self, window, key, down, *, held, seat=False):
        self.events.append(("key", key, down, seat))

    def text(self, window, char, *, seat=False):
        self.events.append(("text", char, seat))

    def press(self, window, node):
        self.events.append(("press", node["node"].name))
        return node.get("press", False)

    def root(self, window):
        return self.tree

    def children(self, node):
        return node["children"]

    def describe(self, node):
        return node["node"]

    def read_clipboard(self):
        return "clip"

    def write_clipboard(self, text):
        self.events.append(("clipboard", text))

    def launch(self, command):
        self.list.extend(self.spawned)
        return 500

    def family(self, pid):
        return {500, 501}


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))


def desk(*windows: Window) -> tuple[NativeDesk, FakeDriver]:
    driver = FakeDriver(list(windows) or [window()])
    return NativeDesk(driver, sleep=lambda _: None), driver


def test_a_point_resolves_through_the_last_shot_scale():
    d, driver = desk()
    shot = d.shot("notes")
    assert shot.scale == 2
    assert d.click("notes", 20, 10) == (110, 55)
    mouse = [e for e in driver.events if e[0] == "mouse"]
    assert [e[1] for e in mouse] == ["move", "down", "up"]
    assert not any(e[-1] for e in mouse), "a click without take_pointer never uses the seat"


def test_a_point_under_another_window_is_refused():
    d, driver = desk()
    driver.top = "0x99"
    with pytest.raises(Covered) as error:
        d.click("notes", 10, 10, scale=1)
    assert error_code(error.value) == "covered"
    assert not [e for e in driver.events if e[0] == "mouse"]


def test_a_button_is_let_go_when_the_action_fails():
    d, driver = desk()
    driver.fail_on = "move"
    with pytest.raises(RuntimeError):
        d.drag("notes", (10, 10), (50, 50), scale=1)
    assert [e for e in driver.events if e[1] == "up"], "the pressed button was released"


def test_hidden_actions_paste_and_the_agent_cursor_are_unavailable():
    d, _ = desk()
    for action in (
        lambda: d.click("notes", 1, 1, scale=1, hidden=True),
        lambda: d.paste("hi", "notes"),
        lambda: d.adopt("0x10"),
        lambda: d.record("notes", Path("out.mp4"), threading.Event()),
        lambda: d.press_cursor(True),
        lambda: d.key("a", "notes", hidden=True),
    ):
        with pytest.raises(Unavailable) as error:
            action()
        assert error_code(error.value) == "unavailable"


def test_a_window_off_screen_needs_show():
    d, driver = desk(window(visible=False))
    with pytest.raises(NotVisible):
        d.click("notes", 1, 1, scale=1)
    driver.list = [window(visible=False)]
    driver.shown_on_focus = window()
    assert d.click("notes", 1, 1, scale=1, show=True) == (101, 51)


def test_a_chord_is_released_in_reverse_and_a_character_is_typed():
    d, driver = desk()
    d.key("ctrl+shift+t", "notes")
    assert [(e[1], e[2]) for e in driver.events] == [
        ("ctrl", True), ("shift", True), ("t", True), ("t", False), ("shift", False), ("ctrl", False),
    ]  # fmt: skip
    driver.events.clear()
    d.type("hi", "notes")
    assert driver.events == [("text", "h", False), ("text", "i", False)]


def test_chord_names_are_checked():
    assert chord("+") == ["+"]
    assert chord("Ctrl+Enter") == ["ctrl", "enter"]
    with pytest.raises(ValueError):
        chord("ctrl+nope")


def test_an_element_is_found_again_and_refused_when_it_changed():
    d, driver = desk()
    button = {"node": Node("button", "Save", (), None, 120, 60, 40, 20), "children": []}
    driver.tree["children"] = [button]
    tree = d.elements("notes")
    assert tree.children[0].name == "Save" and (tree.children[0].x, tree.children[0].y) == (20, 10)
    assert d.click("notes", None, None, element="e2") == (140, 70)
    button["node"] = Node("button", "Open", (), None, 120, 60, 40, 20)
    with pytest.raises(RuntimeError) as error:
        d.click("notes", None, None, element="e2")
    assert error_code(error.value) == "stale"


def test_an_element_click_uses_the_apps_own_action_when_it_has_one():
    d, driver = desk()
    driver.tree["children"] = [{"node": Node("button", "Save", (), None, 120, 60, 40, 20), "children": [],
                                "press": True}]  # fmt: skip
    d.elements("notes")
    assert d.click("notes", None, None, element="e2") == (140, 70)
    assert ("press", "Save") in driver.events and not [e for e in driver.events if e[0] == "mouse"]


def test_an_element_whose_parent_changed_is_stale():
    d, driver = desk()
    group = {"node": Node("group", "a", (), None, 100, 50, 10, 10), "children": []}
    group["children"] = [{"node": Node("button", "Save", (), None, 120, 60, 40, 20), "children": []}]
    driver.tree["children"] = [group]
    d.elements("notes")
    group["node"] = Node("group", "b", (), None, 100, 50, 10, 10)
    with pytest.raises(RuntimeError) as error:
        d.click("notes", None, None, element="e3")
    assert error_code(error.value) == "stale"


def test_launch_returns_the_new_window_of_the_started_process():
    d, driver = desk()
    driver.spawned = [window("0x20", pid=501, title="child")]
    assert d.launch(["app"]).address == "0x20"


def test_descendants_follow_the_parent_chain():
    assert descendants(1, {2: 1, 3: 2, 4: 9}) == {1, 2, 3}


def test_win32_message_parameters():
    assert win32.lparam_point(-1, 2) == (2 << 16) | 0xFFFF
    up = win32.key_lparam(0x1C, down=False, extended=True)
    assert up & 1 and (up >> 16) & 0xFF == 0x1C and up & (1 << 24) and up & (1 << 31)
    assert win32.wheel_wparam(-1) >> 16 == (-120) & 0xFFFF
    assert win32.mk_flags(frozenset({"left", "middle"})) == 0x11


def test_a_window_that_comes_up_along_a_drag_stops_it_and_lets_go():
    d, driver = desk()
    real_top = driver.top_at
    pressed = lambda: any(e[1] == "down" for e in driver.events)  # noqa: E731
    driver.top_at = lambda x, y: "0x99" if pressed() and x > 150 else real_top(x, y)
    with pytest.raises(Covered):
        d.drag("notes", (10, 10), (100, 10), scale=1)
    assert driver.events[-1][1] == "up", "the pressed button was released"


def test_seat_keys_stop_when_the_target_leaves_the_front():
    d, driver = desk(window())
    driver.list = [window(focused=False)]
    with pytest.raises(RuntimeError) as error:
        d.type("hi", "notes", take_focus=True)
    assert error_code(error.value) == "refused"
    assert not [e for e in driver.events if e[0] == "text"]


def test_a_posted_messages_key_state_holds_exactly_its_buttons_and_modifiers():
    state = win32.key_state(bytes([0x80] * 256), {0x01, 0x11})
    assert all(state[vk] & 0x80 for vk in (0x01, 0x11, 0xA2)), "the button, Ctrl and left Ctrl are down"
    assert not any(state[vk] & 0x80 for vk in (0x02, 0x10, 0xA0, 0x12)), "other buttons and modifiers are up"
    assert state[0x41] == 0x80, "keys it does not track keep their state"


def test_only_the_key_state_deskpilot_set_is_put_back():
    saved, wanted = bytes([0x80, 0, 0]), bytes([0, 0x80, 0x80])
    now = bytes([0, 0x80, 0])  # the user released key 2 while the message was handled
    assert win32.restore_state(saved, wanted, now) == bytes([0x80, 0, 0])
    assert win32.restore_state(saved, wanted, bytes([0x80, 0x80, 0x80])) == bytes([0x80, 0, 0])
