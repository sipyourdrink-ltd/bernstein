"""Agent discovery and bounded registered detector execution (``detector_runtime.py``)."""

import importlib
import pkgutil
from typing import Any


def __getattr__(name: str) -> Any:
    """Lazily resolve names from submodules for backward compatibility."""
    for info in pkgutil.iter_modules(__path__):
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception:
            continue
        if hasattr(mod, name):
            globals()[name] = getattr(mod, name)
            return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
