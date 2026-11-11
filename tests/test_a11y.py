import json
from types import SimpleNamespace

import pytest
from jeepney.low_level import HeaderFields
from jeepney.wrappers import DBusErrorResponse

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
        self.failure: Exception | None = None
        # What a container with a Selection interface reports as chosen, by its path.
        self.selected: dict[str, list[tuple[str, str]]] = {}
        # The app's org.a11y.atspi.Application ToolkitName and Version, SCREEN extents where they differ from WINDOW
        # ones, and Parent references.
        self.toolkit: tuple[str, str] | None = None
        self.screen: dict[str, tuple[int, int, int, int]] = {}
        self.parents: dict[str, str] = {}

    def call(self, destination, object_path, interface, method, signature, body):
        self.calls.append((destination, object_path, interface, method, signature, body))
        if self.failure is not None and object_path == "/button":
            raise self.failure
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
            if name == "NSelectedChildren":
                return (("i", len(self.selected[object_path])),)
            if body[0] == a11y.APPLICATION_INTERFACE and self.toolkit is not None:
                return (("s", self.toolkit[0] if name == "ToolkitName" else self.toolkit[1]),)
            if name == "Parent":
                return (("(so)", ("app.bus", self.parents[object_path])),)
        if (interface, method) == (a11y.SELECTION_INTERFACE, "GetSelectedChild"):
            return (self.selected[object_path][body[0]],)
        if (interface, method) == (a11y.COMPONENT_INTERFACE, "GetExtents"):
            if body == (a11y.COORDINATE_SCREEN,):
                return (self.screen.get(object_path, self.bounds[object_path]),)
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


def test_a_child_a_selection_reports_as_chosen_is_selected(tmp_path, monkeypatch):
    # A GTK combo box reports its active item only through its Selection: the item has no selected state itself.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    fake.interfaces["/frame"].append(a11y.SELECTION_INTERFACE)
    fake.selected["/frame"] = [("app.bus", "/button")]
    tree = a11y.snapshot(window(), bus=fake)
    assert "selected" in tree.children[0].states
    assert "selected" not in tree.states


def test_snapshot_reports_ambiguous_toplevel_names(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus(title="Same", duplicate_title=True)
    with pytest.raises(a11y.A11yError, match=r"'Same'.*'Same'") as error:
        a11y.snapshot(window(title="Different"), bus=fake)
    assert error.value.code == "ambiguous"


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

    assert a11y.locate(window(), "e2", bus=fake) == a11y.Located(30, 40, 90, 25)
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
    assert a11y.locate(window(), f"{second}:e2", bus=fake) == a11y.Located(10, 20, 80, 20)
    with pytest.raises(a11y.A11yError, match="replaced by snapshot") as error:
        a11y.locate(window(), f"{first}:e2", bus=fake)
    assert error.value.code == "stale"


def test_a_peek_leaves_the_latest_snapshot_and_its_ids_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    taken = a11y.snapshot(window(), bus=fake).snapshot
    peeked = a11y.snapshot(window(), bus=fake, peek=True)

    assert peeked.snapshot == "" and "snapshot" not in peeked.to_dict()
    assert a11y.locate(window(), f"{taken}:e2", bus=fake) == a11y.Located(10, 20, 80, 20)


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


def test_a_vanished_element_is_stale_and_a_failing_bus_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    a11y.snapshot(window(), bus=fake)
    gone = SimpleNamespace(
        header=SimpleNamespace(fields={HeaderFields.error_name: "org.freedesktop.DBus.Error.UnknownObject"}), body=()
    )
    fake.failure = DBusErrorResponse(gone)
    with pytest.raises(a11y.A11yError, match="is gone") as error:
        a11y.locate(window(), "e2", bus=fake)
    assert error.value.code == "stale"

    fake.failure = TimeoutError("no reply")
    with pytest.raises(a11y.A11yError) as error:
        a11y.locate(window(), "e2", bus=fake)
    assert error.value.code == "unavailable"


def test_a_missing_registry_is_unavailable_not_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    missing = SimpleNamespace(
        header=SimpleNamespace(fields={HeaderFields.error_name: "org.freedesktop.DBus.Error.ServiceUnknown"}), body=()
    )

    class NoRegistry(FakeBus):
        def call(self, destination, object_path, interface, method, signature, body):
            if destination == a11y.A11Y_BUS_NAME:
                raise DBusErrorResponse(missing)
            return super().call(destination, object_path, interface, method, signature, body)

    with pytest.raises(a11y.A11yError) as error:
        a11y.snapshot(window(), bus=NoRegistry())
    assert error.value.code == "unavailable"


def test_a_malformed_record_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    a11y.snapshot(window(), bus=FakeBus())
    path = tmp_path / "deskpilot" / "elements" / "0xabc.json"
    payload = json.loads(path.read_text())
    del payload["elements"]["e2"]["path"]
    path.write_text(json.dumps(payload))
    with pytest.raises(a11y.A11yError, match="unknown") as error:
        a11y.locate(window(), "e2", bus=FakeBus())
    assert error.value.code == "stale"


def test_snapshot_to_dict_marks_a_walk_cut_by_max_nodes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    tree = a11y.snapshot(window(), max_nodes=1, bus=FakeBus())

    assert tree.to_dict()["truncated"] is True
    assert tree.children == ()


def gtk3_bus(toolkit=("gtk", "3.24.52")):
    """A GTK 3 window with a button on the toplevel, an open menu whose item has an open submenu. GTK 3 reports each
    popup element relative to its popup's surface, and its SCREEN extents shifted by the popup's position: the menu's
    geometry is at 20,188 and the submenu's GDK puts at 162,124."""
    fake = FakeBus()
    fake.toolkit = toolkit
    fake.refs.update({"/frame": [("app.bus", "/button"), ("app.bus", "/menu")], "/menu": [("app.bus", "/item")]})
    fake.refs.update({"/item": [("app.bus", "/submenu")], "/submenu": [("app.bus", "/subitem")], "/subitem": []})
    fake.bounds.update({"/menu": (0, 0, 100, 200), "/item": (6, 9, 80, 20)})
    fake.bounds.update({"/submenu": (0, 0, 100, 100), "/subitem": (6, 30, 80, 20)})
    fake.screen.update({"/menu": (20, 188, 100, 200), "/item": (26, 197, 80, 20)})
    fake.screen.update({"/submenu": (162, 124, 100, 100), "/subitem": (168, 154, 80, 20)})
    fake.parents.update({"/subitem": "/submenu", "/submenu": "/item", "/item": "/menu", "/menu": "/frame"})
    fake.parents["/button"] = "/frame"
    for path, name, role in (
        ("/menu", "", "menu"),
        ("/item", "Germany", "menu item"),
        ("/submenu", "", "menu"),
        ("/subitem", "Sub three", "menu item"),
    ):
        fake.names[path], fake.roles[path] = name, (0, role)
        fake.states[path] = fake.states["/frame"]
        fake.interfaces[path] = [a11y.COMPONENT_INTERFACE]
    return fake


POPUPS = [
    a11y.Popup(0, -1, 20, 188, 14, 183, 100, 200),
    a11y.Popup(1, 0, 162, 128, 156, 123, 100, 100),
]


def test_a_gtk3_popup_element_is_placed_through_its_popup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    asked = []
    tree = a11y.snapshot(window(), bus=gtk3_bus(), popups=lambda: asked.append(1) or POPUPS)
    button, menu = tree.children
    item = menu.children[0]
    subitem = item.children[0].children[0]
    assert (button.x, button.y) == (10, 20)
    assert (item.x, item.y, item.width, item.height) == (14 + 6, 183 + 9, 80, 20)
    assert (subitem.x, subitem.y) == (156 + 6, 123 + 30)
    assert asked == [1]

    fake = gtk3_bus()
    assert a11y.locate(window(), "e2", bus=fake) == a11y.Located(10, 20, 80, 20, known=True)
    assert a11y.locate(window(), "e4", bus=fake) == a11y.Located(6, 9, 80, 20, ((20, 188),), True)
    assert a11y.locate(window(), "e6", bus=fake) == a11y.Located(6, 30, 80, 20, ((20, 188), (162, 124)), True)


def test_a_popup_element_whose_popup_is_not_found_has_zero_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    tree = a11y.snapshot(window(), bus=gtk3_bus(), popups=lambda: POPUPS[1:])
    item = tree.children[1].children[0]
    assert (item.x, item.y, item.width, item.height) == (0, 0, 0, 0)
    assert (tree.children[0].x, tree.children[0].y) == (10, 20)


def test_a_popup_at_the_window_origin_is_not_taken_for_the_window(tmp_path, monkeypatch):
    """A menu at the window's origin reports the toplevel's own offset, so its elements and the window's cannot be
    told apart: both get zero bounds, never the window's surface."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = gtk3_bus()
    fake.screen.update({"/menu": (0, 0, 100, 200), "/item": (6, 9, 80, 20)})
    tree = a11y.snapshot(window(), bus=fake, popups=lambda: [a11y.Popup(0, -1, 0, 0, -6, -5, 100, 200)])
    button, menu = tree.children
    assert (button.x, button.y, button.width) == (0, 0, 0)
    assert (menu.children[0].x, menu.children[0].width) == (0, 0)
    assert a11y.locate(window(), "e4", bus=fake) == a11y.Located(6, 9, 80, 20, known=True)


def test_gtk4_and_other_toolkits_keep_their_reported_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = gtk3_bus(toolkit=("GTK", "4.24.1"))
    tree = a11y.snapshot(window(), bus=fake, popups=lambda: pytest.fail("GTK 4 needs no popups"))
    assert (tree.children[1].children[0].x, tree.children[1].children[0].y) == (6, 9)
    assert a11y.locate(window(), "e4", bus=fake) == a11y.Located(6, 9, 80, 20)
    assert not any(call[-1] == (a11y.COORDINATE_SCREEN,) for call in fake.calls)


def test_a_popup_is_resolved_exactly_or_refused():
    assert a11y.resolve(POPUPS, ()) is None
    with pytest.raises(a11y.A11yError) as error:
        a11y.resolve([*POPUPS, a11y.Popup(2, -1, 0, 0, -6, -5, 10, 10)], ())
    assert error.value.code == "ambiguous"
    assert a11y.resolve(POPUPS, ((20, 188),)) == POPUPS[0]
    assert a11y.resolve(POPUPS, ((20, 188), (162, 124))) == POPUPS[1]
    with pytest.raises(a11y.A11yError) as error:
        a11y.resolve(POPUPS, ((21, 188),))
    assert error.value.code == "stale"
    with pytest.raises(a11y.A11yError) as error:
        a11y.resolve([*POPUPS, a11y.Popup(2, -1, 20, 188, 14, 183, 10, 10)], ((20, 188),))
    assert error.value.code == "ambiguous"
    with pytest.raises(a11y.A11yError) as error:
        a11y.resolve([*POPUPS, a11y.Popup(2, 0, 0, 0, 0, 0, 10, 10)], ((20, 188), (162, 124)))
    assert error.value.code == "ambiguous"
    with pytest.raises(a11y.A11yError) as error:
        a11y.resolve(POPUPS[:1], ((20, 188), (162, 124)))
    assert error.value.code == "stale"
