import copy
import os
import unittest
from pathlib import Path

from vco_studio import schematic as S
from vco_studio import topology_view as T

EXAMPLE_PACK = str(Path(__file__).resolve().parents[1] / 'examples' / 'schematic-pack')


def _module(packaged, kind_prefix):
    return next(s for s in packaged['subcircuits'] if s['name'].startswith(kind_prefix))


VARIANTS = [('hegazi', 'tail'), ('hegazi', 'top'), ('hegazi', 'voltage'), ('murphy', 'nmos'), ('murphy', 'cmos'),
            ('lim', 'inverse_f'), ('guo', 'wideband'), ('du', 'autotracking'), ('class_b', 'nmos'), ('class_b', 'tail')]


class PackagingTest(unittest.TestCase):
    def test_every_variant_packages_equivalently_and_connects(self):
        for fam, var in VARIANTS:
            with self.subTest(variant=fam + '/' + var):
                r = S.prepare(fam, var)
                self.assertTrue(r['equivalence']['equivalent'], r['equivalence'])
                bad = {k: v['errors'][:3] for k, v in r['connectivity']['cells'].items() if not v['ok']}
                self.assertEqual(bad, {})
                for sub in r['packaged']['subcircuits']:
                    sym = r['layout']['cells'][sub['name']]['symbol']
                    self.assertEqual(sorted(sym['pins']), sorted(p['name'] for p in sub['ports']))

    def test_equivalence_detects_changes(self):
        r = S.prepare('lim', 'inverse_f')
        p = copy.deepcopy(r['packaged'])
        xf = _module(p, 'IND')
        next(i for i in xf['instances'] if i['role'] == 'ind')['nodes']['n'] = 'OUTN'
        self.assertFalse(S.equivalence(r['graph'], p, r['spec'])['equivalent'])
        p = copy.deepcopy(r['packaged'])
        top = next(i for i in p['instances'] if i['role'] == 'subckt' and i['master'].startswith('IND'))
        a, b = sorted(top['nodes'])[:2]
        top['nodes'][a], top['nodes'][b] = top['nodes'][b], top['nodes'][a]
        self.assertFalse(S.equivalence(r['graph'], p, r['spec'])['equivalent'])

    def test_equivalence_scopes_internal_nets_and_hierarchy_parameters(self):
        r = S.prepare('hegazi', 'tail')
        p = copy.deepcopy(r['packaged'])
        gm = _module(p, 'GM')
        gm['ports'] = [x for x in gm['ports'] if x['name'] != 'OUTP']
        del next(i for i in p['instances'] if i.get('master') == gm['name'])['nodes']['OUTP']
        self.assertFalse(S.equivalence(r['graph'], p, r['spec'])['equivalent'])
        e = {'name': 'top', 'ports': [{'name': 'P'}, {'name': 'N'}], 'parameters': {},
             'instances': [{'name': 'I', 'role': 'subckt', 'master': 'R', 'nodes': {'P': 'P', 'N': 'N'}, 'parameters': {'r': 1000}}],
             'subcircuits': [{'name': 'R', 'ports': [{'name': 'P'}, {'name': 'N'}], 'parameters': {'r': 1000},
                              'instances': [{'name': 'R0', 'role': 'res', 'ideal': True, 'nodes': {'p': 'P', 'n': 'N'}, 'parameters': {'r': 'r'}}]}]}
        q = copy.deepcopy(e)
        q['instances'][0]['parameters']['r'] = 2000
        self.assertTrue(S.equivalence(e, copy.deepcopy(e), {})['equivalent'])
        self.assertFalse(S.equivalence(e, q, {})['equivalent'])

    def test_equivalence_parameter_scopes_and_ports(self):
        def mk(top, sub):
            return {'name': 'top', 'ports': [{'name': 'P'}, {'name': 'N'}], 'parameters': top,
                    'instances': [{'name': 'I', 'role': 'subckt', 'master': 'R', 'nodes': {'P': 'P', 'N': 'N'}, 'parameters': {}}],
                    'subcircuits': [{'name': 'R', 'ports': [{'name': 'P'}, {'name': 'N'}], 'parameters': sub,
                                     'instances': [{'name': 'R0', 'role': 'res', 'ideal': True, 'nodes': {'p': 'P', 'n': 'N'}, 'parameters': {'r': 'r'}}]}]}
        self.assertFalse(S.equivalence(mk({'base': 1000}, {'r': 'base'}), mk({'base': 2000}, {'r': 'base'}), {})['equivalent'])
        flat = {'name': 'top', 'ports': [{'name': 'P'}, {'name': 'N'}], 'parameters': {'r': 1000}, 'subcircuits': [],
                'instances': [{'name': 'R0', 'role': 'res', 'ideal': True, 'nodes': {'p': 'P', 'n': 'N'}, 'parameters': {'r': 'r'}}]}
        packed = {'name': 'top', 'ports': [{'name': 'P'}, {'name': 'N'}], 'parameters': {'r': 1000},
                  'instances': [{'name': 'M', 'role': 'subckt', 'master': 'MOD', 'nodes': {'P': 'P', 'N': 'N'}, 'parameters': {}}],
                  'subcircuits': [{'name': 'MOD', 'ports': [{'name': 'P'}, {'name': 'N'}], 'parameters': {},
                                   'instances': [{'name': 'R0', 'role': 'res', 'ideal': True, 'nodes': {'p': 'P', 'n': 'N'}, 'parameters': {'r': 'r'}}]}]}
        self.assertTrue(S.equivalence(flat, packed, {'modules': [{'name': 'M'}]})['equivalent'])
        # local names shadow globals regardless of order; SPICE suffixes inside expressions
        self.assertTrue(S.equivalence(mk({'base': 1000}, {'r': 'base', 'base': 2000}), mk({}, {'r': 2000}), {})['equivalent'])
        self.assertTrue(S.equivalence(mk({}, {'base': 2000, 'r': 'base'}), mk({}, {'r': 'base', 'base': 2000}), {})['equivalent'])
        self.assertTrue(S.equivalence(mk({'base': 1000}, {'r': 'base+1k'}), mk({}, {'r': 2000}), {})['equivalent'])
        self.assertFalse(S.equivalence(mk({}, {'r': 'undefined_name'}), mk({}, {'r': 'undefined_name'}), {})['equivalent'])
        r = S.prepare('hegazi', 'tail')
        p = copy.deepcopy(r['packaged'])
        p['ports'] = [x for x in p['ports'] if x['name'] != 'VDC']
        self.assertFalse(S.equivalence(r['graph'], p, r['spec'])['equivalent'])

    def test_register_widths_other_than_the_plan(self):
        from vco_studio.catalog import default_project
        for fam, var in VARIANTS:
            for bits in (2, 5, 6):
                with self.subTest(variant=fam + '/' + var, bits=bits):
                    p = default_project(fam, var)
                    p['registers']['main'].update(bits=bits, value=0)
                    if 'aux' in p['registers']:
                        p['registers']['aux'].update(bits=bits, value=0)
                    r = S.prepare(fam, var, project=p)
                    self.assertTrue(r['ok'], {k: v['errors'][:2] for k, v in r['connectivity']['cells'].items() if not v['ok']})

    def test_mutual_cannot_cross_module(self):
        spec = S.load_spec('hegazi', 'tail')
        spec = copy.deepcopy(spec)
        spec['modules'][0]['members'].remove('KDADB')
        from vco_studio.catalog import get_variant
        with self.assertRaises(ValueError):
            S.package_variant(get_variant('hegazi', 'tail')['graph'], spec)

    def test_connectivity_rules(self):
        inst = [{'name': 'A', 'nodes': {'p': 'X'}, 'pins': {'p': (0, 0)}}, {'name': 'B', 'nodes': {'p': 'Y'}, 'pins': {'p': (1, 0)}}]
        cell = {'instances': inst, 'wires': [{'pts': [[0, 0], [2, 0]]}], 'labels': [], 'pins': []}
        self.assertFalse(S.connectivity_check(cell)['ok'])          # B.p mid-wire
        cell = {'instances': inst, 'wires': [{'pts': [[0, 0], [0, 1]]}, {'pts': [[1, 0], [1, 1]]}],
                'labels': [{'net': 'X', 'xy': [0, 1]}, {'net': 'Y', 'xy': [1, 1]}], 'pins': []}
        self.assertTrue(S.connectivity_check(cell)['ok'])
        cell['wires'].append({'pts': [[0, 0.5], [1, 0.5]]})          # T junctions short X and Y
        self.assertFalse(S.connectivity_check(cell)['ok'])
        inst = [{'name': 'A', 'nodes': {'p': 'X'}, 'pins': {'p': (0, 0)}}]
        cell = {'instances': inst, 'wires': [{'pts': [[0, 0], [0, 2]]}, {'pts': [[0, 1], [1, 1]]}, {'pts': [[0, 1], [-1, 1]]}],
                'labels': [{'net': 'X', 'xy': [1, 1]}], 'pins': []}
        self.assertTrue(any('four-way' in e for e in S.connectivity_check(cell)['errors']))

    def test_orientations_are_cadence(self):
        self.assertEqual(S.orient_point('MY', 1, 2), (-1, 2))
        self.assertEqual(S.orient_point('R90', 1, 0), (0, 1))
        self.assertEqual(S.orient_point('MXR90', 1, 2), (2, 1))


class TopologyDrawingTest(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get('VCO_STUDIO_SCHEMATIC_DIR')
        os.environ['VCO_STUDIO_SCHEMATIC_DIR'] = EXAMPLE_PACK

    def tearDown(self):
        if self._env is None:
            os.environ.pop('VCO_STUDIO_SCHEMATIC_DIR', None)
        else:
            os.environ['VCO_STUDIO_SCHEMATIC_DIR'] = self._env

    def test_example_pack_is_used(self):
        r = S.prepare('class_b', 'nmos')
        self.assertTrue(r['ok'])
        self.assertIn('L_TANK', [i['name'] for i in r['packaged']['instances']])
        self.assertIsNotNone(T.load_ui('class_b', 'nmos'))

    def test_available_drawings_match_schematic(self):
        for fam, var in VARIANTS:
            if T.load_ui(fam, var) is None:
                continue
            with self.subTest(variant=fam + '/' + var):
                r = T.check_ui(fam, var)
                self.assertTrue(r['ok'], r)

    def test_unmapped_drawn_terminal_is_an_error(self):
        spec = copy.deepcopy(T.load_ui('class_b', 'nmos'))
        g = next(x for x in spec['glyphs'] if x.get('ref') == 'GM/MN1')
        g['terms'] = {'d': 'd', 's': 's'}
        with self.assertRaises(KeyError):
            T.check_ui('class_b', 'nmos', spec)

    def test_papers_listed_for_each_family(self):
        for fam, var in VARIANTS:
            if fam == 'class_b':
                continue
            self.assertTrue(T.papers_for(fam, var), fam + '/' + var)


if __name__ == '__main__':
    unittest.main()
