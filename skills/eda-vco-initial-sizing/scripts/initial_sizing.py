#!/usr/bin/env python3
"""Thin wrapper: the hand-calculation of the design flow lives in vco_studio/sizing.py (single source).
Same CLI as before: initial_sizing.py input.json [--output result.json]; `launch plan` is the preferred entry."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from vco_studio.sizing import size, number, main  # noqa: E402,F401

if __name__ == '__main__':
    sys.exit(main())
