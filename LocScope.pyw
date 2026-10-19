"""Double-click to start LocScope (opens its window; no console)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import locscope_app  # noqa: E402

locscope_app.main()
