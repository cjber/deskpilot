"""The GTK 4 demo app that scripts/walkthrough.py drives and records. Not part of the deskpilot package.

Run it with the system python3, which needs PyGObject, GTK 4 and librsvg (gir1.2-rsvg-2.0). It shows two of the
repository's own SVGs; selecting one and exporting it copies the file into a library directory. It writes its state
and the exported files under $DESKPILOT_DEMO_DIR (default ~/.cache/deskpilot-walkthrough-app).
"""

import json
import os
import shutil
from pathlib import Path

import gi  # ty: ignore[import-not-found]

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Rsvg", "2.0")
from gi.repository import Gdk, Gio, Gtk, Rsvg  # noqa: E402  # ty: ignore[import-not-found]

ASSETS = Path(__file__).resolve().parent.parent / "assets"
DEMO_DIR = Path(os.environ.get("DESKPILOT_DEMO_DIR") or Path.home() / ".cache" / "deskpilot-walkthrough-app")
TITLE = "Deskpilot local demo"
CSS = b"""
window { background: #141918; color: #edf3ef; font-family: 'Liberation Sans'; }
.shell { background: #171d1b; border: 1px solid #3b4641; border-radius: 18px; }
.top { border-bottom: 1px solid #35413b; padding: 22px 30px; }
.subtle { color: #98aaa0; font-size: 14px; }
.small { color: #98aaa0; font-size: 13px; }
.title { font-size: 30px; font-weight: 600; letter-spacing: -1px; }
.body { padding: 34px 36px 30px; }
.files { background: #202925; border: 1px solid #3b4b42; border-radius: 12px; }
.row { padding: 18px 22px; }
.divider { border-bottom: 1px solid #35433b; }
.filename { font-size: 18px; font-weight: 600; }
checkbutton { padding: 10px; }
check { min-width: 20px; min-height: 20px; border: 1px solid #6e8477; border-radius: 5px; background: #18211c; }
check:checked { background: #94edb8; color: #163221; border-color: #94edb8; }
button { background: #29372f; color: #e5efe8; border: 1px solid #4b6153; border-radius: 8px; padding: 12px 22px; font-size: 15px; box-shadow: none; }
button:hover { background: #3b5143; }
button.primary { background: #9cf2bf; color: #12291b; border: 1px solid #9cf2bf; font-weight: 700; }
button.primary:hover { background: #c6ffdc; }
button:disabled { background: #28352d; color: #6b8073; border-color: #34463a; }
.footer { border-top: 1px solid #35413b; padding: 20px 30px; }
.status { color: #b3c6b9; font-size: 14px; }
.dialog { background: #24322a; border: 1px solid #64836d; border-radius: 14px; padding: 30px; box-shadow: 0 20px 60px #000000; }
.dialog-title { font-size: 25px; font-weight: 600; }
.shade { background: alpha(#050b07, .72); }
.success { background: #263e2e; border: 1px solid #628c6c; border-radius: 10px; padding: 14px 20px; color: #b9f7ca; font-size: 15px; }
"""  # noqa: E501


def label(text, cls=None):
    widget = Gtk.Label(label=text, xalign=0)
    if cls:
        widget.add_css_class(cls)
    return widget


def asset(name, width, height):
    svg = Rsvg.Handle.new_from_file(str(ASSETS / name))
    widget = Gtk.DrawingArea()
    widget.set_content_width(width)
    widget.set_content_height(height)
    widget.set_valign(Gtk.Align.CENTER)

    def draw(_, cr, w, h):
        viewport = Rsvg.Rectangle()
        viewport.x, viewport.y, viewport.width, viewport.height = 0, 0, w, h
        svg.render_document(cr, viewport)

    widget.set_draw_func(draw)
    return widget


class Demo(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.deskpilot.LocalDemo", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.connect("activate", self.activate)
        self.state = {"selected": False, "dialog": False, "exported": False, "files": []}

    def record(self):
        """Publish the state for the driver, atomically so it never reads half a file."""
        DEMO_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DEMO_DIR / "state.json.tmp"
        tmp.write_text(json.dumps(self.state))
        tmp.replace(DEMO_DIR / "state.json")

    def activate(self, app):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, 800)
        self.window = Gtk.ApplicationWindow(application=self, title=TITLE)
        self.window.set_decorated(False)
        self.window.set_default_size(1160, 700)
        overlay = Gtk.Overlay()
        self.window.set_child(overlay)
        self.shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.shell.add_css_class("shell")
        overlay.set_child(self.shell)
        self.shell.append(self.build_top())
        self.shell.append(self.build_body())
        self.shell.append(self.build_footer())
        self.build_dialog(overlay)
        self.choose.connect("toggled", self.selected)
        self.choose_wordmark.connect("toggled", self.selected)
        self.record()
        self.window.present()

    def build_top(self):
        top = Gtk.Box(spacing=20)
        top.add_css_class("top")
        top.append(asset("wordmark-dark.svg", 155, 40))
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top.append(spacer)
        top.append(label("Local demo app", "subtle"))
        return top

    def build_body(self):
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        body.add_css_class("body")
        body.set_vexpand(True)
        headings = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        headings.append(label("Your product assets", "title"))
        headings.append(label("Select a file, then export it to the demo library.", "subtle"))
        body.append(headings)

        files = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        files.add_css_class("files")
        body.append(files)
        self.choose = Gtk.CheckButton()
        self.choose.update_property([Gtk.AccessibleProperty.LABEL], ["Select logo.svg"])
        row = Gtk.Box(spacing=20)
        row.add_css_class("row")
        row.add_css_class("divider")
        row.append(self.choose)
        row.append(asset("logo.svg", 52, 52))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        names.set_hexpand(True)
        names.append(label("logo.svg", "filename"))
        names.append(label("Deskpilot logo", "small"))
        row.append(names)
        row.append(label("SVG", "small"))
        files.append(row)
        other = Gtk.Box(spacing=20)
        other.add_css_class("row")
        self.choose_wordmark = Gtk.CheckButton()
        self.choose_wordmark.update_property([Gtk.AccessibleProperty.LABEL], ["Select wordmark-dark.svg"])
        other.append(self.choose_wordmark)
        other.append(asset("wordmark-dark.svg", 170, 52))
        more = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        more.set_hexpand(True)
        more.append(label("wordmark-dark.svg", "filename"))
        more.append(label("Deskpilot wordmark", "small"))
        other.append(more)
        other.append(label("SVG", "small"))
        files.append(other)

        self.success = label("logo.svg exported to Demo library", "success")
        self.success.set_visible(False)
        body.append(self.success)
        note = label("Illustrative app. Real Deskpilot input and cursor.", "small")
        note.set_vexpand(True)
        note.set_valign(Gtk.Align.END)
        body.append(note)
        return body

    def build_footer(self):
        footer = Gtk.Box(spacing=18)
        footer.add_css_class("footer")
        self.status = label("No files selected", "status")
        self.status.set_hexpand(True)
        footer.append(self.status)
        self.export = Gtk.Button(label="Export selected")
        self.export.add_css_class("primary")
        self.export.set_sensitive(False)
        self.export.connect("clicked", self.show_dialog)
        footer.append(self.export)
        return footer

    def build_dialog(self, overlay):
        self.shade = Gtk.Box()
        self.shade.add_css_class("shade")
        self.shade.set_hexpand(True)
        self.shade.set_vexpand(True)
        self.shade.set_visible(False)
        overlay.add_overlay(self.shade)
        self.dialog = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        self.dialog.add_css_class("dialog")
        self.dialog.set_size_request(430, -1)
        self.dialog.set_halign(Gtk.Align.CENTER)
        self.dialog.set_valign(Gtk.Align.CENTER)
        self.dialog_title = label("Export 1 asset", "dialog-title")
        self.dialog_file = label("logo.svg", "filename")
        self.dialog.append(self.dialog_title)
        self.dialog.append(self.dialog_file)
        self.dialog.append(label("Destination: Demo library", "subtle"))
        actions = Gtk.Box(spacing=12)
        actions.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _: self.hide_dialog())
        actions.append(cancel)
        confirm = Gtk.Button(label="Export asset")
        confirm.add_css_class("primary")
        confirm.connect("clicked", self.finish)
        actions.append(confirm)
        self.dialog.append(actions)
        self.dialog.set_visible(False)
        overlay.add_overlay(self.dialog)

    def selected(self, button):
        checks = [("logo.svg", self.choose), ("wordmark-dark.svg", self.choose_wordmark)]
        self.state["files"] = [name for name, check in checks if check.get_active()]
        count = len(self.state["files"])
        self.state["selected"] = bool(count)
        self.status.set_text(
            f"{count} file" + (" selected" if count == 1 else "s selected") if count else "No files selected"
        )
        self.export.set_sensitive(bool(count))
        self.dialog_title.set_text(f"Export {count} asset" + ("" if count == 1 else "s"))
        self.dialog_file.set_text(", ".join(self.state["files"]))
        self.record()

    def show_dialog(self, _):
        self.state["dialog"] = True
        self.shell.set_sensitive(False)
        self.shade.set_visible(True)
        self.dialog.set_visible(True)
        self.record()

    def hide_dialog(self):
        self.shade.set_visible(False)
        self.dialog.set_visible(False)
        self.state["dialog"] = False
        self.shell.set_sensitive(True)
        self.record()

    def finish(self, _):
        self.hide_dialog()
        (DEMO_DIR / "library").mkdir(parents=True, exist_ok=True)
        for name in self.state["files"]:
            shutil.copyfile(ASSETS / name, DEMO_DIR / "library" / name)
        self.choose.set_sensitive(False)
        self.choose_wordmark.set_sensitive(False)
        self.state["exported"] = True
        self.success.set_text(", ".join(self.state["files"]) + " exported to Demo library")
        self.success.set_visible(True)
        self.status.set_text("Export complete")
        self.export.set_label("Exported")
        self.export.set_sensitive(False)
        self.record()


if __name__ == "__main__":
    Demo().run()
