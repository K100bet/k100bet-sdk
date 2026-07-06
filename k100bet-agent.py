#!/usr/bin/env python3
"""Backward-compatible entrypoint. Prefer: pip install k100bet"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from k100bet.client import main

if __name__ == "__main__":
    main()
