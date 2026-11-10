#!/usr/bin/env python3
"""PN quick triage: noise-contribution breakdown of a Spectre hbnoise (PSF-ASCII) result.

Inputs (any mix): result.json | run directory (has psf/) | hbnoise PSF file (.gz ok) | task workspace (task.json).
Prints per offset: PN, flicker share, closure, contribution by role and by block (top-level instance)
with the removal bound; slopes between offsets; a triage class. --compare puts every input against the
first one (per-role / per-block PN-contribution deltas) to show which source moved.

Facts this relies on (checked on VCO Design Studio runs):
  * Studio PN = 10*log10(2*out^2/amp^2), amp = |V1(OUTP)-V1(OUTN)| (vco_studio/simulator.py parse_result).
  * The per-source 'total' fields sum to out^2 (closure ~1), so share = total/out^2 and a source's
    PN contribution = 10*log10(2*total/amp^2). Removing a source entirely gains at most -10*log10(1-share).
  * Field names come from the PSF TYPE section and differ by model: BSIM3 rd,rs,id,fn,total;
    BSIM4 rd,rs,rgbi,rbp*,rb*b,id,igs,igd,igb,fn,total (trace = instance name, no '.main');
    bsource flicker_noise,thermal_noise,total; resistor rn,total; inductor rn,fn,total; diode [rs,]id,total.
    Flicker = fn + flicker_noise; everything else is white (thermal/shot).
Python: python3 (stdlib only).
"""
import argparse
import gzip
import json
import math
import re
import sys
from pathlib import Path

PSF_NAME = 'hbnoise.pm.pnoise_hbnoise'
FLICKER_FIELDS = ('fn', 'flicker_noise')
INTERNAL = {'main', 'djsb', 'djdb', 'ddg', 'dsg', 'ddd', 'dss', 'lsource', 'ldrain', 'lgate', 'rsub1', 'rsub2',
            'rsub3', 'rds', 'rgate', 'rend1', 'rend2', 'rp', 'rmim_res'}


def _text(path):
    if str(path).endswith('.gz'):
        with gzip.open(str(path), 'rt', errors='replace') as f:
            return f.read()
    return Path(path).read_text(errors='replace')


def parse_psf(path):
    text = _text(path)
    head, sep, body = text.partition('\nVALUE\n')
    if not sep:
        raise ValueError('not a PSF-ASCII file (no VALUE section): %s' % path)
    types = {m.group(1): re.findall(r'^"([^"]+)" FLOAT', m.group(2), re.M)
             for m in re.finditer(r'^"([^"]+)" STRUCT\((.*?)\n\) PROP\(', head, re.S | re.M)}
    trace_part = head.split('\nTRACE\n', 1)
    trace_type = dict(re.findall(r'^"([^"]+)" "([^"]+)"$', trace_part[1], re.M)) if len(trace_part) == 2 else {}
    f0 = re.search(r'^"fundamental frequency" (\S+)$', head, re.M)
    outs = [re.search(r'^"%s output signal" "([^"]+)"$' % k, head, re.M) for k in ('positive', 'negative')]
    points, cur, lines, i = [], None, body.splitlines(), 0
    while i < len(lines):
        m = re.match(r'^"([^"]+)"\s+(.*)$', lines[i].strip())
        i += 1
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == 'relative frequency':
            cur = {'f': float(val), 'vec': {}, 'scalar': {}}
            points.append(cur)
        elif cur is None:
            continue
        elif val == '(':
            nums = []
            while i < len(lines) and lines[i].strip() != ')':
                if lines[i].strip():
                    nums.append(float(lines[i]))
                i += 1
            i += 1
            cur['vec'][key] = nums
        else:
            try:
                cur['scalar'][key] = float(val)
            except ValueError:
                pass
    return {'types': types, 'trace_type': trace_type, 'points': points, 'f0': float(f0.group(1)) if f0 else None,
            'outputs': [m.group(1) if m else d for m, d in zip(outs, ('OUTP', 'OUTN'))]}


def balance(run_dir, pos='OUTP', neg='OUTN'):
    """Fundamental of each single-ended output from psf/hb.fd.pss_hb: |V1P|, |V1N|, imbalance %, |CM1|."""
    fd = Path(run_dir) / 'psf' / 'hb.fd.pss_hb'
    if not fd.is_file():
        return None
    pts, cur = [], None
    for line in _text(fd).split('\nVALUE\n', 1)[-1].splitlines():
        m = re.match(r'^"([^"]+)"\s+(.*)$', line.strip())
        if not m:
            continue
        key, val = m.groups()
        if key == 'freq':
            cur = {'f': float(val)}
            pts.append(cur)
        elif cur is not None and key in (pos, neg) and val.startswith('(') and val.endswith(')'):
            re_im = val.strip('()').split()
            if len(re_im) == 2:
                cur[key] = complex(float(re_im[0]), float(re_im[1]))
    pts = sorted((x for x in pts if x['f'] > 0 and pos in x and neg in x), key=lambda x: x['f'])
    if not pts:
        return None
    vp, vn = pts[0][pos], pts[0][neg]
    return {'v1_pos': abs(vp), 'v1_neg': abs(vn), 'imbalance_pct': 200 * (abs(vp) - abs(vn)) / (abs(vp) + abs(vn)),
            'cm1_v': abs(vp + vn) / 2}


def parse_netlist(run_dir):
    """(top instance name, top master, {subckt: {inst: (master, line)}}) from circuit.scs/tb.scs, or None."""
    circ, tb = Path(run_dir) / 'circuit.scs', Path(run_dir) / 'tb.scs'
    if not circ.is_file() or not tb.is_file():
        return None
    subckts, cur = {}, None
    for raw in circ.read_text(errors='replace').splitlines():
        line = raw.strip()
        m = re.match(r'^subckt\s+(\S+)', line)
        if m:
            cur = subckts.setdefault(m.group(1), {})
        elif re.match(r'^ends\b', line):
            cur = None
        elif cur is not None and line and not line.startswith('//'):
            m = re.match(r'^(\S+)\s+\([^)]*\)\s+(\S+)', line) or re.match(r'^(\S+)\s+(\S+)', line)
            if m:
                cur[m.group(1)] = (m.group(2), line)
    for raw in tb.read_text(errors='replace').splitlines():
        m = re.match(r'^\s*(\S+)\s+\([^)]*\)\s+(\S+)', raw)
        if m and m.group(2) in subckts:
            return m.group(1), m.group(2), subckts
    return None


def role_of(block, master):
    if master and master.startswith('CT_'):
        return 'bank_aux' if 'aux' in master.lower() else 'bank_main'
    if re.match(r'^R[SP]L', block):
        return 'inductor_loss'
    if re.match(r'^L', block):
        return 'inductor'
    if block == 'MB':
        return 'tail'
    if re.match(r'^M[NP]\d*$', block):
        return 'core'
    if re.match(r'^R', block):
        return 'resistor'
    return 'other'


def resolve(name, net):
    """trace name -> (block, device, kind, block master, block netlist line)."""
    parts = name.split('.')
    if net and parts[0] == net[0]:
        scope, path, kinds, bmaster, bline = net[1], [], [], None, ''
        for tok in parts[1:]:
            entry = net[2].get(scope, {}).get(tok)
            path.append(tok)
            kinds.append(tok if len(path) == 1 else re.sub(r'\d+(_\d+)?$', '*', tok))
            if entry is None:
                break
            if len(path) == 1:
                bmaster, bline = entry
            if entry[0] not in net[2]:
                kinds[-1] += '(%s)' % entry[0]
                break
            scope = entry[0]
        return path[0], '.'.join(path), '.'.join(kinds), bmaster, bline
    parts = parts[1:] if len(parts) > 1 else parts
    if len(parts) > 1 and parts[-1] in INTERNAL:
        parts = parts[:-1]
    return parts[0], '.'.join(parts), '.'.join([parts[0]] + [re.sub(r'\d+(_\d+)?$', '*', t) for t in parts[1:]]), None, ''


def locate(path):
    """Return a list of (psf, result_json_or_None, run_dir_or_None, label)."""
    p = Path(path)
    if p.is_dir() and (p / 'task.json').is_file():
        state = json.loads((p / 'task.json').read_text())
        out = []
        for c in state.get('candidates', []):
            for r in c.get('results', []):
                out += [x + (c['id'],) for x in locate(r)]
        return [x[:3] + ('%s %s' % (x[4], x[3]),) for x in out]
    if p.is_file() and p.name.endswith('.json'):
        run = p.parent
        return [(run / 'psf' / PSF_NAME, p, run, str(p))]
    if p.is_dir():
        res = p / 'result.json'
        return [(p / 'psf' / PSF_NAME, res if res.is_file() else None, p, str(p))]
    run = p.parent.parent if p.parent.name == 'psf' else None
    res = run / 'result.json' if run and (run / 'result.json').is_file() else None
    return [(p, res, run, str(p))]


def analyse(psf, result_path, run_dir, focus):
    res = json.loads(Path(result_path).read_text()) if result_path else {}
    if res and res.get('status') != 'measured':
        return {'source': str(result_path), 'skipped': 'status=%s (%s)' % (res.get('status'), res.get('failure_class'))}
    psf = Path(psf)
    if not psf.is_file() and Path(str(psf) + '.gz').is_file():
        psf = Path(str(psf) + '.gz')
    if not psf.is_file():
        return {'source': str(result_path or psf), 'skipped': 'no hbnoise PSF at %s' % psf}
    data = parse_psf(psf)
    if not data['points']:
        return {'source': str(result_path or psf), 'skipped': 'PSF has no relative-frequency points: %s' % psf}
    net = parse_netlist(run_dir) if run_dir else None
    m = res.get('metrics', {})
    amp = m.get('amplitude_diff_peak_v')
    f0 = m.get('frequency_hz') or data['f0']
    out = {'source': str(result_path or psf), 'psf': str(psf), 'f0_hz': f0, 'power_mw': m.get('power_mw'),
           'amp_diff_peak_v': amp, 'controls': res.get('registers') and {k: v.get('value') for k, v in res['registers'].items()},
           'warnings': res.get('warnings', []), 'role_source': 'netlist' if net else 'names-only',
           'balance': balance(run_dir, *data['outputs']) if run_dir else None, 'offsets': {}}
    skipped = set()
    for pt in data['points']:
        o2 = pt['scalar'].get('out', float('nan')) ** 2
        blocks, roles, devices = {}, {}, {}
        tot_sum = fl_sum = 0.0
        for name, vec in pt['vec'].items():
            fields = data['types'].get(data['trace_type'].get(name))
            if not fields or len(fields) != len(vec) or 'total' not in fields:
                skipped.add(name)
                continue
            d = dict(zip(fields, vec))
            tot, fl = d['total'], sum(d.get(k, 0.0) for k in FLICKER_FIELDS)
            block, device, kind, bmaster, bline = resolve(name, net)
            role = role_of(block, bmaster)
            tot_sum += tot
            fl_sum += fl
            for table, key in ((blocks, block), (roles, role), (devices, kind)):
                row = table.setdefault(key, {'psd': 0.0, 'flicker': 0.0})
                row['psd'] += tot
                row['flicker'] += fl
            blocks[block].update(role=role, netlist=bline)

        def rows(table):
            items = sorted(((k, v) for k, v in table.items() if v['psd'] > 0), key=lambda kv: -kv[1]['psd'])
            return [dict(name=k, share=v['psd'] / o2, flicker_share=v['flicker'] / v['psd'],
                         removal_gain_db=(-10 * math.log10(1 - v['psd'] / o2) if v['psd'] < o2 else None),
                         pn_contrib_dbc_hz=(10 * math.log10(2 * v['psd'] / amp ** 2) if amp else None),
                         **{k2: v[k2] for k2 in ('role', 'netlist') if k2 in v}) for k, v in items]
        key = str(int(round(pt['f'])))
        fc = pt['scalar'].get('cornerFreq')
        out['offsets'][key] = {
            'pn_dbc_hz': 10 * math.log10(2 * o2 / amp ** 2) if amp else None,
            'pn_result_json': m.get('pn_%s_dbc_hz' % key), 'fom_db': m.get('fom_%s_db' % key),
            'out2_v2_hz': o2, 'closure': tot_sum / o2, 'flicker_share': fl_sum / o2, 'corner_freq_hz': fc,
            'lorentz_dev_db': 10 * math.log10(1 + (fc / pt['f']) ** 2) if fc else None,
            'roles': rows(roles), 'blocks': rows(blocks), 'devices': rows(devices)}
    offs = sorted(out['offsets'], key=float)
    out['slopes_db_per_dec'] = {'%s-%s' % (a, b): 10 * math.log10(out['offsets'][b]['out2_v2_hz'] / out['offsets'][a]['out2_v2_hz'])
                                / math.log10(float(b) / float(a)) for a, b in zip(offs, offs[1:])}
    if skipped:
        out['unparsed_traces'] = sorted(skipped)[:10]
    out['triage'] = triage(out, focus)
    return out


def triage(r, focus):
    """Class per focus offset: flicker (>=70 % of out^2 is fn), white (<=30 %) or mixed, with the lead role/block."""
    res = {}
    for f in focus:
        o = r['offsets'].get(str(f))
        if not o:
            continue
        if not o['roles']:
            res[str(f)] = {'class': 'unknown', 'flags': ['no noise sources parsed']}
            continue
        fs = o['flicker_share']
        lead = {}
        for kind, w in (('flicker', lambda x: x['share'] * x['flicker_share']), ('white', lambda x: x['share'] * (1 - x['flicker_share']))):
            role, block = max(o['roles'], key=w), max(o['blocks'], key=w)
            lead[kind] = {'role': role['name'], 'block': block['name'], 'block_share': round(w(block), 4)}
        if fs >= 0.7:
            cls = 'flicker/' + lead['flicker']['role']
        elif fs <= 0.3:
            cls = 'white/' + lead['white']['role']
        else:
            cls = 'mixed/%s+%s' % (lead['flicker']['role'], lead['white']['role'])
        flags = []
        if abs(o['closure'] - 1) > 0.01:
            flags.append('closure %.3f: sources missing or unparsed, do not rank causes' % o['closure'])
        if o['lorentz_dev_db'] and o['lorentz_dev_db'] >= 0.1:
            flags.append('near-carrier: Lorentzian deviation %.2f dB (cornerFreq %.3g Hz); rank by 100k/1M'
                         % (o['lorentz_dev_db'], o['corner_freq_hz']))
        bal = r.get('balance')
        if bal and abs(bal['imbalance_pct']) > 5:
            flags.append('single-ended imbalance %+.1f%% (|V1| %s vs %s): asymmetric loading, see bank switch orientation'
                         % (bal['imbalance_pct'], '%.3f' % bal['v1_pos'], '%.3f' % bal['v1_neg']))
        core = [b for b in o['blocks'] if b.get('role') == 'core']
        if len(core) >= 2 and core[0]['share'] > 2 * core[1]['share']:
            flags.append('core pair asymmetric: %s %.1f%% vs %s %.1f%%' % (core[0]['name'], 100 * core[0]['share'],
                                                                         core[1]['name'], 100 * core[1]['share']))
        res[str(f)] = {'class': cls, 'flicker_share': round(fs, 3), 'flicker_lead': lead['flicker'],
                       'white_lead': lead['white'], 'flags': flags}
    return res


def fmt_triage(t):
    if not t:
        return 'n/a'
    if 'flicker_lead' not in t:
        return '%s %s' % (t.get('class'), ' | '.join(t.get('flags', [])))
    return '%s  (flicker %.0f%%: lead %s %.1f%% | white lead %s %.1f%%) %s' % (
        t['class'], 100 * t['flicker_share'], t['flicker_lead']['block'], 100 * t['flicker_lead']['block_share'],
        t['white_lead']['block'], 100 * t['white_lead']['block_share'], ' | '.join(t['flags']))


def fmt_summary(runs, focus):
    s = ['%-34s %-8s %8s %7s %8s %7s %7s  %s' % ('result', 'offset', 'f0(GHz)', 'P(mW)', 'PN', 'FoM', 'flick', 'triage')]
    for r in runs:
        name = '/'.join(Path(r['source']).parts[-3:-1]) if r['source'].endswith('result.json') else Path(r['source']).name
        if 'skipped' in r:
            s.append('%-34s skipped: %s' % (name[-34:], r['skipped']))
            continue
        for f in focus:
            o, t = r['offsets'].get(str(f)), r['triage'].get(str(f))
            if not o:
                continue
            s.append('%-34s %-8s %8.4f %7s %8s %7s %6.0f%%  %s  controls=%s' % (
                name[-34:], f, r['f0_hz'] / 1e9, '%.3f' % r['power_mw'] if r['power_mw'] else '?',
                '%.2f' % o['pn_dbc_hz'] if o['pn_dbc_hz'] is not None else '?', '%.2f' % o['fom_db'] if o['fom_db'] else '?',
                100 * o['flicker_share'], fmt_triage(t), json.dumps(r['controls'])))
    return '\n'.join(s)


def fmt_run(r, focus, top):
    if 'skipped' in r:
        return '== %s\n   skipped: %s' % (r['source'], r['skipped'])
    first = r['offsets'][min(r['offsets'], key=float)]
    s = ['== %s' % r['source'],
         '   f0=%.4f GHz  P=%s mW  amp=%s V  controls=%s  cornerFreq=%s Hz  roles from %s' % (
             r['f0_hz'] / 1e9, '%.3f' % r['power_mw'] if r['power_mw'] else '?',
             '%.3f' % r['amp_diff_peak_v'] if r['amp_diff_peak_v'] else '?', json.dumps(r['controls']),
             '%.3g' % first['corner_freq_hz'] if first['corner_freq_hz'] else '?', r['role_source']),
         '   balance: %s' % ('|V1P|=%.3f |V1N|=%.3f imbalance %+.1f%% |CM1|=%.3f V' % (
             r['balance']['v1_pos'], r['balance']['v1_neg'], r['balance']['imbalance_pct'], r['balance']['cm1_v'])
             if r.get('balance') else 'n/a (no psf/hb.fd.pss_hb)'),
         '   offset      PN      FoM  flicker  closure  Lorentz-dev(dB)']
    for k in sorted(r['offsets'], key=float):
        o = r['offsets'][k]
        s.append('   %-8s %7s %8s  %6.1f%%  %7.4f  %s' % (
            k, '%.2f' % o['pn_dbc_hz'] if o['pn_dbc_hz'] is not None else '?', '%.2f' % o['fom_db'] if o['fom_db'] else '?',
            100 * o['flicker_share'], o['closure'], '%.2f' % o['lorentz_dev_db'] if o['lorentz_dev_db'] is not None else '?'))
    s.append('   slopes dB/dec: ' + '  '.join('%s %.2f' % kv for kv in r['slopes_db_per_dec'].items()))
    for f in focus:
        o = r['offsets'].get(str(f))
        if not o:
            s.append('   @%s: offset not in this PSF' % f)
            continue
        s.append('   @%s roles: ' % f + '  '.join('%s %.1f%%(fn %.0f%%)' % (x['name'], 100 * x['share'], 100 * x['flicker_share'])
                                                for x in o['roles'] if x['share'] >= 0.005))
        for x in o['blocks'][:top]:
            s.append('     %-10s %5.1f%%  fn %3.0f%%  removal<=%s dB  [%s] %s' % (
                x['name'], 100 * x['share'], 100 * x['flicker_share'],
                '%.2f' % x['removal_gain_db'] if x['removal_gain_db'] is not None else 'inf', x['role'], x['netlist'][:90]))
        s.append('     devices: ' + '; '.join('%s %.1f%%' % (x['name'], 100 * x['share']) for x in o['devices'][:top]))
        s.append('   triage @%s: %s' % (f, fmt_triage(r['triage'].get(str(f)))))
    if r.get('warnings'):
        s.append('   result warnings: ' + '; '.join(r['warnings']))
    if r.get('unparsed_traces'):
        s.append('   unparsed traces (first 10): ' + ', '.join(r['unparsed_traces']))
    return '\n'.join(s)


def fmt_compare(runs, focus, top):
    base = runs[0]
    s = ['== compare vs %s  controls=%s' % (base['source'], json.dumps(base['controls']))]
    for r in runs[1:]:
        s.append('-- %s  controls=%s' % (r['source'], json.dumps(r['controls'])))
        if base['f0_hz'] and r['f0_hz']:
            s.append('   f0 %.4f -> %.4f GHz: 20log10(f ratio) = %+.2f dB (PN change expected at constant FoM)' % (
                base['f0_hz'] / 1e9, r['f0_hz'] / 1e9, 20 * math.log10(r['f0_hz'] / base['f0_hz'])))
        for f in focus:
            a, b = base['offsets'].get(str(f)), r['offsets'].get(str(f))
            if not a or not b or a['pn_dbc_hz'] is None or b['pn_dbc_hz'] is None:
                s.append('   @%s: not comparable (offset missing or amplitude unknown)' % f)
                continue
            s.append('   @%s PN %.2f -> %.2f (%+.2f dB)  FoM %s  flicker %.0f%% -> %.0f%%  class %s -> %s' % (
                f, a['pn_dbc_hz'], b['pn_dbc_hz'], b['pn_dbc_hz'] - a['pn_dbc_hz'],
                '%+.2f dB' % (b['fom_db'] - a['fom_db']) if a['fom_db'] and b['fom_db'] else '?',
                100 * a['flicker_share'], 100 * b['flicker_share'],
                base['triage'].get(str(f), {}).get('class'), r['triage'].get(str(f), {}).get('class')))
            for level in ('roles', 'blocks'):
                ra, rb = {x['name']: x for x in a[level]}, {x['name']: x for x in b[level]}
                names = sorted(set(ra) | set(rb), key=lambda n: -max(ra.get(n, {}).get('share', 0), rb.get(n, {}).get('share', 0)))
                for name in names[:top]:
                    pa, pb = ra.get(name, {}).get('pn_contrib_dbc_hz'), rb.get(name, {}).get('pn_contrib_dbc_hz')
                    s.append('     %-5s %-10s %8s -> %8s dBc/Hz  %s   share %4.1f%% -> %4.1f%%' % (
                        level[:-1], name, '%.2f' % pa if pa is not None else '-', '%.2f' % pb if pb is not None else '-',
                        '(%+6.2f)' % (pb - pa) if pa is not None and pb is not None else '        ',
                        100 * ra.get(name, {}).get('share', 0), 100 * rb.get(name, {}).get('share', 0)))
    return '\n'.join(s)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('paths', nargs='+', help='result.json | run dir | hbnoise PSF | task workspace')
    ap.add_argument('--offset', type=int, action='append', help='focus offset(s) in Hz (default: task pn targets, else 1000000)')
    ap.add_argument('--top', type=int, default=5, help='blocks/devices printed per offset (JSON keeps all)')
    ap.add_argument('--compare', action='store_true', help='compare every input against the first one')
    ap.add_argument('--summary', action='store_true', help='one line per result and focus offset (good for a whole task)')
    ap.add_argument('--json', action='store_true', help='full JSON output')
    a = ap.parse_args(argv)
    focus = a.offset
    if not focus:
        for p in a.paths:
            tj = Path(p) / 'task.json'
            if tj.is_file():
                focus = sorted(int(float(k)) for k in json.loads(tj.read_text())['spec'].get('pn_targets_dbc_hz', {}))
        focus = focus or [1000000]
    runs = []
    for p in a.paths:
        for psf, res, run, label in locate(p):
            try:
                r = analyse(psf, res, run, focus)
            except (OSError, ValueError, KeyError, ZeroDivisionError) as exc:
                r = {'source': label, 'skipped': '%s: %s' % (type(exc).__name__, exc)}
            r['label'] = label
            runs.append(r)
    ok = [r for r in runs if 'skipped' not in r]
    if a.json:
        print(json.dumps({'focus_offsets_hz': focus, 'runs': runs}, indent=1, ensure_ascii=False))
    else:
        print(fmt_summary(runs, focus) if a.summary else '\n'.join(fmt_run(r, focus, a.top) for r in runs))
        if a.compare and len(ok) > 1:
            print(fmt_compare(ok, focus, a.top))
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
