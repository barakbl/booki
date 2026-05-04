#!/usr/bin/env python3
"""
bootstrap.py — `booki bootstrap` — interactive config.toml builder.

Walks the user through one section at a time (Bookmarks, Vector DB, Embeddings,
LLM, Web, Sources, Logs) with sensible defaults and short explanations.
Probes the environment when it can: enumerates Ollama models, detects which
sources are currently available.

Refuses to overwrite an existing config file. If the chosen path is occupied,
re-prompts until the user picks a free path or aborts (Ctrl-C).

Usage:
    booki bootstrap                # interactive
    booki bootstrap --output X     # also interactive, but write to X
"""

from __future__ import annotations

import argparse
import os
import shutil as shutil_mod
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import plugins
from .doctor import Style


_PROJECT_ROOT  = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = _PROJECT_ROOT / "config.toml"
# The Rust booki-manager reads this same file to find the active Booki
# checkout. Keeping the path in sync here means autostart-launched
# managers (which don't inherit shell env, so $BOOKI_HOME is empty) just
# work after `booki bootstrap`.
MANAGER_SETTINGS = (
    Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
    / "booki-manager" / "settings.json"
)


# ─── Prompter ─────────────────────────────────────────────────────────────────

class Prompter:
    """Tiny interactive prompt helper — text / choice / yes-no, all themed."""

    def __init__(self, style: Style):
        self.s = style

    # — building blocks —

    def _hint(self, help_text: str) -> None:
        if help_text:
            for line in help_text.splitlines():
                print(f"     {self.s.dim(line)}")

    def text(self, question: str, default: str = "", *, help: str = "",
             validate: Optional[Callable[[str], Optional[str]]] = None) -> str:
        """Free-text prompt. Empty input → default. `validate` returns error msg or None."""
        self._hint(help)
        suffix = f" [{self.s.cyan(default)}]" if default else ""
        while True:
            try:
                raw = input(f"  {self.s.bold(question)}{suffix}: ").strip()
            except EOFError:
                raise KeyboardInterrupt
            value = raw or default
            if not value:
                print(f"     {self.s.red('(required)')}")
                continue
            if validate:
                err = validate(value)
                if err:
                    print(f"     {self.s.red('✗ ' + err)}")
                    continue
            return value

    def choice(self, question: str, options: list[tuple[str, str]],
               default_index: int = 0, *, help: str = "") -> str:
        """Pick-from-list prompt. `options` is [(value, label), ...]."""
        self._hint(help)
        print(f"  {self.s.bold(question)}")
        for i, (_, label) in enumerate(options, 1):
            star = self.s.dim(" ★") if (i - 1) == default_index else ""
            print(f"     {self.s.cyan(str(i))}. {label}{star}")
        while True:
            try:
                raw = input(f"  Choice [{self.s.cyan(str(default_index + 1))}]: ").strip()
            except EOFError:
                raise KeyboardInterrupt
            if not raw:
                return options[default_index][0]
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(options):
                    return options[idx][0]
            print(f"     {self.s.red(f'✗ pick a number 1-{len(options)}')}")

    def yes_no(self, question: str, default: bool = True, *, help: str = "") -> bool:
        self._hint(help)
        tag = "[Y/n]" if default else "[y/N]"
        while True:
            try:
                raw = input(f"  {self.s.bold(question)} {self.s.cyan(tag)}: ").strip().lower()
            except EOFError:
                raise KeyboardInterrupt
            if not raw:
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            print(f"     {self.s.red('✗ answer y or n')}")

    def header(self, glyph: str, title: str, blurb: str = "") -> None:
        print()
        print(f"  {glyph}  {self.s.bold(title)}")
        if blurb:
            print(f"     {self.s.dim(blurb)}")
        print()


# ─── Probes ───────────────────────────────────────────────────────────────────

def _probe_ollama_models() -> list[dict]:
    """Run `ollama list` and parse rows. Returns [] on any failure."""
    if not shutil_mod.which("ollama"):
        return []
    try:
        proc = subprocess.run(
            ["ollama", "list"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    lines = proc.stdout.strip().splitlines()
    if "NAME" not in lines[0].upper():
        return []
    models: list[dict] = []
    for line in lines[1:]:
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        size = f"{parts[2]} {parts[3]}" if len(parts) >= 4 else ""
        models.append({"name": name, "size": size})
    return models


def _probe_source_available(name: str) -> bool:
    """Cheap availability check — same one `booki sync --list-sources` does."""
    cls = plugins.get_source(name)
    if cls is None:
        return False
    try:
        return bool(cls().is_available())
    except Exception:
        return False


# ─── Wizard answers ───────────────────────────────────────────────────────────

@dataclass
class BootstrapAnswers:
    output_path:     Path = field(default_factory=lambda: DEFAULT_CONFIG)

    # bookmarks
    bookmarks_dir:   str = "./bookmarks"
    min_importance:  int = 0

    # vector db
    db_dir:          str = "./db"
    db_collection:   str = "bookmarks"

    # embeddings
    em_provider:     str = "local"
    em_local_model:  str = "all-MiniLM-L6-v2"
    em_openai_model: str = "text-embedding-3-small"

    # llm
    llm_provider:    str = "ollama"
    llm_model:       str = "llama3.2:3b"
    llm_base_url:    str = "http://localhost:11434"
    llm_n_results:   int = 5

    # web
    web_host:        str = "127.0.0.1"
    web_port:        int = 8765

    # sources — name → enabled?
    sources_enabled: dict[str, bool] = field(default_factory=dict)

    # booki-manager (menubar sidecar). When `manager_setup` is True,
    # bootstrap also writes ~/.config/booki-manager/settings.json so the
    # tray app finds this checkout without depending on $BOOKI_HOME.
    manager_setup:        bool = True
    manager_booki_home:   str  = ""    # filled in by _ask_manager
    manager_enrich:       bool = True
    manager_enrich_meta:  bool = True


# ─── Sections ─────────────────────────────────────────────────────────────────

def _pick_output_path(p: Prompter, requested: Optional[Path]) -> Path:
    """Decide where to write. Refuses any path that exists."""
    if requested is not None:
        if requested.exists():
            print(f"  {p.s.red('✗ ' + str(requested) + ' exists — pick a different path')}")
        else:
            return requested

    default = DEFAULT_CONFIG
    if default.exists():
        # Find a free `.local.toml` / `.new.toml` / numbered fallback
        candidate = default.with_name("config.local.toml")
        i = 1
        while candidate.exists():
            i += 1
            candidate = default.with_name(f"config.local.{i}.toml")
        default = candidate
        print(f"  {p.s.yellow('⚠')} {DEFAULT_CONFIG} already exists.")
        print(f"     {p.s.dim('Booki will not overwrite it. Pick a different filename.')}")
        print()

    while True:
        raw = p.text("Output path", str(default),
                     help="Where to save the new config. Relative paths resolve from cwd.")
        out = Path(raw).expanduser().resolve()
        if out.exists():
            print(f"     {p.s.red(f'✗ {out} exists — Booki will not overwrite. Pick another path.')}")
            continue
        if out.is_dir():
            print(f"     {p.s.red(f'✗ {out} is a directory.')}")
            continue
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"     {p.s.red(f'✗ cannot create parent dir: {e}')}")
            continue
        return out


def _ask_bookmarks(p: Prompter, ans: BootstrapAnswers) -> None:
    p.header("📚", "Bookmarks", "Where the .md files for each item live.")
    ans.bookmarks_dir = p.text("Bookmarks directory", ans.bookmarks_dir,
                               help="Relative paths resolve from the config file's location.")
    raw = p.text("Minimum importance to index", str(ans.min_importance),
                 help="Items with importance below this score are skipped at ingest. Range 0-10.")
    try:
        ans.min_importance = max(0, min(10, int(raw)))
    except ValueError:
        ans.min_importance = 0


def _ask_vector_db(p: Prompter, ans: BootstrapAnswers) -> None:
    p.header("🧠", "Vector database", "ChromaDB persistence.")
    ans.db_dir = p.text("Persist directory", ans.db_dir,
                        help="Where ChromaDB stores its files (gitignore this).")
    ans.db_collection = p.text("Collection name", ans.db_collection)


def _ask_embeddings(p: Prompter, ans: BootstrapAnswers) -> None:
    p.header("🔍", "Embeddings", "How items are vectorised for semantic search.")
    ans.em_provider = p.choice("Embedding provider", [
        ("local",  "local  — sentence-transformers (free, offline, ~80 MB model)"),
        ("openai", "openai — text-embedding-3-small (cloud, requires OPENAI_API_KEY)"),
    ], default_index=0)
    if ans.em_provider == "local":
        ans.em_local_model = p.text("Local model", ans.em_local_model,
                                    help="Any sentence-transformers model name (e.g. all-MiniLM-L6-v2).")
    else:
        ans.em_openai_model = p.text("OpenAI model", ans.em_openai_model)
        if not os.environ.get("OPENAI_API_KEY"):
            print(f"     {p.s.yellow('⚠ OPENAI_API_KEY is not set in your environment.')}")


def _ask_llm(p: Prompter, ans: BootstrapAnswers) -> None:
    p.header("🤖", "LLM", "Used for enrichment summaries and chat answers.")
    ans.llm_provider = p.choice("LLM provider", [
        ("ollama", "ollama — fully local (free, requires Ollama running)"),
        ("claude", "claude — Anthropic API (requires ANTHROPIC_API_KEY)"),
        ("openai", "openai — OpenAI API (requires OPENAI_API_KEY)"),
    ], default_index=0)

    if ans.llm_provider == "ollama":
        ans.llm_base_url = p.text("Ollama base URL", ans.llm_base_url)
        print()
        print(f"     {p.s.dim('Detecting installed Ollama models…')}")
        models = _probe_ollama_models()
        if models:
            options = [(m["name"], f"{m['name']:<24} {p.s.dim(m['size'])}") for m in models]
            default_idx = next((i for i, m in enumerate(models)
                                if m["name"] == ans.llm_model), 0)
            ans.llm_model = p.choice("Pick a model", options, default_index=default_idx)
        else:
            print(f"     {p.s.yellow('⚠ Could not list Ollama models — is the daemon running?')}")
            ans.llm_model = p.text("Model name", ans.llm_model,
                                   help="e.g. llama3.2:3b · mistral · gemma3 · qwen2.5:7b")
    elif ans.llm_provider == "claude":
        ans.llm_model = p.text("Claude model", "claude-sonnet-4-6",
                               help="e.g. claude-sonnet-4-6 · claude-haiku-4-5-20251001")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print(f"     {p.s.yellow('⚠ ANTHROPIC_API_KEY is not set in your environment.')}")
    else:
        ans.llm_model = p.text("OpenAI model", "gpt-4o-mini",
                               help="e.g. gpt-4o-mini · gpt-4o")
        if not os.environ.get("OPENAI_API_KEY"):
            print(f"     {p.s.yellow('⚠ OPENAI_API_KEY is not set in your environment.')}")

    raw = p.text("Number of results per query", str(ans.llm_n_results),
                 help="How many items to retrieve from the vector DB before asking the LLM.")
    try:
        ans.llm_n_results = max(1, int(raw))
    except ValueError:
        pass


def _ask_web(p: Prompter, ans: BootstrapAnswers) -> None:
    p.header("🌐", "Web UI", "FastAPI server for browse / search / edit / export.")
    ans.web_host = p.text("Bind host", ans.web_host,
                          help="127.0.0.1 = localhost only · 0.0.0.0 = expose on the LAN")
    raw = p.text("Port", str(ans.web_port))
    try:
        ans.web_port = max(1, min(65535, int(raw)))
    except ValueError:
        pass


def _ask_manager(p: Prompter, ans: BootstrapAnswers) -> None:
    """Optional menubar-sidecar setup. Writes ~/.config/booki-manager/settings.json
    so autostart-launched tray apps find this checkout — they can't rely on
    $BOOKI_HOME because login items don't inherit shell env."""
    p.header("🖥️ ", "Manager (menubar sidecar)",
             "Optional Rust tray app that watches your bookmarks and runs sync/ingest on a schedule.")

    ans.manager_setup = p.yes_no(
        "Set up booki-manager?",
        default=True,
    )
    if not ans.manager_setup:
        return

    # Default the manager's Booki home to wherever the config we're writing
    # lives — that's almost always the right answer (the user is bootstrapping
    # *for* that checkout). Fall back to PWD when the parent isn't a checkout.
    parent = ans.output_path.parent
    default_home = parent if (parent / "booki").is_file() else Path.cwd()
    ans.manager_booki_home = p.text(
        "Booki home for the manager",
        str(default_home),
        help="The directory containing the `booki` script and config.toml. "
             "The manager reads this from settings.json to survive autostart "
             "(login items don't inherit $BOOKI_HOME from your shell).",
    )
    ans.manager_enrich = p.yes_no(
        "Run --enrich on every manager-triggered sync?",
        default=True,
    )
    ans.manager_enrich_meta = p.yes_no(
        "Run --enrich-meta (plugin enrichers: github / photo / document / …)?",
        default=True,
    )


def _ask_sources(p: Prompter, ans: BootstrapAnswers) -> None:
    p.header("🔌", "Sources", "Pick which built-in plugins to enable. You can flip these any time later.")

    notes = {
        "chrome":    "Chrome bookmarks (auto-detected if Chrome is installed)",
        "safari":    "Safari bookmarks — needs Full Disk Access for Terminal on macOS",
        "firefox":   "Firefox bookmarks (auto-detected if Firefox is installed)",
        "youtube":   "YouTube account — needs OAuth, see plugins/youtube/README.md",
        "rss":       "RSS / Atom feeds — feed list goes in [[sources.rss.feeds]]",
        "directory": "Local file trees — paths go in [[sources.directory.dirs]]",
    }

    for name in plugins.all_source_names():
        avail = _probe_source_available(name)
        # Reasonable defaults: auto-enable ones that work today; auto-disable
        # ones that need extra setup (youtube oauth, firefox not installed).
        default = avail
        if name in ("youtube",) and not avail:
            default = False
        if name in ("rss", "directory"):
            default = avail or True   # cheap to enable; user just adds feeds/paths later
        glyph = p.s.green("✓") if avail else p.s.dim("·")
        note = notes.get(name, "")
        print(f"     {glyph} {p.s.bold(name)}  {p.s.dim(note)}")
        ans.sources_enabled[name] = p.yes_no("    Enable?", default=default)
        print()


# ─── TOML rendering ───────────────────────────────────────────────────────────

def _quote(s: str) -> str:
    """Minimal TOML basic-string quoting."""
    escaped = (s.replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\n", "\\n")
                .replace("\t", "\\t"))
    return f"\"{escaped}\""


def _render_manager_section(a: BootstrapAnswers) -> str:
    """Emit the [manager.sync] block when the user opted into manager setup.
    Returns empty when they skipped — keeps the file clean for users who
    don't want the tray app."""
    if not a.manager_setup:
        return ""
    enrich = "true" if a.manager_enrich else "false"
    enrich_meta = "true" if a.manager_enrich_meta else "false"
    return (
        "\n# ─── booki-manager (menubar sidecar) ─────────────────────────────\n"
        "# Flags the Rust tray app appends to every `booki sync` it triggers\n"
        "# (manual 'Sync now' + the schedule below). Both default to true.\n"
        "[manager.sync]\n"
        f"enrich      = {enrich}\n"
        f"enrich-meta = {enrich_meta}\n"
        "\n"
        "# Optional periodic jobs — uncomment and set a cadence to enable.\n"
        "# A job fires when *both* the cadence elapsed and we're inside the\n"
        "# window (or the window already ended today, for catch-up).\n"
        "# [manager.schedule.sync]\n"
        "# cadence = \"daily\"             # off | daily | weekly\n"
        "# window  = \"02:00-05:00\"       # local time; wraps midnight if end <= start\n"
        "# [manager.schedule.ingest]\n"
        "# cadence = \"weekly\"\n"
        "# window  = \"03:00-05:00\"\n"
    )


def _render_toml(ans: BootstrapAnswers) -> str:
    a = ans
    # Disabled sources still get a [sources.X] block with `disabled = true`,
    # so the user can flip them later by editing one line.
    src_blocks: list[str] = []
    for name in plugins.all_source_names():
        enabled = a.sources_enabled.get(name, False)
        head = f"[sources.{name}]"
        if not enabled:
            src_blocks.append(f"{head}\ndisabled = true\n")
            continue

        if name == "rss":
            src_blocks.append(
                f"{head}\n"
                f"# Add one [[sources.rss.feeds]] entry per feed.\n"
                f"# [[sources.rss.feeds]]\n"
                f"# name = \"Hacker News\"\n"
                f"# url  = \"https://hnrss.org/frontpage\"\n"
            )
        elif name == "directory":
            src_blocks.append(
                f"{head}\n"
                f"# Add one [[sources.directory.dirs]] entry per directory.\n"
                f"# [[sources.directory.dirs]]\n"
                f"# name = \"research-papers\"\n"
                f"# path = \"~/Documents/papers\"\n"
            )
        elif name == "youtube":
            src_blocks.append(
                f"{head}\n"
                f"# Requires OAuth credentials — see plugins/youtube/README.md\n"
                f"# client_secret_file = \"~/booki-youtube-client-secret.json\"\n"
                f"# token_file         = \"~/.booki/youtube-token.json\"\n"
            )
        else:
            src_blocks.append(f"{head}\n")

    sources_section = "\n".join(src_blocks)

    em_lines = [
        f"provider     = {_quote(a.em_provider)}",
        f"local_model  = {_quote(a.em_local_model)}",
        f"openai_model = {_quote(a.em_openai_model)}",
    ]

    llm_lines = [
        f"provider  = {_quote(a.llm_provider)}",
        f"model     = {_quote(a.llm_model)}",
    ]
    if a.llm_provider == "ollama":
        llm_lines.append(f"base_url  = {_quote(a.llm_base_url)}")
    llm_lines.append(f"n_results = {a.llm_n_results}")

    return f"""# Booki configuration — generated by `booki bootstrap` on first setup.
# Re-run the wizard at any time with `booki bootstrap --output other.toml`.

[bookmarks]
dir            = {_quote(a.bookmarks_dir)}
min_importance = {a.min_importance}                        # skip items below this score at ingest

[vector_db]
type        = "chromadb"
persist_dir = {_quote(a.db_dir)}
collection  = {_quote(a.db_collection)}

[embeddings]
{chr(10).join(em_lines)}

[llm]
{chr(10).join(llm_lines)}

[enrichment]
enabled           = true
max_content_chars = 4000
fetch_timeout     = 15
llm_timeout       = 120

[web]
host   = {_quote(a.web_host)}
port   = {a.web_port}
reload = false                          # uvicorn auto-reload (dev only)

# ─── Logging ────────────────────────────────────────────────────────────────
[logs]
level         = "INFO"                  # DEBUG | INFO | WARNING | ERROR
console       = "human"                 # "human" | "json" | "off"
file          = "./logs/booki.log"      # "" disables file logging
file_format   = "json"                  # "human" | "json"
max_bytes     = 10_485_760              # 10 MB
backup_count  = 5

[logs.levels]
"chromadb"              = "WARNING"
"sentence_transformers" = "WARNING"
"urllib3"               = "WARNING"
"httpx"                 = "WARNING"
"httpcore"              = "WARNING"
"watchfiles"            = "WARNING"
"htmldate"              = "WARNING"
"trafilatura"           = "WARNING"
"uvicorn.access"        = "INFO"

# ─── Sources (plugins) ──────────────────────────────────────────────────────
# Each [sources.<name>] subtable is passed to that source via Source.configure.
# Set `disabled = true` to skip a source without uninstalling its plugin.

{sources_section}
# ─── Exclude rules (optional) ───────────────────────────────────────────────
[exclude]
domains   = []                          # e.g. ["facebook.com", "twitter.com"]
url_regex = []                          # e.g. ["/login", "utm_"]

# ─── Downloads (yt-dlp) ─────────────────────────────────────────────────────
[downloads]
dir              = "./downloads"
video_height_max = 1080
write_subs       = true
write_thumbnail  = true
sub_langs        = "en.*"

# ─── Exports ────────────────────────────────────────────────────────────────
[export]
configs_dir   = "./exports/configs"
artifacts_dir = "./exports/artifacts"
themes_dir    = "./themes"
{_render_manager_section(a)}"""


# ─── Summary + write ──────────────────────────────────────────────────────────

def _print_summary(p: Prompter, ans: BootstrapAnswers) -> None:
    s = p.s
    enabled = [n for n, v in ans.sources_enabled.items() if v]
    disabled = [n for n, v in ans.sources_enabled.items() if not v]

    print()
    print(f"  {s.bold('Review')}  {s.dim('— about to write the following config:')}")
    print()
    print(f"     {s.dim('Output      ')}  {ans.output_path}")
    print(f"     {s.dim('Bookmarks   ')}  {ans.bookmarks_dir}  "
          f"{s.dim('(min_importance=' + str(ans.min_importance) + ')')}")
    print(f"     {s.dim('Vector DB   ')}  {ans.db_dir}  "
          f"{s.dim('(collection=' + ans.db_collection + ')')}")
    em_detail = ans.em_local_model if ans.em_provider == "local" else ans.em_openai_model
    print(f"     {s.dim('Embeddings  ')}  {ans.em_provider}  {s.dim('· ' + em_detail)}")
    print(f"     {s.dim('LLM         ')}  {ans.llm_provider}  {s.dim('· ' + ans.llm_model)}")
    print(f"     {s.dim('Web         ')}  {ans.web_host}:{ans.web_port}")
    print(f"     {s.dim('Sources on  ')}  {s.green(', '.join(enabled)) if enabled else s.dim('(none)')}")
    if disabled:
        print(f"     {s.dim('Sources off ')}  {s.dim(', '.join(disabled))}")
    if ans.manager_setup:
        flags = []
        if ans.manager_enrich:      flags.append("--enrich")
        if ans.manager_enrich_meta: flags.append("--enrich-meta")
        flag_label = " ".join(flags) if flags else "(no enrichment flags)"
        print(f"     {s.dim('Manager     ')}  {ans.manager_booki_home}  "
              f"{s.dim('· ' + flag_label)}")
    else:
        print(f"     {s.dim('Manager     ')}  {s.dim('skipped')}")
    print()


def _write_config(path: Path, content: str) -> None:
    """Atomic write — refuses if path exists at the last second."""
    if path.exists():
        raise FileExistsError(f"{path} appeared between confirmation and write — aborting")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _write_manager_settings(ans: BootstrapAnswers) -> Optional[Path]:
    """Write ~/.config/booki-manager/settings.json so the tray app finds
    this Booki checkout without depending on $BOOKI_HOME. Returns the
    path it wrote to, or None when the user skipped manager setup.

    Merges with any existing settings — the manager may grow other
    fields later, and bootstrap shouldn't clobber them just because
    `booki_home` is the only one we know about today.
    """
    if not ans.manager_setup:
        return None

    import json

    booki_home = Path(ans.manager_booki_home).expanduser().resolve()
    MANAGER_SETTINGS.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if MANAGER_SETTINGS.exists():
        try:
            existing = json.loads(MANAGER_SETTINGS.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            existing = {}
    existing["booki_home"] = str(booki_home)

    tmp = MANAGER_SETTINGS.with_suffix(MANAGER_SETTINGS.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    tmp.replace(MANAGER_SETTINGS)
    return MANAGER_SETTINGS


def _print_next_steps(p: Prompter, ans: BootstrapAnswers) -> None:
    s = p.s
    print()
    print(f"  {s.bold('✓ Next steps')}")
    print()
    cfg_flag = "" if ans.output_path == DEFAULT_CONFIG else f" --config {ans.output_path}"
    enabled_sources = [n for n, v in ans.sources_enabled.items() if v]

    steps: list[tuple[str, str]] = []
    steps.append((f"booki sync{cfg_flag}", "pull items from every enabled source"))
    if ans.llm_provider != "" and any(ans.sources_enabled.values()):
        steps.append((f"booki sync{cfg_flag} --no-sync --enrich",
                      "LLM-summarize new items (one-time, then incremental)"))
    steps.append((f"booki ingest{cfg_flag}", "build the vector index"))
    steps.append((f"booki web{cfg_flag}", "open the browser UI"))
    steps.append((f"booki doctor{cfg_flag}", "see overall health any time"))

    for cmd, note in steps:
        print(f"     {s.cyan('→')} {s.bold(cmd)}")
        print(f"        {s.dim(note)}")

    if "rss" in enabled_sources:
        print()
        print(f"  {s.dim('rss enabled → add feeds via [[sources.rss.feeds]] in ' + str(ans.output_path))}")
    if "directory" in enabled_sources:
        print(f"  {s.dim('directory enabled → add paths via [[sources.directory.dirs]] in ' + str(ans.output_path))}")
    if "youtube" in enabled_sources:
        print(f"  {s.dim('youtube enabled → set up OAuth: see plugins/youtube/README.md')}")
    if ans.output_path != DEFAULT_CONFIG:
        print()
        print(f"  {s.yellow('⚠ remember to pass --config ' + str(ans.output_path))}"
              f"{s.dim(' to every booki command, or set BOOKI_CONFIG.')}")

    if ans.manager_setup:
        print()
        print(f"  {s.bold('Manager build')}")
        print(f"     {s.cyan('→')} {s.bold('cd tools/booki-manager && cargo build --release')}")
        print(f"        {s.dim('binary lands at target/release/booki-manager')}")
        print(f"     {s.cyan('→')} {s.bold('./target/release/booki-manager')}")
        print(f"        {s.dim('then enable Launch at login from the tray menu for autostart')}")


# ─── Entry point ──────────────────────────────────────────────────────────────

def run(*, color: bool, requested_output: Optional[Path]) -> int:
    s = Style(color)
    p = Prompter(s)

    # Header
    print()
    print(f"  🌱  {s.bold('Booki Bootstrap')}  {s.dim('— config.toml wizard')}")
    print(f"     {s.dim('Press Enter to accept any [default]. Ctrl-C cancels at any prompt.')}")
    print()

    ans = BootstrapAnswers()
    ans.output_path = _pick_output_path(p, requested_output)

    _ask_bookmarks(p, ans)
    _ask_vector_db(p, ans)
    _ask_embeddings(p, ans)
    _ask_llm(p, ans)
    _ask_web(p, ans)
    _ask_sources(p, ans)
    _ask_manager(p, ans)

    _print_summary(p, ans)
    if not p.yes_no("Write this config?", default=True):
        print(f"     {s.yellow('aborted — nothing written')}")
        return 1

    try:
        content = _render_toml(ans)
        _write_config(ans.output_path, content)
    except FileExistsError as e:
        print(f"  {s.red('✗ ' + str(e))}")
        return 2
    except OSError as e:
        print(f"  {s.red(f'✗ failed to write config: {e}')}")
        return 2

    print()
    print(f"  {s.green('✓ wrote ' + str(ans.output_path))}  "
          f"{s.dim(f'({len(content):,} bytes)')}")

    # Manager settings file is optional — only write when the user opted in.
    try:
        mgr_path = _write_manager_settings(ans)
    except OSError as e:
        print(f"  {s.yellow(f'⚠ manager settings: {e}')}")
        mgr_path = None
    if mgr_path is not None:
        print(f"  {s.green('✓ wrote ' + str(mgr_path))}  "
              f"{s.dim('(booki_home → ' + ans.manager_booki_home + ')')}")

    _print_next_steps(p, ans)
    print()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="booki bootstrap",
        description="Interactive config.toml wizard. Refuses to overwrite existing files.",
    )
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Where to write the new config (default: prompt). "
                             "If the file already exists, you'll be prompted for another path.")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable ANSI colors (auto-disabled when stdout isn't a TTY).")
    args = parser.parse_args()

    color = (sys.stdout.isatty() and not args.no_color
             and os.environ.get("NO_COLOR") in (None, ""))

    try:
        sys.exit(run(color=color, requested_output=args.output))
    except KeyboardInterrupt:
        print()
        print("  aborted — nothing written")
        sys.exit(130)


if __name__ == "__main__":
    main()
