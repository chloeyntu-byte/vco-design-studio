"""Offline stage interface tests: the executable replays synthetic PSF-format text, never Spectre.

Freeze documents below are input scaffolds for downstream interfaces, not new
freeze-qualification claims. Real freeze acceptance remains owned by test_flow.
"""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from vco_studio import flow, site, stages
from vco_studio.autosize import Executor
from vco_studio.catalog import default_project
from vco_studio.common import ROOT, read_json, write_json, sha256
from vco_studio.generator import generate

FIXTURES = Path(__file__).parent / 'fixtures/stages_psf'


class StageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.task = self.root / 'task'; self.task.mkdir()
        self.model = self.root / 'model.scs'
        self.model.write_text('\n'.join('section ' + s + '\nendsection ' + s
                              for s in ('tt', 'ff', 'ss', 'fnsp', 'snfp', 'res_tt', 'res_ff', 'res_ss', 'mc')) + '\n')
        self.solver = self.root / 'psf-replay'
        self.solver.write_text('#!' + sys.executable + '\n' + '''from pathlib import Path
import os,re,shutil
source=Path(os.environ['VCO_TEST_PSF'])
with open(os.environ['VCO_TEST_CALLS'],'a') as f:f.write(str(Path.cwd())+'\\n')
psf=Path('psf');psf.mkdir()
tb=Path('tb.scs').read_text()
mc=re.search(r'montecarlo numruns=(\\d+)',tb)
for i in range(int(mc[1]) if mc else 1):
    prefix=('mc1-%03d_'%(i+1)) if mc else ''
    for name in ('hb.fd.pss_hb','hbnoise.pm.pnoise_hbnoise'):
        shutil.copy2(source/('hb.txt' if name.startswith('hb.fd') else 'noise.txt'),psf/(prefix+name))
Path('spectre.log').write_text('Offline PSF replay stub; no circuit was solved.\\n')
''')
        self.solver.chmod(0o700)
        self.sitefile = self.root / 'site.json'
        write_json(self.sitefile, {'python': sys.executable, 'sim_lock_command': [], 'kb': {'enabled': False}})
        self.env = patch.dict(os.environ, {'VCO_SITE': str(self.sitefile), 'VCO_SPECTRE': str(self.solver),
                                          'VCO_STUDIO_PYTHON': sys.executable, 'VCO_CADENCE_ENV': str(self.root / 'absent-env'),
                                          'VCO_TEST_PSF': str(FIXTURES.resolve()), 'VCO_TEST_CALLS': str(self.root / 'calls'),
                                          'PYTHONDONTWRITEBYTECODE': '1'})
        self.env.start(); site.reset_cache()
        self.project = default_project('guo', 'wideband')
        binding = read_json(ROOT / 'examples/bindings/demo_a.json')
        binding['model_includes'] = [{'path': str(self.model), 'section': 'tt'}, {'path': str(self.model), 'section': 'res_tt'}]
        for role in binding['roles'].values():
            role['geometry_policy'] = 'preserve_verified'
        self.project['binding'] = binding
        self.spec = {'frequency_band_hz': [10e9, 12e9], 'power_max_mw': 100, 'main_bits': 4,
                     'pn_targets_dbc_hz': {'1000000': -20}}
        self.state = {'schema': 'vco.task.v2', 'spec': self.spec, 'candidates': [], 'events': [],
                      'stage': 'characterization', 'budget': {}, 'completed': False, 'evidence': [],
                      'created': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        write_json(self.task / 'task.json', self.state)
        self.candidate = flow.add_candidate(self.task, self.project, reason='test candidate')
        self.cid = self.candidate['id']
        self.codes = self.root / 'codes.json'; write_json(self.codes, [{'main': 0}, {'main': 15}])

    def tearDown(self):
        self.env.stop(); site.reset_cache(); self.temp.cleanup()

    def cli(self, *args):
        return subprocess.run([str(ROOT / 'launch'), *map(str, args)], cwd=ROOT, env=os.environ.copy(), text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    def frozen(self):
        ex = Executor(self.root / 'nominal', 2, lock_command=[])
        results = []
        for code in (0, 15):
            p = stages._with_code(self.project, {'main': code})
            r = ex.measure(p, 'nominal')
            self.assertEqual(r['status'], 'measured', r.get('error'))
            results.append(str(Path(r['path']) / 'result.json'))
        directory = Path(self.candidate['directory'])
        manifest = read_json(directory / 'manifest.json')
        evidence = []
        for file in results:
            row = flow._verify_result(file, manifest['physical_identity'], sha256(directory / 'circuit.scs'))
            evidence.append({k: row[k] for k in ('file', 'result_sha256', 'receipt_sha256', 'input_sha256', 'raw_evidence_sha256')})
        freeze = {'candidate': self.cid, 'physical_identity': manifest['physical_identity'],
                  'adopted_control_states': [{'main': {'bits': 4, 'value': c}} for c in (0, 15)],
                  'adopted_results': results, 'convergence_results': [], 'verified_evidence': evidence,
                  'scope': 'test input scaffold; no new physical qualification', 'not_qualified': ['PVT', 'layout']}
        write_json(self.task / 'freeze.json', freeze)
        state = read_json(self.task / 'task.json'); state.update(completed=True, stage='frozen')
        state['candidates'][0]['results'] = results
        write_json(self.task / 'task.json', state)
        return freeze

    def test_fixture_provenance_hashes(self):
        provenance = read_json(FIXTURES / 'provenance.json')
        for name, digest in provenance['files'].items():
            self.assertEqual(sha256(FIXTURES / name), digest)

    def test_corner_mapping_and_passive_fallback(self):
        for corner, active in [('tt', 'tt'), ('ff', 'ff'), ('ss', 'ss'), ('fs', 'fnsp'), ('sf', 'snfp')]:
            cm = stages.derive_corner(self.project['binding'], corner)
            self.assertEqual(cm['status'], 'supported')
            self.assertEqual(cm['model_includes'][0]['section'], active)
            self.assertEqual(cm['model_includes'][1]['section'], 'res_' + (corner if corner in ('tt', 'ff', 'ss') else 'tt'))
            self.assertEqual(len(cm['fallbacks']), int(corner in ('fs', 'sf')))
        # Alternate Example nsp naming is discovered from the actual file, not PDK name.
        self.model.write_text('section tt\nsection nsp\nsection snfp\nsection res_tt\n')
        self.assertEqual(stages.derive_corner(self.project['binding'], 'fs')['model_includes'][0]['section'], 'nsp')

    def test_example_prefixes_and_unsupported_corner(self):
        self.model.write_text('section tt_rfmos\nsection ff_rfmos\nsection fs_rfmos\nsection sf_rfmos\nsection tt_rfmim\n')
        b = {'model_includes': [{'path': str(self.model), 'section': s} for s in ('tt_rfmos', 'tt_rfmim')]}
        self.assertEqual(stages.derive_corner(b, 'fs')['status'], 'supported')
        self.assertEqual(stages.derive_corner(b, 'sf')['status'], 'supported')
        self.assertEqual(stages.derive_corner(b, 'ff')['status'], 'unsupported')
        self.assertEqual(stages.derive_corner(b, 'ss')['status'], 'unsupported')

    def test_real_binding_section_inventory(self):
        # Discover sections from an invented model file, rather than a bundled technology.
        self.model.write_text('section tt\nsection ff\nsection ss\nsection fs\nsection sf\nsection mc\n')
        b = {'model_includes': [{'path': str(self.model), 'section': 'tt'}]}
        for corner in ('tt', 'ff', 'ss', 'fs', 'sf'):
            self.assertEqual(stages.derive_corner(b, corner)['status'], 'supported')
        b['monte_carlo'] = {'model_includes': [{'path': str(self.model), 'section': 'mc'}]}
        _, selected, _ = stages._mc_binding(b)
        self.assertTrue(selected)

    def test_unfrozen_requires_candidate_and_codes(self):
        with self.assertRaisesRegex(ValueError, '--candidate'):
            stages.verify(self.task, 'pvt', dry_run=True)
        with self.assertRaisesRegex(ValueError, '--codes'):
            stages.verify(self.task, 'pvt', candidate=self.cid, dry_run=True)

    def test_max_runs_rejects_before_generation_or_solver(self):
        with self.assertRaisesRegex(ValueError, 'needs 90 runs'):
            stages.verify(self.task, 'pvt', candidate=self.cid, codes=self.codes, max_runs=89)
        self.assertFalse((self.task / 'verification/pvt').exists())
        self.assertFalse((self.root / 'calls').exists())
        self.assertEqual(read_json(self.task / 'verification/status.json')['pvt']['status'], 'failed')

    def test_cli_dry_run_negative_csv_and_no_solver(self):
        p = self.cli('verify', self.task, '--stage', 'pvt', '--candidate', self.cid, '--codes', self.codes,
                     '--corners', 'tt,fs', '--temps', '-40,27', '--vdd-pct', '-10,10', '--dry-run', '--max-runs', 16)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        data = json.loads(p.stdout)
        self.assertTrue(data['provisional'])
        self.assertEqual(len(data['points']), 16)
        self.assertFalse((self.root / 'calls').exists())
        for row in data['points']:
            generated = Path(row['generated'])
            p = read_json(generated / 'project.json')
            self.assertEqual(p['parameters']['temp_c'], row['temp_c'])
            self.assertAlmostEqual(p['parameters']['vdd'], row['vdd'])
            self.assertIn('temp=' + str(row['temp_c']), (generated / 'tb.scs').read_text())
            self.assertFalse((generated / 'sim').exists())
        self.assertEqual(read_json(self.task / 'verification/status.json')['pvt']['status'], 'prepared')

    def test_pvt_stub_measurement_delta_and_condition_coverage(self):
        self.frozen()
        before = sha256(self.task / 'freeze.json')
        data = stages.verify(self.task, 'pvt', corners=('tt', 'ff'), temps=(27,), vdd_pct=(0, 10), max_runs=8)
        self.assertFalse(data['provisional'])
        self.assertEqual(len(data['points']), 8)
        self.assertEqual(read_json(self.task / 'verification/status.json')['pvt']['status'], 'done')
        self.assertFalse(data['pass'])  # same replay frequency cannot cover a 10..12 GHz band
        self.assertEqual(len(data['condition_groups']), 4)
        for row in data['points']:
            self.assertEqual(row['status'], 'measured')
            self.assertEqual(row['delta_tt']['frequency_hz'], 0)
            self.assertIn('pn_10000000_dbc_hz', row['metrics'])
        self.assertEqual(before, sha256(self.task / 'freeze.json'))

    def test_coverage_cannot_combine_different_conditions(self):
        rows = [{'corner': 'tt', 'temp_c': t, 'vdd': 1, 'code': {'main': i}, 'status': 'measured', 'pass': True,
                 'metrics': {'frequency_hz': f, 'power_mw': 5}} for i, (t, f) in enumerate([(-40, 9e9), (125, 13e9)])]
        self.assertTrue(all(not g['covers_spec_band'] for g in stages._aggregate(rows, self.spec)))

    def test_failed_solver_and_unsupported_corner_do_not_claim_done(self):
        self.solver.write_text('#!' + sys.executable + '\nraise SystemExit(7)\n')
        data = stages.verify(self.task, 'pvt', candidate=self.cid, codes=self.codes, corners=('tt',), temps=(27,), vdd_pct=(0,))
        self.assertFalse(data['pass'])
        self.assertEqual(read_json(self.task / 'verification/status.json')['pvt']['status'], 'failed')
        # Remove ff model section before generating a fresh candidate (old hash must be protected).
        b = {'model_includes': [{'path': str(self.model), 'section': 'missing_tt'}]}
        self.assertEqual(stages.derive_corner(b, 'ff')['status'], 'unsupported')

    def test_postsim_top_or_port_mismatch_before_solver(self):
        netlist = self.root / 'extracted.scs'
        graph = read_json(Path(self.candidate['directory']) / 'graph.json')
        pins = [p['name'] for p in graph['ports']]
        pins[0], pins[1] = pins[1], pins[0]
        netlist.write_text('subckt ' + graph['name'] + ' ' + ' '.join(pins) + '\nends\n')
        with self.assertRaisesRegex(ValueError, 'port mismatch'):
            stages.verify(self.task, 'postsim', candidate=self.cid, codes=self.codes, netlist=netlist)
        self.assertFalse((self.root / 'calls').exists())
        good = Path(self.candidate['directory']) / 'circuit.scs'
        with self.assertRaisesRegex(ValueError, 'port mismatch'):
            stages.verify(self.task, 'postsim', candidate=self.cid, codes=self.codes, netlist=good, top_subckt='wrong')

    def test_postsim_stub_comparison_and_extracted_manifest(self):
        self.frozen()
        source = Path(self.candidate['directory']) / 'circuit.scs'
        extracted = self.root / 'extracted.scs'; extracted.write_text(source.read_text() + '\n// extracted test fixture\n')
        data = stages.verify(self.task, 'postsim', netlist=extracted, max_runs=2)
        self.assertEqual(read_json(self.task / 'verification/status.json')['postsim']['status'], 'done')
        for row in data['points']:
            d = Path(row['generated'])
            self.assertEqual(sha256(d / 'circuit.scs'), sha256(extracted))
            self.assertEqual(read_json(d / 'manifest.json')['files']['circuit.scs'], sha256(extracted))
            self.assertEqual(row['delta_presim']['frequency_hz'], 0)
            self.assertEqual(row['delta_presim']['power_mw'], 0)
        self.assertTrue(list((self.task / 'verification/postsim').rglob('comparison.csv')))

    def test_mc_defaults_to_prepared_and_enforces_sample_budget(self):
        data = stages.verify(self.task, 'mc', candidate=self.cid, codes=self.codes, runs=3, max_runs=6)
        self.assertFalse((self.root / 'calls').exists())
        self.assertEqual(read_json(self.task / 'verification/status.json')['mc']['status'], 'prepared')
        for row in data['points']:
            self.assertIn('montecarlo numruns=3', (Path(row['generated']) / 'tb.scs').read_text())
            self.assertIn('section=mc', (Path(row['generated']) / 'models.scs').read_text())
        with self.assertRaisesRegex(ValueError, 'exceeds --max-runs'):
            stages.verify(self.task, 'mc', candidate=self.cid, codes=self.codes, runs=4, max_runs=7, run=True)
        self.assertFalse((self.root / 'calls').exists())

    def test_mc_replay_sample_reader_and_no_nominal_fallback(self):
        data = stages.verify(self.task, 'mc', candidate=self.cid, codes=self.codes, runs=2, max_runs=4, run=True)
        self.assertEqual(read_json(self.task / 'verification/status.json')['mc']['status'], 'done', data)
        self.assertEqual([len(r['samples']) for r in data['points']], [2, 2])
        self.assertTrue(all(s['pass'] for r in data['points'] for s in r['samples']))
        # A solver producing only nominal PSF is not counted as MC samples.
        text = self.solver.read_text().replace("mc=re.search(r'montecarlo numruns=(\\d+)',tb)", 'mc=None')
        self.solver.write_text(text)
        data = stages.verify(self.task, 'mc', candidate=self.cid, codes=self.codes, runs=1, max_runs=2, run=True)
        self.assertTrue(all(r['status'] == 'failed' for r in data['points']))

    def test_mc_without_statistical_models_is_not_configured(self):
        # Fresh task/candidate after removing MC section preserves generation hashes.
        self.model.write_text(self.model.read_text().replace('section mc\nendsection mc\n', ''))
        c = flow.add_candidate(self.task, self.project)
        data = stages.verify(self.task, 'mc', candidate=c['id'], codes=self.codes, runs=1000, max_runs=1)
        self.assertIn('statistical', data['reason'])
        self.assertEqual(read_json(self.task / 'verification/status.json')['mc']['status'], 'not_configured')
        self.assertFalse((self.root / 'calls').exists())

    def test_handoff_files_pins_matching_and_budget(self):
        with self.assertRaisesRegex(ValueError, 'freeze.json'):
            stages.handoff(self.task, self.root / 'handoff-before-freeze')
        self.frozen()
        dest = self.root / 'handoff'
        stages.handoff(self.task, dest)
        for name in ('devices.csv', 'matching.json', 'inductors.json', 'parasitic_budget.json', 'pins.json', 'README.md',
                     'graph.json', 'project.json', 'circuit.scs', 'freeze.json'):
            self.assertTrue((dest / name).is_file(), name)
        groups = read_json(dest / 'matching.json')['groups']
        self.assertTrue(any('LDA' in g['members'] and 'LDB' in g['members'] for g in groups))
        self.assertTrue(any(g['rule'] == 'CT_R3U_*' for g in groups))
        for row in read_json(dest / 'parasitic_budget.json')['budgets']:
            self.assertGreater(row['tank_total_c_estimate_f'], 0)
            self.assertAlmostEqual(row['allowed_added_c_for_minus_1pct_f'] / row['tank_total_c_estimate_f'], 1 / .99**2 - 1)
        self.assertTrue(all(i['srf_min_hz_exclusive'] == 24e9 for i in read_json(dest / 'inductors.json')['inductors']))
        self.assertEqual(read_json(self.task / 'verification/status.json')['handoff']['status'], 'done')

    def test_deliver_requires_provisional_and_marks_unfrozen_first_line(self):
        p = self.cli('deliver', self.task)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse((self.task / 'delivery').exists())
        p = self.cli('deliver', self.task, '--provisional')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        dest = self.task / 'delivery'
        self.assertTrue((dest / 'report.md').read_text().splitlines()[0].startswith('# Unfrozen'))
        self.assertTrue(read_json(dest / 'delivery.json')['provisional'])
        with self.assertRaises(FileExistsError):
            stages.deliver(self.task, provisional=True)

    def test_frozen_delivery_hashes_cards_states_and_tamper_rejection(self):
        freeze = self.frozen()
        (self.task / 'DECISIONS.md').write_text('Chose Guo for fixed return network.\n')
        (self.task / 'knowledge').mkdir()
        (self.task / 'knowledge/consults.jsonl').write_text(json.dumps({'cards': ['card-alpha', 'card-beta']}) + '\n')
        stages.verify(self.task, 'pvt', corners=('tt',), temps=(27,), vdd_pct=(0,), dry_run=True)
        out = stages.deliver(self.task)
        dest = Path(out['output']); data = read_json(dest / 'delivery.json')
        self.assertFalse(data['provisional'])
        self.assertEqual(data['knowledge_card_ids'], ['card-alpha', 'card-beta'])
        self.assertEqual(data['verification']['pvt']['status'], 'prepared')
        self.assertIn('Chose Guo', (dest / 'report.md').read_text())
        self.assertIn('not_started', (dest / 'report.md').read_text())
        for name, digest in data['files'].items():
            self.assertEqual(sha256(dest / name), digest)
        self.assertTrue((dest / 'freeze.json').is_file())
        raw = Path(freeze['adopted_results'][0]).parent / 'psf/hb.fd.pss_hb'
        raw.write_bytes(raw.read_bytes() + b'\n// tampered\n')
        with self.assertRaisesRegex(ValueError, 'Raw evidence hash changed'):
            stages.deliver(self.task, output=self.root / 'bad-delivery')
        self.assertFalse((self.root / 'bad-delivery').exists())

    def test_site_lock_command_is_used(self):
        wrapper = self.root / 'lock-wrapper'
        wrapper.write_text('#!' + sys.executable + '\nimport os,sys\nfrom pathlib import Path\n'
                           'Path(' + repr(str(self.root / 'lock-used')) + ').write_text("locked")\nos.execv(sys.argv[1],sys.argv[1:])\n')
        wrapper.chmod(0o700)
        data = read_json(self.sitefile); data['sim_lock_command'] = [str(wrapper)]; write_json(self.sitefile, data); site.reset_cache()
        stages.verify(self.task, 'pvt', candidate=self.cid, codes=self.codes, corners=('tt',), temps=(27,), vdd_pct=(0,))
        self.assertTrue((self.root / 'lock-used').is_file())
        self.assertEqual(read_json(self.task / 'verification/status.json')['pvt']['status'], 'done')
        circuit = Path(self.candidate['directory']) / 'circuit.scs'
        stages.verify(self.task, 'postsim', candidate=self.cid, codes=self.codes, netlist=circuit)
        self.assertEqual(read_json(self.task / 'verification/status.json')['postsim']['status'], 'done')
        data = stages.verify(self.task, 'mc', candidate=self.cid, codes=self.codes, runs=2, max_runs=4, run=True)
        self.assertEqual(read_json(self.task / 'verification/status.json')['mc']['status'], 'done', data)
        receipt = read_json(Path(data['points'][0]['result']).with_name('receipt.json'))
        self.assertIn('lock_outcome', receipt)
        # The lock's waiting allowance must not enlarge the actual solver timeout.
        self.solver.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(30)\n')
        data = stages.verify(self.task, 'mc', candidate=self.cid, codes=self.codes, runs=1, max_runs=2,
                             run=True, timeout=.2)
        self.assertEqual(read_json(self.task / 'verification/status.json')['mc']['status'], 'failed')
        receipt = read_json(Path(data['points'][0]['result']).with_name('receipt.json'))
        self.assertEqual(receipt['stop_reason'], 'timeout')
        self.assertLess(receipt['elapsed_s'], 5)


if __name__ == '__main__':
    unittest.main()
