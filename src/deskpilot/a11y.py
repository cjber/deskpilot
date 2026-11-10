"""Read an application's AT-SPI accessibility tree."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast

from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import open_dbus_connection
from jeepney.wrappers import DBusErrorResponse, unwrap_msg

from . import screen
from .hypr import Window

SESSION_BUS_NAME = "org.a11y.Bus"
SESSION_BUS_PATH = "/org/a11y/bus"
SESSION_BUS_INTERFACE = "org.a11y.Bus"
A11Y_BUS_NAME = "org.a11y.atspi.Registry"
A11Y_ROOT_PATH = "/org/a11y/atspi/accessible/root"
ACCESSIBLE_INTERFACE = "org.a11y.atspi.Accessible"
COMPONENT_INTERFACE = "org.a11y.atspi.Component"
TEXT_INTERFACE = "org.a11y.atspi.Text"
SELECTION_INTERFACE = "org.a11y.atspi.Selection"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
DBUS_INTERFACE = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
COORDINATE_WINDOW = 1
CALL_TIMEOUT = 2.0
# D-Bus errors meaning the element's app or object no longer exists, so an id that named it is stale. Any other
# failure, such as a timeout, is the bus being unavailable.
GONE_ERRORS = frozenset(
    {
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
        "org.freedesktop.DBus.Error.UnknownObject",
    }
)
TEXT_LIMIT = 4000

_STATE_NAMES = (
    "invalid",
    "active",
    "armed",
    "busy",
    "checked",
    "collapsed",
    "defunct",
    "editable",
    "enabled",
    "expandable",
    "expanded",
    "focusable",
    "focused",
    "has tooltip",
    "horizontal",
    "iconified",
    "modal",
    "multi line",
    "multi selectable",
    "opaque",
    "pressed",
    "resizable",
    "selectable",
    "selected",
    "sensitive",
    "showing",
    "single line",
    "stale",
    "transient",
    "vertical",
    "visible",
    "manages descendants",
    "indeterminate",
    "required",
    "truncated",
    "animated",
    "invalid entry",
    "supports autocompletion",
    "selectable text",
    "is default",
    "visited",
    "checkable",
    "has popup",
    "read only",
)


class A11yError(RuntimeError):
    """An accessibility operation failed with an actionable message. `code` is its kind: no_tree (the app exposes
    none), ambiguous (several toplevels could be the window), stale (the element is gone, changed, or from an older
    snapshot), offscreen (it is not showing), refused (it has no position) or unavailable (the accessibility bus
    failed)."""

    def __init__(self, message: str, code: str = "unavailable"):
        super().__init__(message)
        self.code = code


class _Bus(Protocol):
    def call(
        self,
        destination: str,
        object_path: str,
        interface: str,
        method: str,
        signature: str,
        body: tuple[object, ...],
    ) -> object: ...


class _JeepneyBus:
    """Small blocking Jeepney adapter used by the tree reader."""

    def __init__(self, connection: Any):
        self._connection = connection

    def call(
        self,
        destination: str,
        object_path: str,
        interface: str,
        method: str,
        signature: str,
        body: tuple[object, ...],
    ) -> object:
        address = DBusAddress(object_path, bus_name=destination, interface=interface)
        message = new_method_call(address, method, signature or None, body)
        reply = self._connection.send_and_get_reply(message, timeout=CALL_TIMEOUT)
        return unwrap_msg(reply)

    def close(self) -> None:
        self._connection.close()


@dataclass(frozen=True)
class Element:
    id: str
    role: str
    name: str
    states: tuple[str, ...]
    text: str | None
    x: int
    y: int
    width: int
    height: int
    children: tuple[Element, ...]
    truncated: bool = False  # on the root: max_depth or max_nodes cut the walk short
    snapshot: str = ""  # on the root: the id of the snapshot whose element ids these are

    def to_dict(self) -> dict:
        """Return this element and its descendants as plain JSON data."""
        result = {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "states": list(self.states),
            "text": self.text,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "children": [child.to_dict() for child in self.children],
        }
        if self.truncated:
            result["truncated"] = True
        if self.snapshot:
            result["snapshot"] = self.snapshot
        return result


def _elements_path(window: Window) -> Path:
    return screen.state_dir() / "elements" / f"{window.address}.json"


@contextmanager
def _bus_context(bus: _Bus | None):
    if bus is not None:
        yield bus
        return

    connection: _JeepneyBus | None = None
    try:
        session_connection = open_dbus_connection(bus="SESSION", auth_timeout=CALL_TIMEOUT)
        session = _JeepneyBus(session_connection)
        try:
            address_reply = session.call(
                SESSION_BUS_NAME,
                SESSION_BUS_PATH,
                SESSION_BUS_INTERFACE,
                "GetAddress",
                "",
                (),
            )
        finally:
            session.close()
        address = str(_reply_value(address_reply))
        if not address:
            raise A11yError("The accessibility bus returned an empty address.")
        connection = _JeepneyBus(open_dbus_connection(bus=address, auth_timeout=CALL_TIMEOUT))
        yield connection
    except A11yError:
        raise
    except Exception as exc:
        raise A11yError(f"Could not connect to the accessibility bus: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def _reply_value(reply: object) -> object:
    if isinstance(reply, (tuple, list)) and len(reply) == 1:
        return reply[0]
    return reply


def _variant_value(value: object) -> object:
    if isinstance(value, (tuple, list)) and len(value) == 2 and isinstance(value[0], str):
        return value[1]
    return value


def _call(
    bus: _Bus,
    destination: str,
    object_path: str,
    interface: str,
    method: str,
    signature: str = "",
    body: tuple[object, ...] = (),
) -> object:
    try:
        return bus.call(destination, object_path, interface, method, signature, body)
    except A11yError:
        raise
    except Exception as exc:
        code = "stale" if isinstance(exc, DBusErrorResponse) and exc.name in GONE_ERRORS else "unavailable"
        raise A11yError(f"Could not call {interface}.{method} on {object_path}: {exc}", code) from exc


def _property(bus: _Bus, destination: str, object_path: str, interface: str, name: str) -> object:
    reply = _call(
        bus,
        destination,
        object_path,
        PROPERTIES_INTERFACE,
        "Get",
        "ss",
        (interface, name),
    )
    return _variant_value(_reply_value(reply))


def _as_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if value is None:
        return ()
    return tuple(str(item) for item in cast(Any, value))


def _children(bus: _Bus, destination: str, object_path: str) -> list[tuple[str, str]]:
    reply = _reply_value(_call(bus, destination, object_path, ACCESSIBLE_INTERFACE, "GetChildren"))
    if reply is None:
        return []
    try:
        return [(str(child_bus), str(child_path)) for child_bus, child_path in cast(Any, reply)]
    except (TypeError, ValueError) as exc:
        raise A11yError(f"Accessibility object {object_path} returned invalid child references: {exc}") from exc


def _selected(bus: _Bus, destination: str, object_path: str, interfaces: tuple[str, ...]) -> set[tuple[str, str]]:
    """The objects a container says are chosen, through its Selection interface. A GTK combo box reports its active
    item only here: the item itself carries no selected state, and its name may be the combo box's label."""
    if SELECTION_INTERFACE not in interfaces:
        return set()
    count = _property(bus, destination, object_path, SELECTION_INTERFACE, "NSelectedChildren")
    try:
        count = int(cast(Any, count))
    except (TypeError, ValueError) as exc:
        raise A11yError(f"Accessibility object {object_path} returned an invalid selection count: {count!r}") from exc
    chosen = set()
    for index in range(max(count, 0)):
        reply = _reply_value(
            _call(bus, destination, object_path, SELECTION_INTERFACE, "GetSelectedChild", "i", (index,))
        )
        try:
            child_bus, child_path = cast(Any, reply)
        except (TypeError, ValueError) as exc:
            raise A11yError(f"Accessibility object {object_path} returned an invalid selected child: {exc}") from exc
        chosen.add((str(child_bus), str(child_path)))
    return chosen


def _states(value: object) -> tuple[str, ...]:
    """GetState's `au`: a 64-bit StateType bitset as two 32-bit words, low word first."""
    try:
        low, high = (int(word) for word in cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise A11yError(f"Accessibility object returned invalid states: {value!r}") from exc
    bits = low | high << 32
    return tuple(
        _STATE_NAMES[index] if index < len(_STATE_NAMES) else f"state-{index}"
        for index in range(64)
        if bits >> index & 1
    )


def _role(bus: _Bus, destination: str, object_path: str) -> str:
    return str(_reply_value(_call(bus, destination, object_path, ACCESSIBLE_INTERFACE, "GetRoleName")))


def _name(bus: _Bus, destination: str, object_path: str) -> str:
    return str(_property(bus, destination, object_path, ACCESSIBLE_INTERFACE, "Name") or "")


def _interfaces(bus: _Bus, destination: str, object_path: str) -> tuple[str, ...]:
    return _as_strings(_reply_value(_call(bus, destination, object_path, ACCESSIBLE_INTERFACE, "GetInterfaces")))


def _bounds(bus: _Bus, destination: str, object_path: str) -> tuple[int, int, int, int]:
    reply = cast(
        tuple[Any, Any, Any, Any],
        _reply_value(
            _call(
                bus,
                destination,
                object_path,
                COMPONENT_INTERFACE,
                "GetExtents",
                "u",
                (COORDINATE_WINDOW,),
            )
        ),
    )
    try:
        x, y, width, height = reply
        return int(x), int(y), int(width), int(height)
    except (TypeError, ValueError) as exc:
        raise A11yError(f"Accessibility object {object_path} returned invalid bounds: {reply!r}") from exc


def _text(bus: _Bus, destination: str, object_path: str, interfaces: tuple[str, ...]) -> str | None:
    if TEXT_INTERFACE not in interfaces:
        return None
    count = _property(bus, destination, object_path, TEXT_INTERFACE, "CharacterCount")
    try:
        count = int(cast(Any, count))
    except (TypeError, ValueError) as exc:
        raise A11yError(f"Accessibility object {object_path} returned invalid text length: {count!r}") from exc
    if count < 0:
        raise A11yError(f"Accessibility object {object_path} returned a negative text length.")
    length = min(count, TEXT_LIMIT)
    if length == 0:
        return ""
    reply = _reply_value(
        _call(
            bus,
            destination,
            object_path,
            TEXT_INTERFACE,
            "GetText",
            "ii",
            (0, length),
        )
    )
    text = str(reply)
    if count > TEXT_LIMIT or len(text) > TEXT_LIMIT:
        return text[:TEXT_LIMIT] + " [truncated]"
    return text


def _app_for_pid(bus: _Bus, pid: int) -> tuple[str, str]:
    app_refs = _children(bus, A11Y_BUS_NAME, A11Y_ROOT_PATH)
    for app_bus, app_path in app_refs:
        try:
            process_id = _reply_value(
                _call(
                    bus,
                    DBUS_INTERFACE,
                    DBUS_PATH,
                    DBUS_INTERFACE,
                    "GetConnectionUnixProcessID",
                    "s",
                    (app_bus,),
                )
            )
            if int(cast(Any, process_id)) == pid:
                return app_bus, app_path
        except (A11yError, TypeError, ValueError):
            continue
    raise A11yError(
        f"Application PID {pid} exposes no accessibility tree. Chromium, Electron and Firefox expose one only "
        "when started with accessibility on, for example --force-renderer-accessibility, or when the a11y bus "
        "property org.a11y.Status.IsEnabled is true before they start. Wine and XWayland games expose none.",
        "no_tree",
    )


def _choose_toplevel(bus: _Bus, app_bus: str, app_path: str, window: Window) -> tuple[str, str]:
    candidates = []
    names = []
    for ref in _children(bus, app_bus, app_path):
        role = _role(bus, *ref)
        name = _name(bus, *ref)
        names.append(name)
        if role.casefold() in {"frame", "window", "dialog"}:
            candidates.append((ref, role, name))

    if len(candidates) == 1:
        return candidates[0][0]
    exact = [candidate for candidate in candidates if candidate[2] == window.title]
    if len(exact) == 1:
        return exact[0][0]
    listed = ", ".join(repr(name) for name in names) or "none"
    raise A11yError(
        f"Could not choose an accessibility toplevel for {window.title!r}: candidates are {listed}. "
        "The title must match exactly and uniquely.",
        "ambiguous",
    )


def _write_records(window: Window, snapshot_id: str, records: dict[str, dict[str, str]]) -> None:
    path = _elements_path(window)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"address": window.address, "pid": window.pid, "snapshot": snapshot_id, "elements": records}
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        raise A11yError(f"Could not persist accessibility elements for {window.address}: {exc}") from exc


def snapshot(
    window: Window, max_depth: int = 30, max_nodes: int = 2000, bus: _Bus | None = None, peek: bool = False
) -> Element:
    """Read and persist the accessibility tree for a Hyprland window. A peek reads it without replacing the latest
    snapshot, so ids taken from that snapshot stay good; its own ids name nothing and it has no snapshot id."""
    with _bus_context(bus) as connection:
        app_bus, app_path = _app_for_pid(connection, window.pid)
        root_bus, root_path = _choose_toplevel(connection, app_bus, app_path, window)
        records: dict[str, dict[str, str]] = {}
        node_count = 0
        truncated = False

        # What each container with a Selection reports as chosen, as it is reached: a chosen item lies below it.
        chosen: set[tuple[str, str]] = set()

        def walk(destination: str, object_path: str, depth: int) -> Element:
            nonlocal node_count, truncated
            node_count += 1
            element_id = f"e{node_count}"
            role = _role(connection, destination, object_path)
            name = _name(connection, destination, object_path)
            records[element_id] = {"bus": destination, "path": object_path, "role": role, "name": name}
            states = _states(
                _reply_value(_call(connection, destination, object_path, ACCESSIBLE_INTERFACE, "GetState"))
            )
            interfaces = _interfaces(connection, destination, object_path)
            chosen.update(_selected(connection, destination, object_path, interfaces))
            if (destination, object_path) in chosen and "selected" not in states:
                states = (*states, "selected")
            bounds = (
                _bounds(connection, destination, object_path) if COMPONENT_INTERFACE in interfaces else (0, 0, 0, 0)
            )
            text = _text(connection, destination, object_path, interfaces)
            child_refs = _children(connection, destination, object_path)
            children: list[Element] = []
            if child_refs:
                if depth >= max_depth:
                    truncated = True
                else:
                    for child_bus, child_path in child_refs:
                        if node_count >= max_nodes:
                            truncated = True
                            break
                        children.append(walk(child_bus, child_path, depth + 1))
            return Element(element_id, role, name, states, text, *bounds, tuple(children))

        root = walk(root_bus, root_path, 0)
        # Ids are numbered afresh by each snapshot, so an id is checked against the snapshot it came from.
        if peek:
            return replace(root, truncated=truncated)
        snapshot_id = f"{window.address.removeprefix('0x')}-{time.time_ns()}"
        _write_records(window, snapshot_id, records)
        return replace(root, truncated=truncated, snapshot=snapshot_id)


def _load_record(window: Window, element_id: str, snapshot_id: str | None) -> dict[str, str]:
    path = _elements_path(window)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = payload.get("elements", {}).get(element_id)
        if not isinstance(record, dict) or not {"bus", "path"} <= record.keys():
            raise KeyError(element_id)
        latest = str(payload.get("snapshot", ""))
    except (AttributeError, OSError, ValueError, KeyError, TypeError) as exc:
        raise A11yError(
            f"Element {element_id!r} is unknown for window {window.address}; take a new accessibility snapshot.",
            "stale",
        ) from exc
    if snapshot_id is not None and snapshot_id != latest:
        raise A11yError(
            f"Snapshot {snapshot_id} of window {window.address} was replaced by snapshot {latest}, which numbers its "
            "elements afresh; take the element's id from the new snapshot.",
            "stale",
        )
    return {key: str(value) for key, value in record.items()}


def locate(window: Window, element_id: str, bus: _Bus | None = None) -> tuple[int, int, int, int]:
    """Read an element's current visible bounds from the application. The id is `e5`, from the window's latest
    snapshot, or `<snapshot>:e5`, which is refused unless that snapshot is still the latest. An element whose role or
    name has changed since its snapshot is stale: the id no longer names what the caller saw."""
    snapshot_id, _, element_id = element_id.rpartition(":")
    record = _load_record(window, element_id, snapshot_id or None)
    destination, object_path = record["bus"], record["path"]
    with _bus_context(bus) as connection:
        try:
            interfaces = _interfaces(connection, destination, object_path)
            states = _states(
                _reply_value(_call(connection, destination, object_path, ACCESSIBLE_INTERFACE, "GetState"))
            )
            role = _role(connection, destination, object_path)
            name = _name(connection, destination, object_path)
        except A11yError as exc:
            if exc.code != "stale":
                raise
            raise A11yError(f"Element {element_id} is gone; take a new snapshot: {exc}", "stale") from exc
        # A record written before records kept role and name has nothing to compare.
        if "role" in record and (role, name) != (record["role"], record["name"]):
            raise A11yError(
                f"Element {element_id} was {record['role']} {record['name']!r} and is now {role} {name!r}; take a "
                "new snapshot.",
                "stale",
            )
        if COMPONENT_INTERFACE not in interfaces:
            raise A11yError(f"Element {element_id} has no position on screen; target another element.", "refused")
        if "showing" not in states:
            raise A11yError(
                f"Element {element_id} is not showing; scroll it into view or take a new snapshot.", "offscreen"
            )
        bounds = _bounds(connection, destination, object_path)
    if bounds[2] <= 0 or bounds[3] <= 0:
        raise A11yError(f"Element {element_id} has zero size; scroll it into view or take a new snapshot.", "offscreen")
    # WINDOW coordinates are relative to the app's toplevel, which is the Hyprland window's origin.
    return bounds
