"""Structure selection, seeding and the measured lookup table.

The lookup table is the set of autosize records (data/lookup/<version>/results.jsonl): for each
(pdk, family/variant, f0, power cap) cell, the best design actually simulated, its worst-case PN/FoM
over three codes (min/mid/max), TR, power, and the exact design parameters.

* `select`  ranks families for a spec using the nearest table cells (log-distance in f0 and power);
* `seed`    returns a project for a family whose sizing comes from the nearest measured design,
            rescaled to the requested f0 and power ("tune from experience");
* `table`   prints the table (markdown/json) for a PDK.
Cells that failed are reported as attempted_failed with the reason; missing cells are untested.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path


from .common import ROOT, read_json, write_json

LOOKUP_DIR = ROOT / 'data' / 'lookup'


def _default_results():
    pointer = LOOKUP_DIR / 'current.json'
    if pointer.is_file():
        p = read_json(pointer)
        return [ROOT / x for x in p.get('results', [])]
    return sorted(LOOKUP_DIR.glob('*/results.jsonl'))


def table_results(path=None):
    files = [Path(path)] if path else _default_results()
    rows = []
    for f in files:
        if f.is_file():
            for line in f.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def _usable(r):
    return r.get('status') in ('pass', 'constraint_fail') and r.get('evaluation') and r['evaluation'].get('status') != 'failed'


def _dist(cell, f0_hz, p_mw):
    return abs(math.log(cell['f0_hz'] / f0_hz)) + 0.5 * abs(math.log(cell['p_cap_mw'] / p_mw))


def rank(pdk, f0_hz, p_mw, pn_target_1m=None, results=None, allow_aux=True):
    """Rank families by measured worst-case FoM@1MHz at the nearest table cells for this PDK."""
    from .spec import canonical_pdk
    pdk = canonical_pdk(pdk)
    rows = [r for r in (results if results is not None else table_results()) if r['cell']['pdk'] == pdk]
    fams = {}
    for r in rows:
        key = (r['cell']['family'], r['cell']['variant'])
        fams.setdefault(key, []).append(r)
    out = []
    for (family, variant), recs in fams.items():
        if not allow_aux and family in ('hegazi', 'murphy', 'lim'):
            continue
        near = sorted(recs, key=lambda r: _dist(r['cell'], f0_hz, p_mw))
        best = near[0]
        d = _dist(best['cell'], f0_hz, p_mw)
        ev = best.get('evaluation') or {}
        item = {'family': family, 'variant': variant, 'nearest_cell': best.get('key'), 'cell_distance': round(d, 3),
                'cell_status': best.get('status'), 'reason': best.get('error') or (ev.get('reason') if ev else None)}
        if _usable(best):
            fom = ev['fom_1m_worst']
            # FoM is roughly invariant with f0 and P: predict PN at the requested point
            pred = -(fom - 20 * math.log10(f0_hz / 1e6) + 10 * math.log10(p_mw))
            item.update(fom_worst={o: round(v, 2) for o, v in (ev.get('fom_worst') or {}).items()} or None,
                        gain_lowest_code=(ev.get('gain') or {}).get('gm_rp'), table_ref=best.get('autosize_json'),
                        fom_1m_worst=round(fom, 2), pn_1m_worst_at_cell=round(ev['pn_1m_worst'], 2), tr_pct=round(ev['tr_pct'], 1),
                        p_max_mw=round(ev['p_max_mw'], 3), predicted_pn_1m_at_request=round(pred, 2),
                        margin_db=None if pn_target_1m is None else round(pn_target_1m - pred, 2))
        out.append(item)
    out.sort(key=lambda x: -(x.get('fom_1m_worst') or -1e9))
    return out


def seed(family, variant, pdk, f0_hz, p_mw, results=None):
    """Nearest measured design for this family on this PDK, rescaled to f0 and power.
    Order: lookup table (autosize designs) -> measured evidence points (their generated projects)."""
    from .autosize import _warm_seed
    from .spec import canonical_pdk
    from .catalog import get_variant
    v = get_variant(family, variant)['id']
    cell = {'pdk': canonical_pdk(pdk), 'family': family, 'variant': v, 'f0_hz': f0_hz, 'p_cap_mw': p_mw}
    s = _warm_seed(results if results is not None else table_results(), cell)
    return s or seed_from_evidence(cell)


def seed_from_evidence(cell):
    """Fallback seed from the published measured evidence: the generated project of the nearest point
    (same pdk/family/variant, smallest log-distance in f and P), rescaled like a lookup seed."""
    from .evidence import load_points, default_indexes
    from .site import resolve
    from .autosize import VDC_KNOB
    best = None
    for r in load_points(default_indexes()):
        if r.get('pdk') != cell['pdk'] or r.get('architecture') != cell['family'] or r.get('variant') != cell['variant']:
            continue
        if r.get('status') != 'measured' or not r.get('result_path'):
            continue
        d = abs(math.log(r['frequency_hz'] / cell['f0_hz'])) + 0.5 * abs(math.log(r['power_mw'] / cell['p_cap_mw']))
        if best is None or d < best[0]:
            best = (d, r)
    if best is None:
        return None
    r = best[1]
    try:
        res = read_json(resolve(r['result_path']))
        proj = read_json(resolve(res['receipt']['generated_dir']) / 'project.json')
    except (OSError, ValueError, KeyError):
        return None
    params = {k: v for k, v in proj['parameters'].items() if k not in ('frequency_guess_ghz',)}
    ratio = (r['frequency_hz'] / cell['f0_hz']) ** 2
    for k in ('L_scale',):
        if isinstance(params.get(k), (int, float)):
            params[k] = round(params[k] * ratio ** 0.5, 6)
    for k in ('bank_cap_scale', 'aux_cap_scale'):
        if isinstance(params.get(k), (int, float)):
            params[k] = round(params[k] * ratio ** 0.5, 6)
    if (cell['family'], cell['variant']) not in VDC_KNOB and isinstance(params.get('core_width_scale'), (int, float)):
        params['core_width_scale'] = round(params['core_width_scale'] * cell['p_cap_mw'] / r['power_mw'], 6)
    regs = {k: {'bits': v['bits'], 'value': v['value']} for k, v in proj['registers'].items()}
    out = {'parameters': params, 'registers': regs,
           'source': 'evidence %s (%.3f GHz, %.2f mW, %s)' % (r['id'][:12], r['frequency_hz'] / 1e9, r['power_mw'], r.get('pdk')),
           'overrides': proj.get('overrides'), 'binding': proj.get('binding')}
    return out


def build_table(results, pdk=None):
    rows = [r for r in results if pdk is None or r['cell']['pdk'] == pdk]
    table = {}
    for r in rows:
        c = r['cell']
        ev = r.get('evaluation') or {}
        cellv = {'status': r.get('status'), 'sims': r.get('sims_used')}
        if _usable(r):
            cellv.update(pn_1m=round(ev['pn_1m_worst'], 2), fom_1m=round(ev['fom_1m_worst'], 2), tr_pct=round(ev['tr_pct'], 1),
                         p_max_mw=round(ev['p_max_mw'], 3), center_ghz=round(ev['center_hz'] / 1e9, 4), checks=ev.get('checks'),
                         steady_warnings=ev.get('steady_warnings'))
        else:
            cellv['reason'] = (r.get('error') or ev.get('reason') or r.get('note') or '')[:160]
        table.setdefault(c['pdk'], {}).setdefault('%s/%s' % (c['family'], c['variant']), {})['%gG/%gmW' % (c['f0_hz'] / 1e9, c['p_cap_mw'])] = cellv
    return table


OFFSETS = ('10000', '100000', '1000000', '10000000')


def _table_note():
    pointer = LOOKUP_DIR / 'current.json'
    return read_json(pointer).get('note', '') if pointer.is_file() else ''


def table_rows(results, pdk=None):
    """One flat row per cell: band, power, worst PN/FoM at the four offsets over the evaluated codes (min/mid/max),
    startup gain at the lowest-frequency code, swing, core devices, and references to the raw results."""
    out = []
    for r in results:
        c = r['cell']
        if pdk and c['pdk'] != pdk:
            continue
        ev = r.get('evaluation') or {}
        g = ev.get('gain') or {}
        sw = ev.get('swing') or {}
        ds = r.get('design_summary') or {}
        core = ds.get('core_devices') or []
        row = {'key': r.get('key'), 'pdk': c['pdk'], 'family': c['family'], 'variant': c['variant'],
               'f0_ghz': c['f0_hz'] / 1e9, 'p_cap_mw': c['p_cap_mw'], 'tr_min_pct': 100 * c['tr_min'] if c.get('tr_min') is not None else None,
               'q': c.get('q_target'), 'status': r.get('status'), 'chosen_variant': ev.get('variant'),
               'reason': None if _usable(r) else (r.get('error') or ev.get('reason') or r.get('note') or '')[:200]}
        if _usable(r):
            row.update({'f_lo_ghz': ev['f_lo_hz'] / 1e9, 'f_hi_ghz': ev['f_hi_hz'] / 1e9, 'tr_pct': ev['tr_pct'], 'p_max_mw': ev['p_max_mw'],
                        'codes': ev.get('codes_evaluated') or [x['code'] for x in ev.get('rows', [])],
                        'checks': ev.get('checks')})
            for o in OFFSETS:
                row['pn_worst_%s' % o] = (ev.get('pn_worst') or {}).get(o)
                row['fom_worst_%s' % o] = (ev.get('fom_worst') or {}).get(o) if ev.get('fom_worst') else (
                    min(x['fom'][o] for x in ev['rows']) if ev.get('rows') else None)
            gvalid = bool(g) and not g.get('error') and g.get('valid') is not False
            row.update({'gain_gm_rp_lowest_code': g.get('gm_rp') if gvalid else None, 'gain_margin_lowest_code': g.get('margin') if gvalid else None,
                        'rp_lowest_code_ohm': g.get('rp_ohm') if gvalid else None, 'gain_valid': gvalid if g else None,
                        'gain_note': None if gvalid else (g.get('error') or g.get('warning') or ('loopgain valid=false (merge mismatch)' if g else 'not measured')),
                        'gain_class': g.get('class') if gvalid else 'invalid' if g else None, 'swing_max_v': sw.get('max_amp_v'), 'swing_min_v': sw.get('min_amp_v'),
                        'swing_limit_v': sw.get('limit_v'), 'core_type': sw.get('core_type')})
        seen, uniq = set(), []
        for d in core:            # one entry per distinct device (type + size): a pair shows once, Lim MP and MN both
            k = (d['type'], d['w_finger_um'], d['nf'], d['m'], d['l_um'])
            if k not in seen:
                seen.add(k)
                uniq.append(d)
        row['core'] = '; '.join('%s %s W=%gum(%gx%d,m%d) L=%gum' % (d['name'], d['type'], d['w_total_um'], d['w_finger_um'], d['nf'], d['m'], d['l_um'])
                                for d in uniq) or None
        row['bias'] = '; '.join('%s W=%gum L=%gum' % (d['name'], d['w_total_um'], d['l_um']) for d in ds.get('bias_devices') or []) or None
        row['inductors_nh'] = ', '.join('%s=%g' % (d['name'], d['l_nh']) for d in ds.get('inductors') or []) or None
        row['vdc'] = (ds.get('parameters') or {}).get('vdc')
        row['main_bits'] = ((ds.get('registers') or {}).get('main') or {}).get('bits')
        row['design_summary'] = ds or None
        row['refs'] = {'autosize': r.get('autosize_json'), 'results': [x.get('result') for x in ev.get('rows', [])],
                       'loopgain': g.get('path'), 'generated': ds.get('generated_dir')}
        out.append(row)
    return out


def table_csv(rows):
    import csv
    import io
    cols = ['key', 'pdk', 'family', 'variant', 'f0_ghz', 'p_cap_mw', 'tr_min_pct', 'q', 'status', 'chosen_variant', 'f_lo_ghz', 'f_hi_ghz', 'tr_pct',
            'p_max_mw'] + ['fom_worst_%s' % o for o in OFFSETS] + ['pn_worst_%s' % o for o in OFFSETS] + [
            'gain_gm_rp_lowest_code', 'gain_margin_lowest_code', 'rp_lowest_code_ohm', 'gain_class', 'gain_valid', 'gain_note', 'swing_max_v', 'swing_min_v', 'swing_limit_v',
            'core_type', 'core', 'bias', 'inductors_nh', 'vdc', 'main_bits', 'reason']
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction='ignore')
    w.writeheader()
    for r in rows:
        w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items() if k in cols})
    return buf.getvalue()


def table_markdown_full(rows):
    lines = ['| cell | status | band GHz (TR %) | P mW | FoM worst 10k/100k/1M/10M dB | gm·Rp (margin) @ lowest code | swing min–max/limit V | core | L nH |',
             '|---|---|---|---|---|---|---|---|---|']
    for r in sorted(rows, key=lambda x: (x['pdk'], x['family'], x['f0_ghz'], x['p_cap_mw'])):
        cell = '%s %s/%s %gG %gmW' % (r['pdk'], r['family'], r['variant'], r['f0_ghz'], r['p_cap_mw'])
        if r.get('f_lo_ghz') is None:
            lines.append('| %s | %s | %s | | | | | | |' % (cell, r['status'], (r.get('reason') or '')[:60]))
            continue
        fom = '/'.join('%.1f' % r['fom_worst_%s' % o] if r.get('fom_worst_%s' % o) is not None else '-' for o in OFFSETS)
        gain = ('%.2f (%.2f)' % (r['gain_gm_rp_lowest_code'], r['gain_margin_lowest_code'])) if r.get('gain_gm_rp_lowest_code') else '-'
        sw = ('%.2f–%.2f/%.1f%s' % (r['swing_min_v'], r['swing_max_v'], r['swing_limit_v'], ' OVER' if r['swing_max_v'] > r['swing_limit_v'] else '')) if r.get('swing_limit_v') else '-'
        lines.append('| %s | %s | %.2f–%.2f (%.0f) | %.2f | %s | %s | %s | %s | %s |' % (
            cell, r['status'], r['f_lo_ghz'], r['f_hi_ghz'], r['tr_pct'], r['p_max_mw'], fom, gain, sw, r.get('core') or '', r.get('inductors_nh') or ''))
    return '\n'.join(lines)


def table_markdown(table):
    lines = []
    for pdk, fams in sorted(table.items()):
        cols = sorted({k for f in fams.values() for k in f}, key=lambda s: (float(s.split('G/')[0]), float(s.split('/')[1][:-2])))
        lines.append('### %s — worst PN@1MHz (dBc/Hz) / FoM (dB) / TR %% over codes min/mid/max' % pdk)
        lines.append('| family | ' + ' | '.join(cols) + ' |')
        lines.append('|---|' + '---|' * len(cols))
        for fam, cells in sorted(fams.items()):
            vals = []
            for col in cols:
                v = cells.get(col)
                if not v:
                    vals.append('untested')
                elif 'pn_1m' in v:
                    mark = '' if v['status'] == 'pass' else ' (' + ','.join(k for k, ok in (v.get('checks') or {}).items() if not ok) + ')'
                    vals.append('%.1f / %.1f / %.0f%%%s' % (v['pn_1m'], v['fom_1m'], v['tr_pct'], mark))
                else:
                    vals.append('failed: ' + (v.get('reason') or v['status'])[:40])
            lines.append('| %s | %s |' % (fam, ' | '.join(vals)))
        lines.append('')
    return '\n'.join(lines)


# ------------------------------------------------------------------------------------------ CLI
def cli_register(sub):
    c = sub.add_parser('select', help='rank structures for a spec from the measured lookup table (+ evidence)',
                       description='rank structures for a spec from the measured lookup table (+ evidence)')
    c.add_argument('--spec')
    c.add_argument('--task', help='task workspace (uses its spec, records a consult receipt)')
    c.add_argument('--pdk')
    c.add_argument('--frequency-ghz', type=float)
    c.add_argument('--power-mw', type=float)
    c.add_argument('--pn-1m', type=float, help='PN@1MHz target (dBc/Hz) to compute margins')
    c = sub.add_parser('seed', help='project for a family sized from the nearest measured design (rescaled to f0/power)',
                       description='project for a family sized from the nearest measured design (rescaled to f0/power)')
    c.add_argument('--family', required=True)
    c.add_argument('--variant')
    c.add_argument('--spec')
    c.add_argument('--task')
    c.add_argument('--output', required=True)
    c = sub.add_parser('table', help='print the measured lookup table',
                       description='print the measured lookup table')
    c.add_argument('--pdk')
    c.add_argument('--results', help='results.jsonl (default: shipped table)')
    c.add_argument('--format', choices=['markdown', 'json', 'rows', 'csv', 'full'], default='markdown',
                   help='markdown: compact grid; full: one row per cell with 4-offset FoM, gain, core devices; rows: JSON rows; csv')
    c.add_argument('--output')


def _spec_from(args):
    from .spec import validate
    if getattr(args, 'task', None):
        return read_json(Path(args.task) / 'task.json')['spec']
    if getattr(args, 'spec', None):
        return validate(read_json(args.spec))[0]
    return None


def cli_dispatch(args):
    from .cli import EXIT_OK
    if args.command == 'select':
        spec = _spec_from(args)
        if spec:
            lo, hi = spec['frequency_band_hz']
            pdk, f0, p = spec['pdk'], (lo + hi) / 2, spec['power_max_mw']
            pn = (spec.get('pn_targets_dbc_hz') or {}).get('1000000')
        else:
            if not (args.pdk and args.frequency_ghz and args.power_mw):
                raise ValueError('give --spec/--task or --pdk --frequency-ghz --power-mw')
            pdk, f0, p, pn = args.pdk, args.frequency_ghz * 1e9, args.power_mw, args.pn_1m
        ranking = rank(pdk, f0, p, pn, allow_aux=(spec or {}).get('allow_aux', True))
        from .evidence import query, default_indexes, compact
        ev = compact(query(default_indexes(), pdk=pdk, frequency_hz=f0, power_mw=p, temp_c=27))
        out = {'request': {'pdk': pdk, 'f0_ghz': f0 / 1e9, 'power_mw': p, 'pn_1m_target': pn},
               'lookup_ranking': ranking,
               'evidence_nearby': {'status': ev['status'], 'best_per_architecture': ev['best_per_architecture'], 'near_misses': ev['near_misses'][:5]},
               'how_to_use': 'Shortlist 1-2 families with the best measured FoM and positive margin; seed them (launch seed) and measure. '
                             'A negative margin means the target is beyond the best design found so far for that family at this cell: '
                             'pick a stronger family or relax the spec. Table conditions: ' + _table_note()}
        if getattr(args, 'task', None):
            from .knowledge import consult
            out['knowledge'] = consult(args.task, 'selection', {'pdk': pdk, 'frequency_ghz': f0 / 1e9, 'power_mw': p, 'stage': 'selection'})['cards']
        return out, EXIT_OK
    if args.command == 'seed':
        spec = _spec_from(args)
        if not spec:
            raise ValueError('seed needs --spec or --task')
        lo, hi = spec['frequency_band_hz']
        s = seed(args.family, args.variant, spec['pdk'], (lo + hi) / 2, spec['power_max_mw'])
        from .catalog import default_project
        project = default_project(args.family, args.variant, spec['pdk'], spec=spec)
        if s:
            project['parameters'].update({k: v for k, v in s['parameters'].items() if k not in ('vdd', 'temp_c', 'frequency_guess_ghz')})
            for k, v in s.get('registers', {}).items():
                if k in project['registers']:
                    project['registers'][k].update(v)
            if s.get('overrides'):
                project['overrides'] = s['overrides']
                project['seed']['overrides_unscaled'] = sorted(s['overrides'])
                project['seed']['note'] += '; device overrides copied WITHOUT scaling (%d instances): check them for the new f0/power' % len(s['overrides'])
            from .autosize import VDC_KNOB
            if (args.family, project['variant']) in VDC_KNOB:
                project['seed']['note'] += '; vdc (tail/gate bias) copied from the source design: retune it for the power target (power is nonlinear in vdc)'
            project['seed'] = {'source_cell': s['source'], 'note': 'L/C rescaled by (f_cell/f0)^2 split evenly; power knob scaled for voltage-biased cores; measure and re-center'}
        else:
            project['seed'] = {'source_cell': None, 'note': 'no measured design for this family/PDK yet; template sizing'}
        from .autosize import _legalize, _legal
        fixes = _legalize(project)
        if fixes:
            project['seed']['legalized'] = fixes
        if not _legal(project):
            project['seed']['warning'] = 'seed is outside PDK geometry limits after rescaling; reduce main bits (--set main.bits=N) or use launch autosize'
        if Path(args.output).exists():
            raise FileExistsError('Project already exists: ' + args.output)
        write_json(args.output, project)
        return project, EXIT_OK
    if args.command == 'table':
        from .spec import canonical_pdk
        res = table_results(args.results)
        pdk = canonical_pdk(args.pdk) if args.pdk else None
        if args.format in ('markdown', 'json'):
            t = build_table(res, pdk)
            out = table_markdown(t) if args.format == 'markdown' else t
        else:
            rows = table_rows(res, pdk)
            out = table_csv(rows) if args.format == 'csv' else table_markdown_full(rows) if args.format == 'full' else rows
        if getattr(args, 'output', None):
            Path(args.output).write_text(out if isinstance(out, str) else json.dumps(out, indent=1, ensure_ascii=False))
        return out, EXIT_OK
    return None
