"""Settings shared by all the tests."""

from __future__ import annotations

import logging

# pytest-homeassistant-custom-component sets the SQLAlchemy engine logger to
# INFO when it is loaded, which prints every query of the recorder tests.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
