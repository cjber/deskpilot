"""A small Tk window for scripts/smoke_native.py: every input it receives is written to a JSON state file.

`python smoke_app.py STATE` opens the test window; `python smoke_app.py STATE --cover` opens a topmost window over its
pad, for the covered-point check. The state holds each widget's screen rectangle, so the smoke can aim at a widget
in window pixels without a screenshot model.
"""

from __future__ import annotations

import json
import os
import sys
import tkinter as tk
from pathlib import Path

path = Path(sys.argv[1])
cover = "--cover" in sys.argv
state: dict = {"clicks": [], "scroll": [], "drag": [], "hover": 0, "keys": [], "entry": "", "pressed": 0}
root = tk.Tk()


def save(*_: object) -> None:
    root.update_idletasks()
    state["root"] = [root.winfo_rootx(), root.winfo_rooty()]
    state["widgets"] = {
        name: [w.winfo_rootx(), w.winfo_rooty(), w.winfo_width(), w.winfo_height()] for name, w in widgets.items()
    }
    state["entry"] = entry.get() if not cover else ""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    os.replace(temporary, path)


if cover:
    root.title("deskpilot cover")
    root.attributes("-topmost", True)
    target = json.loads(Path(sys.argv[3]).read_text())["widgets"]["pad"]
    root.geometry(f"160x120+{target[0] + 10}+{target[1] + 10}")
    widgets: dict[str, tk.Widget] = {}
    entry = tk.Entry(root)
    root.after(200, save)
    root.mainloop()
    raise SystemExit

root.title("deskpilot smoke")
root.geometry("520x420+120+120")
pad = tk.Canvas(root, width=240, height=120, background="#3b5bdb", highlightthickness=0)
button = tk.Button(root, text="Press")
track = tk.Canvas(root, width=240, height=60, background="#2b8a3e", highlightthickness=0)
spot = tk.Label(root, text="hover here", width=20, background="#e67700")
entry = tk.Entry(root, width=30)
for w in (pad, button, track, spot, entry):
    w.pack(padx=10, pady=6)
widgets = {"pad": pad, "button": button, "track": track, "spot": spot, "entry": entry}


def record(kind: str):
    def handler(event: tk.Event) -> None:
        state["clicks"].append({"kind": kind, "num": event.num})
        pad.configure(background="#c92a2a")
        save()

    return handler


def press() -> None:
    state["pressed"] += 1
    root.title(f"deskpilot smoke pressed {state['pressed']}")
    save()


def wheel(event: tk.Event) -> None:
    state["scroll"].append(1 if event.delta > 0 or event.num == 4 else -1)
    save()


def drag(kind: str):
    def handler(event: tk.Event) -> None:
        state["drag"].append([kind, event.x, event.y])
        save()

    return handler


def hover(_: tk.Event) -> None:
    state["hover"] += 1
    save()


def key(event: tk.Event) -> None:
    state["keys"].append([event.keysym, isinstance(event.state, int) and event.state & 0x4 != 0])
    save()


pad.bind("<ButtonPress-1>", record("left"))
pad.bind("<Double-Button-1>", record("double"))
# Aqua Tk reports the right button as 2, X11 and Windows as 3.
pad.bind("<ButtonPress-2>", record("right"))
pad.bind("<ButtonPress-3>", record("right"))
pad.bind("<MouseWheel>", wheel)
pad.bind("<Button-4>", wheel)
pad.bind("<Button-5>", wheel)
button.configure(command=press)
track.bind("<ButtonPress-1>", drag("down"))
track.bind("<B1-Motion>", drag("move"))
track.bind("<ButtonRelease-1>", drag("up"))
spot.bind("<Enter>", hover)
entry.bind("<KeyPress>", key)
entry.bind("<KeyRelease>", save)
root.after(200, save)
root.mainloop()
