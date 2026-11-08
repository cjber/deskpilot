"""Key names, chords and US-layout text to Linux input key codes (linux/input-event-codes.h)."""

from __future__ import annotations

SHIFT = 42
CTRL = 29
ALT = 56
SUPER = 125

# Unshifted and shifted characters of a US keyboard, by key code.
_ROWS = {
    2: ("1", "!"), 3: ("2", "@"), 4: ("3", "#"), 5: ("4", "$"), 6: ("5", "%"), 7: ("6", "^"), 8: ("7", "&"),
    9: ("8", "*"), 10: ("9", "("), 11: ("0", ")"), 12: ("-", "_"), 13: ("=", "+"), 26: ("[", "{"), 27: ("]", "}"),
    39: (";", ":"), 40: ("'", '"'), 41: ("`", "~"), 43: ("\\", "|"), 51: (",", "<"), 52: (".", ">"), 53: ("/", "?"),
    57: (" ", " "), 28: ("\n", "\n"), 15: ("\t", "\t"),
}  # fmt: skip
_LETTERS = dict(zip("qwertyuiop", range(16, 26), strict=True))
_LETTERS |= dict(zip("asdfghjkl", range(30, 39), strict=True))
_LETTERS |= dict(zip("zxcvbnm", range(44, 51), strict=True))

CHARS: dict[str, tuple[int, bool]] = {}
for _code, (_plain, _shifted) in _ROWS.items():
    CHARS.setdefault(_plain, (_code, False))
    CHARS.setdefault(_shifted, (_code, _shifted != _plain))
for _ch, _code in _LETTERS.items():
    CHARS[_ch] = (_code, False)
    CHARS[_ch.upper()] = (_code, True)

NAMED = {
    "esc": 1, "escape": 1, "backspace": 14, "tab": 15, "return": 28, "enter": 28, "space": 57,
    "ctrl": CTRL, "control": CTRL, "shift": SHIFT, "alt": ALT, "super": SUPER, "meta": SUPER, "win": SUPER,
    "capslock": 58, "delete": 111, "del": 111, "insert": 110, "home": 102, "end": 107,
    "pageup": 104, "pagedown": 109, "up": 103, "down": 108, "left": 105, "right": 106,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65, "f8": 66, "f9": 67, "f10": 68,
    "f11": 87, "f12": 88,
}  # fmt: skip


def all_codes() -> set[int]:
    return {code for code, _ in CHARS.values()} | set(NAMED.values())


def key_code(name: str) -> int:
    lowered = name.lower()
    if lowered in NAMED:
        return NAMED[lowered]
    if len(name) == 1 and name.lower() in CHARS:
        return CHARS[name.lower()][0]
    raise ValueError(f"unknown key '{name}'; use a character or one of: {', '.join(sorted(NAMED))}")


def parse_chord(combo: str) -> list[int]:
    """'ctrl+shift+t' -> [CTRL, SHIFT, T]. A lone '+' is the plus key."""
    if combo == "+":
        return [SHIFT, CHARS["+"][0]]
    return [key_code(part) for part in combo.split("+") if part]


def text_strokes(text: str) -> list[tuple[int, bool]]:
    """Each character as (key code, needs shift). Characters a US keyboard cannot type are an error, not skipped."""
    missing = sorted({ch for ch in text if ch not in CHARS})
    if missing:
        raise ValueError(f"cannot type {missing!r} on a US layout")
    return [CHARS[ch] for ch in text]
