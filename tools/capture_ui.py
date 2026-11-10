"""Capture synthetic UI states on an explicitly selected display; no EDA process.

Usage: python tools/capture_ui.py --runtime /path/to/new-runtime
Set DISPLAY yourself. Output paths are shortened in the preview only; full logs remain on disk.
"""
import argparse
import os
from pathlib import Path
import sys
import time
import tkinter as tk
from PIL import ImageGrab

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vco_studio.ui import StudioApp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True)
    args = parser.parse_args()
    if not os.environ.get('DISPLAY'):
        parser.error('Set DISPLAY to your allocated X display')
    runtime = Path(args.runtime).resolve()
    root = tk.Tk()
    app = StudioApp(root, runtime=runtime)
    os.chdir(runtime.parent)
    app.output_root.set(runtime.name + '/runs')
    screenshots = ROOT / 'docs/screenshots'
    screenshots.mkdir(parents=True, exist_ok=True)

    def pump(seconds=.2):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            root.update()
            time.sleep(.01)

    def capture(name):
        root.update_idletasks()
        ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(screenshots / name)

    try:
        pump()
        capture('empty.png')
        app.load_example()
        app.pages.select(app.guide_pages)
        app.guide_pages.select(app.concept)
        pump()
        app.concept.fit()
        pump()
        capture('example.png')
        app.generate(False)
        assert app.busy, 'Expected an actual background generation task'
        capture('running.png')
        stop = time.monotonic() + 10
        while app.busy and time.monotonic() < stop:
            pump(.05)
        assert not app.busy and app.generated_dir, app.status.get()
        app.pages.select(app.viewer)
        # Preserve actual messages, abbreviate runtime location for public screenshots.
        app._set_text(app.log_text, app.log_text.get('1.0', 'end').replace(str(runtime), '<runtime>'))
        pump()
        capture('result.png')
        root.geometry('1280x800+0+0')
        pump()
        root.update_idletasks()
        canvas_width = app.viewer.canvas.winfo_width()
        assert app.pages.winfo_width() / root.winfo_width() >= .60
        assert app.log_text.winfo_rooty()+app.log_text.winfo_height() <= root.winfo_rooty()+root.winfo_height()
        capture('compact.png')
        print('Captured empty, example, running, result and compact states; 1280x800 main view fraction=%.3f' % (app.pages.winfo_width()/root.winfo_width()))
    finally:
        root.destroy()


if __name__ == '__main__':
    main()
