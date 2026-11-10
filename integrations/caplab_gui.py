#!/usr/bin/env python3
"""Open only a Studio-exported workbook; isolate CapLab session writes."""
import argparse
import json
from pathlib import Path
import sys
import shutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--runtime', required=True)
    args = parser.parse_args()
    path = Path(args.project).resolve()
    data = json.loads(path.read_text())
    if data.get('kind') != 'caplab-workbook' or not data.get('studio', {}).get('source_graph_hash'):
        parser.error('Explicit Studio workbook with graph identity required')
    runtime = Path(args.runtime).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    # Backend locates helper resources relative to ROOT; copy only its launcher
    # and context helper so work/session outputs stay in the isolated runtime.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from vco_studio import site
    source = site.tool_path('caplab')
    if not source or not source.is_dir():
        parser.error('Cap Array Lab not configured: set tools.caplab in config/site.json')
    (runtime / 'caplab').mkdir(exist_ok=True)
    for relative in ('cadence-child.sh', 'caplab/launch_context.py'):
        target = runtime / relative
        if not target.exists():
            shutil.copy2(source / relative, target)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    from caplab import model
    model.ROOT = runtime  # Session/cache destination only, local to this process.
    from caplab.ui import App
    app = App()
    app.after_idle(lambda: app.load_workbook_path(str(path), session=False))
    app.mainloop()


if __name__ == '__main__':
    main()
