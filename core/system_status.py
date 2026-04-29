"""
system_status.py — runtime check of dependencies and tools used by Booki.

Each entry declares what it powers (so the UI can explain *why* a user might
want to install it) and per-platform install commands. The collector reports
detection state plus the fix command appropriate for the current OS, so the
frontend just renders.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Optional


# ─── Detect host package manager ──────────────────────────────────────────────

def _detect_pm() -> str:
    sysname = platform.system()
    if sysname == "Darwin":
        return "brew" if shutil.which("brew") else "brew_missing"
    if sysname == "Windows":
        for cmd, key in (("winget", "winget"), ("scoop", "scoop"), ("choco", "choco")):
            if shutil.which(cmd):
                return key
        return "winget"
    # Linux / other unix
    for cmd, key in (("apt", "apt"), ("dnf", "dnf"), ("pacman", "pacman"),
                     ("zypper", "zypper"), ("apk", "apk")):
        if shutil.which(cmd):
            return key
    return "pip"


def _platform_info() -> dict:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "package_manager": _detect_pm(),
    }


# ─── Catalog ──────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    id: str
    category: str
    label: str
    feature: str
    required: bool
    ok: bool
    detail: str = ""
    install: dict = field(default_factory=dict)
    fix_command: Optional[str] = None
    docs_url: Optional[str] = None


# (id, label, dist name, import name, feature, required, install per-pm)
# install dict keys map to package-manager identifiers from _detect_pm().
# `pip` is always the universal fallback.
PYTHON_PACKAGES: list[tuple] = [
    ("py-requests", "requests", "requests", "requests",
     "HTTP fetches in sync, enrich, download", True,
     {"pip": "pip install 'requests>=2.31.0'"}),
    ("py-trafilatura", "trafilatura", "trafilatura", "trafilatura",
     "Page-content extraction for `booki sync --enrich`", False,
     {"pip": "pip install 'trafilatura>=1.12.0'"}),
    ("py-chromadb", "chromadb", "chromadb", "chromadb",
     "Local vector index for semantic search (booki ingest / Ask)", True,
     {"pip": "pip install 'chromadb>=0.5.0'"}),
    ("py-sentence-transformers", "sentence-transformers",
     "sentence-transformers", "sentence_transformers",
     "Local embeddings (embeddings.provider = 'local')", False,
     {"pip": "pip install 'sentence-transformers>=2.2.0'"}),
    ("py-anthropic", "anthropic", "anthropic", "anthropic",
     "Claude LLM provider (llm.provider = 'claude')", False,
     {"pip": "pip install 'anthropic>=0.30.0'"}),
    ("py-openai", "openai", "openai", "openai",
     "OpenAI LLM / embeddings (provider = 'openai')", False,
     {"pip": "pip install 'openai>=1.0.0'"}),
    ("py-fastapi", "fastapi", "fastapi", "fastapi",
     "Web UI server (this app)", True,
     {"pip": "pip install 'fastapi>=0.110.0'"}),
    ("py-uvicorn", "uvicorn", "uvicorn", "uvicorn",
     "ASGI server for the web UI", True,
     {"pip": "pip install 'uvicorn[standard]>=0.27.0'"}),
    ("py-pydantic", "pydantic", "pydantic", "pydantic",
     "Request / response models for the web UI", True,
     {"pip": "pip install 'pydantic>=2.5.0'"}),
    ("py-jinja2", "jinja2", "jinja2", "jinja2",
     "Themed HTML exports (link_page exporter)", False,
     {"pip": "pip install 'jinja2>=3.1.0'"}),
    ("py-pyyaml", "pyyaml", "pyyaml", "yaml",
     "Saved export configs (.yaml)", False,
     {"pip": "pip install 'pyyaml>=6.0'"}),
    ("py-yt-dlp", "yt-dlp", "yt-dlp", "yt_dlp",
     "Video / audio downloads (booki download)", False,
     {"pip": "pip install 'yt-dlp>=2024.8.0'"}),
    ("py-google-api", "google-api-python-client",
     "google-api-python-client", "googleapiclient",
     "YouTube source ([sources.youtube])", False,
     {"pip": "pip install google-api-python-client google-auth-oauthlib"}),
    ("py-feedparser", "feedparser", "feedparser", "feedparser",
     "RSS source ([sources.rss])", False,
     {"pip": "pip install 'feedparser>=6.0.0'"}),
]


# (id, label, binary name, feature, required, install per-pm, docs)
BINARIES: list[tuple] = [
    ("bin-ffmpeg", "ffmpeg", "ffmpeg",
     "Mux + audio extraction for downloads", False,
     {
         "brew": "brew install ffmpeg",
         "apt":  "sudo apt install ffmpeg",
         "dnf":  "sudo dnf install ffmpeg",
         "pacman": "sudo pacman -S ffmpeg",
         "winget": "winget install Gyan.FFmpeg",
         "choco":  "choco install ffmpeg",
         "scoop":  "scoop install ffmpeg",
     }, "https://ffmpeg.org/download.html"),
    ("bin-ollama", "ollama", "ollama",
     "Local LLM runtime (llm.provider = 'ollama')", False,
     {
         "brew": "brew install ollama",
         "apt":  "curl -fsSL https://ollama.com/install.sh | sh",
         "dnf":  "curl -fsSL https://ollama.com/install.sh | sh",
         "pacman": "curl -fsSL https://ollama.com/install.sh | sh",
         "winget": "winget install Ollama.Ollama",
     }, "https://ollama.com/download"),
]


# ─── Runners ──────────────────────────────────────────────────────────────────

def _check_python_pkg(entry: tuple, pm: str) -> CheckResult:
    cid, label, dist, imp, feature, required, install = entry
    detail = ""
    ok = False
    try:
        importlib.import_module(imp)
        ok = True
        try:
            detail = f"v{importlib.metadata.version(dist)}"
        except importlib.metadata.PackageNotFoundError:
            detail = "installed"
    except ImportError:
        ok = False
        detail = "not installed"
    fix = None if ok else install.get("pip")
    return CheckResult(
        id=cid, category="Python packages", label=label, feature=feature,
        required=required, ok=ok, detail=detail,
        install=install, fix_command=fix,
    )


def _check_binary(entry: tuple, pm: str) -> CheckResult:
    cid, label, binary, feature, required, install, docs = entry
    path = shutil.which(binary)
    ok = bool(path)
    detail = path or "not found on PATH"
    if ok:
        try:
            out = subprocess.run(
                [binary, "--version"], capture_output=True, text=True,
                timeout=3, check=False,
            )
            line = (out.stdout or out.stderr or "").splitlines()
            if line:
                detail = f"{path} — {line[0].strip()[:80]}"
        except (OSError, subprocess.SubprocessError):
            pass
    fix = None
    if not ok:
        fix = install.get(pm) or install.get("brew") or install.get("apt") \
              or install.get("winget") or next(iter(install.values()), None)
    return CheckResult(
        id=cid, category="External tools", label=label, feature=feature,
        required=required, ok=ok, detail=detail,
        install=install, fix_command=fix, docs_url=docs,
    )


def _check_ollama_service(base_url: str) -> CheckResult:
    """Soft check: only run if the user actually selected ollama."""
    ok = False
    detail = "not reached"
    try:
        import urllib.request, urllib.error
        url = base_url.rstrip("/") + "/api/tags"
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                ok = 200 <= r.status < 300
                detail = f"reachable ({r.status})" if ok else f"HTTP {r.status}"
        except (urllib.error.URLError, OSError) as e:
            detail = f"unreachable: {type(e).__name__}"
    except ImportError:
        detail = "stdlib urllib unavailable"
    return CheckResult(
        id="svc-ollama", category="Services",
        label=f"Ollama daemon ({base_url})",
        feature="Local LLM responses for Ask",
        required=False, ok=ok, detail=detail,
        install={
            "shell": "ollama serve",
        },
        fix_command="ollama serve",
        docs_url="https://github.com/ollama/ollama",
    )


def _check_env_var(name: str, feature: str, required: bool = False) -> CheckResult:
    val = os.environ.get(name, "")
    ok = bool(val)
    detail = f"set ({len(val)} chars)" if ok else "not set"
    return CheckResult(
        id=f"env-{name.lower()}", category="Environment",
        label=name, feature=feature, required=required, ok=ok,
        detail=detail,
        install={"shell": f"export {name}=…"},
        fix_command=f"export {name}=…",
    )


# ─── Public entrypoint ────────────────────────────────────────────────────────

def collect(cfg: Optional[dict] = None) -> dict:
    """Run every check and return a JSON-friendly payload for /api/status."""
    cfg = cfg or {}
    pm = _detect_pm()
    results: list[CheckResult] = []

    for entry in PYTHON_PACKAGES:
        results.append(_check_python_pkg(entry, pm))
    for entry in BINARIES:
        results.append(_check_binary(entry, pm))

    # Conditional / config-aware checks
    llm_provider = str((cfg.get("llm") or {}).get("provider") or "").lower()
    base_url = str((cfg.get("llm") or {}).get("base_url") or "http://localhost:11434")
    if llm_provider == "ollama":
        results.append(_check_ollama_service(base_url))
    if llm_provider == "claude":
        results.append(_check_env_var("ANTHROPIC_API_KEY",
                                      "Required by llm.provider = 'claude'", True))
    if llm_provider == "openai" or \
       str((cfg.get("embeddings") or {}).get("provider") or "").lower() == "openai":
        results.append(_check_env_var("OPENAI_API_KEY",
                                      "Required for OpenAI provider", True))

    payload = {
        "platform": _platform_info(),
        "checks": [asdict(r) for r in results],
        "summary": {
            "total": len(results),
            "ok": sum(1 for r in results if r.ok),
            "missing_required": sum(1 for r in results if not r.ok and r.required),
            "missing_optional": sum(1 for r in results if not r.ok and not r.required),
        },
    }
    return payload
