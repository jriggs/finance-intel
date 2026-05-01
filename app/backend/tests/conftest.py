"""
Shared pytest configuration.
Sets up the Python path so all test files can import backend modules directly.
"""
import sys
from pathlib import Path

backend_dir = Path(__file__).parent.parent
for _pkg in ("core", "services", "ai", "routers"):
    _p = str(backend_dir / _pkg)
    if _p not in sys.path:
        sys.path.insert(0, _p)
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
