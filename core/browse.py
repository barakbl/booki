#!/usr/bin/env python3
"""
browse.py — fzf-powered terminal browser over Booki items.

Each row in the list shows a kind-glyph + compact title + a short context tail
(channel for videos, folder path for bookmarks). The right-hand preview pane
shows everything we know about the selected item — the same fields the web UI
surfaces.

Shortcuts (shown in the fzf header):
    Enter      open the URL in your default browser (and exit)
    Ctrl-O     open the URL without exiting
    Ctrl-Y     copy the URL to the clipboard
    Ctrl-E     open the underlying .md file in $EDITOR
    Ctrl-/     toggle preview pane
    Ctrl-R     reload the list from disk

Requires:
    fzf binary on PATH  (`brew install fzf`)
    the normal Booki deps (this script does NOT load chromadb — fast startup)

Usage:
    booki browse                       # launch
    booki browse --list                # internal — emit TSV rows
    booki browse --preview-file PATH   # internal — render preview
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

# Reuse only the frontmatter parser — no chromadb import, snappy startup.
from .store import _parse_yaml_block


ROOT           = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

# Kind → glyph map, built lazily from the plugin registry on first access.
# Adding a new kind is now a `kind_specs()` declaration on the plugin that
# introduces it — no edit here required.
_KIND_GLYPH_CACHE: dict[str, str] | None = None


def _kind_glyph_map() -> dict[str, str]:
    global _KIND_GLYPH_CACHE
    if _KIND_GLYPH_CACHE is None:
        # Lazy import: the fzf preview is hot-path; we don't want plugin
        # discovery to run unless we actually render a row.
        import plugins
        _KIND_GLYPH_CACHE = {
            slug: spec.get("glyph") or "·"
            for slug, spec in plugins.all_kind_specs().items()
        }
    return _KIND_GLYPH_CACHE


def kind_glyph(kind: str) -> str:
    return _kind_glyph_map().get(kind, "·")


# ─── ANSI color helpers ───────────────────────────────────────────────────────

def _c(s, code): return f"\x1b[{code}m{s}\x1b[0m"
def bold(s):      return _c(s, "1")
def dim(s):       return _c(s, "2")
def red(s):       return _c(s, "31")
def green(s):     return _c(s, "32")
def yellow(s):    return _c(s, "33")
def blue(s):      return _c(s, "34")
def magenta(s):   return _c(s, "35")
def cyan(s):      return _c(s, "36")
def underline(s): return _c(s, "4")


# ─── Config + item loading ────────────────────────────────────────────────────

def _load_config() -> dict:
    if DEFAULT_CONFIG.exists():
        with open(DEFAULT_CONFIG, "rb") as f:
            return tomllib.load(f)
    return {}


def _bookmarks_dir(cfg: dict) -> Path:
    raw = cfg.get("bookmarks", {}).get("dir", "./bookmarks")
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p).resolve()


def _parse_frontmatter(path: Path) -> dict | None:
    """Robust load — broken frontmatter just yields None instead of raising
    so a single bad file doesn't poison the whole fzf list. Loader handles
    error reporting; for browse we only need the success path."""
    from .loader import load_bookmark
    fm, _err = load_bookmark(path)
    return fm


def _iter_items(bookmarks_dir: Path):
    for md in sorted(bookmarks_dir.rglob("*.md")):
        fm = _parse_frontmatter(md)
        if not fm or not fm.get("url"):
            continue
        yield md, fm


# ─── List line formatting ─────────────────────────────────────────────────────

def _compact_title(fm: dict, max_len: int = 90) -> str:
    title = str(fm.get("title", "") or "").strip() or "(untitled)"
    title = title.replace("\n", " ").replace("\t", " ").replace("\r", " ")
    return title if len(title) <= max_len else title[: max_len - 1] + "…"


def _context_tail(fm: dict) -> str:
    kind = str(fm.get("kind", "bookmark") or "bookmark")
    if kind == "video":
        ch = str(fm.get("channel", "") or "").strip()
        return f"  ·  {ch}" if ch else ""
    if kind == "channel":
        return "  ·  subscribed" if fm.get("subscribed") else ""
    bp = str(fm.get("browser_path", "") or "").strip()
    if not bp:
        return ""
    parts = bp.split(" › ")
    tail = " › ".join(parts[-2:]) if len(parts) > 2 else bp
    return f"  ·  {tail}"


def _fzf_display(fm: dict) -> str:
    kind  = str(fm.get("kind", "bookmark") or "bookmark")
    glyph = kind_glyph(kind)
    imp   = int(fm.get("importance", 0) or 0)
    imp_tag = f"★{imp}" if imp else "  "

    flags = []
    if fm.get("liked"):                                    flags.append("♥")
    if fm.get("watched") and not fm.get("liked"):          flags.append("✓")
    if fm.get("subscribed_to_channel") and kind == "video": flags.append("↻")
    flag_tag = " ".join(flags)

    extra = ""
    if fm.get("removed_from_browser") or fm.get("removed_from_source"):
        extra += "  [removed]"
    if fm.get("status") == "dead":
        extra += "  [dead]"

    prefix = f"{glyph} {imp_tag} {flag_tag}".rstrip()
    return f"{prefix}  {_compact_title(fm)}{_context_tail(fm)}{extra}"


def build_list(bookmarks_dir: Path):
    """
    Produce TSV rows: <url>\\t<path>\\t<display>

    fzf gets three fields; the selected row exposes the URL ({1}) and file
    path ({2}) to key bindings, while only the display ({3}) is rendered.
    """
    for md, fm in _iter_items(bookmarks_dir):
        url = str(fm.get("url", "")).replace("\t", " ").replace("\n", " ")
        path_str = str(md)
        display = _fzf_display(fm).replace("\t", " ")
        yield f"{url}\t{path_str}\t{display}"


# ─── Preview renderer ─────────────────────────────────────────────────────────

def _fmt_count(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def render_preview(path: Path) -> None:
    fm = _parse_frontmatter(path)
    if not fm:
        print(red(f"cannot parse: {path}"))
        return

    kind  = str(fm.get("kind", "bookmark") or "bookmark")
    glyph = kind_glyph(kind)

    print(bold(f"{glyph}  {fm.get('title', '(untitled)')}"))
    print(underline(blue(str(fm.get("url", "")))))
    print()

    imp = int(fm.get("importance", 0) or 0)
    head = [
        dim(str(fm.get("source", "") or "?")),
        dim(kind),
        yellow(f"★{imp}") if imp else dim("★0"),
    ]
    status = str(fm.get("status", "") or "")
    if status and status not in ("alive", "unchecked"):
        head.append(red(f"[{status}]"))
    if fm.get("removed_from_browser") or fm.get("removed_from_source"):
        head.append(red("[removed]"))
    print("  ·  ".join(head))

    if bp := str(fm.get("browser_path", "") or "").strip():
        print(dim(bp))

    # YouTube-specific block
    if kind in ("video", "channel"):
        print()
        lines = []
        if ch := fm.get("channel"):
            lines.append(f"  channel:       {cyan(str(ch))}")
        if cid := fm.get("channel_id"):
            lines.append(dim(f"  channel_id:    {cid}"))
        if kind == "video":
            if pub := fm.get("published_at"):
                lines.append(f"  published:     {pub}")
            if dur := fm.get("duration"):
                lines.append(f"  duration:      {dur}")
            if (vc := fm.get("view_count")) is not None:
                lines.append(f"  views:         {_fmt_count(vc)}")
            if (lc := fm.get("like_count")) is not None:
                lines.append(f"  likes:         {_fmt_count(lc)}")
            flags = []
            if fm.get("liked"):                 flags.append(green("♥ liked"))
            if fm.get("watched"):               flags.append(green("✓ watched"))
            if fm.get("subscribed_to_channel"): flags.append(magenta("↻ sub-ch"))
            if flags:
                lines.append("  flags:         " + ", ".join(flags))
        else:
            if fm.get("subscribed"):
                lines.append(f"  status:        {magenta('→ subscribed')}")
            if (sc := fm.get("subscriber_count")) is not None:
                lines.append(f"  subscribers:   {_fmt_count(sc)}")
            if (vc := fm.get("video_count")) is not None:
                lines.append(f"  videos:        {_fmt_count(vc)}")
        if lines:
            print("\n".join(lines))

    if tags := fm.get("tags") or []:
        print()
        print(dim("tags:     ") + " ".join(cyan(f"#{t}") for t in tags))
    if ytags := fm.get("youtube_tags") or []:
        print(dim("yt-tags:  ") + " ".join(dim(str(t)) for t in ytags))
    if kws := fm.get("keywords") or []:
        print(dim("keywords: ") + " ".join(yellow(str(k)) for k in kws))

    if summary := str(fm.get("summary", "") or "").strip():
        print()
        print(bold("summary"))
        print(summary)

    if desc := str(fm.get("description", "") or "").strip():
        print()
        print(bold("description"))
        print(desc)

    if notes := str(fm.get("notes", "") or "").strip():
        print()
        print(bold("notes"))
        print(notes)

    print()
    meta = []
    if db := fm.get("date_bookmarked"):   meta.append(f"added: {db}")
    if ls := fm.get("last_sync"):         meta.append(f"synced: {ls}")
    if le := fm.get("last_enriched"):     meta.append(f"enriched: {le}")
    if au := str(fm.get("archive_url", "") or "").strip():
        meta.append(f"archive: {au}")
    if meta:
        print(dim(" · ".join(meta)))
    try:
        cfg = _load_config()
        rel = path.relative_to(_bookmarks_dir(cfg))
        print(dim(f"file: {rel}"))
    except Exception:
        print(dim(f"file: {path}"))


# ─── Launcher ────────────────────────────────────────────────────────────────

def _clipboard_cmd() -> str:
    if sys.platform == "darwin":
        return "printf %s {1} | pbcopy"
    # Linux: prefer Wayland, fall back to X11
    return "printf %s {1} | (wl-copy 2>/dev/null || xclip -selection clipboard 2>/dev/null || xsel --clipboard --input)"


def _open_cmd() -> str:
    if sys.platform == "darwin":
        return "open {1}"
    return "xdg-open {1}"


def _editor_cmd() -> str:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    return f"{editor} {{2}}"


def launch() -> int:
    if shutil.which("fzf") is None:
        sys.exit("fzf not found on PATH. Install with:  brew install fzf")

    cfg = _load_config()
    bdir = _bookmarks_dir(cfg)
    if not bdir.exists():
        sys.exit(f"Bookmarks directory not found: {bdir}\nRun `booki sync` first.")

    lines = list(build_list(bdir))
    if not lines:
        sys.exit(f"No items in {bdir}. Run `booki sync` first.")

    booki_bin = ROOT / "booki"
    preview_cmd = f"{booki_bin} browse --preview-file {{2}}"
    reload_cmd  = f"{booki_bin} browse --list"

    header = (
        " Enter: open · Ctrl-O: open · Ctrl-Y: copy URL · "
        "Ctrl-E: edit MD · Ctrl-R: reload · Ctrl-/: preview"
    )

    args = [
        "fzf",
        "--ansi",
        "--exact",
        "--delimiter=\t",
        "--with-nth=3",
        "--prompt=booki❯ ",
        "--header", header,
        "--preview", preview_cmd,
        "--preview-window=right,60%,wrap",
        "--bind", f"enter:execute-silent({_open_cmd()})+accept",
        "--bind", f"ctrl-o:execute-silent({_open_cmd()})",
        "--bind", f"ctrl-y:execute-silent({_clipboard_cmd()})",
        "--bind", f"ctrl-e:execute({_editor_cmd()})",
        "--bind", "ctrl-/:toggle-preview",
        "--bind", f"ctrl-r:reload({reload_cmd})",
    ]

    proc = subprocess.run(args, input="\n".join(lines) + "\n", text=True)
    return proc.returncode


def list_only() -> None:
    cfg = _load_config()
    bdir = _bookmarks_dir(cfg)
    for line in build_list(bdir):
        print(line)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="fzf browser over Booki items.")
    p.add_argument("--list",         action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--preview-file", metavar="PATH",      help=argparse.SUPPRESS)
    args = p.parse_args()

    if args.preview_file:
        render_preview(Path(args.preview_file))
        return
    if args.list:
        list_only()
        return
    sys.exit(launch())


if __name__ == "__main__":
    main()
