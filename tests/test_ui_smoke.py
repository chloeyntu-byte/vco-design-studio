import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from vco_studio.ui import StudioApp
from vco_studio import fieldwork_tk as fw


@unittest.skipUnless(os.environ.get('DISPLAY'), 'GUI smoke test needs an X display')
class ThemeSmokeTests(unittest.TestCase):
    def test_create_apply_load_example_destroy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = tk.Tk()
            try:
                app = StudioApp(root, runtime=Path(folder) / 'runtime')
                root.update()
                self.assertEqual(root._fw_accent, fw.accent('vco'))
                self.assertEqual(app.viewer.canvas.cget('background'), fw.tk_plot_colors()['bg'])
                self.assertEqual(app.netlist_button.cget('style'), 'Primary.TButton')
                self.assertTrue(app.empty_state.winfo_ismapped())
                app.load_example()
                root.update()
                self.assertFalse(app.empty_state.winfo_ismapped())
                self.assertEqual(app.family_id, 'murphy')
                self.assertTrue(app.viewer.canvas.find_all())
            finally:
                root.destroy()
