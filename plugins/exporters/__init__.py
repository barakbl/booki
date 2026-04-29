"""
plugins.exporters — export plugin package.

Each subdirectory here is one `Exporter` subclass (or a small cluster).
Importing this package auto-imports every subpackage, which triggers
`@register_exporter` side-effects and populates the exporter registry in
`plugins.base`.

To add a new exporter:
    1. Create `plugins/exporters/<name>/__init__.py` with a
       `@register_exporter`-decorated `Exporter` subclass.
    2. That's it — it's picked up on next `import plugins`.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

_pkg_dir = Path(__file__).parent
for mod in pkgutil.iter_modules([str(_pkg_dir)]):
    if mod.ispkg and not mod.name.startswith("_"):
        importlib.import_module(f"{__name__}.{mod.name}")
