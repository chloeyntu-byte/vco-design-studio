"""Schematic packaging: hierarchy, symbols, placement, wires, preview and checks.

The electrical graph used for simulation is never modified.  A per-variant
packaging spec (data/schematic/<family>/<variant>.json) groups top-level
primitives into reusable modules (inductor, gm core, bias, arrays), and gives
the top-level placement and wiring.  `package()` returns an ordinary graph
(subcircuits + instances + ports) whose flattened netlist is proven identical
to the electrical graph by `equivalence()`.  `layout()` attaches symbol
shapes, instance placements and wires to every cell; `connectivity_check()`
extracts nets from that geometry with the same rules Virtuoso applies, so a
plan can be verified before any OA is written.  Native export consumes the
same layout, so preview, check and OA are one description.
"""
from __future__ import annotations
import copy
import json
import math
import re
from pathlib import Path

from .common import ROOT, read_json

GRID = 0.0625
SPEC_DIR = ROOT / 'data' / 'schematic'
MASTER_SYMBOLS = SPEC_DIR / 'master_symbols.json'

ORIENTS = ('R0', 'R90', 'R180', 'R270', 'MX', 'MY', 'MXR90', 'MYR90')


def snap(v):
    return round(round(float(v) / GRID) * GRID, 6)


def orient_point(orient, x, y):
    """Cadence orientation applied to a master-relative point (before translation)."""
    if orient == 'R0':
        return x, y
    if orient == 'R90':
        return -y, x
    if orient == 'R180':
        return -x, -y
    if orient == 'R270':
        return y, -x
    if orient == 'MX':
        return x, -y
    if orient == 'MY':
        return -x, y
    if orient == 'MXR90':   # mirror about x, then rotate 90
        return y, x
    if orient == 'MYR90':   # mirror about y, then rotate 90
        return -y, -x
    raise ValueError('Invalid orientation ' + str(orient))


def place_point(inst, x, y):
    ox, oy = orient_point(inst.get('orient', 'R0'), x, y)
    return round(inst['xy'][0] + ox, 6), round(inst['xy'][1] + oy, 6)


# ---------------------------------------------------------------- spec / packaging

def pack_dirs():
    """Structure-pack folders, searched in order: $VCO_STUDIO_SCHEMATIC_DIR (os.pathsep list), data/schematic."""
    import os
    dirs = [Path(p).expanduser() for p in os.environ.get('VCO_STUDIO_SCHEMATIC_DIR', '').split(os.pathsep) if p]
    return dirs + [SPEC_DIR]


def pack_file(family, variant, suffix='.json'):
    for d in pack_dirs():
        p = d / family / (variant + suffix)
        if p.is_file():
            return p
    return None


def spec_path(family, variant):
    return pack_file(family, variant) or SPEC_DIR / family / (variant + '.json')


def auto_spec(graph):
    """Packaging derived from the graph when no structure pack exists: every magnetic
    structure (windings joined by couplings, with their RSL/RPL loss) becomes an inductor
    cell, every cross-coupled transistor pair a gm cell; the top level is laid out
    automatically with label stubs."""
    insts = graph['instances']
    inds = [i['name'] for i in insts if i['role'] == 'ind']
    parent = {n: n for n in inds}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    for i in insts:
        if i['role'] == 'mutual':
            a, b = i['parameters']['ind1'], i['parameters']['ind2']
            if a in parent and b in parent:
                parent[find(a)] = find(b)
    groups = {}
    for n in inds:
        groups.setdefault(find(n), []).append(n)
    names = {i['name'] for i in insts}
    modules = []
    for k, (root, members) in enumerate(sorted(groups.items(), key=lambda kv: sorted(kv[1])[0])):
        mem = []
        for w in sorted(members):
            mem += [x for x in ('RSL' + w[1:], w, 'RPL' + w[1:]) if x in names]
        mem += [i['name'] for i in insts if i['role'] == 'mutual' and i['parameters']['ind1'] in members]
        tag = str(k + 1) if len(groups) > 1 else ''
        modules.append({'name': 'L' + ('_' + tag if tag else '_IND'), 'cell': 'IND' + ('_' + tag if tag else ''),
                        'kind': 'inductor', 'members': mem, 'ports': None})
    mos = [i for i in insts if i['role'] in ('nmos', 'pmos')]
    used = set()
    for a in mos:
        for b in mos:
            if a is b or a['name'] in used or b['name'] in used or a['role'] != b['role']:
                continue
            if a['nodes']['g'] == b['nodes']['d'] and b['nodes']['g'] == a['nodes']['d']:
                used |= {a['name'], b['name']}
                cell = 'GM_' + a['role'][0].upper()
                modules.append({'name': cell, 'cell': cell, 'kind': 'gm', 'members': sorted([a['name'], b['name']]), 'ports': None})
    return {'id': 'auto', 'modules': modules, 'rename': {}, 'symbols': {}}


def load_spec(family, variant, graph=None):
    path = pack_file(family, variant)
    if path is not None:
        return read_json(path)
    if graph is None:
        from .catalog import get_variant
        graph = get_variant(family, variant)['graph']
    return auto_spec(graph)


def _nets_of(inst):
    return set(str(v) for v in inst.get('nodes', {}).values())


def package(graph, spec):
    """Return a new hierarchical graph; electrical graph is not modified.

    spec['modules'] = [{name, cell, members:[top instance names], ports:{port: top_net}|None,
                        port_order:[...]}]
    spec['rename'] = {old top instance name: new name}  (applies to top-level instances
                      that stay at top, e.g. ICT -> CT_MAIN)
    spec['cells'] = {graph subcircuit name: schematic cell suffix}
    """
    g = copy.deepcopy(graph)
    top_ports = {p['name'] for p in g['ports']}
    byname = {i['name']: i for i in g['instances']}
    assigned = {}
    modules = []
    for mod in spec.get('modules', []):
        members = mod['members']
        for m in members:
            if m not in byname:
                raise ValueError('Packaging module %s: unknown member %s' % (mod['name'], m))
            if m in assigned:
                raise ValueError('Instance %s packaged twice (%s, %s)' % (m, assigned[m], mod['name']))
            assigned[m] = mod['name']
        inner = [byname[m] for m in members]
        # magnetic coupling must stay inside one cell
        names = set(members)
        for inst in g['instances']:
            if inst['role'] == 'mutual':
                refs = {inst['parameters']['ind1'], inst['parameters']['ind2']}
                if refs & names and not (refs <= names and inst['name'] in names):
                    raise ValueError('Mutual %s crosses the boundary of module %s' % (inst['name'], mod['name']))
        inner_nets = set().union(*[_nets_of(i) for i in inner]) if inner else set()
        outer_nets = set()
        for inst in g['instances']:
            if inst['name'] not in names:
                outer_nets |= _nets_of(inst)
        external = sorted(n for n in inner_nets if n in outer_nets or n in top_ports)
        ports = mod.get('ports')
        if ports is None:
            ports = {n: n for n in external}
        bound = set(ports.values())
        if not (set(external) <= bound <= inner_nets):
            raise ValueError('Module %s ports %s must bind every external net %s (extra ports may expose internal nets only)' % (mod['name'], sorted(bound), external))
        if len(bound) != len(ports):
            raise ValueError('Module %s binds one net to two ports' % mod['name'])
        order = mod.get('port_order') or sorted(ports)
        if sorted(order) != sorted(ports):
            raise ValueError('Module %s port_order differs from ports' % mod['name'])
        inv = {net: port for port, net in ports.items()}
        sub_instances = []
        for inst in inner:
            item = copy.deepcopy(inst)
            item['nodes'] = {k: inv.get(str(v), str(v)) for k, v in inst['nodes'].items()}
            sub_instances.append(item)
        sub = {'name': mod['cell'], 'ports': [{'name': p, 'direction': 'inputOutput'} for p in order],
               'instances': sub_instances, 'parameters': {}, 'kind': mod.get('kind', 'module')}
        modules.append(sub)
        g['subcircuits'].append(sub)
    new_top = []
    placed = set()
    for inst in g['instances']:
        owner = assigned.get(inst['name'])
        if owner is None:
            item = copy.deepcopy(inst)
            item['name'] = spec.get('rename', {}).get(inst['name'], inst['name'])
            new_top.append(item)
        elif owner not in placed:
            mod = next(m for m in spec['modules'] if m['name'] == owner)
            ports = next(s for s in modules if s['name'] == mod['cell'])['ports']
            pmap = mod.get('ports')
            if pmap is None:
                pmap = {p['name']: p['name'] for p in ports}
            new_top.append({'name': mod['name'], 'role': 'subckt', 'master': mod['cell'],
                            'nodes': {p['name']: pmap[p['name']] for p in ports}, 'parameters': {}})
            placed.add(owner)
    names = [i['name'] for i in new_top]
    if len(names) != len(set(names)):
        raise ValueError('Duplicate top-level names after packaging: ' + str(names))
    g['instances'] = new_top
    # rename subcircuit instances (array units, inverters) for readable paths
    for sub in g['subcircuits']:
        ren = spec.get('rename_in', {}).get(sub['name'], {})
        for inst in sub['instances']:
            if inst['name'] in ren:
                inst['name'] = ren[inst['name']]
    g['packaging'] = {'spec': spec.get('id'), 'modules': [m['name'] for m in spec.get('modules', [])]}
    return g


class _Unresolved(str):
    """Marker for a parameter expression that references an undefined name."""


_SUFFIX = {'T': 1e12, 'G': 1e9, 'Meg': 1e6, 'M': 1e6, 'K': 1e3, 'k': 1e3, 'm': 1e-3, 'u': 1e-6,
           'n': 1e-9, 'p': 1e-12, 'f': 1e-15, 'a': 1e-18}
_NUM_SUFFIX = re.compile(r'(?<![A-Za-z_\d.])((?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)(Meg|[TGMKkmunpfa])(?![A-Za-z_\d])')


def _expr_text(value):
    """Expand SPICE scale suffixes inside an expression (2k -> 2000.0)."""
    return _NUM_SUFFIX.sub(lambda m: repr(float(m.group(1)) * _SUFFIX[m.group(2)]), str(value).strip())


def _names(text):
    import ast
    try:
        return {n.id for n in ast.walk(ast.parse(text, mode='eval')) if isinstance(n, ast.Name)}
    except SyntaxError:
        return set()


def _evaluate(value, env):
    from .common import evaluate_expression
    if isinstance(value, (int, float)):
        return float(value)
    text = _expr_text(value)
    return float(evaluate_expression(text, {k: v for k, v in env.items() if not isinstance(v, _Unresolved)}))


def _resolve_env(raw, outer):
    """Resolve one parameter scope. Local names shadow the enclosing scope, values may
    reference each other in any order; a value is evaluated only after every local name it
    uses is resolved. Unresolvable or cyclic values become _Unresolved."""
    env = {k: v for k, v in outer.items() if k not in raw}
    pending = dict(raw)
    while pending:
        progress = False
        for k, v in list(pending.items()):
            if _names(_expr_text(v)) & (set(pending) - {k}) if not isinstance(v, (int, float)) else False:
                continue
            try:
                env[k] = _evaluate(v, env)
            except Exception:  # noqa: BLE001 - undefined name or self reference
                env[k] = _Unresolved('unresolved:' + str(v))
            del pending[k]
            progress = True
        if not progress:
            for k, v in pending.items():
                env[k] = _Unresolved('unresolved:' + str(v))
            break
    return env


def _eval_param(value, env):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return _evaluate(value, env)
    except Exception:  # noqa: BLE001
        try:
            float(str(value))
        except ValueError:
            return _Unresolved('unresolved:' + str(value))
        return value


def flatten(graph):
    """Flatten to {path: {role, params, nodes, ideal}} with effective parameter values.

    Internal nets are prefixed by their instance path, so a net that should be connected
    through a port but is not stays distinct. Parameter scopes follow Spectre: top-level
    parameters are global; a subcircuit sees its own defaults (which may use globals)
    overridden by the calling instance's values (evaluated in the caller's scope)."""
    subs = {s['name']: s for s in graph.get('subcircuits', [])}
    out = {}
    top_env = _resolve_env(graph.get('parameters') or {}, {})

    def walk(g, prefix, netmap, env):
        for inst in g['instances']:
            nodes = {}
            for term, net in inst.get('nodes', {}).items():
                net = str(net)
                nodes[term] = netmap.get(net, prefix + net)
            if inst['role'] == 'subckt':
                child = subs[inst['master']]
                overrides = {k: _eval_param(v, env) for k, v in (inst.get('parameters') or {}).items()}
                defaults = {k: v for k, v in (child.get('parameters') or {}).items() if k not in overrides}
                cenv = _resolve_env(defaults, dict(top_env, **overrides))
                walk(child, prefix + inst['name'] + '.', {p['name']: nodes[p['name']] for p in child['ports']}, cenv)
            else:
                params = {k: (_eval_param(v, env) if k not in ('ind1', 'ind2') else v) for k, v in inst.get('parameters', {}).items()}
                if inst['role'] == 'mutual':
                    params['ind1'] = prefix + params['ind1']
                    params['ind2'] = prefix + params['ind2']
                out[prefix + inst['name']] = {'role': inst['role'], 'params': params, 'nodes': nodes,
                                              'ideal': bool(inst.get('ideal'))}
    walk(graph, '', {}, top_env)
    return out


def _path_normalizer(packaged, spec):
    """Map a packaged device path back to its electrical-graph path (cell-aware renames)."""
    mods = {m['name'] for m in spec.get('modules', [])}
    rename = {v: k for k, v in spec.get('rename', {}).items()}
    inner = {cell: {v: k for k, v in ren.items()} for cell, ren in spec.get('rename_in', {}).items()}
    subs = {s['name']: s for s in packaged.get('subcircuits', [])}

    def norm(path):
        parts = path.split('.')
        res = []
        g = packaged
        for i, part in enumerate(parts):
            inst = next((x for x in g['instances'] if x['name'] == part), None)
            cell = g['name']
            if i == 0 and part in mods:
                g = subs[inst['master']] if inst else g
                continue
            name = rename.get(part, part) if i == 0 else inner.get(cell, {}).get(part, part)
            res.append(name)
            if inst is not None and inst['role'] == 'subckt':
                g = subs[inst['master']]
        return '.'.join(res)
    return norm


def equivalence(electrical, packaged, spec):
    """Prove packaged == electrical: same devices (after path normalisation) with the same
    effective parameters, the same partition of device terminals into nets, and the same
    terminal set on every top-level port net."""
    norm = _path_normalizer(packaged, spec)
    a = flatten(electrical)
    raw = flatten(packaged)
    b = {}
    for path, dev in raw.items():
        d = copy.deepcopy(dev)
        if d['role'] == 'mutual':
            d['params']['ind1'] = norm(d['params']['ind1'])
            d['params']['ind2'] = norm(d['params']['ind2'])
        b[norm(path)] = (d, path)
    missing = sorted(set(a) - set(b))
    extra = sorted(set(b) - set(a))
    diff = []
    unresolved = sorted(p for p, d in list(a.items()) + [(norm(k), v) for k, v in raw.items()]
                        if any(isinstance(v, _Unresolved) for v in d['params'].values()))
    for p in sorted(set(a) & set(b)):
        da, db = a[p], b[p][0]
        if (da['role'], da['params'], da['ideal'], sorted(da['nodes'])) != (db['role'], db['params'], db['ideal'], sorted(db['nodes'])):
            diff.append({'path': p, 'electrical': da, 'packaged': db})

    def partition(devs, key):
        nets = {}
        for path, dev in devs.items():
            for t, n in dev['nodes'].items():
                nets.setdefault(n, set()).add((key(path), t))
        return nets
    na = partition(a, lambda p: p)
    nb = partition(raw, norm)
    pa = {frozenset(v) for v in na.values()}
    pb = {frozenset(v) for v in nb.values()}
    split_or_merged = [sorted(x)[:4] for x in (pa ^ pb)][:6]
    ports = sorted({p['name'] for p in electrical.get('ports', [])} ^ {p['name'] for p in packaged.get('ports', [])})
    for port in electrical.get('ports', []):
        n = port['name']
        if na.get(n, set()) != nb.get(n, set()):
            ports.append(n)
    ok = not missing and not extra and not diff and not split_or_merged and not ports and not unresolved
    return {'equivalent': ok, 'devices': len(a), 'nets': len(na), 'missing': missing[:10], 'extra': extra[:10],
            'unresolved_parameters': unresolved[:10],
            'different': diff[:5], 'net_partition_differences': split_or_merged, 'port_differences': ports}


# ---------------------------------------------------------------- symbols

def _lbl(x, y, text, h=0.0625, just='centerCenter', layer='device'):
    return {'t': 'label', 'xy': [x, y], 'text': text, 'h': h, 'just': just, 'layer': layer}


def _octagon(cx, cy, rx, ry, cut, gap=None, side='bottom'):
    """Open octagon outline (list of points) with an opening `gap` centred on `side`."""
    c = cut
    pts = [(cx - rx + c, cy + ry), (cx + rx - c, cy + ry), (cx + rx, cy + ry - c), (cx + rx, cy - ry + c),
           (cx + rx - c, cy - ry), (cx - rx + c, cy - ry), (cx - rx, cy - ry + c), (cx - rx, cy + ry - c)]
    if not gap:
        return [list(p) for p in pts + [pts[0]]]
    if side == 'bottom':
        # start at right edge of opening, go counter-clockwise around to left edge
        seq = [(cx + gap, cy - ry), (cx + rx - c, cy - ry), (cx + rx, cy - ry + c), (cx + rx, cy + ry - c),
               (cx + rx - c, cy + ry), (cx - rx + c, cy + ry), (cx - rx, cy + ry - c), (cx - rx, cy - ry + c),
               (cx - rx + c, cy - ry), (cx - gap, cy - ry)]
    else:  # top opening
        seq = [(cx - gap, cy + ry), (cx - rx + c, cy + ry), (cx - rx, cy + ry - c), (cx - rx, cy - ry + c),
               (cx - rx + c, cy - ry), (cx + rx - c, cy - ry), (cx + rx, cy - ry + c), (cx + rx, cy + ry - c),
               (cx + rx - c, cy + ry), (cx + gap, cy + ry)]
    return [list(p) for p in seq]


def symbol_box(name, pins, title=None, w=None, h=None, glyph=None):
    """Generic rectangular module symbol. pins: {name: (side, slot)} side in L/R/T/B."""
    sides = {'L': [], 'R': [], 'T': [], 'B': []}
    for p, (side, slot) in pins.items():
        sides[side].append((slot, p))
    n_lr = max([len(sides['L']), len(sides['R']), 2])
    n_tb = max([len(sides['T']), len(sides['B']), 2])
    w = w or max(1.0, (n_tb + 1) * 0.25)
    h = h or max(0.75, (n_lr + 1) * 0.25)
    w, h = snap(w), snap(h)
    shapes = [{'t': 'rect', 'box': [0, -h / 2, w, h / 2], 'layer': 'device'}]
    out = {}
    for side, items in sides.items():
        for slot, p in sorted(items):
            if side == 'L':
                xy = (-0.25, snap(h / 2 - 0.25 * (slot + 1)))
                shapes.append({'t': 'line', 'pts': [[-0.25, xy[1]], [0, xy[1]]], 'layer': 'device'})
                shapes.append(_lbl(0.05, xy[1], p, 0.05, 'centerLeft', 'pin'))
            elif side == 'R':
                xy = (w + 0.25, snap(h / 2 - 0.25 * (slot + 1)))
                shapes.append({'t': 'line', 'pts': [[w, xy[1]], [w + 0.25, xy[1]]], 'layer': 'device'})
                shapes.append(_lbl(w - 0.05, xy[1], p, 0.05, 'centerRight', 'pin'))
            elif side == 'T':
                xy = (snap(0.25 * (slot + 1)), h / 2 + 0.25)
                shapes.append({'t': 'line', 'pts': [[xy[0], h / 2], [xy[0], h / 2 + 0.25]], 'layer': 'device'})
                shapes.append(_lbl(xy[0], h / 2 - 0.06, p, 0.05, 'upperCenter', 'pin'))
            else:
                xy = (snap(0.25 * (slot + 1)), -h / 2 - 0.25)
                shapes.append({'t': 'line', 'pts': [[xy[0], -h / 2], [xy[0], -h / 2 - 0.25]], 'layer': 'device'})
                shapes.append(_lbl(xy[0], -h / 2 + 0.06, p, 0.05, 'lowerCenter', 'pin'))
            out[p] = [snap(xy[0]), snap(xy[1])]
    if glyph:
        shapes.extend(glyph)
    shapes.append(_lbl(w / 2, h / 2 + 0.12, '[@instanceName]', 0.0625, 'lowerCenter', 'instance'))
    shapes.append(_lbl(w / 2, -h / 2 - 0.12, title or name, 0.0625, 'upperCenter', 'device'))
    return {'shapes': shapes, 'pins': out}


def symbol_from_spec(sym):
    """Explicit symbol: {'shapes':[...], 'pins':{name:[x,y]}} validated on grid."""
    s = copy.deepcopy(sym)
    for p, xy in s['pins'].items():
        if abs(snap(xy[0]) - xy[0]) > 1e-9 or abs(snap(xy[1]) - xy[1]) > 1e-9:
            raise ValueError('Symbol pin %s off the %.4f grid: %s' % (p, GRID, xy))
    if not any(sh.get('text') == '[@instanceName]' for sh in s['shapes']):
        xs = [x for sh in s['shapes'] for x, _ in _shape_points(sh)] or [0]
        ys = [y for sh in s['shapes'] for _, y in _shape_points(sh)] or [0]
        s['shapes'].append(_lbl((min(xs) + max(xs)) / 2, max(ys) + 0.1, '[@instanceName]', 0.0625, 'lowerCenter', 'instance'))
    return s


def _shape_points(sh):
    if sh['t'] in ('line', 'poly'):
        return [tuple(p) for p in sh['pts']]
    if sh['t'] in ('rect', 'ellipse', 'arc'):
        b = sh['box']
        return [(b[0], b[1]), (b[2], b[3])]
    if sh['t'] == 'label':
        return [tuple(sh['xy'])]
    return []


# Standard module symbols ----------------------------------------------------

def sym_cross_pair(kind='n', pins=('OUTP', 'OUTN', 'S', 'B')):
    """Cross-coupled pair: OUTP left, OUTN right, common S below (n) / above (p), bulk beside S."""
    p_l, p_r, p_s, p_b = pins
    w = 1.5
    sgn = 1 if kind == 'n' else -1
    shapes = [{'t': 'rect', 'box': [0, -0.5, w, 0.5], 'layer': 'device'}]
    # two device glyphs + cross
    for cx, mir in ((0.45, 1), (1.05, -1)):
        shapes += [{'t': 'line', 'pts': [[cx, 0.2], [cx, -0.2]], 'layer': 'device'},
                   {'t': 'line', 'pts': [[cx + 0.06 * mir, 0.2], [cx + 0.06 * mir, -0.2]], 'layer': 'device'},
                   {'t': 'line', 'pts': [[cx + 0.06 * mir, 0.15 * sgn], [cx + 0.22 * mir, 0.15 * sgn], [cx + 0.22 * mir, 0.35 * sgn]], 'layer': 'device'},
                   {'t': 'line', 'pts': [[cx + 0.06 * mir, -0.15 * sgn], [cx + 0.22 * mir, -0.15 * sgn], [cx + 0.22 * mir, -0.35 * sgn]], 'layer': 'device'}]
        if kind == 'p':
            shapes.append({'t': 'ellipse', 'box': [cx - 0.07, -0.035, cx - 0.0, 0.035], 'layer': 'device'})
    shapes += [{'t': 'line', 'pts': [[0.45, 0.0], [0.6, 0.0], [0.9, 0.3 * sgn], [1.27, 0.3 * sgn]], 'layer': 'device'},
               {'t': 'line', 'pts': [[1.05, 0.0], [0.9, 0.0], [0.6, 0.3 * sgn], [0.23, 0.3 * sgn]], 'layer': 'device'}]
    yS = -0.75 if kind == 'n' else 0.75
    pins_xy = {p_l: [-0.25, 0.25 * sgn], p_r: [w + 0.25, 0.25 * sgn], p_s: [0.75, yS]}
    shapes += [{'t': 'line', 'pts': [[-0.25, 0.25 * sgn], [0, 0.25 * sgn]], 'layer': 'device'},
               {'t': 'line', 'pts': [[w, 0.25 * sgn], [w + 0.25, 0.25 * sgn]], 'layer': 'device'},
               {'t': 'line', 'pts': [[0.75, 0.5 * (-1 if kind == 'n' else 1)], [0.75, yS]], 'layer': 'device'},
               _lbl(0.05, 0.25 * sgn, p_l, 0.05, 'centerLeft', 'pin'), _lbl(w - 0.05, 0.25 * sgn, p_r, 0.05, 'centerRight', 'pin'),
               _lbl(0.82, yS * 0.62, p_s, 0.05, 'centerLeft', 'pin')]
    if p_b:
        pins_xy[p_b] = [1.25, yS]
        shapes += [{'t': 'line', 'pts': [[1.25, 0.5 * (-1 if kind == 'n' else 1)], [1.25, yS]], 'layer': 'device'},
                   _lbl(1.18, yS * 0.62, p_b, 0.05, 'centerRight', 'pin')]
    shapes += [_lbl(w / 2, 0.62 if kind == 'n' else -0.62, '[@instanceName]', 0.0625, 'lowerCenter' if kind == 'n' else 'upperCenter', 'instance'),
               _lbl(-0.3, -0.62 if kind == 'n' else 0.62, 'GM-' + kind.upper(), 0.05, 'centerLeft', 'device')]
    return {'shapes': shapes, 'pins': pins_xy}


def sym_cap_array(reg, bits, two_terminal=True):
    """Switched-capacitor array: P left, N right, bit pins and VDD on top, VSS below.

    P/N/VDD/VSS keep the same position for 1..9 bits (bit pitch 0.25 up to 5 bits, 0.125 above),
    so a top-level plan stays valid when the register width changes."""
    h = 0.75
    pitch = 0.25 if bits <= 5 else 0.125
    span_end = 0.25 + pitch * (bits - 1)
    vdd_x = max(1.5, snap(span_end + 0.25))
    w = max(1.75, snap(vdd_x + 0.25))
    shapes = [{'t': 'rect', 'box': [0, -h / 2, w, h / 2], 'layer': 'device'},
              {'t': 'line', 'pts': [[w / 2 - 0.06, -0.2], [w / 2 - 0.06, 0.1]], 'layer': 'device'},
              {'t': 'line', 'pts': [[w / 2 + 0.06, -0.2], [w / 2 + 0.06, 0.1]], 'layer': 'device'},
              {'t': 'line', 'pts': [[0.2, -0.05], [w / 2 - 0.06, -0.05]], 'layer': 'device'},
              {'t': 'line', 'pts': [[w / 2 + 0.06, -0.05], [w - 0.2, -0.05]], 'layer': 'device'},
              {'t': 'line', 'pts': [[w / 2 - 0.22, -0.25], [w / 2 + 0.22, 0.2]], 'layer': 'device'},
              {'t': 'poly', 'pts': [[w / 2 + 0.22, 0.2], [w / 2 + 0.12, 0.17], [w / 2 + 0.19, 0.1]], 'layer': 'device'}]
    pins = {'P': [-0.25, 0.0], 'N': [snap(w + 0.25), 0.0]}
    shapes += [{'t': 'line', 'pts': [[-0.25, 0], [0, 0]], 'layer': 'device'}, {'t': 'line', 'pts': [[w, 0], [w + 0.25, 0]], 'layer': 'device'},
               _lbl(0.05, -0.05, 'P', 0.05, 'centerLeft', 'pin'), _lbl(w - 0.05, -0.05, 'N', 0.05, 'centerRight', 'pin')]
    x = 0.25
    for b in range(bits - 1, -1, -1):
        name = 'B%d' % b
        pins[name] = [snap(x), snap(h / 2 + 0.25)]
        shapes += [{'t': 'line', 'pts': [[x, h / 2], [x, h / 2 + 0.25]], 'layer': 'device'},
                   _lbl(x, h / 2 - 0.05, name if pitch > 0.2 else str(b), 0.04, 'upperCenter', 'pin')]
        x += pitch
    pins['VDD'] = [vdd_x, snap(h / 2 + 0.25)]
    shapes += [{'t': 'line', 'pts': [[vdd_x, h / 2], [vdd_x, h / 2 + 0.25]], 'layer': 'device'}, _lbl(vdd_x, h / 2 - 0.05, 'VDD', 0.04, 'upperCenter', 'pin')]
    xv = snap(w / 2)
    pins['VSS'] = [xv, snap(-h / 2 - 0.25)]
    shapes += [{'t': 'line', 'pts': [[xv, -h / 2], [xv, -h / 2 - 0.25]], 'layer': 'device'}, _lbl(xv, -h / 2 + 0.05, 'VSS', 0.04, 'lowerCenter', 'pin')]
    shapes += [_lbl(0.05, -h / 2 + 0.05, 'CT_' + reg, 0.05, 'lowerLeft', 'device'),
               _lbl(w / 2, h / 2 + 0.3, '[@instanceName]', 0.0625, 'lowerCenter', 'instance')]
    return {'shapes': shapes, 'pins': pins}


def sym_diff_inductor(ports=('P', 'N', 'CT'), rx=0.75, ry=0.75, legs=0.375, gap=0.25, title='L'):
    """SNPFit-style one-turn differential inductor: P/N legs down, centre tap up."""
    p, n, ct = ports
    shapes = [{'t': 'line', 'pts': _octagon(0, 0, rx, ry, 0.25, gap, 'bottom'), 'layer': 'device'},
              {'t': 'line', 'pts': [[-gap, -ry], [-gap, -ry - legs]], 'layer': 'device'},
              {'t': 'line', 'pts': [[gap, -ry], [gap, -ry - legs]], 'layer': 'device'},
              _lbl(-gap - 0.05, -ry - legs + 0.08, p, 0.05, 'centerRight', 'pin'),
              _lbl(gap + 0.05, -ry - legs + 0.08, n, 0.05, 'centerLeft', 'pin'),
              _lbl(0, 0.05, title, 0.0625, 'centerCenter', 'device'),
              _lbl(0, ry + 0.4, '[@instanceName]', 0.0625, 'lowerCenter', 'instance')]
    pins = {p: [-gap, snap(-ry - legs)], n: [gap, snap(-ry - legs)]}
    if ct:
        shapes += [{'t': 'line', 'pts': [[0, ry], [0, ry + 0.25]], 'layer': 'device'}, _lbl(0.06, ry + 0.15, ct, 0.05, 'centerLeft', 'pin')]
        pins[ct] = [0.0, snap(ry + 0.25)]
    return {'shapes': shapes, 'pins': pins}


def sym_two_terminal_inductor(ports=('P', 'N'), title='LT'):
    """Single winding drawn as a vertical coil: P on top, N at bottom."""
    p, n = ports
    shapes = [{'t': 'line', 'pts': [[0, 0], [0, -0.125]], 'layer': 'device'},
              {'t': 'line', 'pts': [[0, -0.625], [0, -0.75]], 'layer': 'device'}]
    for k in range(4):
        y0 = -0.125 - 0.125 * k
        shapes.append({'t': 'arc', 'box': [-0.0625, y0 - 0.125, 0.0625, y0], 'start': -90, 'stop': 90, 'layer': 'device'})
    shapes += [_lbl(0.12, -0.375, title, 0.0625, 'centerLeft', 'device'),
               _lbl(-0.1, -0.06, p, 0.04, 'centerRight', 'pin'), _lbl(-0.1, -0.69, n, 0.04, 'centerRight', 'pin'),
               _lbl(-0.12, -0.375, '[@instanceName]', 0.0625, 'centerRight', 'instance')]
    return {'shapes': shapes, 'pins': {p: [0.0, 0.0], n: [0.0, -0.75]}}


# ---------------------------------------------------------------- geometry / masters

def master_symbols():
    return read_json(MASTER_SYMBOLS)['masters']


def ideal_master(role):
    return {'cap': 'analogLib/cap', 'res': 'analogLib/res', 'ind': 'analogLib/ind', 'mutual': 'analogLib/mind'}.get(role)


TERM_ALIASES = {'p': 'PLUS', 'n': 'MINUS', 'd': 'D', 'g': 'G', 's': 'S', 'b': 'B'}


def primitive_pins(inst, binding, geometry):
    """Return {graph terminal: master-relative xy} for a primitive instance."""
    if inst.get('ideal') or inst['role'] in ('mutual',):
        key = ideal_master(inst['role'])
        terms = {'p': 'PLUS', 'n': 'MINUS'} if inst['role'] != 'mutual' else {}
    else:
        b = binding['roles'][inst['role']]
        key = b['library'] + '/' + b['cell']
        terms = b['terminals']
    geo = geometry.get(key)
    if geo is None:
        raise KeyError('No pin geometry for master ' + key)
    return key, {t: tuple(geo['pins'][actual]) for t, actual in terms.items() if t in inst['nodes']}


# ---------------------------------------------------------------- connectivity extraction

def _on_segment(p, a, b, eps=1e-6):
    (x, y), (x1, y1), (x2, y2) = p, a, b
    if abs(x1 - x2) < eps:
        return abs(x - x1) < eps and min(y1, y2) - eps <= y <= max(y1, y2) + eps
    if abs(y1 - y2) < eps:
        return abs(y - y1) < eps and min(x1, x2) - eps <= x <= max(x1, x2) + eps
    return False


def connectivity_check(cell):
    """Extract nets from a laid-out cell like the schematic editor and compare with the graph.

    cell: {'instances':[{name, nodes, pins:{term:(x,y)}}], 'wires':[{'pts':[...], 'net':str|None}],
           'pins':[{'name', 'xy'}], 'labels':[{'net','xy'}]}
    Rules: wire endpoints/vertices join anything they touch (including the middle of
    another wire = T junction); a pin or terminal on the middle of a wire is an error;
    crossings without a vertex do not connect; a label names the wire group it lies on.
    """
    errors = []
    parent = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def key(p):
        return (round(p[0], 4), round(p[1], 4))
    terminals = []
    for inst in cell['instances']:
        for term, xy in inst['pins'].items():
            terminals.append((inst['name'], term, key(xy)))
    for p in cell.get('pins', []):
        terminals.append(('PIN', p['name'], key(p['xy'])))
    segs = []
    for wi, w in enumerate(cell.get('wires', [])):
        pts = [key(p) for p in w['pts']]
        for a, b in zip(pts, pts[1:]):
            if a == b:
                errors.append('zero-length wire segment at %s' % (a,))
                continue
            if a[0] != b[0] and a[1] != b[1]:
                errors.append('non-Manhattan wire segment %s-%s' % (a, b))
            segs.append((wi, a, b))
            union(('w', wi), a)
            union(('w', wi), b)
    vertex_points = set()
    for wi, w in enumerate(cell.get('wires', [])):
        for p in w['pts']:
            vertex_points.add(key(p))
    # T-junctions: a vertex of one wire on the interior of another
    for (wi, a, b) in segs:
        for v in vertex_points:
            if v != a and v != b and _on_segment(v, a, b):
                union(('w', wi), v)
    # four-way junctions: Virtuoso flags a solder dot on a crossover
    arms = {}
    for (wi, a, b) in segs:
        for p0, p1 in ((a, b), (b, a)):
            d = ((p1[0] > p0[0]) - (p1[0] < p0[0]), (p1[1] > p0[1]) - (p1[1] < p0[1]))
            arms.setdefault(p0, set()).add(d)
        for v in vertex_points:
            if v != a and v != b and _on_segment(v, a, b):
                dx, dy = (1, 0) if a[1] == b[1] else (0, 1)
                arms.setdefault(v, set()).update({(dx, dy), (-dx, -dy)})
    for v, ds in arms.items():
        if len(ds) >= 4:
            errors.append('four-way junction at %s (solder dot on a crossover); offset one branch' % (v,))
    term_points = {}
    for inst, term, p in terminals:
        term_points.setdefault(p, []).append((inst, term))
        hit = False
        for (wi, a, b) in segs:
            if p == a or p == b:
                union(('t', inst, term), p)
                hit = True
            elif _on_segment(p, a, b):
                errors.append('%s.%s lies on the middle of a wire (not an endpoint)' % (inst, term))
        union(('t', inst, term), p)
    # terminals that coincide directly (abutment) are connected
    for p, items in term_points.items():
        if len(items) > 1:
            for it in items[1:]:
                union(('t',) + items[0], ('t',) + it)
    # labels
    named = {}
    for lab in cell.get('labels', []):
        p = key(lab['xy'])
        on = [wi for (wi, a, b) in segs if _on_segment(p, a, b)]
        if not on:
            errors.append('label %s at %s is not on a wire' % (lab['net'], p))
            continue
        root = find(('w', on[0]))
        named.setdefault(root, set()).add(lab['net'])
    for p in cell.get('pins', []):
        root = find(('t', 'PIN', p['name']))
        named.setdefault(root, set()).add(p['name'])
    # group by declared net names
    groups = {}
    for inst in cell['instances']:
        for term in inst['pins']:
            groups.setdefault(find(('t', inst['name'], term)), []).append((inst['name'], term))
    for p in cell.get('pins', []):
        groups.setdefault(find(('t', 'PIN', p['name'])), [])
    by_net = {}
    for root, names in named.items():
        if len(names) > 1:
            errors.append('one connected group carries several names %s' % sorted(names))
        for n in names:
            by_net.setdefault(n, set()).add(root)
    # expected: instance terminal -> net
    actual_net = {}
    for root, members in groups.items():
        names = named.get(root)
        name = sorted(names)[0] if names else None
        for m in members:
            actual_net[m] = name
    for inst in cell['instances']:
        for term, net in inst['nodes'].items():
            got = actual_net.get((inst['name'], term), None)
            if got is None:
                # unnamed group: fine only if every member of the group expects one common net and it is not split
                root = find(('t', inst['name'], term))
                members = groups.get(root, [])
                want = {next(i for i in cell['instances'] if i['name'] == a)['nodes'][t] for a, t in members}
                if len(want) != 1:
                    errors.append('%s.%s connected to terminals of different nets %s' % (inst['name'], term, sorted(want)))
                elif len(members) == 1:
                    errors.append('%s.%s (%s) is unconnected' % (inst['name'], term, net))
                continue
            if got != str(net):
                errors.append('%s.%s expected %s got %s' % (inst['name'], term, net, got))
    # each net must be one group (no splits)
    expected_groups = {}
    for inst in cell['instances']:
        for term, net in inst['nodes'].items():
            expected_groups.setdefault(str(net), set()).add(find(('t', inst['name'], term)))
    for net, roots in expected_groups.items():
        named_roots = by_net.get(net, set())
        unnamed = {r for r in roots if r not in named}
        if len(named_roots | unnamed) > 1 and not (len(unnamed) == 0 and len(named_roots) >= 1):
            errors.append('net %s is split into %d unnamed pieces' % (net, len(named_roots | unnamed)))
    return {'ok': not errors, 'errors': errors[:40], 'error_count': len(errors)}


# ---------------------------------------------------------------- default naming

CELL_SUFFIX = {'inv': 'INV', 'CT_R3U_main': 'CTU_MAIN', 'CT_R3_main': 'CT_MAIN',
               'CT_R3U_aux': 'CTU_AUX', 'CT_R3_aux': 'CT_AUX'}
INNER_RENAME = {'inv': {'M0': 'MP', 'M1': 'MN'},
                'CT_R3U_main': {'M0': 'MSW', 'I0': 'INV', 'R0': 'RBP', 'R1': 'RBN', 'C2': 'CP', 'C3': 'CN'},
                'CT_R3U_aux': {'M0': 'MSW', 'I0': 'INV', 'R1': 'RBN', 'C2': 'CP'}}


def default_spec_parts(graph):
    """Cell suffixes and inner instance names shared by every family."""
    cells, inner = {}, {}
    for sub in graph.get('subcircuits', []):
        if sub['name'] in CELL_SUFFIX:
            cells[sub['name']] = CELL_SUFFIX[sub['name']]
        if sub['name'] in INNER_RENAME:
            inner[CELL_SUFFIX[sub['name']]] = INNER_RENAME[sub['name']]
        if re.fullmatch(r'CT_R3_(main|aux)', sub['name']):
            inner[CELL_SUFFIX[sub['name']]] = {i['name']: 'U' + i['name'][1:] for i in sub['instances']}
    return cells, inner


def package_variant(graph, spec):
    """Package with the shared naming (array/inverter cells) plus the family spec."""
    cells, inner = default_spec_parts(graph)
    full = copy.deepcopy(spec)
    full.setdefault('rename_in', {})
    for k, v in inner.items():
        full['rename_in'].setdefault(k, {}).update(v)
    g = package(graph, full)
    # rename array / unit / inverter cells to readable suffixes
    ren = dict(cells)
    for sub in g['subcircuits']:
        if sub['name'] in ren:
            sub['name'] = ren[sub['name']]
    for sub in g['subcircuits'] + [g]:
        for inst in sub['instances']:
            if inst['role'] == 'subckt' and inst['master'] in ren:
                inst['master'] = ren[inst['master']]
    for sub in g['subcircuits']:
        r = full['rename_in'].get(sub['name'], {})
        for inst in sub['instances']:
            inst['name'] = r.get(inst['name'], inst['name'])
    return g, full


def equivalence_variant(electrical, packaged, full_spec):
    spec = copy.deepcopy(full_spec)
    # inner renames were applied after cell renaming; equivalence only needs name maps
    return equivalence(electrical, packaged, spec)


# ---------------------------------------------------------------- symbols per cell

def _inv_symbol():
    s = symbol_box('INV', {'IN': ('L', 0), 'OUT': ('R', 0), 'VDD': ('T', 1), 'VSS': ('B', 1)}, title='INV', w=0.75, h=0.75)
    s['shapes'] += [{'t': 'poly', 'pts': [[0.2, 0.2], [0.2, -0.2], [0.5, 0.0]], 'layer': 'device'},
                    {'t': 'ellipse', 'box': [0.5, -0.04, 0.58, 0.04], 'layer': 'device'}]
    return s


BIT_RE = re.compile(r'(main|aux)_B(\d+)$')


def _bit_order(name):
    m = BIT_RE.match(name)
    return (0 if m.group(1) == 'main' else 1, -int(m.group(2)))


def adapt_symbol_bits(sym, ports):
    """Re-create register-bit pins of an explicit symbol for the actual register widths.

    The drawn bit column (position of the first bit pin, pitch, stub direction) is kept;
    other pins are left untouched and the column skips their positions."""
    have = sorted([p for p in sym['pins'] if BIT_RE.match(p)], key=_bit_order)
    want = sorted([p for p in ports if BIT_RE.match(p)], key=_bit_order)
    if have == want:
        return sym
    s = copy.deepcopy(sym)
    old = {p: tuple(s['pins'][p]) for p in have}
    if old:
        xs = [xy[0] for xy in old.values()]
        ys = [xy[1] for xy in old.values()]
        vertical = len(set(xs)) == 1 or (max(ys) - min(ys)) >= (max(xs) - min(xs))
        first = old[have[0]]
        pts = sorted(old.values(), key=lambda q: (-q[1], q[0]) if vertical else (q[0], -q[1]))
        pitch = (abs(pts[1][1] - pts[0][1]) if vertical else abs(pts[1][0] - pts[0][0])) if len(pts) > 1 else 0.125
        pitch = pitch or 0.125
    else:
        xs = [xy[0] for xy in s['pins'].values()]
        ys = [xy[1] for xy in s['pins'].values()]
        first, vertical, pitch = (min(xs), max(ys) - 0.25), True, 0.125
    # drop old bit stubs and their text
    def touches(sh):
        if sh['t'] == 'label':
            return sh.get('text') in old
        if sh['t'] == 'line':
            return any(tuple(round(v, 6) for v in p) in {tuple(round(v, 6) for v in q) for q in old.values()} for p in sh['pts'])
        return False
    s['shapes'] = [sh for sh in s['shapes'] if not touches(sh)]
    for p in have:
        del s['pins'][p]
    taken = {tuple(round(v, 6) for v in xy) for xy in s['pins'].values()}
    body = [sh['box'] for sh in s['shapes'] if sh['t'] == 'rect' and sh.get('layer', 'device') == 'device']
    x0 = body[0][0] if body else first[0] + 0.25
    k = 0
    for name in want:
        while True:
            xy = (snap(first[0]), snap(first[1] - pitch * k)) if vertical else (snap(first[0] + pitch * k), snap(first[1]))
            k += 1
            if xy not in taken:
                break
        s['pins'][name] = [xy[0], xy[1]]
        taken.add(xy)
        if vertical:
            edge = min(b[0] for b in body) if body else xy[0] + 0.25
            s['shapes'] += [{'t': 'line', 'pts': [[xy[0], xy[1]], [edge, xy[1]]], 'layer': 'device'},
                            _lbl(edge + 0.04, xy[1], name, 0.04, 'centerLeft', 'pin')]
        else:
            s['shapes'] += [{'t': 'line', 'pts': [[xy[0], xy[1]], [xy[0], xy[1] - 0.25 * (1 if xy[1] > 0 else -1)]], 'layer': 'device'},
                            _lbl(xy[0], xy[1] - 0.3 * (1 if xy[1] > 0 else -1), name, 0.04, 'upperCenter', 'pin')]
    return s


def cell_symbol(sub, spec):
    sym = spec.get('symbols', {}).get(sub['name'])
    ports = [p['name'] for p in sub['ports']]
    if sym is not None:
        kind = sym.get('generator')
        if kind is None:
            s = symbol_from_spec(sym)
        elif kind == 'diff_inductor':
            s = sym_diff_inductor(**sym.get('args', {}))
        elif kind == 'two_terminal_inductor':
            s = sym_two_terminal_inductor(**sym.get('args', {}))
        elif kind == 'cross_pair':
            s = sym_cross_pair(**sym.get('args', {}))
        elif kind == 'box':
            s = symbol_box(sub['name'], {k: tuple(v) for k, v in sym['pins'].items()}, sym.get('title'), sym.get('w'), sym.get('h'), sym.get('glyph'))
        else:
            raise ValueError('Unknown symbol generator ' + kind)
    elif sub['name'] in ('CT_MAIN', 'CT_AUX'):
        bits = len([p for p in ports if re.fullmatch(r'B\d+', p)])
        s = sym_cap_array(sub['name'][3:].lower(), bits)
    elif sub['name'] in ('CTU_MAIN', 'CTU_AUX'):
        s = symbol_box(sub['name'], {'P': ('L', 0), 'N': ('R', 0), 'SW': ('T', 0), 'VDD': ('T', 2), 'VSS': ('B', 1)}, title=sub['name'], w=1.0, h=0.75)
    elif sub['name'] == 'INV':
        s = _inv_symbol()
    else:
        left = [p for p in ports if p not in ('VDD', 'VSS')]
        pins = {}
        for i, p in enumerate(left):
            pins[p] = ('L' if i % 2 == 0 else 'R', i // 2)
        if 'VDD' in ports:
            pins['VDD'] = ('T', 1)
        if 'VSS' in ports:
            pins['VSS'] = ('B', 1)
        s = symbol_box(sub['name'], pins)
    if sorted(s['pins']) != sorted(ports):
        raise ValueError('Symbol pins %s differ from cell ports %s for %s' % (sorted(s['pins']), sorted(ports), sub['name']))
    return s


# ---------------------------------------------------------------- layouts

def _master_key(inst, binding):
    if inst['role'] == 'subckt':
        return '@' + inst['master']
    if inst.get('ideal') or inst['role'] == 'mutual':
        return ideal_master(inst['role'])
    b = binding['roles'][inst['role']]
    return b['library'] + '/' + b['cell']


def _terms(inst, binding):
    if inst['role'] == 'subckt':
        return {k: k for k in inst['nodes']}
    if inst.get('ideal') or inst['role'] == 'mutual':
        return {'p': 'PLUS', 'n': 'MINUS'} if inst['role'] != 'mutual' else {}
    return binding['roles'][inst['role']]['terminals']


GENERIC_PINS = {
    'nmos': {'d': [0.25, 0.1875], 'g': [0.0, 0.0], 's': [0.25, -0.1875], 'b': [0.25, 0.0]},
    'pmos': {'d': [0.25, -0.1875], 'g': [0.0, 0.0], 's': [0.25, 0.1875], 'b': [0.25, 0.0]},
    'two': {'p': [0.0, 0.0], 'n': [0.0, -0.375], 'b': [0.125, -0.1875]},
}


def generic_binding():
    """Placeholder binding used only to preview/check layouts without a resolved PDK binding."""
    terms = {'nmos': {'d': 'D', 'g': 'G', 's': 'S', 'b': 'B'}, 'pmos': {'d': 'D', 'g': 'G', 's': 'S', 'b': 'B'},
             'cap': {'p': 'PLUS', 'n': 'MINUS', 'b': 'BULK'}, 'res': {'p': 'PLUS', 'n': 'MINUS'}, 'ind': {'p': 'PLUS', 'n': 'MINUS'}}
    return {'name': 'generic-preview', 'roles': {r: {'library': 'generic', 'cell': r, 'terminals': t} for r, t in terms.items()}}


def generic_geometry(role, terminals):
    """Placeholder pin geometry for a master that was not probed (preview/check only;
    native export re-lays out with the probed geometry of the bound masters)."""
    table = GENERIC_PINS.get(role, GENERIC_PINS['two'])
    pins = {actual: table.get(t, [0.0, 0.0]) for t, actual in terminals.items()}
    return {'pins': pins, 'bbox': [-0.5, -0.5, 0.5, 0.5], 'shapes': [], 'generic': True}


def _placed_pins(inst, binding, geo):
    key = inst['master'] if inst.get('master', '').startswith('@') or '/' in inst.get('master', '') else _master_key(inst, binding)
    if key not in geo and not key.startswith('@'):
        geo[key] = generic_geometry(inst['role'], _terms(inst, binding))
    if key not in geo:
        raise KeyError('No geometry for master %s (instance %s)' % (key, inst['name']))
    pins = geo[key]['pins']
    out = {}
    for term, actual in _terms(inst, binding).items():
        if term not in inst['nodes']:
            continue
        if actual not in pins:
            raise KeyError('Master %s has no pin %s' % (key, actual))
        out[term] = place_point(inst, *pins[actual])
        inst.setdefault('mpins', {})[term] = tuple(pins[actual])
    return out


def _stub(inst_pins, term, length=0.25):
    xs = [p[0] for p in inst_pins.values()]
    ys = [p[1] for p in inst_pins.values()]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    x, y = inst_pins[term]
    dx, dy = x - cx, y - cy
    if len(inst_pins) == 1 or (abs(dx) < 1e-9 and abs(dy) < 1e-9):
        return (x, round(y + length, 6))
    if abs(dx) >= abs(dy):
        return (round(x + (length if dx > 0 else -length), 6), y)
    return (x, round(y + (length if dy > 0 else -length), 6))


BODY_BOXES = {}


def _body_box(key):
    if key in BODY_BOXES:
        return BODY_BOXES[key]
    shapes = _CURRENT_SHAPES.get(key, [])
    rects = [sh['box'] for sh in shapes if sh['t'] == 'rect' and sh.get('layer', 'device') == 'device']
    pts = []
    if rects:
        for b in rects:
            pts += [(b[0], b[1]), (b[2], b[3])]
    else:
        pts = [p for sh in shapes if sh['t'] in ('line', 'poly', 'ellipse') for p in _shape_points(sh)]
    box = None
    if pts:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        box = (min(xs), min(ys), max(xs), max(ys))
    return box


_CURRENT_SHAPES = {}


def _stub_dir(inst, term):
    """Outward direction of a pin in master space (from the body box), then oriented."""
    m = inst.get('mpins', {}).get(term)
    box = _body_box(inst.get('master', '')) if inst.get('master', '').startswith('@') else None
    if m is None or box is None:
        return None
    x, y = m
    eps = 1e-6
    if y > box[3] + eps:
        v = (0, 1)
    elif y < box[1] - eps:
        v = (0, -1)
    elif x < box[0] - eps:
        v = (-1, 0)
    elif x > box[2] + eps:
        v = (1, 0)
    else:
        return None
    return orient_point(inst.get('orient', 'R0'), *v)


def auto_stubs(cell, binding, geo, skip=()):
    """Label stubs for every terminal not already ending a wire."""
    ends = set()
    for w in cell['wires']:
        ends.add(tuple(round(v, 4) for v in w['pts'][0]))
        ends.add(tuple(round(v, 4) for v in w['pts'][-1]))
        for p in w['pts']:
            ends.add(tuple(round(v, 4) for v in p))
    for inst in cell['instances']:
        if inst['name'] in skip:
            continue
        for term, xy in inst['pins'].items():
            if tuple(round(v, 4) for v in xy) in ends:
                continue
            d = _stub_dir(inst, term)
            q = (round(xy[0] + 0.25 * d[0], 6), round(xy[1] + 0.25 * d[1], 6)) if d else _stub(inst['pins'], term)
            net = '0' if inst['nodes'][term] == '0' else str(inst['nodes'][term])
            cell['wires'].append({'net': net, 'pts': [list(xy), list(q)], 'auto': True})
            vertical = abs(q[0] - xy[0]) < 1e-9
            cell['labels'].append({'net': net, 'xy': list(q), 'orient': 'R90' if vertical else 'R0'})


def _bbox_of(key, geo, shapes_by_key):
    if key in shapes_by_key:
        pts = [p for sh in shapes_by_key[key] for p in _shape_points(sh) if sh['t'] != 'label']
        pts += [tuple(p) for p in geo[key]['pins'].values()]
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            return min(xs), min(ys), max(xs), max(ys)
    b = geo.get(key, {}).get('bbox')
    if b:
        return b[0], b[1], b[2], b[3]
    return -0.5, -0.5, 0.5, 0.5


def auto_layout(sub, binding, geo, shapes_by_key, kind=None):
    insts = []
    items = list(sub['instances'])
    rows = []
    if sub['name'] in ('CT_MAIN', 'CT_AUX'):
        by_bit = {}
        for inst in items:
            bit = int(re.match(r'U(\d+)_', inst['name']).group(1))
            by_bit.setdefault(bit, []).append(inst)
        rows = [by_bit[b] for b in sorted(by_bit, reverse=True)]
    else:
        mut = [i for i in items if i['role'] == 'mutual']
        other = [i for i in items if i['role'] != 'mutual']
        per = 4 if len(other) > 6 else max(1, len(other))
        rows = [other[i:i + per] for i in range(0, len(other), per)]
        if mut:
            rows += [mut[i:i + 6] for i in range(0, len(mut), 6)]
    y = 0.0
    for row in rows:
        boxes = [_bbox_of(_master_key(i, binding), geo, shapes_by_key) for i in row]
        h = max(b[3] - b[1] for b in boxes) if boxes else 1
        x = 0.0
        for inst, b in zip(row, boxes):
            orient = 'R0'
            xy = [snap(x - b[0] + 0.5), snap(y - b[3] - 0.5)]
            item = {'name': inst['name'], 'master': _master_key(inst, binding), 'role': inst['role'], 'xy': xy,
                    'orient': orient, 'nodes': inst['nodes'], 'ideal': inst.get('ideal')}
            item['pins'] = _placed_pins(item, binding, geo)
            insts.append(item)
            x += (b[2] - b[0]) + 1.25
        y -= h + 1.25
    cell = {'instances': insts, 'wires': [], 'labels': [], 'pins': []}
    xs = [i['xy'][0] for i in insts] or [0]
    left = snap(min(xs) - 2.0)
    top = max([i['xy'][1] for i in insts] or [0])
    for k, p in enumerate(sub['ports']):
        xy = [left, snap(top - 0.375 * k)]
        cell['pins'].append({'name': p['name'], 'xy': xy, 'direction': p.get('direction', 'inputOutput')})
        q = [snap(left + 0.5), xy[1]]
        cell['wires'].append({'net': p['name'], 'pts': [xy, q], 'auto': True})
        cell['labels'].append({'net': p['name'], 'xy': q})
    auto_stubs(cell, binding, geo)
    return cell


def plan_layout(sub, plan, binding, geo):
    """Top-level layout from an explicit plan: placements, wires, labels, pins."""
    insts = []
    for inst in sub['instances']:
        if inst['name'] not in plan['instances']:
            raise ValueError('Plan has no placement for %s' % inst['name'])
        pl = plan['instances'][inst['name']]
        item = {'name': inst['name'], 'master': _master_key(inst, binding), 'role': inst['role'],
                'xy': [snap(pl['xy'][0]), snap(pl['xy'][1])], 'orient': pl.get('orient', 'R0'), 'nodes': inst['nodes'], 'ideal': inst.get('ideal')}
        if item['orient'] not in ORIENTS:
            raise ValueError('Invalid orientation for %s' % inst['name'])
        item['pins'] = _placed_pins(item, binding, geo)
        insts.append(item)
    extra = set(plan['instances']) - {i['name'] for i in sub['instances']}
    if extra:
        raise ValueError('Plan places unknown instances %s' % sorted(extra))
    cell = {'instances': insts, 'wires': [], 'labels': [], 'pins': [], 'notes': plan.get('notes', [])}
    ports_now = {p['name'] for p in sub['ports']}
    plan_bits = {w.get('net') for w in plan.get('wires', [])} | {l['net'] for l in plan.get('labels', [])} | set(plan.get('pins', {}))
    plan_bits = {n for n in plan_bits if n and BIT_RE.match(n)}
    bits_now = {n for n in ports_now if BIT_RE.match(n)}
    reroute_bits = plan_bits != bits_now      # register width changed: bits get automatic stubs and pins
    plan = dict(plan)
    if reroute_bits:
        plan['wires'] = [w for w in plan.get('wires', []) if not (w.get('net') and BIT_RE.match(w['net']))]
        plan['labels'] = [l for l in plan.get('labels', []) if not BIT_RE.match(l['net'])]
        plan['pins'] = {k: v for k, v in plan.get('pins', {}).items() if not BIT_RE.match(k)}
    for w in plan.get('wires', []):
        pts = [[snap(x), snap(y)] for x, y in w['pts']]
        cell['wires'].append({'net': w.get('net'), 'pts': pts})
    for lab in plan.get('labels', []):
        cell['labels'].append({'net': lab['net'], 'xy': [snap(lab['xy'][0]), snap(lab['xy'][1])]})
    ports = [p['name'] for p in sub['ports']]
    placed = plan.get('pins', {})
    unknown = set(placed) - set(ports)
    if unknown:
        raise ValueError('Plan places unknown pins %s' % sorted(unknown))
    xs = [p[0] for i in insts for p in i['pins'].values()] + [v['xy'][0] for v in placed.values()]
    xs += [q[0] for w in cell['wires'] for q in w['pts']]
    ys = [p[1] for i in insts for p in i['pins'].values()] or [0]
    xs = xs or [0]
    auto_x = snap(min(xs) - 1.5)          # a separate column, clear of placed pins and wires
    k = 0
    for name in ports:
        if name in placed:
            xy = [snap(placed[name]['xy'][0]), snap(placed[name]['xy'][1])]
            cell['pins'].append({'name': name, 'xy': xy, 'direction': 'inputOutput'})
            if placed[name].get('stub', True) and not any(tuple(xy) == tuple(p) for w in cell['wires'] for p in (w['pts'][0], w['pts'][-1])):
                d = placed[name].get('side', 'L')
                q = [snap(xy[0] + (0.5 if d == 'L' else -0.5)), xy[1]]
                cell['wires'].append({'net': name, 'pts': [xy, q], 'auto': True})
                cell['labels'].append({'net': name, 'xy': q, 'just': 'lowerLeft' if d == 'L' else 'lowerRight'})
        else:
            xy = [auto_x, snap(max(ys) - 0.375 * k)]
            k += 1
            cell['pins'].append({'name': name, 'xy': xy, 'direction': 'inputOutput'})
            q = [snap(xy[0] + 0.5), xy[1]]
            cell['wires'].append({'net': name, 'pts': [xy, q], 'auto': True})
            cell['labels'].append({'net': name, 'xy': q})
    auto_stubs(cell, binding, geo)
    return cell


# ---------------------------------------------------------------- module templates (wired)

def _mk(inst, binding, geo, xy, orient='R0'):
    item = {'name': inst['name'], 'master': _master_key(inst, binding), 'role': inst['role'],
            'xy': [snap(xy[0]), snap(xy[1])], 'orient': orient, 'nodes': inst['nodes'], 'ideal': inst.get('ideal')}
    item['pins'] = _placed_pins(item, binding, geo)
    return item


def _ports_left(cell, sub, x, y0):
    for k, p in enumerate(sub['ports']):
        xy = [snap(x), snap(y0 - 0.375 * k)]
        cell['pins'].append({'name': p['name'], 'xy': xy, 'direction': p.get('direction', 'inputOutput')})
        q = [snap(x + 0.5), xy[1]]
        cell['wires'].append({'net': p['name'], 'pts': [xy, q], 'auto': True})
        cell['labels'].append({'net': p['name'], 'xy': q})


def template_inductor(sub, binding, geo):
    """Winding cell: per winding a series-loss R over the winding with the parallel-loss R beside it."""
    insts = {i['name']: i for i in sub['instances']}
    windings = [i for i in sub['instances'] if i['role'] == 'ind']
    if not windings or any(not i.get('ideal') for i in sub['instances'] if i['role'] in ('ind', 'res')):
        return None
    cell = {'instances': [], 'wires': [], 'labels': [], 'pins': []}
    used = set()
    x = 0.0
    for w in windings:
        nm = w['name'][1:]
        rs, rp = insts.get('RSL' + nm), insts.get('RPL' + nm)
        if not rs or not rp or rs['nodes']['n'] != w['nodes']['p'] or rp['nodes']['p'] != rs['nodes']['p'] or rp['nodes']['n'] != w['nodes']['n']:
            return None
        a = _mk(rs, binding, geo, (x, 0.0))
        b = _mk(w, binding, geo, (x, -0.625))
        c = _mk(rp, binding, geo, (x + 0.75, -0.3125))
        cell['instances'] += [a, b, c]
        used |= {rs['name'], w['name'], rp['name']}
        top, bot = str(rs['nodes']['p']), str(w['nodes']['n'])
        cell['wires'].append({'net': str(rs['nodes']['n']), 'pts': [list(a['pins']['n']), list(b['pins']['p'])]})
        ytop, ybot = 0.3125, b['pins']['n'][1] - 0.3125
        cell['wires'].append({'net': top, 'pts': [list(a['pins']['p']), [x, ytop], [x + 0.75, ytop], list(c['pins']['p'])]})
        cell['wires'].append({'net': bot, 'pts': [list(b['pins']['n']), [x, ybot], [x + 0.75, ybot], list(c['pins']['n'])]})
        cell['labels'].append({'net': top, 'xy': [snap(x + 0.375), ytop]})
        cell['labels'].append({'net': bot, 'xy': [snap(x + 0.375), snap(ybot)]})
        x += 2.0
    k = 0
    for i in sub['instances']:
        if i['name'] in used:
            continue
        if i['role'] != 'mutual':
            return None
        cell['instances'].append(_mk(i, binding, geo, (0.25 + 1.0 * k, -2.25)))
        k += 1
    _ports_left(cell, sub, -2.0, 0.25)
    return cell


def template_cross_pair(sub, binding, geo):
    mos = [i for i in sub['instances'] if i['role'] in ('nmos', 'pmos')]
    if len(sub['instances']) != 2 or len(mos) != 2 or mos[0]['role'] != mos[1]['role']:
        return None
    a, b = mos
    if not (a['nodes']['g'] == b['nodes']['d'] and b['nodes']['g'] == a['nodes']['d']):
        return None
    key = _master_key(a, binding)
    if key not in geo:
        geo[key] = generic_geometry(a['role'], _terms(a, binding))
    pins = geo[key]['pins']
    t = binding['roles'][a['role']]['terminals']
    G, D, S_ = pins[t['g']], pins[t['d']], pins[t['s']]
    sgn = 1 if a['role'] == 'nmos' else -1
    if not (G[0] < D[0] and abs(D[0] - S_[0]) < 1e-9 and (D[1] - S_[1]) * sgn > 0):
        return None
    dx = D[0] - G[0]
    xl, xr = 0.0, 1.0
    L = _mk(a, binding, geo, (xl, 0.0), 'MY')
    R = _mk(b, binding, geo, (xr, 0.0), 'R0')
    cell = {'instances': [L, R], 'wires': [], 'labels': [], 'pins': []}
    xm = (xl + xr) / 2
    x1, x2 = snap(xm - 0.125), snap(xm + 0.125)
    gy = L['pins']['g'][1]
    dl, dr = L['pins']['d'], R['pins']['d']
    top = snap(dl[1] + sgn * 0.75)
    cell['wires'].append({'net': str(a['nodes']['d']), 'pts': [list(dl), [dl[0], top]]})
    cell['wires'].append({'net': str(b['nodes']['d']), 'pts': [list(dr), [dr[0], top]]})
    y1, y2 = snap(gy + sgn * 0.4375), snap(gy + sgn * 0.5625)
    cell['wires'].append({'net': str(a['nodes']['g']), 'pts': [list(L['pins']['g']), [x1, gy], [x1, y1], [dr[0], y1]]})
    cell['wires'].append({'net': str(b['nodes']['g']), 'pts': [list(R['pins']['g']), [x2, gy], [x2, y2], [dl[0], y2]]})
    cell['labels'].append({'net': str(a['nodes']['d']), 'xy': [dl[0], top]})
    cell['labels'].append({'net': str(b['nodes']['d']), 'xy': [dr[0], top]})
    sl, sr = L['pins']['s'], R['pins']['s']
    bot = snap(sl[1] - sgn * 0.3125)
    cell['wires'].append({'net': str(a['nodes']['s']), 'pts': [list(sl), [sl[0], bot], [sr[0], bot], list(sr)]})
    cell['labels'].append({'net': str(a['nodes']['s']), 'xy': [snap(xm), bot]})
    _ports_left(cell, sub, -2.25, 0.75)
    auto_stubs(cell, binding, geo)
    return cell


def build_layout(packaged, full_spec, binding, geometry=None):
    """Symbols + schematic layout for every cell of a packaged graph."""
    geo = copy.deepcopy(geometry) if geometry is not None else copy.deepcopy(master_symbols())
    shapes_by_key = {k: v.get('shapes', []) for k, v in geo.items()}
    global _CURRENT_SHAPES
    _CURRENT_SHAPES = shapes_by_key
    BODY_BOXES.clear()
    cells = {}
    for sub in packaged['subcircuits']:
        sym = cell_symbol(sub, full_spec)
        geo['@' + sub['name']] = {'pins': sym['pins']}
        shapes_by_key['@' + sub['name']] = sym['shapes']
        cells[sub['name']] = {'symbol': sym}
    top_sym_spec = full_spec.get('top_symbol')
    top_sub = {'name': packaged['name'], 'ports': packaged['ports']}
    if top_sym_spec:
        if top_sym_spec.get('generator') is None:
            top_sym_spec = adapt_symbol_bits(top_sym_spec, [p['name'] for p in packaged['ports']])
        top_sym = cell_symbol(top_sub, {'symbols': {packaged['name']: top_sym_spec}})
    else:
        top_sym = cell_symbol(top_sub, {})
    cells[packaged['name']] = {'symbol': top_sym}
    for sub in packaged['subcircuits']:
        cell = None
        if sub.get('kind') == 'inductor':
            cell = template_inductor(sub, binding, geo)
        elif sub.get('kind') == 'gm':
            cell = template_cross_pair(sub, binding, geo)
        cells[sub['name']]['schematic'] = cell or auto_layout(sub, binding, geo, shapes_by_key)
    plan = full_spec.get('top')
    if plan:
        cells[packaged['name']]['schematic'] = plan_layout(packaged, plan, binding, geo)
    else:
        cells[packaged['name']]['schematic'] = auto_layout(packaged, binding, geo, shapes_by_key)
    for c in cells.values():
        name_all_nets(c['schematic'])
    return {'top': packaged['name'], 'cells': cells, 'shapes': shapes_by_key}


def name_all_nets(cell):
    """Every wired net gets at least one label so OA keeps the graph net name."""
    have = {lab['net'] for lab in cell['labels']} | {p['name'] for p in cell['pins']}
    for w in cell['wires']:
        net = w.get('net')
        if net is None or net in have:
            continue
        a, b = w['pts'][0], w['pts'][1]
        cell['labels'].append({'net': net, 'xy': [round((a[0] + b[0]) / 2, 6), round((a[1] + b[1]) / 2, 6)]})
        have.add(net)


def check_layout(layout):
    report = {}
    for name, cell in layout['cells'].items():
        report[name] = connectivity_check(cell['schematic'])
    return {'ok': all(r['ok'] for r in report.values()), 'cells': report}


# ---------------------------------------------------------------- preview (PNG)

def render_cell(layout, cell_name, path, scale=110, margin=1.0, view='schematic', title=None):
    from PIL import Image, ImageDraw, ImageFont
    cell = layout['cells'][cell_name]
    shapes_by_key = layout['shapes']
    ink, wire_c, pin_c, lab_c, inst_c = (30, 45, 70), (40, 110, 160), (200, 40, 40), (20, 120, 60), (150, 90, 20)
    prims = []   # (kind, data, color)

    def add_shapes(shapes, tf, color):
        for sh in shapes:
            if sh['t'] in ('line', 'poly'):
                prims.append(('line' if sh['t'] == 'line' else 'poly', [tf(*p) for p in sh['pts']], color))
            elif sh['t'] == 'rect':
                b = sh['box']
                pts = [tf(b[0], b[1]), tf(b[2], b[1]), tf(b[2], b[3]), tf(b[0], b[3]), tf(b[0], b[1])]
                prims.append(('line', pts, color))
            elif sh['t'] in ('ellipse', 'arc'):
                b = sh['box']
                c = tf((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
                rx, ry = abs(b[2] - b[0]) / 2, abs(b[3] - b[1]) / 2
                a0, a1 = (0, 360) if sh['t'] == 'ellipse' else (sh['start'], sh['stop'])
                n = 24
                pts = []
                for k in range(n + 1):
                    a = math.radians(a0 + (a1 - a0) * k / n)
                    # the transform handles orientation; sample in master space
                    mx = (b[0] + b[2]) / 2 + rx * math.cos(a)
                    my = (b[1] + b[3]) / 2 + ry * math.sin(a)
                    pts.append(tf(mx, my))
                prims.append(('line', pts, color))
            elif sh['t'] == 'label':
                prims.append(('text', (tf(*sh['xy']), sh['text'], sh.get('just', 'centerCenter'), sh.get('h', 0.0625)), lab_c if sh.get('layer') == 'pin' else color))

    if view == 'symbol':
        sym = cell['symbol']
        add_shapes(sym['shapes'], lambda x, y: (x, y), ink)
        for p, xy in sym['pins'].items():
            prims.append(('pin', tuple(xy), pin_c))
    else:
        sch = cell['schematic']
        for inst in sch['instances']:
            def tf(x, y, inst=inst):
                return place_point(inst, x, y)
            shapes = [dict(s) for s in shapes_by_key.get(inst['master'], [])]
            if not shapes:
                xs = [p[0] for p in inst['pins'].values()] or [inst['xy'][0]]
                ys = [p[1] for p in inst['pins'].values()] or [inst['xy'][1]]
                prims.append(('line', [(min(xs), min(ys)), (max(xs), min(ys)), (max(xs), max(ys)), (min(xs), max(ys)), (min(xs), min(ys))], ink))
            for s in shapes:
                if s['t'] == 'label' and s['text'].startswith('[@'):
                    if s['text'] == '[@instanceName]':
                        s = dict(s, text=inst['name'])
                    else:
                        continue
                add_shapes([s], tf, inst_c if s['t'] == 'label' and s.get('layer') == 'instance' else ink)
            if not any(s.get('text') == '[@instanceName]' for s in shapes):
                xs = [p[0] for p in inst['pins'].values()] or [inst['xy'][0]]
                prims.append(('text', ((max(xs) + 0.15, inst['xy'][1] - 0.1), inst['name'], 'centerLeft', 0.07), inst_c))
        for w in sch['wires']:
            prims.append(('wire', [tuple(p) for p in w['pts']], wire_c))
        for lab in sch['labels']:
            prims.append(('text', (tuple(lab['xy']), lab['net'], lab.get('just', 'lowerLeft') if lab.get('orient', 'R0') == 'R0' else 'R90', 0.06), wire_c))
        for p in sch['pins']:
            prims.append(('pin', tuple(p['xy']), pin_c))
            prims.append(('text', ((p['xy'][0] - 0.1, p['xy'][1]), p['name'], 'centerRight', 0.07), pin_c))
    pts = []
    for kind, data, _ in prims:
        if kind in ('line', 'poly', 'wire'):
            pts += data
        elif kind == 'pin':
            pts.append(data)
        else:
            pts.append(data[0])
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs) - margin, max(xs) + margin, min(ys) - margin, max(ys) + margin
    W, H = int((x1 - x0) * scale), int((y1 - y0) * scale) + 40
    img = Image.new('RGB', (W, H), 'white')
    d = ImageDraw.Draw(img)

    def P(p):
        return ((p[0] - x0) * scale, (y1 - p[1]) * scale + 40)

    def font(h):
        size = max(9, int(h * scale * 1.6))
        for f in ('/usr/share/fonts/dejavu/DejaVuSans.ttf', '/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf'):
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                pass
        return ImageFont.load_default()
    for kind, data, color in prims:
        if kind == 'line':
            d.line([P(p) for p in data], fill=color, width=2)
        elif kind == 'poly':
            d.polygon([P(p) for p in data], outline=color)
        elif kind == 'wire':
            d.line([P(p) for p in data], fill=color, width=3)
            for p in (data[0], data[-1]):
                q = P(p)
                d.ellipse([q[0] - 2, q[1] - 2, q[0] + 2, q[1] + 2], fill=color)
        elif kind == 'pin':
            q = P(data)
            d.rectangle([q[0] - 4, q[1] - 4, q[0] + 4, q[1] + 4], outline=color, width=2)
    for kind, data, color in prims:
        if kind != 'text':
            continue
        (x, y), text, just, h = data
        f = font(h)
        q = P((x, y))
        if just == 'R90':
            tw, th = d.textbbox((0, 0), text, font=f)[2:]
            tmp = Image.new('RGBA', (tw + 2, th + 2), (255, 255, 255, 0))
            ImageDraw.Draw(tmp).text((0, 0), text, fill=color, font=f)
            tmp = tmp.rotate(90, expand=True)
            img.paste(tmp, (int(q[0] - tmp.width), int(q[1] - tmp.height)), tmp)
            continue
        tw, th = d.textbbox((0, 0), text, font=f)[2:]
        ax = {'Left': 0, 'Center': -tw / 2, 'Right': -tw}
        ay = {'lower': -th, 'center': -th / 2, 'upper': 0}
        m = re.match(r'(lower|center|upper)(Left|Center|Right)', just)
        dx, dy = (ax[m.group(2)], ay[m.group(1)]) if m else (-tw / 2, -th / 2)
        d.text((q[0] + dx, q[1] + dy), text, fill=color, font=f)
    d.text((8, 8), title or (cell_name + ' / ' + view), fill=(0, 0, 0), font=font(0.09))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return {'path': str(path), 'size': [W, H]}


# ---------------------------------------------------------------- one-call entry

def prepare(family, variant, project=None, spec=None, binding=None, geometry=None, graph=None):
    """Electrical graph -> packaged graph + layout + checks for one variant.

    Pass `graph` (+ `binding`) to package an already generated design (graph.json)."""
    from .generator import make_graph
    from .catalog import default_project
    if graph is None:
        project = project or default_project(family, variant)
        graph, resolved = make_graph(project)
        binding = binding or resolved['binding']
    if binding is None:
        raise ValueError('binding is required with an explicit graph')
    if not all(v.get('library') for v in binding.get('roles', {}).values()):
        # netlist-only binding: draw with generic placeholder masters (preview/check only;
        # native export probes the masters of the user's resolved binding)
        binding = generic_binding()
    spec = spec if spec is not None else load_spec(family, variant, graph)
    packaged, full = package_variant(graph, spec)
    eq = equivalence_variant(graph, packaged, full)
    layout = build_layout(packaged, full, binding, geometry)
    conn = check_layout(layout)
    return {'graph': graph, 'packaged': packaged, 'spec': full, 'layout': layout, 'binding': binding,
            'equivalence': eq, 'connectivity': conn, 'ok': eq['equivalent'] and conn['ok']}


# ---------------------------------------------------------------- CLI

def describe(prep):
    """Text summary of the top cell for plan authors: masters, nets, R0 pin offsets."""
    lay = prep['layout']
    top = prep['packaged']
    geo = {}
    lines = ['TOP CELL %s  ports: %s' % (top['name'], ' '.join(p['name'] for p in top['ports'])), '',
             'Top-level instances (pin offsets are master-relative, orientation R0; apply orient then add xy):']
    for inst in top['instances']:
        if inst['role'] == 'subckt':
            sym = lay['cells'][inst['master']]['symbol']
            pins = sym['pins']
            kind = 'module ' + inst['master']
        else:
            key = ideal_master(inst['role']) if inst.get('ideal') else prep['binding']['roles'][inst['role']]['library'] + '/' + prep['binding']['roles'][inst['role']]['cell']
            ms = master_symbols().get(key, {})
            terms = {'p': 'PLUS', 'n': 'MINUS'} if inst.get('ideal') else prep['binding']['roles'][inst['role']]['terminals']
            pins = {t: ms.get('pins', {}).get(a) for t, a in terms.items()}
            kind = key + (' %s' % inst.get('parameters', {}).get('c', '') if inst['role'] == 'cap' else '')
        lines.append('  %-10s %s' % (inst['name'], kind))
        for t, net in inst['nodes'].items():
            lines.append('      %-8s net=%-10s pin@%s' % (t, net, pins.get(t)))
    lines.append('')
    lines.append('Module symbols (current spec); IND_* symbols may be redesigned in the plan:')
    for sub in prep['packaged']['subcircuits']:
        if any(i['role'] == 'subckt' and i['master'] == sub['name'] for i in top['instances']):
            lines.append('  %-10s ports %s' % (sub['name'], [p['name'] for p in sub['ports']]))
    return '\n'.join(lines)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='VCO schematic packaging: describe / check / preview')
    sp = ap.add_subparsers(dest='cmd', required=True)
    for name in ('describe', 'check', 'preview'):
        c = sp.add_parser(name)
        c.add_argument('family')
        c.add_argument('variant')
        c.add_argument('--spec', help='packaging spec file (default data/schematic/<family>/<variant>.json)')
        if name == 'preview':
            c.add_argument('--out', required=True)
            c.add_argument('--all', action='store_true', help='also render every module cell')
            c.add_argument('--scale', type=int, default=110)
    a = ap.parse_args(argv)
    spec = read_json(a.spec) if a.spec else None
    prep = prepare(a.family, a.variant, spec=spec)
    if a.cmd == 'describe':
        print(describe(prep))
        return 0
    report = {'equivalence': prep['equivalence'],
              'connectivity': {k: v for k, v in prep['connectivity']['cells'].items() if not v['ok']} or 'all cells ok',
              'ok': prep['ok']}
    if a.cmd == 'preview':
        out = Path(a.out)
        lay = prep['layout']
        files = [render_cell(lay, lay['top'], out / 'top_schematic.png', scale=a.scale),
                 render_cell(lay, lay['top'], out / 'top_symbol.png', scale=a.scale * 2, view='symbol')]
        for sub in prep['packaged']['subcircuits']:
            if a.all or sub['name'].startswith('IND'):
                files.append(render_cell(lay, sub['name'], out / (sub['name'] + '_symbol.png'), scale=a.scale * 2, view='symbol'))
            if a.all:
                files.append(render_cell(lay, sub['name'], out / (sub['name'] + '_schematic.png'), scale=a.scale))
        report['files'] = [f['path'] for f in files]
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0 if prep['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())


# ---------------------------------------------------------------- launch integration

def cli_register(sub):
    h = 'schematic packaging of a variant: describe the top level, check equivalence/connectivity, render previews'
    c = sub.add_parser('schematic', help=h, description=h)
    c.add_argument('action', choices=['describe', 'check', 'preview'])
    c.add_argument('family')
    c.add_argument('variant')
    c.add_argument('--spec', help='packaging spec file (default data/schematic/<family>/<variant>.json)')
    c.add_argument('--out', help='preview output folder (required for preview)')
    c.add_argument('--all', action='store_true', help='preview every module cell too')


def cli_dispatch(args):
    if args.command != 'schematic':
        return None
    from .cli import EXIT_OK, EXIT_INVALID
    if args.action == 'preview' and not args.out:
        raise ValueError('preview needs --out')
    spec = read_json(args.spec) if args.spec else None
    prep = prepare(args.family, args.variant, spec=spec)
    if args.action == 'describe':
        return describe(prep), EXIT_OK
    report = {'equivalence': prep['equivalence'],
              'connectivity': {k: v for k, v in prep['connectivity']['cells'].items() if not v['ok']} or 'all cells ok',
              'ok': prep['ok']}
    if args.action == 'preview':
        out = Path(args.out)
        lay = prep['layout']
        files = [render_cell(lay, lay['top'], out / 'top_schematic.png'),
                 render_cell(lay, lay['top'], out / 'top_symbol.png', scale=220, view='symbol')]
        for sub in prep['packaged']['subcircuits']:
            if args.all or sub['name'].startswith('IND'):
                files.append(render_cell(lay, sub['name'], out / (sub['name'] + '_symbol.png'), scale=220, view='symbol'))
            if args.all:
                files.append(render_cell(lay, sub['name'], out / (sub['name'] + '_schematic.png')))
        report['files'] = [f['path'] for f in files]
    return report, (EXIT_OK if prep['ok'] else EXIT_INVALID)
