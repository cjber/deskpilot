"""Key names, chords and text as the deskpilot plugin's key events.

A named key is a Linux input key code (linux/input-event-codes.h). A character is sent as its code point, and the
plugin finds the key that types it on the user's own keymap, so text arrives as written on any layout and with Caps
Lock on. A character no key on that keymap types is refused by the plugin, never skipped.
"""

from __future__ import annotations

CTRL, SHIFT, ALT, SUPER = 29, 42, 56, 125

NAMED = {
    "esc": 1, "escape": 1, "backspace": 14, "tab": 15, "return": 28, "enter": 28, "space": 57,
    "ctrl": CTRL, "control": CTRL, "shift": SHIFT, "alt": ALT, "super": SUPER, "meta": SUPER, "win": SUPER,
    "capslock": 58, "delete": 111, "del": 111, "insert": 110, "home": 102, "end": 107,
    "pageup": 104, "pagedown": 109, "up": 103, "down": 108, "left": 105, "right": 106,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65, "f8": 66, "f9": 67, "f10": 68,
    "f11": 87, "f12": 88,
}  # fmt: skip


def press_release(part: str) -> tuple[str, str]:
    """A chord part's press and release: `+code` and `-code` for a named key, or `p` and `r` with the character for
    the key that gives it unmodified (ctrl+A is ctrl+a, as on a keyboard)."""
    if part.lower() in NAMED:
        code = NAMED[part.lower()]
        return f"+{code}", f"-{code}"
    if len(part) == 1:
        char = f"{ord(part.lower()):x}"
        return f"p{char}", f"r{char}"
    raise ValueError(f"unknown key '{part}'; use a character or one of: {', '.join(sorted(NAMED))}")


def chord_words(combo: str) -> list[str]:
    """'ctrl+shift+t' -> ctrl, shift and t down, then up in reverse. A lone character, '+' included, is typed as
    written, so `key A` types a capital A."""
    parts = [part for part in combo.split("+") if part] if combo != "+" else ["+"]
    if len(parts) == 1 and len(parts[0]) == 1:
        return text_words(parts[0])
    keys = [press_release(part) for part in parts]
    return [press for press, _ in keys] + [release for _, release in reversed(keys)]


def text_words(text: str) -> list[str]:
    """Each character typed as one keystroke."""
    return [f"t{ord(ch):x}" for ch in text]
