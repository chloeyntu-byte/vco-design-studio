"""Real installed-tool contract tests; analytic inputs are explicitly fixtures."""
import json
import math
from pathlib import Path
import tempfile
import unittest

from vco_studio import integrations as api
from vco_studio.common import electrical_hash


class RealToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.generated = self.root / 'generated'
        self.generated.mkdir()
        self.graph = {'schema': 'vco.graph.v1', 'name': 'fixture', 'parameters': {'c': 1e-12, 'vdd': 1.0},
                      'registers': [], 'instances': [
            {'name': 'L1', 'role': 'ind', 'nodes': {'p': 'P', 'n': 'N'}, 'parameters': {'l': 1e-9}},
            {'name': 'C1', 'role': 'cap', 'nodes': {'p': 'P', 'n': 'N'}, 'parameters': {'c': 1e-12}},
            {'name': 'R1', 'role': 'res', 'nodes': {'p': 'P', 'n': 'N'}, 'parameters': {'r': 1000.0}}]}
        self.hash = electrical_hash(self.graph)
        for name, value in [('graph', self.graph), ('project', {'parameters': {'c': 1e-12}}), ('manifest', {'graph_hash': self.hash})]:
            (self.generated / (name + '.json')).write_text(json.dumps(value))

    def tearDown(self):
        self.temp.cleanup()

    @unittest.skipUnless(api.TOOL_ROOTS['z11'].is_dir(), 'Optional external z11 tool not configured')
    def test_z11_real_solver_matches_parallel_rlc(self):
        receipt = api.z11_analyze(self.generated, self.root / 'z11', ['P', 'N'], [1e9, 3e9, 8e9])
        self.assertEqual(receipt['sources'][0]['module'], 'network_model_v2')
        for row in receipt['rows']:
            omega = 2 * math.pi * row['frequency_Hz']
            expected = 1 / (1 / 1000 + 1j * (omega * 1e-12 - 1 / (omega * 1e-9)))
            self.assertAlmostEqual(row['Z_real_ohm'], expected.real, places=8)
            self.assertAlmostEqual(row['Z_imag_ohm'], expected.imag, places=8)
        self.assertFalse(receipt['qualified'])
        self.assertEqual(receipt['ignored_component_count'], 0)

    @unittest.skipUnless(api.TOOL_ROOTS['z11'].is_dir(), 'Optional external z11 tool not configured')
    def test_z11_signed_coupling_dot_equivalence_and_psd(self):
        import copy
        import numpy as np
        graph = copy.deepcopy(self.graph)
        graph['instances'] = [
            {'name': 'L1', 'role': 'ind', 'nodes': {'p': 'P', 'n': 'MID'}, 'parameters': {'l': 1e-9}},
            {'name': 'L2', 'role': 'ind', 'nodes': {'p': 'MID', 'n': 'N'}, 'parameters': {'l': 1e-9}},
            {'name': 'K12', 'role': 'mutual', 'nodes': {'l1': 'L1', 'l2': 'L2'}, 'parameters': {'k': -.3}}]
        data = {'graph': graph, 'graph_hash': electrical_hash(graph)}
        document, _ = api._document(data, ['P', 'N'])
        model = api._module('z11', 'network_model_v2')
        frequencies = np.array([1., 2.])
        negative, _ = model.solve(document, frequencies)
        expected = 1j * 2 * np.pi * frequencies * 1e9 * 1.4e-9
        np.testing.assert_allclose(negative, expected, rtol=1e-12)
        dotted = copy.deepcopy(document)
        next(p for p in dotted['parts'] if p['kind'] == 'K')['value'] = .3
        next(p for p in dotted['parts'] if p['id'] == 'L2')['dot'] = 1
        np.testing.assert_allclose(model.solve(dotted, frequencies)[0], negative, rtol=1e-12)
        positive = copy.deepcopy(dotted)
        next(p for p in positive['parts'] if p['id'] == 'L2')['dot'] = 0
        np.testing.assert_allclose(model.solve(positive, frequencies)[0], 1j * 2 * np.pi * frequencies * 1e9 * 2.6e-9, rtol=1e-12)
        invalid = copy.deepcopy(document)
        invalid['parts'].append({'id': 'L3', 'kind': 'L', 'value': 1000, 'q': 'inf', 'x': 1900, 'y': 100, 'rotation': 0})
        next(p for p in invalid['parts'] if p['kind'] == 'K')['value'] = -.9
        for name, other in [('K13', 'L1'), ('K23', 'L2')]:
            invalid['parts'].append({'id': name, 'kind': 'K', 'value': -.9, 'l1': other, 'l2': 'L3', 'x': 0, 'y': 0, 'rotation': 0})
        with self.assertRaisesRegex(ValueError, '非正半定'):
            model.validate(invalid)

    @unittest.skipUnless(api.TOOL_ROOTS['z11'].is_dir(), 'Optional external z11 tool not configured')
    def test_z11_actual_gui_and_optimizer_accept_negative_k(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import numpy as np
        studio = api._module('z11', 'studio_v2')
        optimizer = api._module('z11', 'optimizer')
        document = {'parts': [
            {'id': 'L1', 'kind': 'L', 'value': 1000., 'q': 30, 'dot': 0, 'x': 100, 'y': 100, 'rotation': 0},
            {'id': 'L2', 'kind': 'L', 'value': 1000., 'q': 30, 'dot': 0, 'x': 300, 'y': 100, 'rotation': 0},
            {'id': 'K12', 'kind': 'K', 'value': .3, 'l1': 'L1', 'l2': 'L2', 'x': 0, 'y': 0, 'rotation': 0},
            {'id': 'PORT1', 'kind': 'P', 'value': 0, 'q': 0, 'x': 500, 'y': 100, 'rotation': 0}], 'wires': []}
        def var(value):
            return SimpleNamespace(get=lambda: value)
        fake = SimpleNamespace(document=document, selection={'K12'}, value_var=var('-.4'),
                               q_var=var('0'), l1_var=var('L1'), l2_var=var('L2'),
                               _changed=lambda: None, _refresh_parameter_choices=lambda: None,
                               _update_inspector=lambda: None, redraw=lambda: None)
        with patch.object(studio.messagebox, 'showerror') as error:
            studio.Studio.apply_properties(fake)
            error.assert_not_called()
        self.assertEqual(document['parts'][2]['value'], -.4)
        settings = [{'enabled': True, 'parameter': 'K12.value', 'group': '独立',
                     'start': '-.6', 'stop': '.2', 'count': '5', 'scale': 'linear'}]
        fake._sweep_settings = lambda: settings
        curves = studio.Studio._sweep_curves(fake)
        self.assertEqual(len(curves), 5)
        self.assertAlmostEqual(curves[0]['K12.value'], -.6)
        settings[0]['scale'] = 'log'
        with self.assertRaisesRegex(ValueError, '对数'):
            studio.Studio._sweep_curves(fake)
        config = studio.default_optimization_settings()
        config.update(mode='peak', target='magnitude')
        config['variables'] = [{'parameter': 'K12.value', 'low': -.8, 'high': .5}]
        normalized = optimizer.validate_settings(document, np.array([1., 2., 3.]), config)
        self.assertEqual(normalized['variables'][0]['low'], -.8)
        config['variables'][0]['low'] = -1
        with self.assertRaisesRegex(ValueError, '范围'):
            optimizer.validate_settings(document, np.array([1., 2., 3.]), config)

    @unittest.skipUnless(api.TOOL_ROOTS['caplab'].is_dir(), 'Optional external caplab tool not configured')
    def test_caplab_real_matrix_postprocessor_and_workbook(self):
        samples = []
        for f, b in [(1e9, -.01), (2e9, .01)]:
            y = complex(.001, b)
            row = {'point': 0, 'frequency_Hz': f}
            for pair, value in [('11', y), ('12', -y), ('21', -y), ('22', y)]:
                row['Y' + pair + '_G_S'], row['Y' + pair + '_B_S'] = value.real, value.imag
            samples.append(row)
        receipt = api.caplab_analyze(self.generated, samples, self.root / 'caplab', ['P', 'N'],
                                    {'source_graph_hash': self.hash, 'port': ['P', 'N'], 'kind': 'analytic_fixture'})
        self.assertAlmostEqual(receipt['resonances']['0']['frequency_Hz'], 1.5e9)
        self.assertTrue(receipt['resonances']['0']['estimated'])
        workbook = api._module('caplab', 'caplab.workbook')
        book = workbook.workbook(json.loads(Path(receipt['workbook']).read_text()))
        self.assertEqual(len(workbook.cached_rows(book['pages'][0]['result'])), 2)
        self.assertEqual(receipt['workbook_scope']['kind'], 'static_passive_projection')
        self.assertEqual(receipt['measurement_scope']['kind'], 'analytic_fixture')
        self.assertFalse(receipt['measurement_scope']['derived_from_workbook_projection'])
        self.assertTrue(receipt['launch_action']['explicit_project'])
        self.assertIn('--runtime', receipt['launch_action']['argv'])

    def test_refuse_changed_source_and_output_overwrite(self):
        spec = api.action_spec('z11', self.generated, self.root / 'out', port=['P', 'N'], frequencies_hz=[1e9])
        self.graph['instances'][0]['parameters']['l'] = 2e-9
        (self.generated / 'graph.json').write_text(json.dumps(self.graph))
        with self.assertRaises(ValueError):
            api.execute_action(spec)

    def _mco_fixture(self):
        from vco_studio.catalog import default_project
        from vco_studio.generator import generate
        directory = self.root / 'real-generated'
        generate(default_project('murphy', 'nmos'), directory)
        return directory, [{'parameter_id': 'L_scale', 'minimum': .9, 'maximum': 1.1}]

    @unittest.skipUnless(api.TOOL_ROOTS['mco'].is_dir(), 'Optional external mco tool not configured')
    def test_mco_actual_failed_evaluator_never_becomes_feasible(self):
        import subprocess
        directory, variables = self._mco_fixture()
        def failed_evaluator(project, candidate_dir):
            failed = subprocess.run(['/bin/false'], check=False)
            return {'status': 'failed', 'feasible': False, 'total_violation': 0,
                    'evidence': {'kind': 'intentional_external_failure_fixture', 'returncode': failed.returncode}}
        receipt = api.optimize(directory, variables, failed_evaluator, self.root / 'failed-mco', budget=2, batch_size=1)
        self.assertEqual(receipt['status'], 'failed')
        self.assertIsNone(receipt['best'])
        self.assertIsNone(receipt['best_unqualified'])
        self.assertEqual(receipt['failed_evaluations'], 2)
        self.assertEqual(receipt['completed_evaluations'], 0)
        history = json.loads(Path(receipt['history']).read_text())
        self.assertTrue(all(row['evaluation_class'] == 'evaluation_failure' and not row['metrics']['feasible'] for row in history))
        self.assertTrue(all(row['metrics']['merit'] is None and row['evidence']['returncode'] != 0 for row in history))

    @unittest.skipUnless(api.TOOL_ROOTS['mco'].is_dir(), 'Optional external mco tool not configured')
    def test_mco_constraint_failure_is_evaluated_but_not_adoptable(self):
        directory, variables = self._mco_fixture()
        def evaluator(project, candidate_dir):
            return {'status': 'completed', 'feasible': False, 'total_violation': 2,
                    'merit': 10, 'objectives': [10], 'violations': ['frequency outside target']}
        receipt = api.optimize(directory, variables, evaluator, self.root / 'infeasible-mco', budget=2, batch_size=1)
        self.assertEqual(receipt['status'], 'completed')
        self.assertIsNone(receipt['best'])
        self.assertEqual(receipt['best_unqualified']['evaluation_class'], 'constraint_failure')
        self.assertEqual(receipt['completed_evaluations'], 2)
        self.assertEqual(receipt['feasible_evaluations'], 0)

    @unittest.skipUnless(api.TOOL_ROOTS['mco'].is_dir(), 'Optional external mco tool not configured')
    def test_mco_missing_feasibility_contract_rejected(self):
        directory, variables = self._mco_fixture()
        with self.assertRaisesRegex(ValueError, 'explicitly provide'):
            api.optimize(directory, variables, lambda project, path: {'merit': 0},
                         self.root / 'malformed-mco', budget=1, batch_size=1)

    def test_mco_requires_explicit_evaluator(self):
        with self.assertRaisesRegex(ValueError, 'explicit project evaluator'):
            api.optimize(self.generated, [{'parameter_id': 'c', 'minimum': .1e-12, 'maximum': 2e-12}], None, self.root / 'mco')
        receipt = json.loads((self.root / 'mco' / 'receipt.json').read_text())
        self.assertEqual(receipt['status'], 'failed')

    def test_matrix_graph_identity_required(self):
        with self.assertRaisesRegex(ValueError, 'provenance'):
            api.caplab_analyze(self.generated, [], self.root / 'bad', ['P', 'N'], {'kind': 'native_ac'})


if __name__ == '__main__':
    unittest.main()
