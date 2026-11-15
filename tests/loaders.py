"""Load project modules without importing Home Assistant.

Importing ``custom_components.tankwise.model`` would execute the integration's
``__init__.py`` (which needs Home Assistant), so the model package is loaded
directly from its folder under the alias ``tankwise_model``. The export tool
is a standalone script and is loaded from its file.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

ROOT = Path(__file__).parents[1]
MODEL_PACKAGE = "tankwise_model"
MODEL_DIR = ROOT / "custom_components" / "tankwise" / "model"
EXPORT_TOOL = ROOT / "tools" / "export_history.py"


def load_model_package() -> ModuleType:
    """Load the pure-Python model package as ``tankwise_model``."""
    if MODEL_PACKAGE in sys.modules:
        return sys.modules[MODEL_PACKAGE]
    spec = importlib.util.spec_from_file_location(
        MODEL_PACKAGE,
        MODEL_DIR / "__init__.py",
        submodule_search_locations=[str(MODEL_DIR)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODEL_PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def load_export_tool() -> ModuleType:
    """Load tools/export_history.py as ``tankwise_export_history``."""
    name = "tankwise_export_history"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, EXPORT_TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
