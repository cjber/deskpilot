import json

import pytest

from deskpilot import a11y
from deskpilot.hypr import Window


def window(pid=42, title="Editor"):
    return Window("0xabc", "editor", title, 100, 200, 800, 600, 1, "1", False, pid)


class FakeBus:
    def __init__(self, pid=42, title="Editor", duplicate_title=False):
        self.calls = []
        self.bounds = {"/frame": (0, 0, 800, 600), "/dialog": (0, 0, 800, 600), "/button": (10, 20, 80, 20)}
        self.refs = {
            a11y.A11Y_ROOT_PATH: [("app.bus", "/app")],
            "/app": [("app.bus", "/frame"), ("app.bus", "/dialog")] if duplicate_title else [("app.bus", "/frame")],
            "/frame": [("app.bus", "/button")],
            "/dialog": [],
            "/button": [],
        }
        self.names = {"/frame": title, "/dialog": title, "/button": "Save"}
        self.roles = {"/frame": (23, "frame"), "/dialog": (16, "dialog"), "/button": (43, "push button")}
        # GetState's two 32-bit words of the StateType bitset, low first: enabled (8), focusable (11), showing (25),
        # visible (30), and for the button checkable (41) in the high word.
        visible = 1 << 8 | 1 << 25 | 1 << 30
        self.states = {
            "/frame": [visible, 0],
            "/dialog": [visible, 0],
            "/button": [visible | 1 << 11, 1 << (41 - 32)],
        }
        self.interfaces = {path: ["org.a11y.atspi.Component"] for path in self.names}
        self.interfaces["/button"].append("org.a11y.atspi.Text")
        self.text = {"/button": "Save"}
        self.pid = pid

    def call(self, destination, object_path, interface, method, signature, body):
        self.calls.append((destination, object_path, interface, method, signature, body))
        if (interface, method) == (a11y.ACCESSIBLE_INTERFACE, "GetChildren"):
            return (self.refs.get(object_path, []),)
        if (interface, method) == (a11y.DBUS_INTERFACE, "GetConnectionUnixProcessID"):
            return (self.pid if body[0] == "app.bus" else 99,)
        if (interface, method) == (a11y.ACCESSIBLE_INTERFACE, "GetRole"):
            return (self.roles[object_path][0],)
        if (interface, method) == (a11y.ACCESSIBLE_INTERFACE, "GetRoleName"):
            return (self.roles[object_path][1],)
        if (interface, method) == (a11y.ACCESSIBLE_INTERFACE, "GetState"):
            return (self.states[object_path],)
        if (interface, method) == (a11y.ACCESSIBLE_INTERFACE, "GetInterfaces"):
            return (self.interfaces[object_path],)
        if (interface, method) == (a11y.PROPERTIES_INTERFACE, "Get"):
            name = body[1]
            if name == "Name":
                return (("s", self.names[object_path]),)
            if name == "CharacterCount":
                return (("i", len(self.text[object_path])),)
        if (interface, method) == (a11y.COMPONENT_INTERFACE, "GetExtents"):
            return (self.bounds[object_path],)
        if (interface, method) == (a11y.TEXT_INTERFACE, "GetText"):
            return (self.text[object_path],)
        raise AssertionError((destination, object_path, interface, method, signature, body))


def test_snapshot_matches_pid_and_chooses_one_toplevel_by_exact_title(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus(title="Editor", duplicate_title=True)
    fake.names["/dialog"] = "Other"
    tree = a11y.snapshot(window(), bus=fake)

    assert tree.role == "frame"
    assert tree.children[0].name == "Save"
    assert tree.children[0].text == "Save"
    assert tree.children[0].states == ("enabled", "focusable", "showing", "visible", "checkable")
    assert (tmp_path / "deskpilot" / "elements" / "0xabc.json").exists()
    assert json.loads((tmp_path / "deskpilot" / "elements" / "0xabc.json").read_text())["elements"]["e2"] == {
        "bus": "app.bus",
        "path": "/button",
        "role": "push button",
        "name": "Save",
    }


def test_snapshot_reports_ambiguous_toplevel_names(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus(title="Same", duplicate_title=True)
    with pytest.raises(a11y.A11yError, match=r"'Same'.*'Same'"):
        a11y.snapshot(window(title="Different"), bus=fake)


def test_snapshot_reports_an_app_without_accessibility(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus(pid=99)
    with pytest.raises(a11y.A11yError, match="no accessibility tree") as error:
        a11y.snapshot(window(), bus=fake)
    assert error.value.code == "no_tree"
    assert "force-renderer-accessibility" in str(error.value)
    assert "Wine and XWayland games" in str(error.value)


def test_locate_reads_live_bounds_and_unknown_ids_are_actionable(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    a11y.snapshot(window(), bus=fake)
    fake.bounds["/button"] = (30, 40, 90, 25)

    assert a11y.locate(window(), "e2", bus=fake) == (30, 40, 90, 25)
    with pytest.raises(a11y.A11yError, match=r"unknown.*take a new accessibility snapshot") as error:
        a11y.locate(window(), "e99", bus=fake)
    assert error.value.code == "stale"


def test_an_id_from_a_replaced_snapshot_is_stale_and_its_own_snapshot_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    first = a11y.snapshot(window(), bus=fake).snapshot
    monkeypatch.setattr(a11y.time, "time_ns", lambda: 10**18)
    second = a11y.snapshot(window(), bus=fake).snapshot

    assert first != second
    assert a11y.locate(window(), f"{second}:e2", bus=fake) == (10, 20, 80, 20)
    with pytest.raises(a11y.A11yError, match="replaced by snapshot") as error:
        a11y.locate(window(), f"{first}:e2", bus=fake)
    assert error.value.code == "stale"


def test_an_element_whose_name_changed_is_stale_and_one_not_showing_is_offscreen(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    a11y.snapshot(window(), bus=fake)
    fake.names["/button"] = "Delete"
    with pytest.raises(a11y.A11yError, match="was push button 'Save' and is now push button 'Delete'") as error:
        a11y.locate(window(), "e2", bus=fake)
    assert error.value.code == "stale"

    fake.names["/button"] = "Save"
    fake.states["/button"] = [1 << 8, 0]
    with pytest.raises(a11y.A11yError, match="not showing") as error:
        a11y.locate(window(), "e2", bus=fake)
    assert error.value.code == "offscreen"


def test_snapshot_to_dict_marks_a_walk_cut_by_max_nodes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    tree = a11y.snapshot(window(), max_nodes=1, bus=FakeBus())

    assert tree.to_dict()["truncated"] is True
    assert tree.children == ()
