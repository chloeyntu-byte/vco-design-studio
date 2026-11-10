"""Phase-noise contributor decomposition from a Spectre psfascii hbnoise result.

The hbnoise PSF lists, for every noisy instance, a STRUCT of noise terms (MOS: rd, rs, rgbi, ...,
id, igs, igd, igb, fn, total; resistor: rn, total; inductor: rn, fn, total) in V^2/Hz at the output.
We read the sweep point nearest the requested offset and group instances by their top-level role.
"""
from __future__ import annotations

import re
from pathlib import Path

_NUM = re.compile(r'^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$')


def _parse(path):
    lines = Path(path).read_text(errors='replace').splitlines()
    section = None
    types = {}
    current_type = None
    depth = 0
    trace = {}
    values = []          # list of (offset, {instance: [numbers] or number})
    point = None
    inst = None
    buf = None
    for line in lines:
        s = line.strip()
        if s in ('HEADER', 'TYPE', 'SWEEP', 'TRACE', 'VALUE', 'END'):
            section = s
            continue
        if section == 'TYPE':
            opens, closes = s.count('('), s.count(')')
            if depth == 0:
                m = re.match(r'^"([^"]+)"\s+STRUCT\($', s)
                current_type = m.group(1) if m else None
                if current_type:
                    types[current_type] = []
            elif current_type and depth == 1:
                fm = re.match(r'^"([^"]+)"\s+FLOAT', s)
                if fm:
                    types[current_type].append(fm.group(1))
            depth += opens - closes
            if depth <= 0:
                depth = 0
                current_type = None
        elif section == 'TRACE':
            m = re.match(r'^"([^"]+)"\s+"([^"]+)"', s)
            if m:
                trace[m.group(1)] = m.group(2)
        elif section == 'VALUE':
            if buf is not None:
                if s == ')':
                    point[inst] = buf
                    buf = None
                elif _NUM.match(s):
                    buf.append(float(s))
                continue
            m = re.match(r'^"([^"]+)"\s+(.*)$', s)
            if not m:
                continue
            key, rest = m.groups()
            if key in ('relative frequency', 'sweep', 'freq'):
                point = {}
                values.append((float(rest), point))
            elif point is not None:
                if rest == '(':
                    inst = key
                    buf = []
                elif _NUM.match(rest):
                    point[key] = float(rest)
    return types, trace, values


def _group(instance):
    name = instance[4:] if instance.startswith('DUT.') else instance
    top = name.split('.')[0]
    if top.startswith(('RSL', 'RPL')) or top.startswith('L'):
        return 'tank_loss(' + top + ')'
    if re.match(r'^MB', top):
        return 'tail(' + top + ')'
    if re.match(r'^M[NP]', top):
        return 'core(' + top + ')'
    if top.startswith('IC'):
        return 'bank(' + top + ')'
    if top.startswith('R'):
        return 'resistor(' + top + ')'
    return top


def contributors(psf_path, offset_hz=1e6, top=8):
    types, trace, values = _parse(psf_path)
    if not values:
        raise ValueError('no noise sweep points in ' + str(psf_path))
    off, point = min(values, key=lambda v: abs(v[0] - offset_hz))
    rows = []
    for inst, vals in point.items():
        if inst not in trace or not types.get(trace[inst]):
            continue   # only device noise STRUCTs (skip out, cornerFreq, ConvIndicator, ...)
        fields = types.get(trace[inst], [])
        if isinstance(vals, list):
            d = dict(zip(fields, vals))
            total = d.get('total', sum(vals))
            fn = d.get('fn', 0.0)
        else:
            total, fn = float(vals), 0.0
        if total > 0:
            rows.append((inst, total, fn))
    grand = sum(t for _, t, _ in rows)
    if grand <= 0:
        raise ValueError('zero total noise at offset')
    groups = {}
    for inst, t, fn in rows:
        g = _group(inst)
        acc = groups.setdefault(g, [0.0, 0.0])
        acc[0] += t
        acc[1] += fn
    out_groups = sorted(({'group': g, 'share': round(v[0] / grand, 4), 'flicker_share': round(v[1] / v[0], 4) if v[0] else 0.0}
                         for g, v in groups.items()), key=lambda x: -x['share'])
    # coarse classes for diagnosis
    classes = {}
    for g in out_groups:
        cls = g['group'].split('(')[0]
        c = classes.setdefault(cls, {'share': 0.0, 'flicker': 0.0})
        c['share'] += g['share']
        c['flicker'] += g['share'] * g['flicker_share']
    classes = {k: {'share': round(v['share'], 4), 'flicker_share': round(v['flicker'] / v['share'], 4) if v['share'] else 0.0}
               for k, v in classes.items()}
    top_inst = sorted(rows, key=lambda r: -r[1])[:top]
    return {'offset_hz': off, 'groups': out_groups[:top], 'classes': classes,
            'top_instances': [{'instance': i, 'share': round(t / grand, 4), 'flicker_share': round(f / t, 4) if t else 0.0} for i, t, f in top_inst]}
