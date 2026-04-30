"""
plugins — source plugin package.

Each subdirectory of `plugins/` is one plugin (one `Source` subclass, or a
handful of closely-related ones). Importing this package side-effect-registers
the built-ins so the registry in `plugins.base` is populated by the time
`sync.py` / `web.py` ask for it.

To add a new plugin:
    1. Create `plugins/<name>/__init__.py` with a `@register`-decorated `Source`
       subclass.
    2. Add `from . import <name>` below (or let auto-discovery pick it up).
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from .base import (
    Enricher,
    Item,
    Source,
    TabContribution,
    all_enricher_names,
    all_kind_specs,
    all_source_names,
    all_tab_ids,
    get_enricher,
    get_source,
    get_tab,
    iter_enrichers,
    iter_registered,
    iter_tabs,
    register,
    register_enricher,
    register_tab,
)

# Auto-discover every subpackage under plugins/. Each one's __init__.py runs
# its registration side-effects on import. Plugins with missing optional deps
# should still import cleanly and self-report via is_available().
_pkg_dir = Path(__file__).parent
for mod in pkgutil.iter_modules([str(_pkg_dir)]):
    if mod.ispkg and not mod.name.startswith("_"):
        importlib.import_module(f"{__name__}.{mod.name}")

__all__ = [
    "Enricher",
    "Item",
    "Source",
    "TabContribution",
    "all_enricher_names",
    "all_kind_specs",
    "all_source_names",
    "all_tab_ids",
    "get_enricher",
    "get_source",
    "get_tab",
    "iter_enrichers",
    "iter_registered",
    "iter_tabs",
    "register",
    "register_enricher",
    "register_tab",
]
