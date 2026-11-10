#!/usr/bin/env python3
"""Compact readout of `launch bank-ac` results (Cap Array Lab decomposition) for VCO bank decisions.

  bankac_table.py RESULT [RESULT ...] [--json]
  bankac_table.py RESULT --at 0:3.33,15:5.44             # interpolate at each code's measured f0
  bankac_table.py RESULT --cfix 0:3.326,15:5.442 --bits 4 # endpoint calibration + bit-count what-if

RESULT is a bank-ac result.json or the bank-ac output directory. Columns:
  diff  floating P-N equivalent = what the differential tank sees (R = 1/G, Q only if capacitive, G > 0)
  side  per-side shunt to ground from the reciprocal pi decomposition ('~' = approximate fit,
        '-' = not available: matrix non-reciprocal, e.g. switches on)
  in(P) Y11 = P driven, N AC-grounded; read this for single-ended banks (aux bank with N = VSS)
With --tank-inductance-h the rows include the assumed L (C < 0 below resonance): take bank C/G/Q from a
run without it; its resonance line ignores fixed C and core parasitics, so it reads high.
--cfix ON:F_ON,OFF:F_OFF uses the HB-measured f0 of the all-on code (lowest f) and of the all-off code
(highest f) of the SAME candidate: r = (F_OFF/F_ON)^2, C_fix = (C_on - r*C_off)/(r - 1), single-mode,
same-L estimate. --bits N then predicts TR and steps for N-2..N+2 bits with the same unit cell.
Works with Python >= 3.6; reads JSON only, writes nothing.
"""
import argparse
import json
import math
import os
import sys


def load(path):
    if os.path.isdir(path):
        path = os.path.join(path, 'result.json')
    if not os.path.isfile(path):
        raise SystemExit('%s: not found (give a bank-ac result.json or its output directory)' % path)
    with open(path) as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or 'rows' not in data:
        raise SystemExit('%s: not a bank-ac result (no "rows")' % path)
    return path, data


def _metric(m):
    if not m:
        return None
    capacitive = m.get('status') == 'Capacitive' and m['G_S'] > 0
    return {'C_fF': m['C_F'] * 1e15, 'G_mS': m['G_S'] * 1e3,
            'R_ohm': (1.0 / m['G_S']) if m['G_S'] > 0 else None,
            'Q': m.get('Q') if capacitive else None, 'status': m.get('status')}


def rows_of(data):
    out = []
    for row in data['rows']:
        d = row.get('decomposition') or {}
        total = d.get('total') or {k: row.get(k) for k in ('C_F', 'G_S', 'Q', 'status')}
        side, kind = (d.get('p_ground'), d.get('n_ground')), 'exact'
        if not side[0]:
            approx = d.get('approximate') or {}
            side, kind = (approx.get('p_ground'), approx.get('n_ground')), 'approx'
        if not side[0]:
            side, kind = (None, None), 'none'
        out.append({'code': int(row.get('point', row.get('code', 0))), 'f_GHz': row['frequency_Hz'] / 1e9,
                    'diff': _metric(total), 'side_kind': kind,
                    'side_p': _metric(side[0]), 'side_n': _metric(side[1]),
                    'in_p': _metric(d.get('p_input')), 'in_n': _metric(d.get('n_input')),
                    'flags': list(d.get('flags') or []), 'valid': d.get('valid')})
    out.sort(key=lambda r: (r['code'], r['f_GHz']))
    return out


def _f(v, fmt):
    return '-' if v is None else fmt % v


def print_table(path, data, rows):
    prov = data.get('sample_provenance') or {}
    print('bank-ac %s' % path)
    print('  bank=%s vcm=%s V vdd=%s V T=%s C tank_L=%s graph=%s rows=%d' % (
        prov.get('bank'), prov.get('vcm_v'), prov.get('vdd_v'), prov.get('temperature_c'),
        prov.get('tank_inductance_h') or 'none', str(prov.get('source_graph_hash', ''))[:12], len(rows)))
    print('  code   f_GHz |  diff C_fF   G_mS   R_ohm     Q |  side(P/N) C_fF      R_ohm        Q    |  in(P) C_fF   G_mS     Q | flags')
    for r in rows:
        dm, sp, sn, ip = r['diff'], r['side_p'], r['side_n'], r['in_p']
        mark = {'exact': ' ', 'approx': '~', 'none': '-'}[r['side_kind']]
        if sp:
            side = '%s%7.2f/%-7.2f %6s/%-6s %5s/%-5s' % (mark, sp['C_fF'], sn['C_fF'], _f(sp['R_ohm'], '%.0f'),
                                                      _f(sn['R_ohm'], '%.0f'), _f(sp['Q'], '%.2f'), _f(sn['Q'], '%.2f'))
        else:
            side = '%-38s' % '-'
        print('  %4d %7.3f | %9.2f %7.4f %7s %5s | %s | %9.2f %7.4f %5s | %s' % (
            r['code'], r['f_GHz'], dm['C_fF'], dm['G_mS'], _f(dm['R_ohm'], '%.0f'), _f(dm['Q'], '%.2f'),
            side, ip['C_fF'], ip['G_mS'], _f(ip['Q'], '%.2f'), ','.join(r['flags']) or '-'))
    for code, res in sorted((data.get('resonances') or {}).items(), key=lambda kv: int(kv[0])):
        if res.get('available'):
            print('  resonance code %s: %.4f GHz (bank + assumed tank L only; excludes fixed C/core -> reads high)' % (
                code, res['frequency_Hz'] / 1e9))
        else:
            print('  resonance code %s: n/a (%s)' % (code, res.get('reason', '')))


def interp(rows, code, f_ghz, key='diff'):
    pts = [(r['f_GHz'], r[key]) for r in rows if r['code'] == code and r[key]]
    if not pts:
        raise SystemExit('code %d not in result' % code)
    pts.sort(key=lambda t: t[0])
    if f_ghz < pts[0][0] - 1e-12 or f_ghz > pts[-1][0] + 1e-12:
        raise SystemExit('code %d: %.4f GHz outside sampled %.4f..%.4f GHz; rerun bank-ac with --freq-ghz around it'
                         % (code, f_ghz, pts[0][0], pts[-1][0]))
    for (fa, a), (fb, b) in zip(pts, pts[1:] + [pts[-1]]):
        if fa - 1e-12 <= f_ghz <= fb + 1e-12:
            t = 0.0 if fb == fa else (f_ghz - fa) / (fb - fa)
            c = a['C_fF'] + t * (b['C_fF'] - a['C_fF'])
            g = a['G_mS'] + t * (b['G_mS'] - a['G_mS'])
            return {'C_fF': c, 'G_mS': g, 'R_ohm': (1e3 / g) if g > 0 else None,
                    'Q': (2 * math.pi * f_ghz * 1e9 * c * 1e-15) / (g * 1e-3) if g > 0 else None}
    return None


def pairs(text):
    out = []
    for item in text.split(','):
        code, f = item.split(':')
        out.append((int(code), float(f)))
    return out


def tr(lo, hi):
    return 2 * (hi - lo) / (hi + lo)


def cfix_report(rows, spec, bits):
    (c_on_code, f_on), (c_off_code, f_off) = pairs(spec)
    if not f_off > f_on:
        raise SystemExit('--cfix expects ON_CODE:F_LOW,OFF_CODE:F_HIGH (all-on code has the lower f0)')
    on, off = interp(rows, c_on_code, f_on), interp(rows, c_off_code, f_off)
    r = (f_off / f_on) ** 2
    c_fix = (on['C_fF'] - r * off['C_fF']) / (r - 1)
    out = {'C_on_fF': on['C_fF'], 'C_off_fF': off['C_fF'], 'bank_ratio': on['C_fF'] / off['C_fF'],
           'tank_C_ratio': r, 'C_fix_fF': c_fix, 'TR_pct': tr(f_on, f_off) * 100, 'predictions': []}
    if bits:
        n = 2 ** bits - 1
        du = (on['C_fF'] - off['C_fF']) / n
        center = (f_on + f_off) / 2
        for b in range(max(1, bits - 2), min(12, bits + 2) + 1):
            k = (2 ** b - 1) / float(n)
            ctot_on, ctot_off = c_fix + on['C_fF'] * k, c_fix + off['C_fF'] * k
            lo = f_on * math.sqrt((c_fix + on['C_fF']) / ctot_on)
            hi = f_off * math.sqrt((c_fix + off['C_fF']) / ctot_off)
            s = center / ((lo + hi) / 2)   # frequency factor after re-centering with L
            out['predictions'].append({
                'bits': b, 'units': 2 ** b - 1, 'TR_pct': tr(lo, hi) * 100,
                'f_lo_GHz_same_L': lo, 'f_hi_GHz_same_L': hi, 'L_multiplier_to_recenter': (1 / s) ** 2,
                'max_step_MHz_equal_units': hi * s * (1 - (1 + du / ctot_off) ** -0.5) * 1e3,
                'step_lower_bound_MHz': (hi - lo) * s / (2 ** b - 1) * 1e3})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('results', nargs='+')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--at', help='CODE:F_GHZ[,CODE:F_GHZ...] interpolate diff and in(P) at these frequencies')
    ap.add_argument('--cfix', help='ON_CODE:F_ON_GHZ,OFF_CODE:F_OFF_GHZ (HB-measured endpoint f0) -> C_fix')
    ap.add_argument('--bits', type=int, help='current register width; with --cfix predicts N-2..N+2 bits')
    a = ap.parse_args(argv)
    report = []
    for p in a.results:
        path, data = load(p)
        rows = rows_of(data)
        item = {'path': path, 'provenance': {k: (data.get('sample_provenance') or {}).get(k)
                                             for k in ('bank', 'vcm_v', 'vdd_v', 'temperature_c', 'tank_inductance_h')},
                'rows': rows, 'resonances': data.get('resonances')}
        if a.at:
            item['at'] = [{'code': c, 'f_GHz': f, 'diff': interp(rows, c, f), 'in_p': interp(rows, c, f, 'in_p')}
                          for c, f in pairs(a.at)]
        if a.cfix:
            item['cfix'] = cfix_report(rows, a.cfix, a.bits)
        report.append(item)
        if a.json:
            continue
        print_table(path, data, rows)
        for x in item.get('at', []):
            d, i = x['diff'], x['in_p']
            print('  at code %d %.4f GHz: diff C=%.2f fF G=%.4f mS R=%s ohm Q=%s | in(P) C=%.2f fF Q=%s' % (
                x['code'], x['f_GHz'], d['C_fF'], d['G_mS'], _f(d['R_ohm'], '%.0f'), _f(d['Q'], '%.2f'),
                i['C_fF'], _f(i['Q'], '%.2f')))
        if 'cfix' in item:
            c = item['cfix']
            print('  cfix: C_on=%.2f fF C_off=%.2f fF bank_ratio=%.2f tank_ratio=%.4f C_fix=%.1f fF TR=%.2f %%' % (
                c['C_on_fF'], c['C_off_fF'], c['bank_ratio'], c['tank_C_ratio'], c['C_fix_fF'], c['TR_pct']))
            for q in c['predictions']:
                print('    bits %2d (%4d units): TR %6.2f %%  same-L band %.4f-%.4f GHz  L x%.3f to recenter  '
                      'max step %.1f MHz (equal units)  lower bound %.1f MHz' % (
                          q['bits'], q['units'], q['TR_pct'], q['f_lo_GHz_same_L'], q['f_hi_GHz_same_L'],
                          q['L_multiplier_to_recenter'], q['max_step_MHz_equal_units'], q['step_lower_bound_MHz']))
            print('    (single-mode estimate: same L, constant C_fix; amplitude/core changes move C_fix)')
    if a.json:
        json.dump(report if len(report) > 1 else report[0], sys.stdout, indent=1)
        print()


if __name__ == '__main__':
    main()
