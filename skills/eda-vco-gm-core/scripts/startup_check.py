#!/usr/bin/env python3
"""Quantify VCO startup from the HB pre-transient (tstab) waveform.

Input : a run directory produced by `launch simulate` / flow-measure / flow-sweep / flow-converge
        with project simulation.save_startup_waveform=true (Spectre hb saveinit=yes), i.e. it holds
        psf/hb.tran.pss_hb.tran (or .gz) and normally result.json.
Output: JSON on stdout -- initial kick, block-RMS envelope, 10 %/90 % rise times, small-signal
        growth rate (dB/cycle; negative = the kick decays, loop gain < 1 at the DC point), amplitude
        spread over the last 25 % of the record, zero-crossing frequency vs the HB result, verdict.
Exit  : 0 STARTUP_OK, 3 STARTED_NOT_SETTLED / NO_STARTUP / INCONCLUSIVE, 2 bad input.

Scope : startup from the testbench initial condition (default 1 mV differential kick) at the
        simulated corner only. It does not replace Spectre's periodic convergence and says nothing
        about PVT. Optional --q-loaded Q gives alpha_est = 1 + 2*Q*sigma/omega0 (parallel-RLC model,
        Q = loaded differential tank Q at f0; using the magnetic Q only gives an upper bound).
"""
import argparse
import gzip
import json
import math
import re
import sys
from pathlib import Path

LINE = re.compile(r'^"([^"]+)"\s+(\S+)\s*$')


def read_waveform(path, pos, neg):
    """Stream a PSF-ASCII transient; return lists t, vdiff (pos - neg)."""
    opener = gzip.open if str(path).endswith('.gz') else open
    t, vd = [], []
    cur = {}
    in_values = False
    with opener(path, 'rt', errors='replace') as fh:
        for raw in fh:
            line = raw.strip()
            if not in_values:
                in_values = line == 'VALUE'
                continue
            if line == 'END':
                break
            m = LINE.match(line)
            if not m:
                continue
            key, val = m.groups()
            if key == 'time':
                if pos in cur and neg in cur and 'time' in cur:
                    t.append(cur['time'])
                    vd.append(cur[pos] - cur[neg])
                cur = {'time': float(val)}
            elif key in (pos, neg):
                try:
                    cur[key] = float(val)
                except ValueError:
                    pass
    if pos in cur and neg in cur and 'time' in cur:
        t.append(cur['time'])
        vd.append(cur[pos] - cur[neg])
    return t, vd


def rising_crossings(t, v):
    out = []
    for i in range(1, len(v)):
        if v[i - 1] < 0 <= v[i]:
            dt = t[i] - t[i - 1]
            out.append(t[i - 1] - v[i - 1] * dt / (v[i] - v[i - 1]) if v[i] != v[i - 1] else t[i])
    return out


def block_rms(t, v, t0, t1):
    """sqrt(2)*RMS of v over [t0, t1] (trapezoid on the samples inside)."""
    acc, last_t, last_v2 = 0.0, None, None
    for ti, vi in zip(t, v):
        if ti < t0:
            continue
        if ti > t1:
            break
        if last_t is not None:
            acc += 0.5 * (vi * vi + last_v2) * (ti - last_t)
        last_t, last_v2 = ti, vi * vi
    span = t1 - t0
    return math.sqrt(2 * acc / span) if span > 0 and acc > 0 else 0.0


def analyse(run, pos='OUTP', neg='OUTN', cycles_per_block=8, tol=0.005, q_loaded=None, min_amp=0.05, f0_ghz=None):
    run = Path(run)
    cands = [run / 'psf' / 'hb.tran.pss_hb.tran', run / 'psf' / 'hb.tran.pss_hb.tran.gz']
    src = next((c for c in cands if c.is_file()), None)
    if src is None:
        raise FileNotFoundError('no psf/hb.tran.pss_hb.tran(.gz) in %s: set simulation.save_startup_waveform=true and re-run' % run)
    res = {}
    if (run / 'result.json').is_file():
        res = json.loads((run / 'result.json').read_text())
    t, v = read_waveform(src, pos, neg)
    if len(t) < 10:
        raise ValueError('waveform has %d samples; check --pos/--neg node names' % len(t))
    out = {'source': str(src), 'samples': len(t), 'record_ns': (t[-1] - t[0]) * 1e9,
           'initial_differential_v': v[0], 'hb_warnings': res.get('warnings', []),
           'hb_status': res.get('status'), 'hb_frequency_hz': (res.get('metrics') or {}).get('frequency_hz')}
    zc = rising_crossings(t, v)
    out['rising_crossings'] = len(zc)
    if len(zc) < 2 * cycles_per_block + 2:
        if out['record_ns'] < 1.0:
            out.update(verdict='INCONCLUSIVE', reason='record only %.3g ns: transient aborted; read spectre.log' % out['record_ns'])
        else:
            out.update(verdict='NO_STARTUP', reason='fewer than %d rising zero crossings' % (2 * cycles_per_block + 2))
        return out
    blocks = []
    for i in range(0, len(zc) - cycles_per_block, cycles_per_block):
        a, b = zc[i], zc[i + cycles_per_block]
        blocks.append({'t_mid_ns': 0.5 * (a + b) * 1e9, 't0': a, 't1': b, 'amp_v': block_rms(t, v, a, b),
                       'f_hz': cycles_per_block / (b - a)})
    # one-cycle envelope (rise times, growth fit); K-cycle blocks above (settling statistic)
    cyc = [(0.5 * (a + b), block_rms(t, v, a, b)) for a, b in zip(zc, zc[1:])]
    amp_final = blocks[-1]['amp_v']
    f_last = blocks[-1]['f_hz']
    t_end = t[-1]
    last = [bk for bk in blocks if bk['t0'] >= t_end - 0.25 * (t_end - t[0])] or blocks[-3:]
    if len(last) < 3:
        last = blocks[-3:]
    amps = [bk['amp_v'] for bk in last]
    spread = (max(amps) - min(amps)) / (sum(amps) / len(amps)) if min(amps) > 0 else float('inf')
    lz = [z for z in zc if z >= last[0]['t0']]
    f_tran = (len(lz) - 1) / (lz[-1] - lz[0]) if len(lz) > 2 else f_last

    def first_time(frac):
        for tm, amp in cyc:
            if amp >= frac * amp_final:
                return tm * 1e9
        return None

    t10, t90 = first_time(0.1), first_time(0.9)
    # start level: the smaller of the IC kick and the first cycles (the bias network may add a kick)
    a0 = min([abs(v[0])] + [amp for _, amp in cyc[:5]]) if abs(v[0]) > 1e-9 else max(min(amp for _, amp in cyc[:5]), 1e-12)

    def fit(points):
        if len(points) < 3:
            return None
        n = len(points)
        mx = sum(p[0] for p in points) / n
        my = sum(p[1] for p in points) / n
        sxx = sum((p[0] - mx) ** 2 for p in points)
        return sum((p[0] - mx) * (p[1] - my) for p in points) / sxx if sxx > 0 else None

    if amp_final >= min_amp:
        # small-signal growth: cycles between 3*a0 and 0.1*amp_final (exponential phase; slight underestimate)
        sigma = fit([(tm, math.log(amp)) for tm, amp in cyc if 3 * a0 < amp < 0.1 * amp_final])
    else:
        # never reached full swing: the trend over the second half of the record decides growth vs decay
        t_half = t[0] + 0.5 * (t[-1] - t[0])
        sigma = fit([(tm, math.log(amp)) for tm, amp in cyc if tm >= t_half and amp > 1e-12])
    # reference frequency: HB result, else --f0-ghz, else the HB guess (fundfreqs) in the run's tb.scs
    f_expect = out['hb_frequency_hz'] or (f0_ghz * 1e9 if f0_ghz else None)
    if not f_expect and (run / 'tb.scs').is_file():
        mg = re.search(r'fundfreqs=\[([0-9.eE+-]+)G\]', (run / 'tb.scs').read_text(errors='replace'))
        f_expect = float(mg.group(1)) * 1e9 if mg else None
    out['frequency_expected_hz'] = f_expect
    f_ref = f_expect or f_tran
    out.update({
        'amplitude_final_v': amp_final,
        'growth_ratio': amp_final / a0,
        't10_ns': t10, 't90_ns': t90,
        'cycles_to_90pct': None if t90 is None else t90 * 1e-9 * f_ref,
        'small_signal_sigma_per_ns': None if sigma is None else sigma * 1e-9,
        'small_signal_growth_db_per_cycle': None if sigma is None else 20 * math.log10(math.e) * sigma / f_ref,
        'last_window_ns': [last[0]['t0'] * 1e9, last[-1]['t1'] * 1e9],
        'last_window_amp_spread': spread,
        'frequency_tran_hz': f_tran,
        'frequency_vs_hb_rel': None if not out['hb_frequency_hz'] else abs(f_tran / out['hb_frequency_hz'] - 1),
        'cycles_in_record': (t_end - t[0]) * f_ref,
        'tstab_ns_for_500_cycles': 500.0 / f_ref * 1e9,
        'envelope': [{'t_ns': round(bk['t_mid_ns'], 3), 'amp_v': bk['amp_v']} for bk in blocks[::max(1, len(blocks) // 40)]],
    })
    if q_loaded and sigma is not None:
        out['alpha_est'] = 1 + 2 * q_loaded * sigma / (2 * math.pi * f_ref)
        out['alpha_note'] = 'parallel-RLC estimate with Q=%g; magnetic-only Q overestimates alpha' % q_loaded
    rate = out['small_signal_growth_db_per_cycle']
    rate_txt = '' if rate is None else ', small-signal %+.3g dB/cycle' % rate
    expected_cycles = (t_end - t[0]) * f_expect if f_expect else None
    # frequency sanity: 30 % against a measured HB f0, factor 2 against a guess (endpoint codes move f)
    f_bad = bool(f_expect) and (abs(f_tran / f_expect - 1) > 0.3 if out['hb_frequency_hz'] else not 0.5 < f_tran / f_expect < 2.0)
    if out['record_ns'] < 1.0 or (expected_cycles is not None and expected_cycles < 20) or f_bad:
        out.update(verdict='INCONCLUSIVE', reason='record %.3g ns, transient f %.4g GHz vs expected %s GHz: transient aborted or wrong mode; read spectre.log' % (
            out['record_ns'], f_tran / 1e9, None if not f_expect else round(f_expect / 1e9, 4)))
    elif amp_final < min_amp and (sigma is None or sigma <= 0):
        out.update(verdict='NO_STARTUP', reason='kick decays: %.3g V at the end of %.1f ns%s' % (amp_final, out['record_ns'], rate_txt))
    elif amp_final < min_amp:
        out.update(verdict='STARTED_NOT_SETTLED', reason='still growing: %.3g V at the end of %.1f ns%s; low margin or tstab too short' % (
            amp_final, out['record_ns'], rate_txt))
    elif spread > tol or t90 is None or t90 > 0.75 * out['record_ns']:
        out.update(verdict='STARTED_NOT_SETTLED',
                   reason='last-window spread %.3g (tol %.3g), t90 %s ns of %.1f ns record: lengthen tstab_ns' % (spread, tol, t90, out['record_ns']))
    else:
        out.update(verdict='STARTUP_OK', reason='grew x%.3g to %.4g V, t90=%.1f ns, last-window spread %.2g%s' % (
            out['growth_ratio'], amp_final, t90, spread, rate_txt))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('run', help='run directory containing psf/hb.tran.pss_hb.tran(.gz)')
    ap.add_argument('--pos', default='OUTP')
    ap.add_argument('--neg', default='OUTN')
    ap.add_argument('--cycles-per-block', type=int, default=8)
    ap.add_argument('--tol', type=float, default=0.005, help='max relative amplitude spread in the last 25 %% of the record')
    ap.add_argument('--q-loaded', type=float, default=None, help='loaded tank Q at f0 for alpha_est (optional)')
    ap.add_argument('--f0-ghz', type=float, default=None, help='expected frequency when the HB run failed (default: tb.scs fundfreqs)')
    ap.add_argument('--no-envelope', action='store_true', help='omit the envelope samples from the output')
    a = ap.parse_args(argv)
    try:
        out = analyse(a.run, a.pos, a.neg, a.cycles_per_block, a.tol, a.q_loaded, f0_ghz=a.f0_ghz)
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'error', 'message': str(exc)}), file=sys.stderr)
        return 2
    if a.no_envelope:
        out.pop('envelope', None)
    print(json.dumps(out, indent=1))
    return 0 if out['verdict'] == 'STARTUP_OK' else 3


if __name__ == '__main__':
    sys.exit(main())
