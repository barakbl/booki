#!/usr/bin/env python3
"""
doctor.py — `booki doctor` — a visual health check.

Combines four signals:
  • System    — Python, OS, packages, binaries, services (from system_status.collect)
  • Sources   — registered + disabled + per-source item count and last sync date
  • Library   — total items, enriched %, vector DB doc count, latest sync date
  • Suggestions — actionable next-step prompts based on what's stale or missing

Read-only. Never modifies anything. Safe to run any time.

Usage:
    booki doctor               # pretty output (auto-colors when stderr is a TTY)
    booki doctor --no-color    # plain ASCII (logs / piping)
    booki doctor --config X    # alternate config.toml
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

import plugins
from .ingest import (
    chromadb_installed,
    parse_bookmark_file,
    vector_db_enabled,
)
from .sync import _is_disabled

log = logging.getLogger("booki.doctor")


_PROJECT_ROOT  = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = _PROJECT_ROOT / "config.toml"


# ─── ANSI palette ─────────────────────────────────────────────────────────────

class Style:
    """Tiny color helper — switches all codes to '' when disabled."""

    def __init__(self, enabled: bool):
        on = enabled
        self.RESET     = "\x1b[0m"  if on else ""
        self.BOLD      = "\x1b[1m"  if on else ""
        self.DIM       = "\x1b[2m"  if on else ""
        self.UNDERLINE = "\x1b[4m"  if on else ""
        self.RED       = "\x1b[31m" if on else ""
        self.GREEN     = "\x1b[32m" if on else ""
        self.YELLOW    = "\x1b[33m" if on else ""
        self.BLUE      = "\x1b[34m" if on else ""
        self.MAGENTA   = "\x1b[35m" if on else ""
        self.CYAN      = "\x1b[36m" if on else ""

    def bold(self, s):    return f"{self.BOLD}{s}{self.RESET}"
    def dim(self, s):     return f"{self.DIM}{s}{self.RESET}"
    def green(self, s):   return f"{self.GREEN}{s}{self.RESET}"
    def red(self, s):     return f"{self.RED}{s}{self.RESET}"
    def yellow(self, s):  return f"{self.YELLOW}{s}{self.RESET}"
    def blue(self, s):    return f"{self.BLUE}{s}{self.RESET}"
    def cyan(self, s):    return f"{self.CYAN}{s}{self.RESET}"
    def magenta(self, s): return f"{self.MAGENTA}{s}{self.RESET}"


# Glyphs — all 1 visible character wide so rows align with simple `<` padding.
GOK   = "✓"   # ok / passing / available
GBAD  = "✗"   # missing / failing / disabled
GINFO = "·"   # informational / not yet exercised
GWARN = "⚠"   # warning / stale / partial
GTIP  = "→"   # suggestion


# ─── Data loading ─────────────────────────────────────────────────────────────

def _load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _resolve_path(raw: str, config_path: Path) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (config_path.parent / p).resolve()
    return p


def _bookmarks_dir(cfg: dict, config_path: Path) -> Path:
    return _resolve_path(
        (cfg.get("bookmarks", {}) or {}).get("dir", "./bookmarks"),
        config_path,
    )


def _vector_db_dir(cfg: dict, config_path: Path) -> Path:
    return _resolve_path(
        (cfg.get("vector_db", {}) or {}).get("persist_dir", "./db"),
        config_path,
    )


def _scan_library(bookmarks_dir: Path) -> dict:
    """
    One pass over bookmarks/*.md.

    Returns a dict with:
        total              — int
        enriched           — int (has summary text)
        dead               — int (status: dead)
        removed            — int (removed_from_source / removed_from_browser)
        by_source          — {source_slug: count}
        by_kind            — {kind: count}
        last_sync_by_source — {source_slug: "YYYY-MM-DD"}
        last_sync_global   — "YYYY-MM-DD" (max across all items)
    """
    out = {
        "total": 0, "enriched": 0, "dead": 0, "removed": 0,
        "by_source": defaultdict(int),
        "by_kind": defaultdict(int),
        "last_sync_by_source": {},
        "last_sync_global": "",
    }
    if not bookmarks_dir.exists():
        return out

    last_sync_by: dict[str, str] = {}
    for md in bookmarks_dir.rglob("*.md"):
        fm = parse_bookmark_file(md)
        if not fm or not fm.get("url"):
            continue
        out["total"] += 1
        if str(fm.get("summary", "") or "").strip():
            out["enriched"] += 1
        if str(fm.get("status", "") or "") == "dead":
            out["dead"] += 1
        if fm.get("removed_from_source") or fm.get("removed_from_browser"):
            out["removed"] += 1

        # Source slug — prefer top-level dir name (chrome / youtube / …).
        try:
            src = md.relative_to(bookmarks_dir).parts[0]
        except (ValueError, IndexError):
            src = str(fm.get("source", "")).lower() or "unknown"
        out["by_source"][src] += 1

        kind = str(fm.get("kind", "bookmark") or "bookmark")
        out["by_kind"][kind] += 1

        ls = str(fm.get("last_sync", "") or "")
        if ls:
            if ls > out["last_sync_global"]:
                out["last_sync_global"] = ls
            prev = last_sync_by.get(src, "")
            if ls > prev:
                last_sync_by[src] = ls

    out["last_sync_by_source"] = last_sync_by
    out["by_source"] = dict(out["by_source"])
    out["by_kind"] = dict(out["by_kind"])
    return out


def _vector_db_count(persist_dir: Path, collection_name: str) -> Optional[int]:
    """Return doc count, or None if the DB / collection isn't there yet."""
    if not persist_dir.exists():
        return None
    if not chromadb_installed():
        return None
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(persist_dir))
        try:
            col = client.get_collection(collection_name)
        except Exception:
            return None
        return int(col.count())
    except Exception as e:
        log.debug("vector_db_count_failed", extra={"error": str(e)})
        return None


def _days_since(iso_date: str) -> Optional[int]:
    if not iso_date:
        return None
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (date.today() - d).days


def _human_count(n: int) -> str:
    return f"{n:,}"


# ─── Section renderers ────────────────────────────────────────────────────────

def _hr(s: Style, width: int) -> str:
    return s.dim("─" * max(20, width))


def _header(s: Style, width: int) -> None:
    title = "📚 Booki Doctor"
    print()
    print(f"  {s.bold(title)}  {s.dim('— health check')}")
    print(f"  {_hr(s, width - 2)}")


def _section(s: Style, title: str, glyph: str = "") -> None:
    g = f"{glyph}  " if glyph else ""
    print()
    print(f"  {s.bold(g + title)}")


def _row(s: Style, mark: str, mark_color: str, body: str, detail: str = "") -> None:
    """One indented row: `   ✓ body            detail`."""
    color_fn = {
        "green":   s.green,
        "red":     s.red,
        "yellow":  s.yellow,
        "blue":    s.blue,
        "cyan":    s.cyan,
        "dim":     s.dim,
    }.get(mark_color, s.dim)
    if detail:
        print(f"     {color_fn(mark)} {body}  {s.dim(detail)}")
    else:
        print(f"     {color_fn(mark)} {body}")


# ─── Sources section ──────────────────────────────────────────────────────────

def _print_sources(s: Style, cfg: dict, lib: dict) -> None:
    _section(s, "Sources", "🔌")

    by_source = lib["by_source"]
    last_sync_by = lib["last_sync_by_source"]

    for name in plugins.all_source_names():
        cls = plugins.get_source(name)
        if cls is None:
            continue

        item_count = by_source.get(name, 0)
        count_str = (f"{_human_count(item_count)} item"
                     + ("s" if item_count != 1 else "")
                     if item_count else "no items yet")

        if _is_disabled(name, cfg):
            mark, color = GBAD, "red"
            tag = "disabled in config.toml"
            label = f"{name:<10}"
            detail = f"{tag} · {count_str}" if item_count else tag
            _row(s, mark, color, label, detail)
            continue

        inst = cls()
        inst.configure((cfg.get("sources", {}) or {}).get(name, {}) or {})
        avail = inst.is_available()

        if avail:
            mark, color = GOK, "green"
        elif item_count:
            # Synced before, no longer reachable — yellow, not red.
            mark, color = GWARN, "yellow"
        else:
            mark, color = GINFO, "dim"

        label = f"{name:<10}"
        ls = last_sync_by.get(name, "")
        days = _days_since(ls)
        if ls:
            sync_tag = f"last sync {ls}"
            if days is not None:
                if days == 0:
                    sync_tag += " (today)"
                elif days == 1:
                    sync_tag += " (yesterday)"
                else:
                    sync_tag += f" ({days}d ago)"
        else:
            sync_tag = ""

        if not avail and not item_count:
            detail = inst.availability_hint() or "not available"
        else:
            parts = [count_str]
            if sync_tag:
                parts.append(sync_tag)
            if not avail:
                parts.append(inst.availability_hint() or "currently unavailable")
            detail = " · ".join(parts)

        _row(s, mark, color, label, detail)


# ─── Library section ──────────────────────────────────────────────────────────

def _print_paths(s: Style, cfg: dict, config_path: Path,
                 bookmarks_dir: Path, db_dir: Path) -> None:
    _section(s, "Paths", "📁")

    rows: list[tuple[str, str]] = [
        ("Booki",     str(_PROJECT_ROOT)),
        ("Config",    str(config_path) + ("" if config_path.exists() else "  (missing)")),
        ("Bookmarks", str(bookmarks_dir)),
        ("Vector DB", str(db_dir)),
    ]

    log_file = str((cfg.get("logs", {}) or {}).get("file") or "").strip()
    if log_file:
        rows.append(("Logs", str(_resolve_path(log_file, config_path))))

    dl_dir = str((cfg.get("downloads", {}) or {}).get("dir") or "").strip()
    if dl_dir:
        rows.append(("Downloads", str(_resolve_path(dl_dir, config_path))))

    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"     {s.dim(label.ljust(width))}  {value}")


def _print_library(s: Style, cfg: dict, lib: dict, db_count: Optional[int]) -> None:
    _section(s, "Library", "📚")

    total = lib["total"]
    enriched = lib["enriched"]
    pct = int((enriched / total) * 100) if total else 0
    dead = lib["dead"]
    removed = lib["removed"]

    if total == 0:
        _row(s, GWARN, "yellow", "library is empty", "run `booki sync` to populate")
        return

    # Items + breakdown
    kind_summary = ", ".join(
        f"{k}: {_human_count(v)}"
        for k, v in sorted(lib["by_kind"].items(), key=lambda x: -x[1])
    )
    _row(s, GOK, "green", f"{_human_count(total)} items", kind_summary)

    # Enrichment
    if enriched == total:
        _row(s, GOK, "green", f"all items enriched", f"{_human_count(enriched)}/{_human_count(total)}")
    elif enriched == 0:
        _row(s, GWARN, "yellow", "no items enriched",
             "run `booki sync --no-sync --enrich` to add LLM summaries")
    else:
        unenriched = total - enriched
        bar = _bar(pct, width=14)
        _row(s, GWARN if pct < 80 else GOK, "yellow" if pct < 80 else "green",
             f"{_human_count(enriched)}/{_human_count(total)} enriched ({pct}%)",
             f"{bar} · {_human_count(unenriched)} unenriched")

    # Vector DB
    if not vector_db_enabled(cfg):
        _row(s, GINFO, "dim", "vector DB disabled in config",
             "[vector_db] enabled = false — Ask tab + `booki ingest` are off")
    elif not chromadb_installed():
        _row(s, GINFO, "dim", "vector DB skipped",
             "ChromaDB not installed (optional) — Ask tab + `booki ingest` are disabled")
    elif db_count is None:
        _row(s, GINFO, "dim", "vector DB not built",
             "run `booki ingest` to build the search index (optional)")
    elif db_count < total:
        _row(s, GWARN, "yellow",
             f"vector DB has {_human_count(db_count)} docs",
             f"{_human_count(total - db_count)} fewer than the library — re-run `booki ingest`")
    else:
        _row(s, GOK, "green",
             f"{_human_count(db_count)} docs in vector DB",
             "search index is up to date")

    # Last sync
    ls = lib["last_sync_global"]
    days = _days_since(ls)
    if not ls:
        _row(s, GINFO, "dim", "no last_sync timestamps found")
    elif days is None:
        _row(s, GINFO, "dim", f"latest last_sync: {ls}")
    elif days == 0:
        _row(s, GOK, "green", f"latest sync: {ls} (today)")
    elif days <= 1:
        _row(s, GOK, "green", f"latest sync: {ls} (yesterday)")
    elif days <= 7:
        _row(s, GOK, "green", f"latest sync: {ls}", f"{days} days ago")
    elif days <= 30:
        _row(s, GWARN, "yellow", f"latest sync: {ls}",
             f"{days} days ago — consider running `booki sync`")
    else:
        _row(s, GBAD, "red", f"latest sync: {ls}",
             f"{days} days ago — definitely run `booki sync`")

    # Quality flags
    if dead:
        _row(s, GWARN, "yellow",
             f"{_human_count(dead)} dead links flagged",
             "Wayback Machine archives recorded — visit each item to confirm")
    if removed:
        _row(s, GINFO, "dim",
             f"{_human_count(removed)} items marked removed at source",
             "your notes / tags are preserved")


def _bar(pct: int, *, width: int = 14) -> str:
    """Mini progress bar `[████░░░░░░░░░░]` — dim helper, no color."""
    pct = max(0, min(100, pct))
    filled = int(round(width * pct / 100))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


# ─── Suggestions section ──────────────────────────────────────────────────────

def _print_suggestions(s: Style, cfg: dict, lib: dict, db_count: Optional[int],
                       sys_payload: dict) -> None:
    """Data-driven nudges. We bias toward fewer, more targeted suggestions."""
    tips: list[tuple[str, str]] = []   # (priority_color, message)

    # 1. Empty library — the only thing to do is sync.
    if lib["total"] == 0:
        tips.append(("cyan", "run `booki sync` — your library is empty"))
    else:
        # 2. Stale sync (when the library exists)
        days = _days_since(lib["last_sync_global"])
        if days is not None:
            if days > 30:
                tips.append(("red", f"run `booki sync` — last sync was {days} days ago"))
            elif days > 7:
                tips.append(("yellow", f"run `booki sync` — last sync was {days} days ago"))

        # 3. Unenriched items
        unenriched = lib["total"] - lib["enriched"]
        if unenriched >= 10:
            pct = int(lib["enriched"] / lib["total"] * 100) if lib["total"] else 0
            tips.append(("yellow",
                f"run `booki sync --no-sync --enrich` — {_human_count(unenriched)} item(s) "
                f"unenriched ({pct}% enriched)"))

        # 4. Vector DB out of sync — only nudge when the user actually opted
        # into vector search (chromadb installed AND not disabled in config).
        if vector_db_enabled(cfg) and chromadb_installed():
            if db_count is None:
                tips.append(("cyan", "run `booki ingest` — vector index not built yet"))
            elif db_count < lib["total"]:
                tips.append(("yellow",
                    f"run `booki ingest` — index has {_human_count(db_count)} docs, "
                    f"library has {_human_count(lib['total'])}"))

        # 5. Dead-link sweep (only when the user has links)
        if lib["dead"] == 0 and any(k == "bookmark" for k in lib["by_kind"]):
            # No dead-link checks done in a long time? hard to tell — only suggest if very stale.
            if days is not None and days > 30:
                tips.append(("dim",
                    "consider `booki sync --check-dead-links` "
                    "— re-verify URLs that haven't been checked"))

    # 6. Required system pieces missing
    for c in sys_payload["checks"]:
        if not c["ok"] and c["required"]:
            tips.append(("red",
                f"install: {c.get('fix_command','') or c['label']} "
                f"({c['feature']})"))

    # 7. Ollama down when it's the chosen LLM
    llm_provider = str((cfg.get("llm") or {}).get("provider") or "").lower()
    if llm_provider == "ollama":
        for c in sys_payload["checks"]:
            if c["id"] == "svc-ollama" and not c["ok"]:
                tips.append(("yellow",
                    "start Ollama (`ollama serve`) — it's your configured LLM provider"))

    # 8. API key missing for cloud providers
    for c in sys_payload["checks"]:
        if c["id"].startswith("env-") and c["required"] and not c["ok"]:
            tips.append(("red", f"export {c['label']}=… in your shell rc"))

    _section(s, "Suggestions", "💡")
    if not tips:
        _row(s, GOK, "green", "everything looks good — nothing to do",
             "your library is fresh and indexed")
        return
    for color, msg in tips:
        _row(s, GTIP, color, msg)


# ─── Footer ───────────────────────────────────────────────────────────────────

def _footer(s: Style, width: int) -> None:
    print()
    print(f"  {_hr(s, width - 2)}")
    print(f"  {s.dim('Run `booki <subcommand> --help` for any command, or `booki doctor --no-color` for plain output.')}")
    print()


# ─── Entry point ──────────────────────────────────────────────────────────────

def run(cfg: dict, config_path: Path, *, color: bool) -> int:
    s = Style(color)
    width = shutil.get_terminal_size((100, 20)).columns

    bookmarks_dir = _bookmarks_dir(cfg, config_path)
    db_dir        = _vector_db_dir(cfg, config_path)
    db_collection = (cfg.get("vector_db", {}) or {}).get("collection", "bookmarks")

    _header(s, width)

    # System checks first — fast, gives the user something to read while we
    # scan the library below. We need the payload twice (once for display,
    # once for suggestions), so collect once.
    from . import system_status
    sys_payload = system_status.collect(cfg)
    _print_system_from_payload(s, sys_payload)

    # Library scan — this is the slow part for big libraries. Print a progress
    # nudge so the user knows we're working, but only when stdout is a TTY:
    # writing carriage-return tricks to a pipe just leaves litter.
    show_progress = bookmarks_dir.exists() and sys.stdout.isatty()
    if show_progress:
        print()
        print(f"  {s.dim('Scanning ' + str(bookmarks_dir) + '…')}", end="\r", flush=True)
    lib = _scan_library(bookmarks_dir)
    if show_progress:
        # Erase the progress line so it doesn't pollute the output.
        sys.stdout.write("\x1b[2K\r")
        sys.stdout.flush()

    db_count = _vector_db_count(db_dir, db_collection)

    _print_paths(s, cfg, config_path, bookmarks_dir, db_dir)
    _print_sources(s, cfg, lib)
    _print_library(s, cfg, lib, db_count)
    _print_suggestions(s, cfg, lib, db_count, sys_payload)
    _footer(s, width)
    return 0


def _print_system_from_payload(s: Style, payload: dict) -> None:
    """Same as _print_system but accepts a pre-collected payload."""
    plat = payload.get("platform", {})
    _section(s, "System", "🖥️")
    print(f"     {s.dim('Python')}  {plat.get('python','?')}  "
          f"{s.dim('·')}  {plat.get('system','?')} {plat.get('machine','')}  "
          f"{s.dim('·')}  pkg manager: {plat.get('package_manager','?')}")

    by_cat: dict[str, list] = {}
    for c in payload["checks"]:
        by_cat.setdefault(c["category"], []).append(c)

    for cat in ("Python packages", "External tools", "Services", "Environment"):
        items = by_cat.get(cat, [])
        if not items:
            continue
        print(f"     {s.dim(cat + ':')}")
        for c in items:
            ok = c["ok"]
            required = c["required"]
            if ok:
                mark, color = GOK, "green"
                detail = c.get("detail", "")
            elif required:
                mark, color = GBAD, "red"
                detail = "REQUIRED — " + (c.get("fix_command", "") or c.get("detail", ""))
            else:
                mark, color = GINFO, "dim"
                detail = "optional — " + (c.get("fix_command", "") or c.get("detail", ""))
            label = f"{c['label']:<26}"
            _row(s, mark, color, label, detail)

    summary = payload["summary"]
    tail = (f"{summary['ok']}/{summary['total']} ok"
            f" · {summary['missing_required']} required missing"
            f" · {summary['missing_optional']} optional missing")
    print(f"     {s.dim(tail)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="booki doctor",
        description="Visual health check — what's installed, what's working, what to run next.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"config.toml to read (default: {DEFAULT_CONFIG})")
    parser.add_argument("--no-color", action="store_true",
                        help="disable ANSI colors (auto-disabled when stdout isn't a TTY)")
    args = parser.parse_args()

    cfg = _load_config(args.config)

    color = sys.stdout.isatty() and not args.no_color and os.environ.get("NO_COLOR") in (None, "")
    sys.exit(run(cfg, args.config, color=color))


if __name__ == "__main__":
    main()
