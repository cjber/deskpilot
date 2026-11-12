"""Small GTK calibration client used by the isolated input runner."""

from __future__ import annotations

import argparse


def main() -> int:
    import gi  # ty: ignore[import-not-found] - system Python provides PyGObject

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk  # ty: ignore[import-not-found]

    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default="")
    args = parser.parse_args()
    window = Gtk.Window(title="Deskpilot calibration")
    entry = Gtk.Entry()
    entry.set_text(args.text)
    button = Gtk.Button(label="Clicks: 0")
    count = [0]

    def clicked(_button):
        count[0] += 1
        button.set_label(f"Clicks: {count[0]}")

    button.connect("clicked", clicked)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.add(entry)
    box.add(button)
    window.add(box)
    window.set_default_size(500, 100)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
