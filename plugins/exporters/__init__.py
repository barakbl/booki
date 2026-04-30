"""
plugins.exporters — exporter plugin package.

Each subpackage is one Exporter. Importing this package side-effect-registers
all built-ins by importing every subpackage.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

_pkg_dir = Path(__file__).parent
for mod in pkgutil.iter_modules([str(_pkg_dir)]):
    if mod.ispkg and not mod.name.startswith("_"):
        importlib.import_module(f"{__name__}.{mod.name}")
