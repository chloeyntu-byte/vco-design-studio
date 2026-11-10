#!/bin/sh
set -eu
base=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$base"
py=${VCO_STUDIO_PYTHON:-python3.11}
export VCO_STUDIO_PYTHON="$py" PYTHONDONTWRITEBYTECODE=1
"$py" -m unittest discover -s tests -p 'test_*.py'
./launch kb check
"$py" tools/check_docs.py
