"""Read an application's AT-SPI accessibility tree."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast

from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import open_dbus_connection
from jeepney.wrappers import unwrap_msg

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
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
DBUS_INTERFACE = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
COORDINATE_WINDOW = 1
CALL_TIMEOUT = 2.0
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
    """An accessibility operation failed with an actionable message."""


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
        raise A11yError(f"Could not call {interface}.{method} on {object_path}: {exc}") from exc


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
        "property org.a11y.Status.IsEnabled is true before they start. Wine and XWayland games expose none."
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
        "The title must match exactly and uniquely."
    )


def _write_records(window: Window, records: dict[str, dict[str, str]]) -> None:
    path = _elements_path(window)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"address": window.address, "pid": window.pid, "elements": records}, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        raise A11yError(f"Could not persist accessibility elements for {window.address}: {exc}") from exc


def snapshot(window: Window, max_depth: int = 30, max_nodes: int = 2000, bus: _Bus | None = None) -> Element:
    """Read and persist the accessibility tree for a Hyprland window."""
    with _bus_context(bus) as connection:
        app_bus, app_path = _app_for_pid(connection, window.pid)
        root_bus, root_path = _choose_toplevel(connection, app_bus, app_path, window)
        records: dict[str, dict[str, str]] = {}
        node_count = 0
        truncated = False

        def walk(destination: str, object_path: str, depth: int) -> Element:
            nonlocal node_count, truncated
            node_count += 1
            element_id = f"e{node_count}"
            records[element_id] = {"bus": destination, "path": object_path}
            role = _role(connection, destination, object_path)
            name = _name(connection, destination, object_path)
            states = _states(
                _reply_value(_call(connection, destination, object_path, ACCESSIBLE_INTERFACE, "GetState"))
            )
            interfaces = _interfaces(connection, destination, object_path)
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
        _write_records(window, records)
        return replace(root, truncated=truncated)


def _load_record(window: Window, element_id: str) -> tuple[str, str]:
    path = _elements_path(window)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = payload.get("elements", {}).get(element_id)
        if not isinstance(record, dict):
            raise KeyError(element_id)
        return str(record["bus"]), str(record["path"])
    except (AttributeError, OSError, ValueError, KeyError, TypeError) as exc:
        raise A11yError(
            f"Element {element_id!r} is unknown for window {window.address}; take a new accessibility snapshot."
        ) from exc


def locate(window: Window, element_id: str, bus: _Bus | None = None) -> tuple[int, int, int, int]:
    """Read an element's current visible bounds from the application."""
    destination, object_path = _load_record(window, element_id)
    with _bus_context(bus) as connection:
        try:
            interfaces = _interfaces(connection, destination, object_path)
            states = _states(
                _reply_value(_call(connection, destination, object_path, ACCESSIBLE_INTERFACE, "GetState"))
            )
        except A11yError as exc:
            raise A11yError(f"Element {element_id} is gone; take a new snapshot: {exc}") from exc
        if COMPONENT_INTERFACE not in interfaces:
            raise A11yError(f"Element {element_id} has no position on screen; target another element.")
        if "showing" not in states:
            raise A11yError(f"Element {element_id} is not showing; choose a visible element or take a new snapshot.")
        bounds = _bounds(connection, destination, object_path)
    if bounds[2] <= 0 or bounds[3] <= 0:
        raise A11yError(f"Element {element_id} has zero size; choose another element or take a new snapshot.")
    # WINDOW coordinates are relative to the app's toplevel, which is the Hyprland window's origin.
    return bounds
