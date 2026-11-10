"""Automatic sizing of one topology for one (PDK, f0, power cap, TR) cell, and grid campaigns.

This is the engine behind the measured lookup table and `launch seed`: every number it reports
comes from an actual bounded Spectre HB/PN run of a generated design (no extrapolation).

Algorithm (deterministic, budgeted):
  0. project from the template + PDK binding + spec conditions; optional warm start (seed params);
     magnetic loss scaled so that the max magnetic-mode Q at f0 equals q_target (rule: Q in (0,30]).
  A. realize at the mid code: scale L*C by (f/f0)^2 (split between L and the banks), and move the
     power knob (tail/gate bias vdc, or core width for voltage-biased cores) by a secant step until
     |f/f0-1| < 1 % and 0.85*Pcap <= P <= Pcap.
  B. measure the band endpoints (main = 0 and 2^B-1): TR, center, worst PN/FoM, max power;
     widen the bank if TR < tr_min, re-center if the band center is off by > 2 %.
  D. families with an aux register: choose the aux code (at the mid main code) that minimizes PN@1MHz.
  G. startup gain (docs/METHODS.md): `launch loopgain` on the mid code -> gm*Rp at the drain port. Gain variants
     guided by it (the design flow of vco_studio/sizing.py): gain above 6 -> try the nominal gain (L down, smaller
     core for bias-voltage families, lower power); gain at or below 6 -> explore excess gain (L up, wider core).
     Excess gain is allowed: every variant is kept or dropped only by the measured worst-code FoM@1MHz, and its
     gm*Rp is recorded so the table shows whether excess helped.
  Swing is reported against the structure limit (CMOS ~VDD, NMOS/PMOS ~2*VDD; docs/RULES.md).
Every simulation is wrapped by the site `sim_lock_command`.
"""
from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import subprocess
import time
import traceback
from pathlib import Path

from .common import ROOT, read_json, write_json

VDC_KNOB = {('hegazi', 'tail'), ('hegazi', 'top'), ('class_b', 'tail'), ('guo', 'wideband')}
PMOS_BIAS = {('hegazi', 'top')}
PRIMARY = [('hegazi', 'tail'), ('murphy', 'nmos'), ('lim', 'inverse_f'), ('guo', 'wideband'),
           ('du', 'autotracking'), ('class_b', 'tail')]


class BudgetExhausted(RuntimeError):
    pass


class DesignInvalid(ValueError):
    pass


def _launch():
    return str(ROOT / 'launch')


class Executor:
    """Generate + simulate one design point; counts solver runs against a budget."""

    def __init__(self, workdir, budget, timeout_s=300, lock_command=None, log=None):
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.budget = int(budget)
        self.used = 0
        self.timeout_s = timeout_s
        from . import site
        self.lock_command = lock_command if lock_command is not None else site.load().get('sim_lock_command')
        self.log = log or (lambda *a: None)
        self.history = []
        self.lg_used = 0

    def measure(self, project, label):
        if self.used >= self.budget:
            raise BudgetExhausted('cell budget of %d solver runs exhausted' % self.budget)
        from .generator import generate
        n = len(list(self.workdir.glob('d[0-9][0-9][0-9]*'))) + 1
        dest = self.workdir / ('d%03d-%s' % (n, label))
        try:
            generate(project, dest)
        except (ValueError, FileNotFoundError) as exc:
            raise DesignInvalid(str(exc))
        self.used += 1
        out = dest / 'sim'
        if self.lock_command:
            from .runner import run_bounded
            cmd = list(self.lock_command) + [_launch(), 'simulate', str(dest), '--output', str(out), '--timeout', str(self.timeout_s)]
            os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
            # bounded: an interrupted campaign terminates the lock holder, which terminates simulate -> spectre
            run_bounded(cmd, dest, self.timeout_s + 7200, dest / 'lock.log')
            try:
                result = read_json(out / 'result.json')
            except (OSError, ValueError):
                result = {'status': 'failed', 'error': 'no result.json (lock or launch failure)', 'failure_class': 'infrastructure', 'path': str(out)}
        else:
            from .simulator import run
            result = run(dest, out, self.timeout_s)
        if result.get('status') != 'measured':
            try:
                from .simulator import dc_power_mw, is_guess_failure
                result['dc_power_mw'] = dc_power_mw(out, project)
                result['guess_failure'] = is_guess_failure(result)
            except Exception:
                pass
        _compact(out)
        result['_generated'] = str(dest)
        result['_label'] = label
        self.history.append(_summ(result, project))
        self.log('%s %s' % (label, _line(result)))
        return result


def _compact(run_dir):
    """Campaign disk hygiene: drop the AHDL cache, gzip the operating-point dump (neither is hashed evidence)."""
    import gzip
    import shutil
    shutil.rmtree(Path(run_dir) / 'tb.ahdlSimDB', ignore_errors=True)
    op = Path(run_dir) / 'psf' / 'dcOpInfo.info'
    if op.is_file():
        with open(op, 'rb') as fi, gzip.open(str(op) + '.gz', 'wb', 6) as fo:
            shutil.copyfileobj(fi, fo)
        op.unlink()


def _probe(ex, generated_dir, label):
    """Short transient (counts as one solver run) to find the free-running frequency."""
    if ex.used >= ex.budget:
        raise BudgetExhausted('cell budget exhausted')
    from .simulator import probe_frequency
    ex.used += 1
    out = Path(generated_dir) / 'probe'
    if ex.lock_command:
        import json as _json
        from .runner import run_bounded
        code = ('import sys,json;sys.path.insert(0,%r);from vco_studio.simulator import probe_frequency;'
                'r=probe_frequency(%r,%r);print(json.dumps({k:r.get(k) for k in (\'status\',\'frequency_hz\',\'amplitude_v\',\'error\')}))') % (str(ROOT), str(generated_dir), str(out))
        from . import site
        run_bounded(list(ex.lock_command) + [site.python(), '-c', code], generated_dir, 600, Path(generated_dir) / 'probe-lock.log')
        try:
            r = read_json(out / 'probe.json')
        except (OSError, ValueError):
            r = {'status': 'failed', 'error': 'probe produced no result'}
    else:
        r = probe_frequency(generated_dir, out)
    ex.log('%s probe %s' % (label, ('f=%.4fG amp=%.3f' % (r['frequency_hz'] / 1e9, r['amplitude_v'])) if r.get('status') == 'measured' else r.get('error')))
    return r


def _summ(r, project):
    m = r.get('metrics', {})
    return {'label': r.get('_label'), 'status': r.get('status'), 'error': r.get('error'),
            'controls': {k: v['value'] for k, v in project['registers'].items()},
            'params': {k: project['parameters'][k] for k in ('L_scale', 'bank_cap_scale', 'aux_cap_scale', 'core_width_scale', 'loss_scale', 'vdc') if k in project['parameters']},
            'f_hz': m.get('frequency_hz'), 'p_mw': m.get('power_mw'), 'pn_1m': m.get('pn_1000000_dbc_hz'),
            'fom_1m': m.get('fom_1000000_db'), 'result': str(Path(r.get('path', '')) / 'result.json') if r.get('path') else None}


def _line(r):
    if r.get('status') != 'measured':
        return 'FAILED %s' % (r.get('error') or '')[:120]
    m = r['metrics']
    return 'f=%.4fG P=%.3fmW PN1M=%.2f FoM=%.2f amp=%.3f' % (m['frequency_hz'] / 1e9, m['power_mw'], m['pn_1000000_dbc_hz'], m['fom_1000000_db'], m['amplitude_diff_peak_v'])


def cell_spec(cell):
    f0 = cell['f0_hz']
    tr = cell.get('tr_min', 0.30)
    from .spec import validate
    spec, _ = validate({'pdk': cell['pdk'], 'frequency_band_hz': [f0 * (1 - tr / 2), f0 * (1 + tr / 2)],
                        'power_max_mw': cell['p_cap_mw'], 'temp_c': 27, 'main_bits': cell.get('main_bits', 4)})
    return spec


def _project(cell, params=None, regs=None):
    from .catalog import default_project
    p = default_project(cell['family'], cell['variant'], cell['pdk'], spec=cell_spec(cell))
    if params:
        p['parameters'].update(params)
    if regs:
        for k, v in regs.items():
            if k in p['registers']:
                p['registers'][k].update(v)
    p['parameters']['frequency_guess_ghz'] = round(cell['f0_hz'] / 1e9, 6)
    p['simulation'] = dict(p.get('simulation', {}), timeout_s=300)
    return p


def _set_code(project, main=None, aux=None, guess_hz=None):
    p = copy.deepcopy(project)
    if main is not None:
        p['registers']['main']['value'] = int(main)
    if aux is not None and 'aux' in p['registers']:
        p['registers']['aux']['value'] = int(aux)
    if guess_hz:
        p['parameters']['frequency_guess_ghz'] = round(guess_hz / 1e9, 6)
    return p


def _apply_q(project, cell):
    """Scale loss so the max magnetic-mode Q at f0 equals q_target."""
    from .generator import make_graph
    from .magnetics import loss_scale_for_q
    graph, _ = make_graph(copy.deepcopy(project))
    from .magnetics import q_limit
    target = min(float(cell.get('q_target', 20)), q_limit(graph)[0])
    loss, q_before = loss_scale_for_q(graph, cell['f0_hz'], project['parameters'].get('loss_scale', 1), target)
    project['parameters']['loss_scale'] = round(loss, 6)
    graph, _ = make_graph(copy.deepcopy(project))
    from .magnetics import magnetic_q
    q_after, detail = magnetic_q(graph, cell['f0_hz'])
    return q_before, q_after


def _lc_scale(project, ratio, split):
    """Multiply the L*C product by ratio: L by ratio**split, banks by ratio**(1-split).
    If the bank change would violate PDK geometry limits, the remainder goes to L."""
    p = project['parameters']
    c_factor = ratio ** (1 - split)
    applied = _scale_param_legal(project, 'bank_cap_scale', c_factor, also=('aux_cap_scale',)) if abs(c_factor - 1) > 1e-9 else 1.0
    p['L_scale'] = round(float(p['L_scale']) * ratio / applied, 6)


class PowerKnob:
    def __init__(self, cell, project):
        key = (cell['family'], cell['variant'])
        self.kind = 'vdc' if key in VDC_KNOB else 'core_width_scale'
        self.direction = -1 if key in PMOS_BIAS else 1
        self.vdd = float(project['parameters']['vdd'])
        self.points = []

    def get(self, project):
        return float(project['parameters'][self.kind])

    def record(self, value, power):
        self.points = [pt for pt in self.points if abs(pt[0] - value) > 1e-9] + [(value, power)]

    def step(self, project, power, target, failed=False):
        """Return the next knob value; secant on (knob, P) when two points exist."""
        x = self.get(project)
        if failed:
            new = x + 0.08 * self.direction if self.kind == 'vdc' else x * 1.4
        elif len(self.points) >= 2:
            (x1, p1), (x2, p2) = sorted(self.points, key=lambda t: abs(t[1] - target))[:2]
            if abs(p2 - p1) < 1e-6:
                new = x * (target / power) if self.kind != 'vdc' else x + 0.05 * self.direction * (1 if target > power else -1)
            else:
                new = x1 + (target - p1) * (x2 - x1) / (p2 - p1)
        else:
            if self.kind == 'vdc':
                new = x + self.direction * max(min(0.25 * (target / power - 1), 0.12), -0.12)
            else:
                new = x * (target / power) ** 0.9
        if self.kind == 'vdc':
            new = min(max(new, x - 0.15, 0.2), x + 0.15, self.vdd)
        else:
            new = min(max(new, x * 0.4, 0.05), x * 2.5)
        project['parameters'][self.kind] = round(new, 6)
        return new


def _ok(r):
    return r is not None and r.get('status') == 'measured'


def _realize(ex, project, cell, knob, split=0.5, max_iter=5, label='mid', p_frac=1.0):
    """Center f0 and hit the power target at the mid code. Returns (result, project)."""
    f0 = cell['f0_hz']
    cap = cell['p_cap_mw'] * p_frac
    target = 0.92 * cap
    bits = project['registers']['main']['bits']
    mid = 2 ** (bits - 1)
    last = None
    last_project = None
    fails = 0
    nosc = 0
    widen = 0
    for it in range(max_iter + 3):
        if it >= max_iter and not nosc:
            break
        p = _set_code(project, main=mid, guess_hz=(last['metrics']['frequency_hz'] if _ok(last) else f0))
        measured_project = copy.deepcopy(project)
        r = ex.measure(p, '%s-i%d' % (label, it))
        if not _ok(r):
            fails += 1
            err = (r.get('error') or '').lower()
            no_start = r.get('failure_class') in ('no_oscillation', 'no_growing_mode')
            nosc += 1 if no_start else 0
            if fails > (6 if no_start else 3):
                return r, project
            pdc = r.get('dc_power_mw')
            low_current = pdc is not None and pdc < 0.6 * target
            if low_current or no_start or 'oscillation' in err:
                # raise the gain without exceeding the power cap (docs/METHODS.md §1/§3):
                # 1) power while there is headroom, 2) wider core at the same current (bias-voltage families),
                # 3) L x1.5 at the same f0 (gain -> Rp -> L; costs tuning range)
                before = knob.get(project)
                headroom = pdc is None or pdc < 0.85 * cap
                if low_current:
                    knob.record(before, pdc)
                    knob.step(project, pdc, target)
                elif headroom:
                    knob.step(project, None, target, failed=True)
                moved = abs(knob.get(project) - before) > 1e-6
                if not moved and knob.kind == 'vdc' and widen < 2:
                    widen += 1
                    _scale_param_legal(project, 'core_width_scale', 1.4)
                elif not moved:
                    applied = _scale_param_legal(project, 'bank_cap_scale', 1 / 1.5, also=('aux_cap_scale',))
                    project['parameters']['L_scale'] = round(float(project['parameters']['L_scale']) / applied, 6)
            elif r.get('guess_failure') and r.get('_generated'):
                pr = _probe(ex, r['_generated'], '%s-i%d' % (label, it))
                if pr.get('status') == 'measured':
                    fp = pr['frequency_hz']
                    project['parameters']['frequency_guess_ghz'] = round(fp / 1e9, 6)
                    if abs(fp / f0 - 1) > 0.01:
                        _lc_scale(project, (fp / f0) ** 2, split)
                        project['parameters']['frequency_guess_ghz'] = round(f0 / 1e9, 6)
                else:
                    knob.step(project, None, target, failed=True)
            else:
                project['parameters']['frequency_guess_ghz'] = round(f0 / 1e9 * (0.85 if fails == 1 else 1.15), 6)
            last = None
            continue
        last, last_project = r, measured_project
        m = r['metrics']
        f, pw = m['frequency_hz'], m['power_mw']
        knob.record(knob.get(project), pw)
        f_ok = abs(f / f0 - 1) < 0.01
        p_ok = 0.85 * cap <= pw <= cap
        if f_ok and p_ok:
            return r, project
        if not p_ok:
            before = knob.get(project)
            knob.step(project, pw, target)
            if abs(knob.get(project) - before) < 1e-6:
                # knob at its limit (e.g. vdc = VDD): power cannot move further; do not repeat the same design
                project['parameters'][knob.kind] = before
                if f_ok:
                    return r, measured_project
                p_ok = True
        if not f_ok:
            _lc_scale(project, (f / f0) ** 2, split)
    if last is not None:
        # never pair a result with parameters changed after it was measured (review T-0081 P1)
        return last, last_project
    return r, project


def _endpoints(ex, project, cell, aux=None, mid_result=None):
    bits = project['registers']['main']['bits']
    top = 2 ** bits - 1
    f0 = cell['f0_hz']
    tr = cell.get('tr_min', 0.30)
    guess_lo = f0 * (1 - tr * 0.6)
    guess_hi = f0 * (1 + tr * 0.6)
    out = {}
    for code, guess in ((0, guess_lo), (top, guess_hi)):
        r = ex.measure(_set_code(project, main=code, aux=aux, guess_hz=guess), 'code%d' % code)
        if not _ok(r):
            alt = guess * (0.92 if code == 0 else 1.08)
            r = ex.measure(_set_code(project, main=code, aux=aux, guess_hz=alt), 'code%d-retry' % code)
        out[code] = r
    if mid_result is not None:
        out[2 ** (bits - 1)] = mid_result
    return out


def _evaluate(codes, cell):
    ids = {r.get('physical_identity') for r in codes.values() if _ok(r)}
    if len(ids) > 1:
        return {'status': 'failed', 'reason': 'codes come from different physical designs', 'rows': []}
    rows = []
    for code, r in sorted(codes.items()):
        if not _ok(r):
            return {'status': 'failed', 'reason': 'code %d: %s' % (code, (r.get('error') or '')[:160]), 'rows': rows}
        m = r['metrics']
        rows.append({'code': code, 'controls': {k: v.get('value') for k, v in (r.get('registers') or {}).items()},
                     'f_hz': m['frequency_hz'], 'p_mw': m['power_mw'], 'amp_v': m['amplitude_diff_peak_v'],
                     'pn': {str(o): m['pn_%d_dbc_hz' % o] for o in (10000, 100000, 1000000, 10000000)},
                     'fom': {str(o): m['fom_%d_db' % o] for o in (10000, 100000, 1000000, 10000000)},
                     'warnings': r.get('warnings', []), 'result': str(Path(r['path']) / 'result.json')})
    fs = [x['f_hz'] for x in rows]
    lo, hi = min(fs), max(fs)
    center = (lo + hi) / 2
    tr = (hi - lo) / center
    pmax = max(x['p_mw'] for x in rows)
    worst_pn = max(x['pn']['1000000'] for x in rows)
    worst_fom = min(x['fom']['1000000'] for x in rows)
    checks = {'center': abs(center / cell['f0_hz'] - 1) <= 0.02, 'tr': tr >= cell.get('tr_min', 0.30) * 0.999,
              'power': pmax <= cell['p_cap_mw'] * 1.0001}
    return {'status': 'pass' if all(checks.values()) else 'constraint_fail', 'checks': checks, 'rows': rows,
            'center_hz': center, 'f_lo_hz': lo, 'f_hi_hz': hi, 'tr_pct': tr * 100, 'p_max_mw': pmax,
            'pn_1m_worst': worst_pn, 'fom_1m_worst': worst_fom,
            'pn_worst': {o: max(x['pn'][o] for x in rows) for o in ('10000', '100000', '1000000', '10000000')},
            'fom_worst': {o: min(x['fom'][o] for x in rows) for o in ('10000', '100000', '1000000', '10000000')},
            'codes_evaluated': [x['code'] for x in rows],
            'steady_warnings': sum(1 for x in rows if any('steady' in w.lower() for w in x['warnings']))}


def _better(a, b):
    """Is evaluation a better than b? pass beats constraint_fail beats failed; then worst FoM."""
    rank = {'pass': 2, 'constraint_fail': 1, 'failed': 0}
    if b is None:
        return True
    ka = (rank.get(a['status'], 0), a.get('fom_1m_worst', -1e9))
    kb = (rank.get(b['status'], 0), b.get('fom_1m_worst', -1e9))
    return ka > kb


def _legal(project):
    """Cheap legality check (graph + declared PDK limits), no simulation."""
    from .generator import make_graph, validate_graph
    try:
        g, pp = make_graph(copy.deepcopy(project))
        return validate_graph(g, pp['binding'])['valid']
    except (ValueError, KeyError):
        return False


def _scale_param_legal(project, key, factor, also=()):
    """Multiply parameters[key] (and keys in `also`) by factor; if that is illegal, use the factor
    closest to the request that is still legal (bisection toward 1). Returns the factor applied."""
    base = {k: float(project['parameters'][k]) for k in (key,) + tuple(also)}
    def apply(f):
        for k, v in base.items():
            project['parameters'][k] = round(v * f, 6)
    apply(factor)
    if _legal(project):
        return factor
    lo, hi = factor, 1.0          # hi is assumed legal (current design)
    for _ in range(12):
        mid = (lo * hi) ** 0.5 if lo > 0 and hi > 0 else (lo + hi) / 2
        apply(mid)
        if _legal(project):
            hi = mid
        else:
            lo = mid
    apply(hi)
    return hi


def _errors(project):
    from .generator import make_graph, validate_graph
    try:
        g, pp = make_graph(copy.deepcopy(project))
        return validate_graph(g, pp['binding'])['errors']
    except (ValueError, KeyError) as exc:
        return [str(exc)]


_BANK_KEYS = {'bank_cap_scale': 'CT_R3U_main', 'aux_cap_scale': 'CT_R3U_aux'}


def _legalize(project):
    """Move an illegal starting design (e.g. a warm seed rescaled to another f0) back inside the PDK limits.
    Each bank scale is raised to the smallest value that clears *its own* subcircuit errors; the main bank's
    change is compensated in L (same L*C). Device-width limits are handled by the generator (nf/m)."""
    notes = []
    for key, sub in _BANK_KEYS.items():
        if key not in project['parameters']:
            continue
        def bad(value):
            trial = dict(project, parameters=dict(project['parameters'], **{key: value}))
            return any(e.startswith(sub + '/') for e in _errors(trial))
        before = float(project['parameters'][key])
        if not bad(before):
            continue
        hi = max(before, 1.0)
        while bad(hi) and hi < 16:
            hi *= 1.5
        if bad(hi):
            continue
        lo = before
        for _ in range(14):
            mid = (lo * hi) ** 0.5
            if bad(mid):
                lo = mid
            else:
                hi = mid
        project['parameters'][key] = round(hi, 6)
        if key == 'bank_cap_scale':
            project['parameters']['L_scale'] = round(float(project['parameters']['L_scale']) * before / hi, 6)
        notes.append('%s %.4g -> %.4g (PDK minimum geometry)' % (key, before, hi))
    return notes


def _shape(ex, project, cell, knob, split, label, p_frac=1.0, tr_shrink=True):
    """realize -> endpoints -> fix TR (widen or shrink the bank) -> re-center on the band center.
    Keeps the best (ev, project, codes) seen, so a failed adjustment never replaces a working design."""
    tr_min = cell.get('tr_min', 0.30)
    mid, project = _realize(ex, project, cell, knob, split=split, label=label, p_frac=p_frac)
    if not _ok(mid):
        return {'status': 'failed', 'reason': 'mid code: ' + (mid.get('error') or '')[:200], 'rows': []}, project, {}
    codes = _endpoints(ex, project, cell, mid_result=mid)
    best = (_evaluate(codes, cell), copy.deepcopy(project), codes)
    for step in range(2):
        ev = best[0]
        if ev['status'] == 'failed' or ex.budget - ex.used < 5:
            break
        tr = ev['tr_pct'] / 100
        if tr < tr_min:
            factor = min(3.0, (tr_min * 1.2 / max(tr, 1e-3)))
        elif tr > 1.6 * tr_min and tr_shrink:
            factor = max(0.25, tr_min * 1.25 / tr)
        else:
            break
        trial = copy.deepcopy(best[1])
        applied = _scale_param_legal(trial, 'bank_cap_scale', factor)
        if abs(applied - 1.0) < 0.02:
            regs = trial['registers']['main']
            if factor < 1 and regs['bits'] > 3 and tr > 2.0 * tr_min:
                # bank at the PDK minimum and TR far wider than needed: one bit fewer halves the bank
                regs['bits'] -= 1
                regs['value'] = 2 ** (regs['bits'] - 1)
                ex.log('%s: bank at PDK minimum (TR %.1f %%), main bits -> %d' % (label, tr * 100, regs['bits']))
            else:
                break   # keep the current TR
        if not _legal(trial):
            break
        mid, trial = _realize(ex, trial, cell, knob, split=1.0, label='%s-tr%d' % (label, step), p_frac=p_frac)
        if not _ok(mid):
            break
        tcodes = _endpoints(ex, trial, cell, mid_result=mid)
        tev = _evaluate(tcodes, cell)
        if _better(tev, best[0]) or (tev['status'] != 'failed' and tev['checks']['tr'] and not best[0]['checks']['tr']):
            best = (tev, copy.deepcopy(trial), tcodes)
        else:
            break
    ev, project, codes = best
    if ev['status'] != 'failed' and not ev['checks']['center'] and ex.budget - ex.used >= 3:
        trial = copy.deepcopy(project)
        _lc_scale(trial, (ev['center_hz'] / cell['f0_hz']) ** 2, split)
        mid = ex.measure(_set_code(trial, main=2 ** (trial['registers']['main']['bits'] - 1)), label + '-recenter')
        if _ok(mid):
            tcodes = _endpoints(ex, trial, cell, mid_result=mid)
            tev = _evaluate(tcodes, cell)
            if _better(tev, ev):
                ev, project, codes = tev, trial, tcodes
    return ev, project, codes


def _aux_calibrate(ex, project, cell, ev, codes, quick=False, prev_aux=None):
    """Choose the aux code per evaluated main code (mid search + refine, endpoints +-3).
    Returns (ev, project, codes, info); keeps the input when nothing improves."""
    regs = project['registers']
    amax = 2 ** regs['aux']['bits'] - 1
    mbits = regs['main']['bits']
    mid, top = 2 ** (mbits - 1), 2 ** mbits - 1
    cap = cell['p_cap_mw']
    a0 = regs['aux']['value']
    mid_res = codes.get(mid)
    if not _ok(mid_res):
        return ev, project, codes, {'skipped': 'mid code not measured'}
    tried = {a0: mid_res}
    def pn(r):
        return r['metrics']['pn_1000000_dbc_hz'] if _ok(r) and r['metrics']['power_mw'] <= cap * 1.0001 else 1e9
    def try_mid(a):
        if a in tried or not 0 <= a <= amax or ex.budget - ex.used < 3:
            return
        tried[a] = ex.measure(_set_code(project, main=mid, aux=a, guess_hz=mid_res['metrics']['frequency_hz']), 'aux%d-m%d' % (a, mid))
    first = [prev_aux] if (quick and prev_aux is not None) else [round(amax * x) for x in (0.15, 0.5, 0.85)]
    for a in first:
        try_mid(a)
    best_a = min(tried, key=lambda a: pn(tried[a]))
    for a in (best_a - 2, best_a + 2):
        try_mid(a)
    best_a = min(tried, key=lambda a: pn(tried[a]))
    new_codes = {mid: tried[best_a]}
    for code in (0, top):
        base = codes.get(code)
        cands = {}
        if best_a == a0 and _ok(base):
            cands[a0] = base
        elif ex.budget - ex.used >= 2:
            f_guess = base['metrics']['frequency_hz'] if _ok(base) else None
            cands[best_a] = ex.measure(_set_code(project, main=code, aux=best_a, guess_hz=f_guess), 'aux%d-m%d' % (best_a, code))
        if not quick:
            for a in (best_a - 3, best_a + 3):
                if 0 <= a <= amax and a not in cands and ex.budget - ex.used >= 3:
                    f_guess = base['metrics']['frequency_hz'] if _ok(base) else None
                    cands[a] = ex.measure(_set_code(project, main=code, aux=a, guess_hz=f_guess), 'aux%d-m%d' % (a, code))
        if not cands:
            return ev, project, codes, {'skipped': 'budget'}
        new_codes[code] = cands[min(cands, key=lambda a: pn(cands[a]))]
    ev2 = _evaluate(new_codes, cell)
    info = {'mid_search': {str(a): (round(r['metrics']['pn_1000000_dbc_hz'], 2) if _ok(r) else None) for a, r in sorted(tried.items())},
            'aux_map': {str(c): (r.get('registers') or {}).get('aux', {}).get('value') for c, r in new_codes.items()}}
    ev2['aux_map'] = info['aux_map']
    if _better(ev2, ev):
        p2 = copy.deepcopy(project)
        p2['registers']['aux']['value'] = best_a
        return ev2, p2, new_codes, info
    return ev, project, codes, info


def _gain(ex, r, label):
    """Startup gain of a measured design (launch loopgain, docs/METHODS.md). Small-signal runs, not counted in the
    HB budget (ex.lg_used counts them). Returns None when there is nothing to measure, a dict with 'error' on failure."""
    if not _ok(r) or not r.get('_generated'):
        return None
    from .sizing import gain_class
    gen = Path(r['_generated'])
    out = gen / 'loopgain'
    if not (out / 'loopgain.json').exists():
        cmd = [_launch(), 'loopgain', str(gen), '--output', str(out), '--f-ghz', '%.6f' % (r['metrics']['frequency_hz'] / 1e9)]
        if ex.lock_command:
            cmd = list(ex.lock_command) + cmd
        from .runner import run_bounded
        run_bounded(cmd, gen, 3600, gen / 'loopgain-lock.log')
        ex.lg_used += 1
    try:
        lg = read_json(out / 'loopgain.json')
    except (OSError, ValueError):
        ex.log('%s gain: loopgain failed (see %s)' % (label, gen / 'loopgain-lock.log'))
        return {'error': 'loopgain failed', 'log': str(gen / 'loopgain-lock.log')}
    g = {'gm_rp': lg.get('gm_Rp_ext'), 'gm_rp_op': lg.get('gm_Rp_op'), 'margin': lg.get('margin'), 'rp_ohm': lg.get('Rp_diff_ohm'),
         'valid': lg.get('valid'), 'port': lg.get('port'), 'merged': [(m['node'], m['to']) for m in lg.get('merged', [])],
         'class': gain_class(lg.get('gm_Rp_ext')), 'path': str(out / 'loopgain.json')}
    ex.log('%s gain: gm*Rp %.2f (op %.2f) margin %.2f Rp %.0f ohm [%s]' % (label, g['gm_rp'] or -1, g['gm_rp_op'] or -1, g['margin'] or -1, g['rp_ohm'] or -1, g['class']))
    return g


def design_summary(generated_dir):
    """Device-level summary of a generated design for the lookup table (reuse as a reference next time)."""
    g = read_json(Path(generated_dir) / 'graph.json')
    proj = read_json(Path(generated_dir) / 'project.json')
    by = {i['name']: i for i in g['instances']}
    devs, bias, inds, caps, ks = [], [], [], [], []
    for i in g['instances']:
        q = i['parameters']
        if i['role'] in ('nmos', 'pmos'):
            d = {'name': i['name'], 'type': i['role'], 'w_finger_um': round(q.get('w', 0) * 1e6, 4), 'l_um': round(q.get('l', 0) * 1e6, 4),
                 'nf': int(q.get('nf', 1)), 'm': int(q.get('m', 1)), 'w_total_um': round(q.get('w', 0) * q.get('nf', 1) * q.get('m', 1) * 1e6, 3)}
            (bias if i['name'].startswith('MB') else devs).append(d)
        elif i['role'] == 'ind':
            inds.append({'name': i['name'], 'l_nh': round(q['l'] * 1e9, 5), 'rs_ohm': round(by.get('RS' + i['name'], {}).get('parameters', {}).get('r', 0), 4)})
        elif i['role'] == 'cap':
            caps.append({'name': i['name'], 'c_ff': round(q['c'] * 1e15, 3)})
        elif i['role'] == 'mutual':
            ks.append({'pair': [q['ind1'], q['ind2']], 'k': q['coupling']})
    banks = {}
    for sc in g.get('subcircuits', []):
        if sc['name'].startswith('CT_') and 'U_' in sc['name']:
            banks[sc['name']] = [dict({'name': i['name'], 'role': i['role']},
                                      **{k: (round(v * 1e6, 4) if k in ('w', 'l') else v) for k, v in i['parameters'].items()
                                         if k in ('w', 'l', 'nf', 'm', 'c', 'r', 'layer')})
                                 for i in sc['instances'] if i['role'] in ('cap', 'nmos', 'pmos', 'res')]
    # w/l in um for devices, resistors and MIM/MOM caps; 'c' (F) only for ideal capacitors
    p = proj['parameters']
    return {'core_devices': devs, 'bias_devices': bias, 'inductors': inds, 'couplings': ks, 'fixed_caps': caps, 'bank_unit_cells': banks,
            'registers': {k: {'bits': v['bits'], 'value': v['value']} for k, v in proj['registers'].items()},
            'parameters': {k: p.get(k) for k in ('vdd', 'vdc', 'L_scale', 'bank_cap_scale', 'aux_cap_scale', 'core_width_scale', 'loss_scale') if k in p},
            'generated_dir': str(generated_dir)}


def _gain_variants(knob, g):
    """Variants (name, kind, factor) guided by the measured gain; excess gain is explored, not forbidden."""
    from .sizing import GAIN_EXCESS, GAIN_LOW, GAIN_TARGET
    gm_rp = g.get('gm_rp') if (g and not g.get('error') and g.get('valid')) else None
    if gm_rp is None:
        return [('L_up', 'L', 1.4), ('L_down', 'L', 0.7)]
    if gm_rp > GAIN_EXCESS:
        out = [('gain_nominal_L', 'L', min(max(GAIN_TARGET / gm_rp, 0.4), 0.8))]
        if knob.kind == 'vdc':
            out.insert(0, ('gain_nominal_width', 'width', 0.65))
        out.append(('power_low', 'power', 0.55))
        return out
    out = [('gain_excess_L', 'L', min(max(2 * GAIN_TARGET / gm_rp, 1.25), 2.0))]
    if knob.kind == 'vdc':
        out.append(('gain_excess_width', 'width', 1.5))
    if gm_rp >= GAIN_LOW:
        out.append(('power_low', 'power', 0.55))
    return out


def _swing(ev, ctype, vdd):
    from .sizing import swing_limit_v
    if not ev or not ev.get('rows'):
        return
    lim = swing_limit_v(ctype, vdd)
    amax = max(x['amp_v'] for x in ev['rows'])
    amin = min(x['amp_v'] for x in ev['rows'])
    ev['swing'] = {'core_type': ctype, 'limit_v': lim, 'max_amp_v': amax, 'min_amp_v': amin, 'max_over_limit': amax / lim}


def autosize(cell, workdir, budget=44, seed=None, log=None, timeout_s=300, lock_command=None, gain=True):
    """Size one cell. cell: {pdk, family, variant, f0_hz, p_cap_mw, tr_min, main_bits, q_target}."""
    t0 = time.time()
    cell = dict(cell)
    cell.setdefault('tr_min', 0.30)
    cell.setdefault('q_target', 20.0)
    cell.setdefault('main_bits', 4)
    Path(workdir).mkdir(parents=True, exist_ok=True)
    if log is None:
        def log(msg, _f=Path(workdir) / 'progress.log'):
            with open(_f, 'a') as fh:
                fh.write(time.strftime('%H:%M:%S ') + msg + '\n')
    ex = Executor(workdir, budget, timeout_s=timeout_s, log=log, lock_command=lock_command)
    project = _project(cell, (seed or {}).get('parameters'), (seed or {}).get('registers'))
    keep = {}
    record = {'schema': 'vco.autosize.v2', 'cell': cell, 'seed': (seed or {}).get('source'), 'status': 'failed',
              'started': t0}
    try:
        fixes = _legalize(project)
        if fixes:
            record['legalized'] = fixes
        if not _legal(project):
            raise DesignInvalid('no legal starting design for this cell (device or bank geometry outside PDK limits)')
        q_before, q_after = _apply_q(project, cell)
        record['q'] = {'template_q_at_f0': q_before, 'q_at_f0': q_after, 'target': cell.get('q_target', 20)}
        knob = PowerKnob(cell, project)
        record['power_knob'] = knob.kind
        best = None
        best_project = None
        keep = {}
        split = 0.5
        # A + B: base L/C split; then aux calibration; then one L/C-split variant (L x1.4, else x0.7)
        has_aux = 'aux' in project['registers']
        aux_info = []
        try:
            ev, project, codes = _shape(ex, project, cell, knob, split, 'base')
        except DesignInvalid as exc:
            ev, codes = {'status': 'failed', 'reason': 'design invalid: ' + str(exc)[:200], 'rows': []}, {}
        ev['variant'] = 'base'
        if ev['status'] != 'failed':
            keep.update(best=ev, project=copy.deepcopy(project))
        if has_aux and ev['status'] != 'failed':
            ev, project, codes, info = _aux_calibrate(ex, project, cell, ev, codes)
            aux_info.append(dict(info, variant='base'))
            ev['variant'] = 'base'
        best, best_project = ev, copy.deepcopy(project)
        keep.update(best=best, project=best_project, codes=codes)
        from .generator import make_graph
        from .magnetics import core_type
        ctype = core_type(make_graph(copy.deepcopy(best_project))[0])
        vdd = float(best_project['parameters']['vdd'])
        _swing(best, ctype, vdd)

        def mid_of(cds, prj):
            # lowest-gain point of the band: the lowest-frequency code (all bank cells on); docs/METHODS.md
            ok = [r for r in cds.values() if _ok(r)]
            return min(ok, key=lambda r: r['metrics']['frequency_hz']) if ok else None
        if gain and best['status'] != 'failed':
            best['gain'] = _gain(ex, mid_of(codes, best_project), 'base')
        variants = _gain_variants(knob, best.get('gain')) if best['status'] != 'failed' else []
        tried = record['variants'] = [{'variant': 'base', 'status': best['status'], 'fom_1m_worst': best.get('fom_1m_worst'), 'gain': best.get('gain'),
                  'swing': best.get('swing'), 'p_max_mw': best.get('p_max_mw')}]
        base_project = copy.deepcopy(best_project)
        best_eval_base = best
        for variant_name, kind, factor in variants:
            if best['status'] == 'failed' or ex.budget - ex.used < 9:
                break
            trial = copy.deepcopy(base_project)
            p_frac = 1.0
            split_v = split
            if kind == 'L':
                applied = _scale_param_legal(trial, 'bank_cap_scale', 1 / factor, also=('aux_cap_scale',))
                if abs(applied - 1) < 0.02:
                    continue   # bank cannot move this way within PDK limits
                trial['parameters']['L_scale'] = round(trial['parameters']['L_scale'] / applied, 6)
                split_v = 0.0      # keep the chosen L: re-center with C
            elif kind == 'width':
                trial['parameters']['core_width_scale'] = round(float(trial['parameters']['core_width_scale']) * factor, 6)
                if not _legal(trial):
                    continue
            elif kind == 'power':
                p_base = best_eval_base.get('p_max_mw') or cell['p_cap_mw']
                p_frac = factor * p_base / cell['p_cap_mw']
            knob.points = []
            try:
                ev2, p2, codes2 = _shape(ex, trial, cell, knob, split_v, variant_name, p_frac=p_frac, tr_shrink=(kind != 'L'))
            except DesignInvalid as exc:
                ev2, p2, codes2 = {'status': 'failed', 'reason': 'design invalid: ' + str(exc)[:200], 'rows': []}, trial, {}
            ev2['variant'] = variant_name
            if has_aux and ev2['status'] != 'failed':
                ev2, p2, codes2, info = _aux_calibrate(ex, p2, cell, ev2, codes2, quick=True, prev_aux=base_project['registers']['aux']['value'])
                aux_info.append(dict(info, variant=variant_name))
            ev2['variant'] = variant_name
            _swing(ev2, ctype, vdd)
            if gain and ev2['status'] != 'failed':
                ev2['gain'] = _gain(ex, mid_of(codes2, p2), variant_name)
            tried.append({'variant': variant_name, 'kind': kind, 'factor': factor, 'status': ev2['status'],
                          'fom_1m_worst': ev2.get('fom_1m_worst'), 'gain': ev2.get('gain'), 'swing': ev2.get('swing'),
                          'p_max_mw': ev2.get('p_max_mw')})
            if _better(ev2, best):
                best, best_project = ev2, copy.deepcopy(p2)
                keep.update(best=best, project=best_project, codes=codes2)
        lowest = mid_of(keep.get('codes') or {}, best_project) if keep.get('codes') else None
        if lowest is not None and lowest.get('_generated'):
            record['design_summary'] = design_summary(lowest['_generated'])
        record['aux_search'] = aux_info
        record['evaluation'] = best
        record['status'] = best['status'] if best else 'failed'
        record['design'] = {'parameters': best_project['parameters'] if best_project else None,
                            'registers': best_project['registers'] if best_project else None}
    except BudgetExhausted as exc:
        record['note'] = str(exc)
        if keep.get('best'):
            record['evaluation'] = keep['best']
            record['status'] = keep['best']['status']
            record['budget_exhausted'] = True
            record['design'] = {'parameters': keep['project']['parameters'], 'registers': keep['project']['registers']}
            ok = [r for r in (keep.get('codes') or {}).values() if _ok(r) and r.get('_generated')]
            if ok:
                try:
                    record['design_summary'] = design_summary(min(ok, key=lambda r: r['metrics']['frequency_hz'])['_generated'])
                except (OSError, ValueError, KeyError):
                    pass
        record.setdefault('evaluation', None)
    except DesignInvalid as exc:
        record['status'] = 'design_invalid'
        record['error'] = str(exc)[:500]
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # keep the campaign alive; record the defect
        from .runner import SolverInterrupted
        if isinstance(exc, SolverInterrupted):
            raise
        record['status'] = 'error'
        record['error'] = '%s: %s' % (type(exc).__name__, exc)
        record['traceback'] = traceback.format_exc()[-2000:]
    record['sims_used'] = ex.used
    record['loopgain_runs'] = ex.lg_used
    record['history'] = ex.history
    record['elapsed_s'] = time.time() - t0
    write_json(Path(workdir) / 'autosize.json', record)
    return record


# ------------------------------------------------------------------------------------------ campaign
def _cells(grid):
    out = []
    order = [(10, 5), (10, 10), (10, 1), (5, 5), (5, 10), (5, 1), (20, 5), (20, 10), (20, 1)]
    for pdk in grid['pdks']:
        for fam in grid['families']:
            family, variant = fam.split('/')
            fs = grid['freqs_ghz']
            ps = grid['powers_mw']
            pairs = [(f, p) for f, p in order if f in fs and p in ps] + [(f, p) for f in fs for p in ps if (f, p) not in order]
            for f, p in pairs:
                out.append({'pdk': pdk, 'family': family, 'variant': variant, 'f0_hz': f * 1e9, 'p_cap_mw': float(p),
                            'tr_min': grid.get('tr_min', 0.30), 'main_bits': grid.get('main_bits', 4),
                            'q_target': grid.get('q_target', 20)})
    return out


def cell_key(c):
    return '%s_%s_%s_f%g_p%g' % (c['pdk'], c['family'], c['variant'], c['f0_hz'] / 1e9, c['p_cap_mw'])


def _append(path, record):
    with open(path, 'a', encoding='utf-8') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
        fcntl.flock(f, fcntl.LOCK_UN)


def load_results(path):
    rows = []
    p = Path(path)
    if p.is_file():
        for line in p.read_text(encoding='utf-8').splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _warm_seed(done, cell):
    """Nearest finished cell of the same pdk/family/variant: copy its design, rescale for f0 and power."""
    same = [r for r in done if r['cell']['pdk'] == cell['pdk'] and r['cell']['family'] == cell['family']
            and r['cell']['variant'] == cell['variant'] and r.get('design', {}).get('parameters')
            and r.get('status') in ('pass', 'constraint_fail')]
    if not same:
        return None
    best = min(same, key=lambda r: abs(math.log(r['cell']['f0_hz'] / cell['f0_hz'])) + 0.5 * abs(math.log(r['cell']['p_cap_mw'] / cell['p_cap_mw'])))
    params = copy.deepcopy(best['design']['parameters'])
    ratio = (best['cell']['f0_hz'] / cell['f0_hz']) ** 2
    params['L_scale'] = round(params['L_scale'] * ratio ** 0.5, 6)
    for k in ('bank_cap_scale', 'aux_cap_scale'):
        params[k] = round(params[k] * ratio ** 0.5, 6)
    key = (cell['family'], cell['variant'])
    if key not in VDC_KNOB:
        params['core_width_scale'] = round(params['core_width_scale'] * cell['p_cap_mw'] / best['cell']['p_cap_mw'], 6)
    return {'parameters': params, 'registers': {k: {'value': v['value']} for k, v in best['design']['registers'].items() if k == 'aux'},
            'source': cell_key(best['cell'])}


def run_worker(grid_file, outdir, worker_key, budget):
    grid = read_json(grid_file)
    outdir = Path(outdir)
    results = outdir / 'results.jsonl'
    cells = [c for c in _cells(grid) if '%s|%s/%s' % (c['pdk'], c['family'], c['variant']) == worker_key]
    for cell in cells:
        done = load_results(results)
        if any(cell_key(r['cell']) == cell_key(cell) for r in done):
            continue
        logf = outdir / 'logs' / (cell_key(cell) + '.log')
        logf.parent.mkdir(parents=True, exist_ok=True)
        def log(msg, _f=logf):
            with open(_f, 'a') as fh:
                fh.write(time.strftime('%H:%M:%S ') + msg + '\n')
        seed = _warm_seed(done, cell) if grid.get('warm_start', True) else None
        log('start %s seed=%s' % (cell_key(cell), seed and seed['source']))
        rec = autosize(cell, outdir / 'cells' / cell_key(cell), budget=budget, seed=seed, log=log,
                       timeout_s=grid.get('timeout_s', 300), lock_command=grid.get('lock_command'))
        rec['key'] = cell_key(cell)
        rec['autosize_json'] = str(outdir / 'cells' / cell_key(cell) / 'autosize.json')
        rec.pop('history', None)        # per-run history stays in autosize.json; the table row stays compact
        _append(results, rec)
        log('done status=%s sims=%s' % (rec['status'], rec.get('sims_used')))


def campaign(grid_file, outdir, budget=44, workers=None):
    """Run all cells; one worker process per (pdk, family/variant), each warm-starting its own cells."""
    grid = read_json(grid_file)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    write_json(outdir / 'grid.json', grid)
    keys = sorted({'%s|%s/%s' % (c['pdk'], c['family'], c['variant']) for c in _cells(grid)})
    procs = []
    for key in keys[: workers or len(keys)]:
        cmd = [_launch(), 'campaign-worker', str(outdir / 'grid.json'), '--outdir', str(outdir), '--key', key, '--budget', str(budget)]
        logf = open(outdir / ('worker-%s.log' % key.replace('|', '_').replace('/', '_')), 'a')
        procs.append(subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True))
    write_json(outdir / 'workers.json', {'pids': [p.pid for p in procs], 'keys': keys, 'started': time.time()})
    return {'outdir': str(outdir), 'workers': len(procs), 'cells': len(_cells(grid)), 'pids': [p.pid for p in procs]}


# ------------------------------------------------------------------------------------------ CLI
def cli_register(sub):
    c = sub.add_parser('autosize', help='automatically size one topology for one (pdk, f0, power cap) cell with real HB/PN runs',
                       description='automatically size one topology for one (pdk, f0, power cap) cell with real HB/PN runs')
    c.add_argument('--pdk', required=True)
    c.add_argument('--family', required=True)
    c.add_argument('--variant')
    c.add_argument('--f0-ghz', type=float, required=True)
    c.add_argument('--power-mw', type=float, required=True, help='power cap (all codes)')
    c.add_argument('--tr', type=float, default=0.30, help='minimum tuning range as a fraction (default 0.30)')
    c.add_argument('--bits', type=int, default=4)
    c.add_argument('--q', type=float, default=20, help='max magnetic-mode Q at f0 (rule: <= 30)')
    c.add_argument('--budget', type=int, default=44)
    c.add_argument('--seed-from', help='lookup-table results.jsonl to warm start from (default: shipped table)')
    c.add_argument('--no-seed', action='store_true')
    c.add_argument('--output', required=True)
    c = sub.add_parser('campaign', help='run a grid of autosize cells in background workers (lookup-table builder)',
                       description='run a grid of autosize cells in background workers (lookup-table builder)')
    c.add_argument('grid', help='grid JSON: {pdks, families:["hegazi/tail",...], freqs_ghz, powers_mw, tr_min, main_bits, q_target}')
    c.add_argument('--outdir', required=True)
    c.add_argument('--budget', type=int, default=44)
    c = sub.add_parser('campaign-worker', help=argparse_suppress())
    c.add_argument('grid')
    c.add_argument('--outdir', required=True)
    c.add_argument('--key', required=True)
    c.add_argument('--budget', type=int, default=44)


def argparse_suppress():
    import argparse
    return argparse.SUPPRESS


def cli_dispatch(args):
    from .cli import EXIT_OK, EXIT_MEASURE_FAILED
    if args.command == 'autosize':
        from .catalog import get_variant
        from .spec import canonical_pdk
        v = get_variant(args.family, args.variant)
        cell = {'pdk': canonical_pdk(args.pdk), 'family': args.family, 'variant': v['id'], 'f0_hz': args.f0_ghz * 1e9,
                'p_cap_mw': args.power_mw, 'tr_min': args.tr, 'main_bits': args.bits, 'q_target': args.q}
        if not 0 < args.q <= 60:
            raise ValueError('--q must be positive; it is capped per design at 30*(1+|k|) (stage rule)')
        seed = None
        if not args.no_seed:
            from .select import table_results
            seed = _warm_seed(table_results(args.seed_from), cell)
        if Path(args.output).exists() and any(Path(args.output).iterdir()):
            raise FileExistsError('output exists and is not empty: ' + args.output)
        rec = autosize(cell, args.output, budget=args.budget, seed=seed)
        rec.pop('history', None)
        return rec, (EXIT_OK if rec['status'] == 'pass' else EXIT_MEASURE_FAILED)
    if args.command == 'campaign':
        return campaign(args.grid, args.outdir, budget=args.budget), EXIT_OK
    if args.command == 'campaign-worker':
        run_worker(args.grid, args.outdir, args.key, args.budget)
        return {'worker': args.key, 'status': 'finished'}, EXIT_OK
    return None
