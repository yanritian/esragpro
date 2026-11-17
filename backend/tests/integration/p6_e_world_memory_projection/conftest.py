"""P6-E world-memory projection handler composition tests.

The shared P6-C fixtures are imported directly so they are registered under
the P6-E directory only.  pytest 9 rejects ``pytest_plugins`` in
non-top-level conftests, and registering the P6-C conftest module from a
top-level ``pytest_plugins`` would collide with its directory-conftest
registration, so the fixtures are re-exported here instead.
"""
from __future__ import annotations

from tests.integration.p6_c_world_memory_projection.conftest import (
    _isolate_tables as _isolate_tables,
    session_factory as session_factory,
)
