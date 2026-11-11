import copy
import json
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest

from vco_studio import catalog
from vco_studio.drawing import connectivity, graph_views, concept_model
from vco_studio.ui import StudioApp, read_json, register_definitions, runtime_root, save_json


class UIDataTests(unittest.TestCase):
    def test_real_catalog_controls_and_networks(self):
        self.assertEqual(len(catalog.list_families()), 5)
        for family in catalog.list_families():
            for entry in family['variants']:
                variant = catalog.get_variant(family['id'], entry['id'])
                before = copy.deepcopy(variant)
                controls = register_definitions(variant)
                self.assertIn('main', controls)
                self.assertEqual('aux' in controls, family['id'] not in ('guo', 'du'))
                overview = concept_model(variant)
                original = {item['name']: item for item in variant['graph']['instances']}
                for item in overview['components']:
                    self.assertEqual(item['nodes'], {pin: overview['collapsed_loss_nodes'].get(net, net) for pin, net in original[item['name']]['nodes'].items()})
                views = graph_views(variant['graph'])
                self.assertGreater(len(views), 1)
                for name, graph in views:
                    nets = connectivity(graph)
                    for instance in graph['instances']:
                        for terminal, net in instance['nodes'].items():
                            self.assertIn((instance['name'], terminal), nets[str(net)])
                self.assertEqual(variant, before)

    def test_atomic_project_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'project.json'
            first = {'schema':'vco.project.v1', 'family':'du'}
            second = dict(first, variant='autotracking')
            save_json(path, first)
            save_json(path, second)
            self.assertEqual(read_json(path, 'vco.project.v1'), second)
            backups = list(Path(folder).glob('project.json.bak-*'))
            self.assertEqual(len(backups),1)
            self.assertEqual(json.loads(backups[0].read_text()),first)
            self.assertFalse(list(Path(folder).glob('*.tmp')))
            with self.assertRaises(ValueError):
                read_json(path, 'vco.binding.v1')

    def test_runtime_separation(self):
        from vco_studio.ui import INSTALL_ROOT
        with self.assertRaises(ValueError):
            runtime_root(runtime=INSTALL_ROOT/'runtime')
        self.assertEqual(runtime_root(workspace='/tmp/vco-project'), Path('/tmp/vco-project/vco-design-studio_runtime'))


@unittest.skipUnless(os.environ.get('DISPLAY'), 'Requires actual X11 display')
class ActualTkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.app = StudioApp(self.root, runtime=Path(self.temp.name)/'runtime')
        self.root.update()

    def tearDown(self):
        self.root.destroy()
        self.temp.cleanup()

    def pump(self, until=None, timeout=3):
        stop = time.monotonic()+timeout
        while time.monotonic()<stop:
            self.root.update()
            if until and until():
                return
            time.sleep(.015)
        if until:
            self.fail('Timed out waiting for UI condition')

    def test_actual_family_tabs_and_project_roundtrip(self):
        for family in catalog.list_families():
            self.app._choose_family(family['id'])
            for i, entry in enumerate(family['variants']):
                self.app.variant_box.current(i)
                self.app._choose_variant()
                self.root.update()
                self.assertEqual(self.app.variant_id.get(), entry['id'])
                self.assertEqual('aux' in self.app.register_vars, family['id'] not in ('guo','du'))
                self.assertTrue(self.app.viewer.canvas.find_all())
                p = self.app.collect_project()
                p['registers']['main'] = {'bits':3,'value':2}
                p['tools'] = {'spectre':'/explicit/solver'}
                p['spec'] = {'power_max_mw':12}
                self.app.apply_project(p)
                self.assertEqual(self.app.collect_project()['registers']['main'], p['registers']['main'])
                self.assertEqual(self.app.collect_project()['tools'], p['tools'])
                self.assertEqual(self.app.collect_project()['spec'], p['spec'])
        self.root.geometry('1024x768')
        self.pump(timeout=.15)
        self.assertTrue(self.app.log_text.winfo_ismapped())
        self.assertLessEqual(self.app.log_text.winfo_rooty()+self.app.log_text.winfo_height(), self.root.winfo_rooty()+self.root.winfo_height())
        self.assertGreater(self.app.viewer.canvas.winfo_width(), 400)
        self.app.pages.select(self.app.topology)
        self.pump(timeout=.15)
        self.assertGreater(self.app.topology.canvas.winfo_width(), 400)
        self.app.settings.select(1)
        self.root.update()
        self.assertTrue(self.app.roles_frame.winfo_ismapped())

    def test_frozen_background_result_and_cancel(self):
        applied=[]
        def work(cancel, log):
            log('test bounded worker')
            cancel.wait(.3)
            return {'cancelled':cancel.is_set()}
        self.app._start('cancel test', work, applied.append)
        self.app.cancel()
        self.pump(lambda: not self.app.busy)
        self.assertTrue(applied[0]['cancelled'])
        self.app._start('stale-result test', work, applied.append)
        self.app.target_cell.set('changed_while_running')
        self.pump(lambda: not self.app.busy)
        self.assertEqual(len(applied),1)
        self.assertIn('Inputs changed while running',self.app.log_path.read_text())


if __name__ == '__main__':
    unittest.main()
