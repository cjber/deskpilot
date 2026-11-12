"""GTK4 two-window keyboard and pointer calibration target."""

import gi  # ty: ignore[import-not-found] - system Python provides PyGObject

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk  # noqa: E402 # ty: ignore[import-not-found]


class CalibrationApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="org.deskpilot.Gtk4Calibration", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.windows: list[Gtk.ApplicationWindow] = []

    def _window(self, title: str) -> Gtk.ApplicationWindow:
        window = Gtk.ApplicationWindow(application=self, title=title)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        entry = Gtk.Entry()
        entry.set_hexpand(True)
        button = Gtk.Button(label="Clicks: 0")
        count = 0

        def clicked(_button: Gtk.Button) -> None:
            nonlocal count
            count += 1
            button.set_label(f"Clicks: {count}")

        button.connect("clicked", clicked)
        box.append(entry)
        box.append(button)
        window.set_child(box)
        window.set_default_size(500, 140)
        window.present()
        entry.grab_focus()
        self.windows.append(window)
        return window

    def do_activate(self) -> None:
        self._window("Deskpilot GTK4 target")
        self._window("Deskpilot GTK4 companion")

    def do_shutdown(self) -> None:
        for window in self.windows:
            window.close()
        self.windows.clear()
        super().do_shutdown()


if __name__ == "__main__":
    raise SystemExit(CalibrationApp().run(None))
