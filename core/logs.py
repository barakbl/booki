"""
Logging configuration for Booki.

TOML-driven, stdlib `logging`. One root setup function — `setup_logging(cfg)` —
wires up:

  * a colored human handler on stderr (or JSON, or off)
  * an optional rotating file handler (human or JSON)
  * per-logger level overrides (so noisy libs like chromadb can be quieted)

It's idempotent: every call wipes prior handlers and re-installs from `cfg`,
so the dispatcher, the web subprocess, and `--reload` children all converge
on the same configuration without duplicate handlers.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional


_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Keys present on every LogRecord — used by JsonFormatter to decide which
# attributes are user-supplied "extra" fields worth serialising.
_STD_LOGRECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
}

_LEVEL_COLORS = {
    "DEBUG":    "\x1b[36m",     # cyan
    "INFO":     "\x1b[32m",     # green
    "WARNING":  "\x1b[33m",     # yellow
    "ERROR":    "\x1b[31m",     # red
    "CRITICAL": "\x1b[1;31m",   # bold red
}
_RESET = "\x1b[0m"
_DIM   = "\x1b[2m"


class HumanFormatter(logging.Formatter):
    """`HH:MM:SS  LEVEL    logger.name  message` — color when stderr is a TTY."""

    def __init__(self, color: bool):
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        lvl = record.levelname
        name = record.name
        msg = record.getMessage()
        # If the caller passed extras (extra={...}), render them as k=v on the
        # tail so a human can still read them without flipping to JSON output.
        extras = _extract_extras(record)
        if extras:
            msg = f"{msg}  " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            msg = msg + "\n" + self.formatException(record.exc_info)
        if self.color:
            color = _LEVEL_COLORS.get(lvl, "")
            return f"{_DIM}{ts}{_RESET} {color}{lvl:<7}{_RESET} {_DIM}{name}{_RESET}  {msg}"
        return f"{ts} {lvl:<7} {name}  {msg}"


class JsonFormatter(logging.Formatter):
    """One JSON object per line — `{ts, level, logger, msg, …extras}`."""

    def format(self, record: logging.LogRecord) -> str:
        d = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        d.update(_extract_extras(record))
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False, default=str)


def _extract_extras(record: logging.LogRecord) -> dict:
    """Return any caller-supplied `extra={...}` keys, JSON-coerced."""
    out: dict = {}
    for key, val in record.__dict__.items():
        if key in _STD_LOGRECORD_KEYS or key.startswith("_"):
            continue
        try:
            json.dumps(val)
            out[key] = val
        except (TypeError, ValueError):
            out[key] = str(val)
    return out


def setup_logging(cfg: dict, *, level_override: Optional[str] = None) -> None:
    """
    Apply the `[logs]` section of a parsed config.toml.

    Schema (all optional):

        [logs]
        level         = "INFO"               # global default for booki.* loggers
        console       = "human"              # "human" | "json" | "off"
        file          = "./logs/booki.log"   # path (relative→project root); "" = no file
        file_format   = "json"               # "human" | "json"
        max_bytes     = 10485760
        backup_count  = 5

        [logs.levels]                        # per-logger overrides
        "chromadb" = "WARNING"

    Idempotent — replaces existing handlers on the root logger.
    """
    log_cfg = cfg.get("logs", {}) or {}

    level = (level_override or log_cfg.get("level") or "INFO").upper()
    console_format = (log_cfg.get("console") or "human").lower()
    file_path_str = log_cfg.get("file") or ""
    file_format = (log_cfg.get("file_format") or "json").lower()
    max_bytes = int(log_cfg.get("max_bytes") or 10_485_760)
    backup_count = int(log_cfg.get("backup_count") or 5)
    per_logger = log_cfg.get("levels") or {}

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    # Root accepts everything; per-handler levels do the actual filtering.
    root.setLevel(logging.DEBUG)

    if console_format != "off":
        ch = logging.StreamHandler(stream=sys.stderr)
        ch.setLevel(level)
        ch.setFormatter(
            JsonFormatter() if console_format == "json"
            else HumanFormatter(color=sys.stderr.isatty())
        )
        root.addHandler(ch)

    if file_path_str:
        file_path = Path(file_path_str).expanduser()
        if not file_path.is_absolute():
            file_path = (_PROJECT_ROOT / file_path).resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            file_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(
            HumanFormatter(color=False) if file_format == "human"
            else JsonFormatter()
        )
        root.addHandler(fh)

    for name, lvl in per_logger.items():
        logging.getLogger(name).setLevel(str(lvl).upper())

    # Uvicorn ships its own log_config that monkey-patches its three loggers
    # and disables propagation. We pass log_config=None to uvicorn.run() so
    # those loggers fall through to root, but defensively re-enable propagation
    # in case anything else has touched them already.
    for n in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(n)
        lg.propagate = True
        for h in list(lg.handlers):
            lg.removeHandler(h)


def load_logs_config(config_path: Path) -> dict:
    """Best-effort TOML load — never raises. Returns {} on any failure."""
    if not config_path.exists():
        return {}
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}
