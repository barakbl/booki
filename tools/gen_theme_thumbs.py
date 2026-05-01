"""
Regenerate `thumbnail.png` for every theme under `themes/export/<kind>/<slug>/`.

Each thumbnail is a small mock (~280×176) that uses the theme's declared
default colors so the wizard can preview palettes before the user picks.
Run after editing a theme.toml or adding a new theme:

    python tools/gen_theme_thumbs.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:                                                # py<3.11
    import tomli as tomllib  # type: ignore

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
THEMES_ROOT = ROOT / "themes" / "export"
SIZE = (280, 176)


def _load(theme_toml: Path) -> dict:
    with open(theme_toml, "rb") as f:
        meta = tomllib.load(f)
    out = {}
    for name, spec in (meta.get("vars") or {}).items():
        if (spec or {}).get("type") == "color":
            out[name] = spec.get("default") or "#888888"
    return out


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch + ch for ch in c)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(round(ax + (bx - ax) * t)) for ax, bx in zip(a, b))


def _font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _round_rect(d: ImageDraw.ImageDraw, box, fill, radius=6, outline=None):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline)


def render_link_thumb(colors: dict) -> Image.Image:
    bg = _hex(colors.get("bg", "#0b0d12"))
    text = _hex(colors.get("text", "#e7e9ee"))
    link = _hex(colors.get("link", "#06b6d4"))
    accent = _hex(colors.get("accent", "#8b5cf6"))
    panel = _mix(bg, text, 0.06)
    border = _mix(bg, text, 0.12)
    dim = _mix(bg, text, 0.55)

    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)

    # Title
    d.text((14, 12), "Booki Links", fill=text, font=_font(15))
    # Search input mock
    _round_rect(d, (14, 36, SIZE[0] - 14, 54), fill=panel, outline=border, radius=5)

    # 3 rows
    y = 64
    titles = ["The future of bookmarks", "Lightweight RAG cookbook", "Building local search"]
    for i, t in enumerate(titles):
        # bullet
        d.ellipse((16, y + 4, 22, y + 10), fill=accent)
        # title (link color)
        d.text((28, y), t, fill=link, font=_font(11))
        # tag pill
        pill_x0 = SIZE[0] - 60
        _round_rect(d, (pill_x0, y + 1, SIZE[0] - 14, y + 14),
                    fill=_mix(bg, accent, 0.18), radius=7)
        d.text((pill_x0 + 6, y + 1), "#tag", fill=accent, font=_font(9))
        # url line
        d.line((28, y + 18, SIZE[0] - 14, y + 18), fill=dim, width=1)
        y += 30
    return img


def render_photo_thumb(colors: dict) -> Image.Image:
    bg = _hex(colors.get("bg", "#0b0d12"))
    text = _hex(colors.get("text", "#e7e9ee"))
    accent = _hex(colors.get("accent", "#8b5cf6"))
    panel = _mix(bg, text, 0.06)
    border = _mix(bg, text, 0.12)

    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)

    d.text((14, 12), "Photos", fill=text, font=_font(15))

    # 3×2 grid of tiles, each with a different accent tint
    cols, rows = 3, 2
    pad = 14
    gap = 6
    top = 38
    avail_w = SIZE[0] - pad * 2 - gap * (cols - 1)
    avail_h = SIZE[1] - top - pad - gap * (rows - 1)
    tw = avail_w // cols
    th = avail_h // rows

    for r in range(rows):
        for c in range(cols):
            x0 = pad + c * (tw + gap)
            y0 = top + r * (th + gap)
            tint = _mix(panel, accent, 0.15 + 0.12 * ((r * cols + c) % 5))
            _round_rect(d, (x0, y0, x0 + tw, y0 + th), fill=tint, outline=border, radius=4)
            # Inner glyph triangle/circle
            cx, cy = x0 + tw // 2, y0 + th // 2
            d.ellipse((cx - 4, cy - 4, cx + 4, cy + 4),
                      fill=_mix(accent, text, 0.2))
    return img


def render_archive_thumb(colors: dict) -> Image.Image:
    """Reuse link-page mock — both archive index and link page share `any/`."""
    return render_link_thumb(colors)


def _mono_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/Library/Fonts/Andale Mono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            try: return ImageFont.truetype(p, size)
            except Exception: continue
    return _font(size)


# Apple Color Emoji is an sbix bitmap font that only ships a few specific
# point sizes (notably 109pt for the in-OS render). Pillow can load it but
# only at one of those sizes; we then downscale the painted glyph onto the
# thumbnail. Linux has Noto Color Emoji (96px). Falls back to None if no
# emoji font is available, in which case _draw_text_with_emoji() just skips
# the emoji glyphs.
_EMOJI_FONT_PATHS = [
    ("/System/Library/Fonts/Apple Color Emoji.ttc", 160),
    ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", 96),
    ("/usr/share/fonts/noto-emoji/NotoColorEmoji.ttf", 96),
]


def _emoji_font():
    for p, native in _EMOJI_FONT_PATHS:
        if Path(p).exists():
            try:
                # Pillow needs `size=native` for sbix/CBDT color fonts.
                return ImageFont.truetype(p, native), native
            except Exception:
                continue
    return None, None


_EMOJI_RE = re.compile(
    r"["
    r"\U0001F300-\U0001FAFF"      # symbols & pictographs, supplemental, etc.
    r"\U00002600-\U000027BF"      # misc symbols + dingbats
    r"\U0001F000-\U0001F2FF"      # mahjong / domino / playing cards / enclosed
    r"]"
)


def _draw_text_with_emoji(canvas: Image.Image, xy, text: str, *,
                          font: ImageFont.ImageFont, fill,
                          target_emoji_size: int) -> int:
    """
    Paint `text` left-to-right starting at xy. Regular characters use `font`
    and `fill`; emoji are painted from the system color-emoji font onto a
    transparent canvas at its native size and downscaled to
    `target_emoji_size`. Returns total advance width in pixels.

    Falls back to the regular font for emoji when no color-emoji font is
    found (will render as `□` boxes — not ideal but at least not broken).
    """
    em_font, em_native = _emoji_font()
    d = ImageDraw.Draw(canvas)

    x, y = xy
    start_x = x
    pos = 0
    while pos < len(text):
        m = _EMOJI_RE.match(text, pos)
        if m and em_font is not None:
            ch = m.group(0)
            # Paint the emoji to its own transparent canvas at native size,
            # then downscale + paste onto our thumbnail.
            tile = Image.new("RGBA", (em_native + 8, em_native + 8), (0, 0, 0, 0))
            td = ImageDraw.Draw(tile)
            try:
                td.text((4, 4), ch, font=em_font, embedded_color=True)
            except TypeError:
                # Older Pillow without embedded_color support.
                td.text((4, 4), ch, font=em_font, fill=fill)
            scaled = tile.resize((target_emoji_size, target_emoji_size),
                                  Image.LANCZOS)
            canvas.paste(scaled, (int(x), int(y)), scaled)
            x += target_emoji_size
            pos += len(ch)
        else:
            # Eat one regular char.
            ch = text[pos]
            d.text((int(x), int(y)), ch, fill=fill, font=font)
            x += d.textlength(ch, font=font)
            pos += 1
    return int(x - start_x)


def render_ratatui_thumb(colors: dict) -> Image.Image:
    """
    Terminal/TUI mock for the ratatui theme: a labelled box around a list,
    a status bar at the bottom, monospace font.
    """
    bg = _hex(colors.get("bg", "#0c0c14"))
    text = _hex(colors.get("text", "#cdd6f4"))
    link = _hex(colors.get("link", "#7dd3fc"))
    accent = _hex(colors.get("accent", "#fbbf24"))
    border = _mix(bg, text, 0.22)
    dim = _mix(bg, text, 0.5)

    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)
    f_title = _mono_font(11)
    f_row = _mono_font(10)
    f_status = _mono_font(10)

    # Status bar at the bottom (3 char-rows tall).
    bar_h = 14
    d.rectangle((0, SIZE[1] - bar_h, SIZE[0], SIZE[1]), fill=accent)
    d.text((6, SIZE[1] - bar_h + 2),
           "F1 help  /  filter  ↵ open",
           fill=bg, font=f_status)

    # Outer block border with title.
    box = (8, 14, SIZE[0] - 8, SIZE[1] - bar_h - 6)
    d.rectangle(box, outline=border, width=1)
    title = "┤ Booki Links ├"
    tw = d.textlength(title, font=f_title)
    d.rectangle((box[0] + 14 - 2, box[1] - 6, box[0] + 14 + tw + 2, box[1] + 6), fill=bg)
    d.text((box[0] + 14, box[1] - 6), title, fill=accent, font=f_title)

    # Filter input box.
    fy = box[1] + 8
    d.rectangle((box[0] + 6, fy, box[2] - 6, fy + 14), outline=border)
    d.text((box[0] + 12, fy + 1), "▶  filter…", fill=dim, font=f_row)

    # Three list rows.
    titles = ["the future of bookmarks", "lightweight RAG cookbook", "building local search"]
    y = fy + 22
    for i, t in enumerate(titles, start=1):
        d.text((box[0] + 8, y), f"{i:03}", fill=dim, font=f_row)
        d.text((box[0] + 32, y), "🔖", fill=accent, font=f_row)
        d.text((box[0] + 50, y), t, fill=link, font=f_row)
        d.text((box[2] - 60, y), "[#tag]", fill=accent, font=f_row)
        y += 16
    return img


def render_ratatui_photo_thumb(colors: dict) -> Image.Image:
    """Ratatui photo gallery mock — labelled block + 3×2 framed tiles."""
    bg = _hex(colors.get("bg", "#0c0c14"))
    text = _hex(colors.get("text", "#cdd6f4"))
    accent = _hex(colors.get("accent", "#fbbf24"))
    border = _mix(bg, text, 0.22)

    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)
    f_title = _mono_font(11)

    box = (8, 14, SIZE[0] - 8, SIZE[1] - 8)
    d.rectangle(box, outline=border, width=1)
    title = "┤ Photos ├"
    tw = d.textlength(title, font=f_title)
    d.rectangle((box[0] + 14 - 2, box[1] - 6, box[0] + 14 + tw + 2, box[1] + 6), fill=bg)
    d.text((box[0] + 14, box[1] - 6), title, fill=accent, font=f_title)

    cols, rows, gap = 3, 2, 4
    pad = 12
    grid_x0 = box[0] + pad
    grid_y0 = box[1] + 14
    grid_w = (box[2] - box[0]) - pad * 2
    grid_h = (box[3] - box[1]) - 14 - pad
    tw = (grid_w - gap * (cols - 1)) // cols
    th = (grid_h - gap * (rows - 1)) // rows
    for r in range(rows):
        for c in range(cols):
            x0 = grid_x0 + c * (tw + gap)
            y0 = grid_y0 + r * (th + gap)
            tint = _mix(bg, accent, 0.10 + 0.06 * ((r * cols + c) % 4))
            d.rectangle((x0, y0, x0 + tw, y0 + th), fill=tint, outline=border)
            cx, cy = x0 + tw // 2, y0 + th // 2
            d.ellipse((cx - 4, cy - 4, cx + 4, cy + 4),
                      fill=_mix(accent, text, 0.2))
    return img


def render_fun_thumb(colors: dict) -> Image.Image:
    """
    Childish, colorful mock for the Fun theme: rainbow title bar, three
    rotated white "card" rows with bright pill buttons, sticker emoji.
    """
    bg = _hex(colors.get("bg", "#fef3c7"))
    text = _hex(colors.get("text", "#1f2937"))
    accent = _hex(colors.get("accent", "#7c3aed"))
    secondary = _hex(colors.get("secondary", "#06b6d4"))

    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)
    f_title = _font(15)
    f_row = _font(10)
    f_pill = _font(9)

    # Soft radial-ish blob in two corners (approximate via translucent rects).
    d.rectangle((0, 0, 110, 80),
                fill=_mix(bg, secondary, 0.18))
    d.rectangle((SIZE[0] - 110, 96, SIZE[0], SIZE[1]),
                fill=_mix(bg, accent, 0.15))

    # Rainbow title with leading + trailing star emoji (color-emoji font is
    # used per-character; regular characters paint with rainbow gradient).
    rainbow = ["#ec4899", "#f59e0b", "#10b981", "#06b6d4", "#8b5cf6"]
    em_size = 18
    x = 14
    # leading star
    x += _draw_text_with_emoji(img, (x, 11), "🌟",
                                font=f_title, fill=accent,
                                target_emoji_size=em_size)
    x += 4
    label = " My Fun Page "
    for i, ch in enumerate(label):
        col = _hex(rainbow[i % len(rainbow)])
        d.text((x, 14), ch, fill=col, font=f_title)
        x += int(d.textlength(ch, font=f_title)) + 1
    _draw_text_with_emoji(img, (x, 11), "🌟",
                          font=f_title, fill=accent,
                          target_emoji_size=em_size)

    titles = [("✨", "a fun bookmark"), ("🎈", "another cool link"), ("🦄", "unicorn page")]
    pills = [accent, secondary, _hex("#ec4899")]
    y = 50
    for i, (em, t) in enumerate(titles):
        # drop-shadow pill (rotated card simulated as offset block).
        sx = 18 - (i % 2 * 2)
        sy = y
        # shadow
        _round_rect(d, (sx + 3, sy + 3, sx + 240, sy + 32),
                    fill=_mix(accent, bg, 0.3), radius=8)
        # card
        _round_rect(d, (sx, sy, sx + 240, sy + 32),
                    fill=(255, 255, 255), outline=_mix(text, bg, 0.5), radius=8)
        # leading emoji + text
        ex = sx + 8
        ex += _draw_text_with_emoji(img, (ex, sy + 8), em,
                                     font=f_row, fill=text,
                                     target_emoji_size=14)
        d.text((ex + 4, sy + 8), t, fill=text, font=f_row)
        # pill on the right
        pillx = sx + 192
        _round_rect(d, (pillx, sy + 8, pillx + 36, sy + 22),
                    fill=pills[i], radius=8)
        d.text((pillx + 6, sy + 9), "tag", fill=(255, 255, 255), font=f_pill)
        y += 38
    return img


def render_fun_photo_thumb(colors: dict) -> Image.Image:
    """Fun gallery preview: rainbow header + 3×2 rotated photo "stickers"."""
    bg = _hex(colors.get("bg", "#fef3c7"))
    accent = _hex(colors.get("accent", "#7c3aed"))
    secondary = _hex(colors.get("secondary", "#06b6d4"))

    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)
    f_title = _font(15)

    rainbow = ["#ec4899", "#f59e0b", "#10b981", "#06b6d4", "#8b5cf6"]
    label = "Photos"
    em_size = 18
    text_w = sum(int(d.textlength(ch, font=f_title)) + 1 for ch in label)
    total_w = em_size + 6 + text_w + 6 + em_size
    x = (SIZE[0] - total_w) // 2
    x += _draw_text_with_emoji(img, (x, 7), "🌟",
                                font=f_title, fill=accent,
                                target_emoji_size=em_size)
    x += 6
    for i, ch in enumerate(label):
        col = _hex(rainbow[i % len(rainbow)])
        d.text((x, 10), ch, fill=col, font=f_title)
        x += int(d.textlength(ch, font=f_title)) + 1
    x += 6
    _draw_text_with_emoji(img, (x, 7), "🌟",
                          font=f_title, fill=accent,
                          target_emoji_size=em_size)

    cols, rows, gap = 3, 2, 8
    pad = 18
    grid_top = 38
    avail_w = SIZE[0] - pad * 2 - gap * (cols - 1)
    avail_h = SIZE[1] - grid_top - pad - gap * (rows - 1)
    tw = avail_w // cols
    th = avail_h // rows
    tints = [
        _mix(secondary, bg, 0.2),
        _mix(accent, bg, 0.2),
        _hex("#fcd34d"),
        _hex("#86efac"),
        _hex("#fda4af"),
        _hex("#a5b4fc"),
    ]
    for r in range(rows):
        for c in range(cols):
            x0 = pad + c * (tw + gap)
            y0 = grid_top + r * (th + gap)
            shadow = (x0 + 3, y0 + 3, x0 + tw + 3, y0 + th + 3)
            _round_rect(d, shadow, fill=_mix(accent, bg, 0.3), radius=10)
            _round_rect(d, (x0, y0, x0 + tw, y0 + th),
                        fill=tints[(r * cols + c) % len(tints)],
                        outline=accent, radius=10)
            cx, cy = x0 + tw // 2, y0 + th // 2
            d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6),
                      fill=(255, 255, 255), outline=accent)
    return img


# Theme-slug-specific overrides take precedence over the kind-default renderers.
SLUG_RENDERERS = {
    ("any", "ratatui"):       render_ratatui_thumb,
    ("photo", "ratatui"):     render_ratatui_photo_thumb,
    ("video", "ratatui"):     render_ratatui_thumb,
    ("document", "ratatui"):  render_ratatui_thumb,
    ("any", "fun"):           render_fun_thumb,
    ("photo", "fun"):         render_fun_photo_thumb,
    ("video", "fun"):         render_fun_photo_thumb,
    ("document", "fun"):      render_fun_thumb,
}

KIND_RENDERERS = {
    "any": render_link_thumb,
    "photo": render_photo_thumb,
    "video": render_photo_thumb,        # if reintroduced
    "document": render_link_thumb,
}


def main() -> int:
    if not THEMES_ROOT.is_dir():
        print(f"no themes root at {THEMES_ROOT}", file=sys.stderr)
        return 1
    n = 0
    for kind_dir in sorted(THEMES_ROOT.iterdir()):
        if not kind_dir.is_dir():
            continue
        kind = kind_dir.name
        for theme_dir in sorted(kind_dir.iterdir()):
            tt = theme_dir / "theme.toml"
            if not tt.exists():
                continue
            slug = theme_dir.name
            renderer = SLUG_RENDERERS.get((kind, slug)) or KIND_RENDERERS.get(kind, render_link_thumb)
            colors = _load(tt)
            img = renderer(colors)
            out = theme_dir / "thumbnail.png"
            img.save(out, "PNG", optimize=True)
            print(f"wrote {out.relative_to(ROOT)}")
            n += 1
    print(f"\n{n} thumbnails written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
