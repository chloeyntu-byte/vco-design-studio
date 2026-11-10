"""v1.2 core contracts: spec schema, CLI exit codes/errors, bounded runner, evidence validation,
flow robustness, knowledge cards, autosize legality helpers. No Spectre needed."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LAUNCH = str(ROOT / 'launch')


def run_cli(*args, env=None):
    e = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    if env:
        e.update(env)
    p = subprocess.run([LAUNCH] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e, timeout=120)
    return p.returncode, p.stdout, p.stderr


class SpecTests(unittest.TestCase):
    def test_required_and_defaults(self):
        from vco_studio.spec import validate
        s, w = validate({'pdk': 'DEMO_C', 'frequency_band_hz': [2.28e9, 2.52e9], 'power_max_mw': 3})
        self.assertEqual(s['pdk'], 'demo_c')
        self.assertEqual(s['vdd_v'], 1.2)
        self.assertEqual(s['temp_c'], 27)
        self.assertEqual(s['enforcement'], 'guided')
        self.assertAlmostEqual(s['derived']['center_hz'], 2.4e9)

    def test_rejects_bad_fields(self):
        from vco_studio.spec import validate
        with self.assertRaisesRegex(ValueError, 'missing required field pdk'):
            validate({'frequency_band_hz': [1e9, 2e9], 'power_max_mw': 1})
        with self.assertRaisesRegex(ValueError, 'offset'):
            validate({'pdk': 'demo_a', 'frequency_band_hz': [1e9, 2e9], 'power_max_mw': 1, 'pn_targets_dbc_hz': {'2000000': -100}})
        with self.assertRaisesRegex(ValueError, 'frequency_band_hz'):
            validate({'pdk': 'demo_a', 'frequency_band_hz': [2e9, 1e9], 'power_max_mw': 1})
        with self.assertRaisesRegex(ValueError, 'pdk'):
            validate({'pdk': 'invalid binding id', 'frequency_band_hz': [1e9, 2e9], 'power_max_mw': 1})

    def test_pn_key_normalized(self):
        from vco_studio.spec import validate
        s, _ = validate({'pdk': 'demo_b', 'frequency_band_hz': [1e9, 2e9], 'power_max_mw': 1, 'pn_targets_dbc_hz': {'1e6': -100}})
        self.assertEqual(s['pn_targets_dbc_hz'], {'1000000': -100})


class CliContractTests(unittest.TestCase):
    def test_version_and_quickstart(self):
        code, out, _ = run_cli('--version')
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), (ROOT / 'VERSION').read_text().strip())
        code, out, _ = run_cli('quickstart')
        self.assertEqual(code, 0)
        self.assertIn('flow-next', out)

    def test_nan_rejected_by_argparse(self):
        code, _, err = run_cli('evidence-query', '--pdk', 'demo_c', '--frequency-hz', 'nan', '--power-mw', '4')
        self.assertEqual(code, 2)
        self.assertIn('finite positive', err)

    def test_invalid_request_json_error(self):
        code, _, err = run_cli('describe', 'nosuchfamily')
        self.assertEqual(code, 2)
        payload = json.loads(err.strip().splitlines()[-1])
        self.assertEqual(payload['kind'], 'invalid_request')
        self.assertIn('Unknown topology family', payload['message'])

    def test_evidence_compact_has_near_misses(self):
        code, out, _ = run_cli('evidence-query', '--pdk', 'demo_c', '--frequency-hz', '2.4e9', '--power-mw', '3', '--temp-c', '27')
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertIn('near_misses', d)
        self.assertIn('counts', d)
        self.assertNotIn('matches', d)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='vco-runner-'))

    def test_timeout_kills_group(self):
        from vco_studio.runner import run_bounded
        marker = self.tmp / 'child.pid'
        cmd = ['/bin/bash', '-c', 'sleep 30 & echo $! > %s; wait' % marker]
        out = run_bounded(cmd, self.tmp, 1.0, self.tmp / 'out.log', poll_s=0.05)
        self.assertEqual(out['stop_reason'], 'timeout')
        child = int(marker.read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_interrupt_kills_group_and_raises(self):
        from vco_studio.runner import run_bounded, SolverInterrupted
        marker = self.tmp / 'child.pid'
        cmd = ['/bin/bash', '-c', 'sleep 30 & echo $! > %s; wait' % marker]

        def boom(pid):
            time.sleep(0.3)
            raise KeyboardInterrupt
        with self.assertRaises(SolverInterrupted):
            run_bounded(cmd, self.tmp, 10, self.tmp / 'out.log', on_start=boom)
        time.sleep(0.2)
        child = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_rejects_nonpositive_timeout(self):
        from vco_studio.runner import run_bounded
        with self.assertRaises(ValueError):
            run_bounded(['true'], self.tmp, -5, self.tmp / 'o.log')


class SimulateExitCodeTests(unittest.TestCase):
    """simulate with a stub solver: failure -> exit 3, timeout -> exit 4."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='vco-sim-'))
        code, out, err = run_cli('project', 'class_b', '--variant', 'tail', '--binding', 'demo_a', '--output', str(cls.tmp / 'p.json'))
        assert code == 0, err
        code, out, err = run_cli('generate', str(cls.tmp / 'p.json'), '--output', str(cls.tmp / 'gen'))
        assert code == 0, err
        (cls.tmp / 'fail.sh').write_text('#!/bin/sh\necho stub failure > spectre.log\nexit 7\n')
        (cls.tmp / 'slow.sh').write_text('#!/bin/sh\nsleep 30\n')
        for f in ('fail.sh', 'slow.sh'):
            os.chmod(cls.tmp / f, 0o755)

    def test_failure_exit_3(self):
        code, out, _ = run_cli('simulate', str(self.tmp / 'gen'), '--output', str(self.tmp / 'r1'), env={'VCO_SPECTRE': str(self.tmp / 'fail.sh'), 'VCO_CADENCE_ENV': '/nonexistent'})
        self.assertEqual(code, 3)
        d = json.loads(out)
        self.assertEqual(d['failure_class'], 'solver_error')

    def test_timeout_exit_4(self):
        code, out, _ = run_cli('simulate', str(self.tmp / 'gen'), '--output', str(self.tmp / 'r2'), '--timeout', '1', env={'VCO_SPECTRE': str(self.tmp / 'slow.sh'), 'VCO_CADENCE_ENV': '/nonexistent'})
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)['failure_class'], 'timeout')

    def test_sigterm_exit_130_and_no_orphan(self):
        e = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', VCO_SPECTRE=str(self.tmp / 'slow.sh'), VCO_CADENCE_ENV='/nonexistent')
        p = subprocess.Popen([LAUNCH, 'simulate', str(self.tmp / 'gen'), '--output', str(self.tmp / 'r3'), '--timeout', '60'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=e)
        deadline = time.time() + 20
        while time.time() < deadline and not (self.tmp / 'r3' / 'receipt.json').exists():
            time.sleep(0.1)
        time.sleep(0.5)
        receipt = json.loads((self.tmp / 'r3' / 'receipt.json').read_text())
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=20)
        self.assertEqual(p.returncode, 130)
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.killpg(receipt['pid'], 0)
        r = json.loads((self.tmp / 'r3' / 'result.json').read_text())
        self.assertEqual(r['failure_class'], 'interrupted')


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='vco-flow-'))
        (self.tmp / 'spec.json').write_text(json.dumps({'pdk': 'demo_a', 'frequency_band_hz': [9.5e9, 10.5e9], 'power_max_mw': 3,
                                                        'pn_targets_dbc_hz': {'1000000': -110}, 'budget': {'solver_processes': 4}}))
        env = {'VCO_KB_COMMAND': '/bin/false'}
        code, _, err = run_cli('flow-init', '--spec', str(self.tmp / 'spec.json'), '--workspace', str(self.tmp / 'task'), env=env)
        self.assertEqual(code, 0, err)

    def test_temp_conflict_rejected_and_spec_project_ok(self):
        code, _, _ = run_cli('project', 'hegazi', '--variant', 'tail', '--output', str(self.tmp / 'ref.json'))
        self.assertEqual(code, 0)
        code, _, err = run_cli('flow-add', str(self.tmp / 'task'), '--project', str(self.tmp / 'ref.json'), '--reason', 'x')
        self.assertEqual(code, 2)
        self.assertIn('temp_c', err)
        code, _, err = run_cli('project', 'hegazi', '--variant', 'tail', '--spec', str(self.tmp / 'spec.json'), '--output', str(self.tmp / 'ok.json'))
        self.assertEqual(code, 0, err)
        code, out, err = run_cli('flow-add', str(self.tmp / 'task'), '--project', str(self.tmp / 'ok.json'), '--reason', 'x')
        self.assertEqual(code, 0, err)

    def test_unknown_candidate_and_bad_codes(self):
        run_cli('project', 'class_b', '--variant', 'tail', '--spec', str(self.tmp / 'spec.json'), '--output', str(self.tmp / 'p.json'))
        run_cli('flow-add', str(self.tmp / 'task'), '--project', str(self.tmp / 'p.json'), '--reason', 'x')
        code, _, err = run_cli('flow-measure', str(self.tmp / 'task'), 'candidate_999')
        self.assertEqual(code, 2)
        self.assertIn('known candidates: candidate_001', err)
        (self.tmp / 'bad.json').write_text('[{"mian": 1}]')
        code, _, err = run_cli('flow-sweep', str(self.tmp / 'task'), 'candidate_001', '--codes', str(self.tmp / 'bad.json'))
        self.assertEqual(code, 2)
        self.assertIn("unknown register 'mian'", err)
        self.assertFalse((self.tmp / 'task' / 'candidates' / 'candidate_001' / 'code_grid').exists())
        many = json.dumps([{'main': i} for i in range(6)])
        (self.tmp / 'many.json').write_text(many)
        code, _, err = run_cli('flow-sweep', str(self.tmp / 'task'), 'candidate_001', '--codes', str(self.tmp / 'many.json'))
        self.assertEqual(code, 2)
        self.assertIn('remain in the task budget', err)

    def test_flow_next_progression(self):
        code, out, _ = run_cli('flow-next', str(self.tmp / 'task'))
        self.assertEqual(json.loads(out)['action'], 'select')
        run_cli('project', 'class_b', '--variant', 'tail', '--spec', str(self.tmp / 'spec.json'), '--output', str(self.tmp / 'p.json'))
        run_cli('flow-add', str(self.tmp / 'task'), '--project', str(self.tmp / 'p.json'), '--reason', 'x')
        code, out, _ = run_cli('flow-next', str(self.tmp / 'task'))
        d = json.loads(out)
        self.assertEqual(d['action'], 'measure')
        self.assertTrue((self.tmp / 'task' / 'knowledge' / 'consults.jsonl').exists())

    def test_strict_requires_evidence(self):
        spec = json.loads((self.tmp / 'spec.json').read_text())
        spec['enforcement'] = 'strict'
        (self.tmp / 'strict.json').write_text(json.dumps(spec))
        run_cli('flow-init', '--spec', str(self.tmp / 'strict.json'), '--workspace', str(self.tmp / 'task2'), env={'VCO_KB_COMMAND': '/bin/false'})
        run_cli('project', 'class_b', '--variant', 'tail', '--spec', str(self.tmp / 'strict.json'), '--output', str(self.tmp / 'p2.json'))
        code, _, err = run_cli('flow-add', str(self.tmp / 'task2'), '--project', str(self.tmp / 'p2.json'), '--reason', 'x', env={'VCO_KB_COMMAND': '/bin/false'})
        self.assertEqual(code, 2)
        self.assertIn('strict enforcement', err)
        code, _, err = run_cli('flow-add', str(self.tmp / 'task2'), '--project', str(self.tmp / 'p2.json'), '--reason', 'x',
                               '--evidence', 'vco-card-symptom-pn-flicker', env={'VCO_KB_COMMAND': '/bin/false'})
        self.assertEqual(code, 0, err)


class KnowledgeTests(unittest.TestCase):
    def test_cards_valid(self):
        from vco_studio.knowledge import load_cards, validate_card
        cards = load_cards()
        self.assertGreaterEqual(len(cards), 15)
        for cid, c in cards.items():
            self.assertEqual(validate_card(c), [], cid)

    def test_symptom_matching_is_specific(self):
        from vco_studio.knowledge import find_cards
        ids = [c['id'] for c in find_cards(stage='tuning', symptoms=['PN_FLICKER'])]
        self.assertIn('vco-card-symptom-pn-flicker', ids)
        self.assertFalse(any(i.startswith('vco-card-pdk-') for i in ids))
        ids = [c['id'] for c in find_cards(stage='tuning', symptoms=['AMP_LOW'], pdk='demo_c')]
        self.assertIn('vco-card-procedure-device-domains', ids)


class GeometryTests(unittest.TestCase):
    def test_mim_minimum_enforced_and_legal_scaling(self):
        from vco_studio.catalog import default_project
        from vco_studio.autosize import _legal, _scale_param_legal
        p = default_project('hegazi', 'tail', 'demo_c')
        self.assertTrue(_legal(p))
        p['parameters']['bank_cap_scale'] = 0.40
        self.assertFalse(_legal(p))
        p['parameters']['bank_cap_scale'] = 1.0
        applied = _scale_param_legal(p, 'bank_cap_scale', 0.2)
        self.assertGreater(applied, 0.2)
        self.assertTrue(_legal(p))

    def test_width_beyond_finger_limit_uses_more_fingers(self):
        from vco_studio.catalog import default_project
        from vco_studio.generator import make_graph
        p = default_project('hegazi', 'tail', 'demo_a')
        p['parameters']['core_width_scale'] = 4
        g, _ = make_graph(p)
        mn1 = next(i for i in g['instances'] if i['name'] == 'MN1')
        self.assertLessEqual(mn1['parameters']['w'], 5e-6 * (1 + 1e-9))
        self.assertAlmostEqual(mn1['parameters']['w'] * mn1['parameters']['nf'] * mn1['parameters'].get('m', 1), 3e-6 * 6 * 4, delta=1e-9)


if __name__ == '__main__':
    unittest.main()


class NoiseAndSpecModeTests(unittest.TestCase):
    PSF = ROOT / 'tests/fixtures/stages_psf/noise.txt'

    @unittest.skipUnless(PSF.is_file(), 'Synthetic PSF fixture not available')
    def test_noise_contributors_match_synthetic_fixture(self):
        from vco_studio.noise import contributors
        c = contributors(self.PSF, 1e6)
        self.assertAlmostEqual(c['classes']['tail']['share'], 0.75, delta=0.01)
        self.assertGreater(c['classes']['tail']['flicker_share'], 0.79)

    def test_center_tr_spec(self):
        from vco_studio.spec import validate
        s, w = validate({'pdk': 'demo_b', 'center_hz': 20e9, 'tr_min_pct': 10, 'power_max_mw': 10})
        self.assertEqual(s['band_mode'], 'center_tr')
        self.assertEqual(s['frequency_band_hz'], [19e9, 21e9])
        s2, _ = validate(s)
        self.assertEqual(s2['band_mode'], 'center_tr')
        with self.assertRaisesRegex(ValueError, 'center_hz \\+ tr_min_pct'):
            validate({'pdk': 'demo_b', 'power_max_mw': 10})

    def test_probe_tb_strips_hb(self):
        from vco_studio.simulator import _probe_tb
        tb = 'simulator lang=spectre\nhb (OUTP OUTN) hb tstab=60n\nhbnoise (OUTP OUTN) hbnoise\nsave OUTP OUTN VDD_SRC:p\ndcOp dc\n'
        out = _probe_tb(tb, 30, 4)
        self.assertNotIn('hbnoise', out)
        self.assertIn('probe tran stop=30n', out)
        self.assertIn('save OUTP OUTN', out)


class GuessRecoveryTests(unittest.TestCase):
    def test_recover_guess_reruns_with_probed_frequency(self):
        from unittest import mock
        from vco_studio import flow
        tmp = Path(tempfile.mkdtemp(prefix='vco-recov-'))
        (tmp / 'spec.json').write_text(json.dumps({'pdk': 'demo_a', 'center_hz': 11e9, 'tr_min_pct': 10, 'power_max_mw': 5,
                                                   'budget': {'solver_processes': 6}}))
        run_cli('flow-init', '--spec', str(tmp / 'spec.json'), '--workspace', str(tmp / 'task'), env={'VCO_KB_COMMAND': '/bin/false'})
        run_cli('project', 'hegazi', '--variant', 'tail', '--spec', str(tmp / 'spec.json'), '--output', str(tmp / 'p.json'))
        run_cli('flow-add', str(tmp / 'task'), '--project', str(tmp / 'p.json'), '--reason', 'x', env={'VCO_KB_COMMAND': '/bin/false'})
        state = json.loads((tmp / 'task' / 'task.json').read_text())
        cand = state['candidates'][0]
        failed = {'status': 'failed', 'error': 'Solver exited 1', 'path': str(tmp / 'fake-run')}
        seen = {}

        def fake_run(dest, timeout_s=300):
            seen['guess'] = json.loads((Path(dest) / 'project.json').read_text())['parameters']['frequency_guess_ghz']
            out = Path(dest) / 'sim'
            out.mkdir()
            (out / 'result.json').write_text(json.dumps({'status': 'measured', 'metrics': {}}))
            return {'status': 'measured', 'path': str(out), 'metrics': {}}
        with mock.patch('vco_studio.simulator.is_guess_failure', return_value=True), \
             mock.patch('vco_studio.simulator.dc_power_mw', return_value=4.0), \
             mock.patch('vco_studio.simulator.probe_frequency', return_value={'status': 'measured', 'frequency_hz': 11.5e9, 'amplitude_v': 0.8}), \
             mock.patch('vco_studio.simulator.run', side_effect=fake_run):
            note = flow._recover_guess(tmp / 'task', state, cand, failed, 300)
        self.assertEqual(note['probe']['status'], 'measured')
        self.assertAlmostEqual(seen['guess'], 11.5)
        self.assertEqual(note['result']['status'], 'measured')
        self.assertTrue(any(e.get('action') == 'probe' for e in state['events']))

    def test_recover_guess_skips_when_current_too_low(self):
        from unittest import mock
        from vco_studio import flow
        state = {'spec': {'power_max_mw': 5}, 'candidates': [], 'events': [], 'budget': {}, 'created': '2026-11-10T00:00:00+00:00'}
        tmp = Path(tempfile.mkdtemp(prefix='vco-recov2-'))
        (tmp / 'cand').mkdir()
        from vco_studio.catalog import default_project
        (tmp / 'cand' / 'project.json').write_text(json.dumps(default_project('hegazi', 'tail', 'demo_a')))
        with mock.patch('vco_studio.simulator.is_guess_failure', return_value=True), \
             mock.patch('vco_studio.simulator.dc_power_mw', return_value=0.4):
            note = flow._recover_guess(tmp, state, {'id': 'c', 'directory': str(tmp / 'cand'), 'results': []}, {'status': 'failed', 'path': str(tmp)}, 300)
        self.assertIn('far below', note['action'])
