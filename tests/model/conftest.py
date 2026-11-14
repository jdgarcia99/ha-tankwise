"""Load the TankWise model package without importing Home Assistant.

Importing ``custom_components.tankwise.model`` would execute the integration's
``__init__.py`` (which needs Home Assistant), so the model package is loaded
directly from its folder under the alias ``tankwise_model``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

MODEL_PACKAGE = "tankwise_model"
MODEL_DIR = Path(__file__).parents[2] / "custom_components" / "tankwise" / "model"


def _load_model_package() -> None:
    if MODEL_PACKAGE in sys.modules:
        return
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


_load_model_package()
