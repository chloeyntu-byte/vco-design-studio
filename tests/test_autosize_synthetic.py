"""Autosize control flow against a synthetic oscillator model (no Spectre): it must converge to a
passing design within budget, respect PDK legality, calibrate aux, and never crash."""
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vco_studio import autosize  # noqa: E402


def synthetic(project):
    p = project['parameters']
    regs = project['registers']
    bits = regs['main']['bits']
    code = regs['main']['value']
    frac = code / (2 ** bits - 1)
    c_fixed = 1.0
    c_bank = 3.0 * p['bank_cap_scale'] * (1 - 0.8 * frac)      # higher code -> less C -> higher f
    f = 30e9 / math.sqrt(p['L_scale'] * (c_fixed + c_bank))
    knob = p['vdc'] if 'tail' in project['variant'] or project['family'] in ('guo', 'class_b') else p['core_width_scale']
    power = 8.0 * max(knob - 0.3, 0.02) ** 1.5 if knob < 2 else 3.0 * knob
    aux_pen = 0.0
    if 'aux' in regs:
        aux_pen = 0.25 * abs(regs['aux']['value'] - (11 + 2 * frac))      # best aux depends on the main code
    pn = -110 - 10 * math.log10(max(power, 1e-3) / 3) + 20 * math.log10(f / 10e9) - 2 * math.log10(p['L_scale'] + 0.5) + aux_pen
    m = {'frequency_hz': f, 'power_mw': power, 'amplitude_diff_peak_v': 1.0}
    for off in (10000, 100000, 1000000, 10000000):
        pn_o = pn + 20 * math.log10(1e6 / off) + (10 if off == 10000 else 0)
        m['pn_%d_dbc_hz' % off] = pn_o
        m['fom_%d_db' % off] = -pn_o + 20 * math.log10(f / off) - 10 * math.log10(power)
    return {'status': 'measured', 'metrics': m, 'registers': regs, 'warnings': [], 'path': '/tmp/synthetic'}


class SyntheticAutosize(unittest.TestCase):
    def run_cell(self, family, variant, f0=10e9, p=5.0, budget=34):
        calls = []

        def fake_measure(self_ex, project, label):
            if self_ex.used >= self_ex.budget:
                raise autosize.BudgetExhausted('budget')
            if not autosize._legal(project):
                raise autosize.DesignInvalid('illegal geometry')
            self_ex.used += 1
            r = synthetic(project)
            r['_label'] = label
            calls.append(label)
            self_ex.history.append({'label': label})
            return r
        cell = {'pdk': 'demo_a', 'family': family, 'variant': variant, 'f0_hz': f0, 'p_cap_mw': p,
                'tr_min': 0.10, 'main_bits': 4, 'q_target': 15}
        with mock.patch.object(autosize.Executor, 'measure', fake_measure):
            rec = autosize.autosize(cell, tempfile.mkdtemp(prefix='synth-'), budget=budget, log=lambda *a: None)
        return rec, calls

    def test_hegazi_tail_passes_with_aux_map(self):
        rec, calls = self.run_cell('hegazi', 'tail')
        self.assertEqual(rec['status'], 'pass', rec.get('error') or rec.get('evaluation'))
        self.assertLessEqual(rec['sims_used'], 34)
        ev = rec['evaluation']
        self.assertTrue(ev['checks']['center'] and ev['checks']['tr'] and ev['checks']['power'])
        self.assertIn('aux_map', ev)
        self.assertTrue(any(c.startswith('aux') for c in calls))

    def test_voltage_biased_family_uses_width_knob(self):
        rec, _ = self.run_cell('du', 'autotracking', f0=20e9, p=10.0)
        self.assertEqual(rec['power_knob'], 'core_width_scale')
        self.assertIn(rec['status'], ('pass', 'constraint_fail'))

    def test_budget_is_respected(self):
        rec, calls = self.run_cell('murphy', 'nmos', budget=6)
        self.assertLessEqual(len(calls), 6)


if __name__ == '__main__':
    unittest.main()


class ReviewRegressions(unittest.TestCase):
    """Codex review T-0081: mixed parameters, lost best design, center_tr in stages."""

    def test_realize_returns_the_measured_project(self):
        cell = {'pdk': 'demo_a', 'family': 'hegazi', 'variant': 'tail', 'f0_hz': 10e9, 'p_cap_mw': 5.0, 'tr_min': 0.1, 'main_bits': 4}

        class Ex:
            used, budget = 0, 99
            def measure(self, project, label):
                self.used += 1
                r = synthetic(project)
                r['metrics']['frequency_hz'] *= 1.2   # never converges -> loop exhausts max_iter
                r['_vdc'] = project['parameters']['vdc']
                return r
        p = autosize._project(cell)
        knob = autosize.PowerKnob(cell, p)
        r, proj = autosize._realize(Ex(), p, cell, knob, max_iter=3)
        self.assertEqual(r['_vdc'], proj['parameters']['vdc'])

    def test_budget_exhaustion_keeps_best(self):
        orig = autosize._aux_calibrate

        def boom(*a, **k):
            raise autosize.BudgetExhausted('budget')
        with mock.patch.object(autosize, '_aux_calibrate', boom):
            rec, _ = SyntheticAutosize.run_cell(self, 'hegazi', 'tail')
        self.assertTrue(rec.get('budget_exhausted'))
        self.assertIsNotNone(rec.get('design'))
        self.assertIn(rec['status'], ('pass', 'constraint_fail'))

    def test_stages_center_tr(self):
        from vco_studio.stages import band_ok
        spec = {'band_mode': 'center_tr', 'center_hz': 10e9, 'tr_min_pct': 20, 'frequency_band_hz': [9e9, 11e9]}
        self.assertTrue(band_ok([9.09e9, 11.11e9], spec))
        self.assertFalse(band_ok([9.09e9, 11.11e9], {'frequency_band_hz': [9e9, 11e9]}))


class GainVariants(unittest.TestCase):
    """docs/METHODS.md: gain measured by loopgain steers the variants; excess gain is explored, not forbidden."""

    def test_variant_choice(self):
        class K:
            kind = 'vdc'
        names = [v[0] for v in autosize._gain_variants(K(), {'gm_rp': 8.0, 'valid': True})]
        self.assertEqual(names[0], 'gain_nominal_width')
        self.assertIn('power_low', names)
        names = [v[0] for v in autosize._gain_variants(K(), {'gm_rp': 3.5, 'valid': True})]
        self.assertEqual(names[:2], ['gain_excess_L', 'gain_excess_width'])
        self.assertEqual([v[0] for v in autosize._gain_variants(K(), None)], ['L_up', 'L_down'])

    def test_gain_recorded_per_variant(self):
        seen = []

        def fake_gain(ex, r, label):
            seen.append(label)
            return {'gm_rp': 8.0 if label == 'base' else 4.2, 'valid': True, 'class': 'x'}
        with mock.patch.object(autosize, '_gain', fake_gain):
            rec, _ = SyntheticAutosize.run_cell(self, 'hegazi', 'tail', budget=60)
        self.assertEqual(seen[0], 'base')
        self.assertTrue(any(v['variant'].startswith('gain_nominal') for v in rec['variants']))
        self.assertTrue(all('gain' in v for v in rec['variants']))
        self.assertIn('swing', rec['evaluation'])


class Plan(unittest.TestCase):
    def test_plan_follows_flow(self):
        from vco_studio.sizing import plan, gain_class
        spec = {'frequency_band_hz': [9.5e9, 10.5e9], 'power_max_mw': 5.0, 'vdd_v': 1.0, 'main_bits': 4}
        r = plan(spec, q=20, gain=4, gm_over_id=10)
        self.assertAlmostEqual(r['gm_single_s'], 0.025)
        self.assertAlmostEqual(r['rp_ohm'] * r['gm_single_s'], 4.0)
        self.assertAlmostEqual(r['l_differential_h'], r['rp_ohm'] / (20 * 2 * math.pi * r['f_center_hz']))
        self.assertEqual(gain_class(8), 'excess')
        self.assertEqual(gain_class(4), 'nominal')


class TableRows(unittest.TestCase):
    def test_rows_have_four_offsets_and_gain(self):
        from vco_studio.select import table_rows, table_csv, table_markdown_full
        rec, _ = SyntheticAutosize.run_cell(self, 'hegazi', 'tail', budget=40)
        rec['key'] = 'k'
        rows = table_rows([rec])
        r = rows[0]
        for o in ('10000', '100000', '1000000', '10000000'):
            self.assertIsNotNone(r['fom_worst_%s' % o])
        self.assertEqual(sorted(r['codes']), [0, 8, 15])
        self.assertIn('fom_worst_10000', table_csv(rows).splitlines()[0])
        self.assertIn('hegazi', table_markdown_full(rows))


class NoStartup(unittest.TestCase):
    def test_no_oscillation_raises_power_then_L(self):
        cell = {'pdk': 'demo_a', 'family': 'hegazi', 'variant': 'tail', 'f0_hz': 10e9, 'p_cap_mw': 5.0, 'tr_min': 0.1, 'main_bits': 4}
        seen = []

        class Ex:
            used, budget = 0, 99
            def measure(self, project, label):
                self.used += 1
                seen.append((project['parameters']['vdc'], project['parameters']['L_scale']))
                if len(seen) <= 3:
                    return {'status': 'failed', 'failure_class': 'no_oscillation', 'error': 'No oscillation: ...',
                            'dc_power_mw': 3.0 if len(seen) == 1 else 4.6}
                return synthetic(project)
        p = autosize._project(cell)
        p['parameters']['vdc'] = 0.9
        autosize._realize(Ex(), p, cell, autosize.PowerKnob(cell, p), max_iter=3)
        self.assertGreater(seen[1][0], seen[0][0])          # headroom: power knob up
        self.assertEqual(seen[2][0], seen[1][0])            # at the cap: power unchanged (core wider instead)
        self.assertGreater(p['parameters']['core_width_scale'], 1.0)
