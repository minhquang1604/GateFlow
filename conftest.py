"""Pytest config — make the repository root importable for case studies."""

from __future__ import annotations

import sys
from pathlib import Path

# Add the repository root to sys.path so case studies (which live at the
# repo root, not under src/) are importable as `case_studies.*`.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
