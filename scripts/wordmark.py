#!/usr/bin/env python3
"""Regenerate the wordmark and social preview from assets/logo.svg and Liberation Sans.

    uv run python scripts/wordmark.py
    rsvg-convert -w 1280 -h 640 assets/social-preview.svg -o assets/social-preview.png

The icon is the traced logo as committed, not redrawn here; the text is outlined, so the SVGs need no font to show.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
FONTS = Path("/usr/share/fonts/liberation")
BOLD = TTFont(FONTS / "LiberationSans-BoldItalic.ttf")
REGULAR = TTFont(FONTS / "LiberationSans-Regular.ttf")
INK, PAPER, MUTED, MINT = "#17150f", "#f5f2ea", "#b9b3a4", "#7cf0b0"

WORD = "deskpilot"
SIZE, BASELINE, ICON, GAP = 74, 80, 64, 18


def inner(svg: str) -> str:
    """The drawing of an SVG, without its declaration, root tag or title."""
    start = svg.index("<svg")
    body = svg[svg.index(">", start) + 1 : svg.rindex("</svg>")]
    return re.sub(r"<title>.*?</title>", "", body)


def glyphs(font: TTFont):
    # fontTools' stubs leave unitsPerEm off the head table, so cast past them.
    return font.getGlyphSet(), font.getBestCmap() or {}, cast(Any, font["head"]).unitsPerEm


def width(font: TTFont, text: str, size: float) -> float:
    glyph_set, cmap, upem = glyphs(font)
    return sum(glyph_set[cmap[ord(char)]].width for char in text) * size / upem


def outline(font: TTFont, text: str, x: float, baseline: float, size: float) -> tuple[str, float]:
    glyph_set, cmap, upem = glyphs(font)
    pen = SVGPathPen(glyph_set)
    for char in text:
        glyph = glyph_set[cmap[ord(char)]]
        scale = size / upem
        glyph.draw(TransformPen(pen, (scale, 0, 0, -scale, x, baseline)))
        x += glyph.width * scale
    return pen.getCommands(), x


def wordmark(ink: str) -> str:
    text, end = outline(BOLD, WORD, ICON + GAP, BASELINE, SIZE)
    body = f'<g transform="translate(0,{18}) scale({ICON / 1024:.8f})">{inner((ASSETS / "logo.svg").read_text())}</g>'
    body += f'<path d="{text}" fill="{ink}"/>'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {end + 10:.2f} 100" role="img" '
        f'aria-label="{WORD}"><title>{WORD}</title>{body}</svg>\n'
    )


def social() -> str:
    lockup = inner(wordmark(PAPER))
    box_width = ICON + GAP + width(BOLD, WORD, SIZE) + 10
    scale = 2.0
    x = (1280 - box_width * scale) / 2
    tagline = "Desktop control for agents on Hyprland."
    tagline_path, _ = outline(REGULAR, tagline, (1280 - width(REGULAR, tagline, 34)) / 2, 480, 34)
    stack = "HYPRLAND  ·  WAYLAND  ·  MCP"
    stack_path, _ = outline(REGULAR, stack, (1280 - width(REGULAR, stack, 22)) / 2, 560, 22)
    body = (
        '<rect width="1280" height="640" fill="#17150f"/>'
        f'<g transform="translate({x:.2f},150) scale({scale})">{lockup}</g>'
        f'<path d="{tagline_path}" fill="{MUTED}"/>'
        f'<path d="{stack_path}" fill="{MINT}"/>'
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 640" role="img" '
        f'aria-label="{WORD}"><title>{WORD}</title>{body}</svg>\n'
    )


(ASSETS / "wordmark.svg").write_text(wordmark(INK))
(ASSETS / "wordmark-dark.svg").write_text(wordmark(PAPER))
(ASSETS / "social-preview.svg").write_text(social())
print("wrote assets/wordmark.svg, assets/wordmark-dark.svg and assets/social-preview.svg")
