"""Generate tunhub's icons, logos and social preview image.

The mark: a cyan straight line (direct route) under a purple arc (the SSH
detour) with a diamond (the relay) on top, between two endpoints.

Two masters keep it sharp at every size:
- large (128 grid): gradients and a soft glow, for 48 px and up;
- small (16 grid): flat colours on the pixel grid, for favicons.

Usage (dev only; needs cairosvg, fonttools and uharfbuzz):

    uv run --with cairosvg --with fonttools --with uharfbuzz \\
        tools/make_icons.py --font-bold Inter-Bold.ttf --font-regular Inter-Regular.ttf

The fonts are only used to turn the wordmark and tagline into paths.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import cairosvg

ROOT = Path(__file__).resolve().parent.parent
STATIC_ICONS = ROOT / "tunhub" / "static" / "icons"
DOC_IMAGES = ROOT / "docs" / "images"

TAGLINE = "Open the web UIs on your private network like any public website."


@dataclass(frozen=True)
class Palette:
    tile_top: str
    tile_bottom: str
    tile_border: str
    tile_border_opacity: float
    arc_top: str
    arc_end: str
    line_edge: str
    line_mid: str
    glow_opacity: float
    rim: str
    core: str
    node_hi: str
    node: str
    text: str


DARK = Palette(
    tile_top="#18233f", tile_bottom="#0a1024", tile_border="#8ea2ff", tile_border_opacity=0.16,
    arc_top="#c7b4ff", arc_end="#8a62f2", line_edge="#2cc8ee", line_mid="#62eef8", glow_opacity=0.45,
    rim="#dccfff", core="#8e69f3", node_hi="#ffffff", node="#d9d5f3", text="#f4f2ff",
)
LIGHT = Palette(
    tile_top="#ffffff", tile_bottom="#eef0fb", tile_border="#1d2748", tile_border_opacity=0.12,
    arc_top="#9d7dff", arc_end="#6a3fe0", line_edge="#0aa5c9", line_mid="#1cc8e8", glow_opacity=0.22,
    rim="#c3b1ff", core="#6a3fe0", node_hi="#3a4670", node="#1d2748", text="#1d2748",
)

# Large-master geometry (128 x 128).
Y, X0, X1, CX = 80, 30, 98, 64
R = (X1 - X0) / 2  # the arc is a semicircle
TOP = Y - R
STROKE, NODE_R = 8, 9
DIAMOND, DIAMOND_CORE, DIAMOND_GAP = 15, 8.5, 3.5


def _arc_gap_angle() -> float:
    """Angle from the top where the arc leaves the diamond plus its gap."""
    reach = DIAMOND + DIAMOND_GAP + STROKE / 2
    lo, hi = 0.0, math.pi / 2
    for _ in range(60):  # |dx| + |dy| = reach on the circle, solved by bisection
        mid = (lo + hi) / 2
        dx, dy = R * math.sin(mid), R - R * math.cos(mid)
        lo, hi = (mid, hi) if dx + dy < reach else (lo, mid)
    return lo


def _arc_paths() -> str:
    """The arc as two pieces, cut around the diamond (transparent on any background)."""
    a = _arc_gap_angle()
    px, py = R * math.sin(a), Y - R * math.cos(a)
    return (
        f"M{X0} {Y}A{R} {R} 0 0 1 {CX - px:.3f} {py:.3f}"
        f"M{CX + px:.3f} {py:.3f}A{R} {R} 0 0 1 {X1} {Y}"
    )


def _diamond(half: float) -> str:
    return f"M{CX} {TOP - half}L{CX + half} {TOP}L{CX} {TOP + half}L{CX - half} {TOP}Z"


def mark_defs(p: Palette, prefix: str) -> str:
    return f"""
    <linearGradient id="{prefix}arc" gradientUnits="userSpaceOnUse" x1="0" y1="{TOP}" x2="0" y2="{Y}">
      <stop offset="0" stop-color="{p.arc_top}"/><stop offset="1" stop-color="{p.arc_end}"/>
    </linearGradient>
    <linearGradient id="{prefix}line" gradientUnits="userSpaceOnUse" x1="{X0}" y1="0" x2="{X1}" y2="0">
      <stop offset="0" stop-color="{p.line_edge}"/><stop offset="0.5" stop-color="{p.line_mid}"/><stop offset="1" stop-color="{p.line_edge}"/>
    </linearGradient>
    <radialGradient id="{prefix}glow">
      <stop offset="0" stop-color="{p.line_mid}" stop-opacity="{p.glow_opacity}"/><stop offset="1" stop-color="{p.line_mid}" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="{prefix}node" cx="0.4" cy="0.35" r="0.7">
      <stop offset="0" stop-color="{p.node_hi}"/><stop offset="1" stop-color="{p.node}"/>
    </radialGradient>"""


def mark_body(p: Palette, prefix: str) -> str:
    return f"""
  <ellipse cx="{CX}" cy="{Y + 2}" rx="{R + 14}" ry="{STROKE * 1.6}" fill="url(#{prefix}glow)"/>
  <path d="M{X0} {Y}H{X1}" stroke="url(#{prefix}line)" stroke-width="{STROKE}" stroke-linecap="round"/>
  <path d="{_arc_paths()}" fill="none" stroke="url(#{prefix}arc)" stroke-width="{STROKE}"/>
  <path d="{_diamond(DIAMOND)}" fill="{p.rim}" stroke="{p.rim}" stroke-width="1.5" stroke-linejoin="round"/>
  <path d="{_diamond(DIAMOND_CORE)}" fill="{p.core}"/>
  <circle cx="{X0}" cy="{Y}" r="{NODE_R}" fill="url(#{prefix}node)"/>
  <circle cx="{X1}" cy="{Y}" r="{NODE_R}" fill="url(#{prefix}node)"/>"""


def large_icon(p: Palette = DARK, shape: str = "rounded") -> str:
    """App icon on a tile. shape: 'rounded' (transparent corners) or 'square' (full bleed)."""
    if shape == "rounded":
        tile = f"""
  <rect width="128" height="128" rx="28" fill="url(#tile)"/>
  <rect x="0.75" y="0.75" width="126.5" height="126.5" rx="27.25" fill="none" stroke="{p.tile_border}" stroke-opacity="{p.tile_border_opacity}" stroke-width="1.5"/>"""
    else:
        tile = '\n  <rect width="128" height="128" fill="url(#tile)"/>'
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{p.tile_top}"/><stop offset="1" stop-color="{p.tile_bottom}"/>
    </linearGradient>{mark_defs(p, "")}
  </defs>{tile}{mark_body(p, "")}
</svg>
"""


def small_icon() -> str:
    """16 x 16 favicon: flat colours, thin parts, the whole tile used."""
    p = DARK
    y, x0, x1, ry = 11, 2.75, 13.25, 5.5
    top = y - ry
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">
  <rect width="16" height="16" rx="3.5" fill="#121b35"/>
  <path d="M{x0} {y}H{x1}" stroke="{p.line_mid}" stroke-width="2" stroke-linecap="round"/>
  <path d="M{x0} {y}A{(x1 - x0) / 2} {ry} 0 0 1 {x1} {y}" fill="none" stroke="#a98cfb" stroke-width="1.5"/>
  <path d="M8 {top - 2.1}L10.1 {top}L8 {top + 2.1}L5.9 {top}Z" fill="{p.rim}"/>
  <circle cx="{x0}" cy="{y}" r="1.6" fill="#f4f2ff"/>
  <circle cx="{x1}" cy="{y}" r="1.6" fill="#f4f2ff"/>
</svg>
"""


# ---------------------------------------------------------------------- text


class TextShaper:
    """Turn a string into one SVG path, shaped with HarfBuzz (kerning)."""

    def __init__(self, font_path: Path) -> None:
        import uharfbuzz as hb
        from fontTools.ttLib import TTFont

        self._hb = hb
        self._font = hb.Font(hb.Face(hb.Blob.from_file_path(str(font_path))))
        self._tt = TTFont(str(font_path))
        self._glyphs = self._tt.getGlyphSet()
        self._order = self._tt.getGlyphOrder()
        self.upm = self._tt["head"].unitsPerEm

    def path(self, text: str, size: float, x: float, baseline: float, tracking: float = 0.0) -> tuple[str, float]:
        """Return (path data, advance width); tracking is in em."""
        from fontTools.pens.svgPathPen import SVGPathPen
        from fontTools.pens.transformPen import TransformPen

        buf = self._hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        self._hb.shape(self._font, buf, {"kern": True, "liga": True})
        scale = size / self.upm
        cursor, parts = 0.0, []
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            pen = SVGPathPen(self._glyphs)
            transform = (scale, 0, 0, -scale, x + (cursor + pos.x_offset) * scale, baseline - pos.y_offset * scale)
            self._glyphs[self._order[info.codepoint]].draw(TransformPen(pen, transform))
            parts.append(pen.getCommands())
            cursor += pos.x_advance + tracking * self.upm
        width = (cursor - tracking * self.upm) * scale
        return " ".join(parts), width


def logo(p: Palette, bold: TextShaper) -> str:
    """Bare mark + 'tunhub' wordmark, for README headers."""
    text_x = 128 + 6
    d, width = bold.path("tunhub", 84, text_x, 91, tracking=-0.02)
    # Crop the mark's empty margins: it spans x 21..107 and y 28..92.
    vx, vy, vw, vh = 17, 22, text_x + width + 4 - 17, 76
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx} {vy} {vw:.1f} {vh}" width="{vw * 1.5:.0f}" height="{vh * 1.5:.0f}" role="img" aria-label="tunhub">
  <defs>{mark_defs(p, "")}
  </defs>{mark_body(p, "")}
  <path d="{d}" fill="{p.text}"/>
</svg>
"""


def social_preview(bold: TextShaper, regular: TextShaper) -> str:
    """1280 x 640 card shown when the repository link is shared."""
    w, h = 1280, 640
    scale = 2.2  # the mark, from its 128 grid
    word_size = 150
    _, word_w = bold.path("tunhub", word_size, 0, 0, tracking=-0.02)
    mark_w = (107 - 21) * scale
    gap = 36
    total = mark_w + gap + word_w
    left = (w - total) / 2
    baseline = 330
    mark_x = left - 21 * scale
    mark_y = baseline - 92 * scale + 2
    word_d, _ = bold.path("tunhub", word_size, left + mark_w + gap, baseline, tracking=-0.02)
    tag_size = 32
    _, tag_w = regular.path(TAGLINE, tag_size, 0, 0)
    tag_d, _ = regular.path(TAGLINE, tag_size, (w - tag_w) / 2, baseline + 110)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{DARK.tile_top}"/><stop offset="1" stop-color="{DARK.tile_bottom}"/>
    </linearGradient>
    <radialGradient id="halo" cx="0.5" cy="0.42" r="0.55">
      <stop offset="0" stop-color="#8a62f2" stop-opacity="0.22"/><stop offset="1" stop-color="#8a62f2" stop-opacity="0"/>
    </radialGradient>{mark_defs(DARK, "m")}
  </defs>
  <rect width="{w}" height="{h}" fill="url(#bg)"/>
  <rect width="{w}" height="{h}" fill="url(#halo)"/>
  <g transform="translate({mark_x:.1f} {mark_y:.1f}) scale({scale})">{mark_body(DARK, "m")}
  </g>
  <path d="{word_d}" fill="{DARK.text}"/>
  <path d="{tag_d}" fill="#aab3d6"/>
</svg>
"""


# -------------------------------------------------------------------- output


def png(svg: str, path: Path, size: int | None = None, width: int | None = None) -> None:
    kwargs = {"output_width": size or width}
    if size:
        kwargs["output_height"] = size
    cairosvg.svg2png(bytestring=svg.encode(), write_to=str(path), **kwargs)


def manifest() -> str:
    data = {
        "name": "tunhub",
        "short_name": "tunhub",
        "description": "Open your private web UIs; tunhub sets up the SSH forwarding.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": DARK.tile_bottom,
        "theme_color": DARK.tile_bottom,
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return json.dumps(data, indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--font-bold", type=Path, required=True)
    parser.add_argument("--font-regular", type=Path, required=True)
    args = parser.parse_args()
    bold, regular = TextShaper(args.font_bold), TextShaper(args.font_regular)

    STATIC_ICONS.mkdir(parents=True, exist_ok=True)
    DOC_IMAGES.mkdir(parents=True, exist_ok=True)

    # Dashboard and PWA: always the dark tile.
    (STATIC_ICONS / "favicon.svg").write_text(small_icon())
    png(small_icon(), STATIC_ICONS / "favicon-32.png", size=32)
    png(large_icon(DARK, "rounded"), STATIC_ICONS / "icon-192.png", size=192)
    png(large_icon(DARK, "rounded"), STATIC_ICONS / "icon-512.png", size=512)
    # Full bleed: the OS applies its own mask. The mark stays inside the
    # maskable safe zone (a circle of 80 % diameter).
    png(large_icon(DARK, "square"), STATIC_ICONS / "icon-maskable-512.png", size=512)
    png(large_icon(DARK, "square"), STATIC_ICONS / "apple-touch-icon.png", size=180)
    (ROOT / "tunhub" / "static" / "manifest.webmanifest").write_text(manifest())

    # README and GitHub.
    (DOC_IMAGES / "logo-dark.svg").write_text(logo(DARK, bold))
    (DOC_IMAGES / "logo-light.svg").write_text(logo(LIGHT, bold))
    (DOC_IMAGES / "icon.svg").write_text(large_icon(DARK, "rounded"))
    social = social_preview(bold, regular)
    png(social, DOC_IMAGES / "social-preview.png", width=1280)
    print("icons written to", STATIC_ICONS, "and", DOC_IMAGES)


if __name__ == "__main__":
    main()
