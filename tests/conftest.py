"""
Pytest configuration and shared fixtures for the PersonalOS test suite.

Test categories
---------------
unit        Pure unit tests — only Redis needed (no running system).
            Run: pytest -m unit
integration Live integration tests — requires ``python main.py`` on ports 8000+8080.
            Run: pytest -m integration

Quick start
-----------
# Unit tests only (fast, ~25 s)
pytest tests/test_backend.py -m unit -v

# Full suite (requires running system, ~4 min)
pytest tests/test_backend.py -v

See pytest.ini for asyncio_mode = auto configuration.
"""
import sys
from pathlib import Path

# Ensure the project root is on sys.path so that ``import agents`` etc. works
# regardless of the directory pytest is invoked from.
sys.path.insert(0, str(Path(__file__).parent.parent))
