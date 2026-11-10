"""Small-signal startup margin at the DC operating point (method of the reference DM testbench).

1. DC operating point of the generated design (same tb sources/codes, HB removed) -> gm, gmbs of every
   active device of the top-level core (cross-coupled devices and the bias device).
2. Differential AC with current forcing +1 A into OUTP and -1 A into OUTN (VDD and bias at nominal):
   - Z_on : devices active;
   - Z_off: the same devices with their terminal transconductance at f (Re Y21, Re Y_db, measured on a standalone copy
            biased at the in-circuit terminal voltages) cancelled by AC-only (high-passed) VCCS, so the DC point is
            unchanged; i.e. the loaded tank seen by the core (gds, device parasitics, bank, bias network kept).
   (Cancelling the intrinsic dcOpInfo gm at the terminals over-cancels by the rs/rg loss and, being DC-coupled, can
    move the DC point of a cross-coupled pair; both errors were seen on Hegazi 10 GHz.)
3. At f (default: the measured HB f0): Rp_diff = 1/Re(Y_off), G_loss = Re(Y_off), G_neg = G_loss - Re(Y_on),
   margin = G_neg / G_loss  (the documented flow targets margin ~2, i.e. gm_single * Rp_diff ~ 4 for a
   single cross-coupled pair; for CMOS/multi-core cores only the measured margin is meaningful).
"""
from __future__ import annotations

import math
import re
import shutil
from pathlib import Path

from .common import read_json, write_json
from .simulator import psf_values


def _strip_tb(tb):
    out = []
    for line in tb.splitlines():
        head = line.split()[0] if line.split() else ''
        if head in ('hb', 'hbnoise') or line.startswith('save '):
            continue
        out.append(line)
    return out


def _op_records(path, names):
    """gm/gmbs/vgs/vbs of devices from a psfascii dcOpInfo (instance 'DUT.X' or first 'DUT.X.*' with gm)."""
    import gzip
    text = (gzip.open(path, 'rt') if str(path).endswith('.gz') else open(path)).read()
    lines = text.splitlines()
    types, cur, depth, sec = {}, None, 0, None
    vals, inst, tname, buf = {}, None, None, None
    for raw in lines:
        s = raw.strip()
        if s in ('HEADER', 'TYPE', 'VALUE', 'END', 'SWEEP', 'TRACE'):
            sec = s
            continue
        if sec == 'TYPE':
            if depth == 0:
                m = re.match(r'^"([^"]+)"\s+STRUCT\($', s)
                cur = m.group(1) if m else None
                if cur:
                    types[cur] = []
            elif cur and depth == 1:
                fm = re.match(r'^"([^"]+)"\s+(FLOAT|INT|STRING)', s)
                if fm:
                    types[cur].append(fm.group(1))
            depth = max(0, depth + s.count('(') - s.count(')'))
            if depth == 0:
                cur = None
        elif sec == 'VALUE':
            if buf is not None:
                if s == ')':
                    vals[inst] = dict(zip(types.get(tname, []), buf))
                    buf = None
                    continue
                try:
                    buf.append(float(s))
                except ValueError:
                    buf.append(s)
                continue
            m = re.match(r'^"([^"]+)"\s+"([^"]+)"\s+\($', s)
            if m:
                inst, tname, buf = m.group(1), m.group(2), []
    out = {}
    for n in names:
        cands = [k for k in vals if (k == 'DUT.' + n or k.startswith('DUT.' + n + '.')) and 'gm' in vals[k]]
        if cands:
            r = vals[sorted(cands, key=len)[0]]
            out[n] = {k: r.get(k) for k in ('gm', 'gmbs', 'gds', 'vgs', 'vbs', 'vds', 'id', 'region')}
    return out


def _run(dest, tb_lines, circuit_text, models, solver, env, timeout_s):
    from .runner import run_bounded, wrap_env
    dest.mkdir(parents=True)
    (dest / 'circuit.scs').write_text(circuit_text)
    shutil.copy2(models, dest / 'models.scs')
    (dest / 'tb.scs').write_text('\n'.join(tb_lines) + '\n')
    argv = [solver, '-64', 'tb.scs', '+mt=1', '-format', 'psfascii', '-raw', 'psf', '+log', 'spectre.log']
    out = run_bounded(wrap_env(argv, env, 'loopgain'), dest, timeout_s, dest / 'stdout.log')
    shutil.rmtree(dest / 'tb.ahdlSimDB', ignore_errors=True)
    if out['exit_code'] or out['stop_reason']:
        raise RuntimeError('spectre failed in %s (%s)' % (dest, out['stop_reason'] or out['exit_code']))
    return out


def _vd(path):
    pts = psf_values(path, 'freq')
    return [(p['freq'], complex(p['OUTP']) - complex(p['OUTN'])) for p in pts if 'OUTP' in p and 'OUTN' in p]


def _at(curve, f):
    best = min(curve, key=lambda t: abs(t[0] - f))
    return best[1]


def _dc_nodes(path):
    out = {}
    for m in re.finditer(r'^"([^"]+)"\s+"V"\s+(\S+)$', Path(path).read_text(), re.M):
        out[m.group(1)] = float(m.group(2))
    return out


def _dut_map(tb_lines, circuit, top):
    """top-subckt port name -> testbench net name (for node-voltage lookup)."""
    ports = re.search(r'^subckt\s+%s\s+(.*)$' % re.escape(top), circuit, re.M).group(1).split()
    dut = next(l for l in tb_lines if re.match(r'^\S+\s+\([^)]*\)\s+%s\b' % re.escape(top), l))
    inst = dut.split()[0]
    nets = re.match(r'^\S+\s+\(([^)]*)\)', dut).group(1).split()
    return inst, dict(zip(ports, nets))


def _terminal_gm(dest, devices, tb_lines, f_hz, solver, env, timeout_s, models):
    """Terminal-level transconductances Re(dId/dVg), Re(dId/dVb) at f of each device biased at its in-circuit DC
    terminal voltages (standalone copy; includes rg/rs/substrate of the PDK RF model)."""
    opts = [l for l in tb_lines if re.match(r'^\S+\s+options\b', l)]
    net = ['simulator lang=spectre', 'global 0', 'include "models.scs"'] + opts
    saves = []
    for name, d in devices.items():
        for drv in ('g', 'b'):
            tag = '%s_%s' % (name, drv)
            for t in ('d', 'g', 's', 'b'):
                net.append('V%s_%s (n%s_%s 0) vsource dc=%.12g%s' % (tag, t, tag, t, d['v'][t], ' mag=1' if t == drv else ''))
            net.append('X%s (%s) %s' % (tag, ' '.join('n%s_%s' % (tag, t) for t in d['order']), d['model_tail']))
            saves.append('V%s_d:p' % tag)
    net += ['tg ac values=[%.12g]' % f_hz, 'save ' + ' '.join(saves)]
    dest.mkdir(parents=True)
    shutil.copy2(models, dest / 'models.scs')
    (dest / 'tb.scs').write_text('\n'.join(net) + '\n')
    from .runner import run_bounded, wrap_env
    argv = [solver, '-64', 'tb.scs', '-format', 'psfascii', '-raw', 'psf', '+log', 'spectre.log']
    out = run_bounded(wrap_env(argv, env, 'loopgain'), dest, timeout_s, dest / 'stdout.log')
    if out['exit_code'] or out['stop_reason']:
        raise RuntimeError('spectre failed in %s' % dest)
    pt = psf_values(dest / 'psf' / 'tg.ac')[0]
    return {n: {'gm_ext': -complex(pt['V%s_g_d:p' % n]).real, 'gmb_ext': -complex(pt['V%s_b_d:p' % n]).real} for n in devices}


def _inject(lines, top_end, extra):
    return lines[:top_end] + extra + lines[top_end:]


def run(generated_dir, output_dir, f_hz=None, timeout_s=300, span=0.4, points=401):
    """One design idea for every structure: gm x Rp_diff at the drain port of the cross-coupled pair.  Port = drains of the cross-coupled pair; multi-core cores (more than two drain nodes) are first
    merged into one core by tying in-phase drain nodes together with ideal wires (phase and amplitude from the HB
    fundamental of the design; the mismatch is reported)."""
    from . import site
    gen = Path(generated_dir).resolve()
    project = read_json(gen / 'project.json')
    graph = read_json(gen / 'graph.json')
    binding = project['binding'] if isinstance(project['binding'], dict) else read_json(gen / 'binding.json')
    dest = Path(output_dir).resolve()
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError('output exists and is not empty: %s' % dest)
    dest.mkdir(parents=True, exist_ok=True)
    solver, env = site.spectre(), site.cadence_env()
    f0 = float(f_hz or float(project['parameters']['frequency_guess_ghz']) * 1e9)
    core = [i for i in graph['instances'] if i['role'] in ('nmos', 'pmos')]
    names = [i['name'] for i in core]
    tb = _strip_tb((gen / 'tb.scs').read_text())
    circuit = (gen / 'circuit.scs').read_text()
    models = gen / 'models.scs'
    order = {r: binding['roles'][r].get('netlist_terminal_order', ['d', 'g', 's', 'b']) for r in ('nmos', 'pmos') if r in binding.get('roles', {})}
    inst, ports = _dut_map(tb, circuit, graph['name'])
    lines = circuit.splitlines()
    top_start = next(i for i, l in enumerate(lines) if l.startswith('subckt ' + graph['name'] + ' '))
    top_end = next(i for i in range(top_start + 1, len(lines)) if lines[i].startswith('ends'))
    devices = {}
    for i in range(top_start + 1, top_end):
        m = re.match(r'^(\S+)\s+\(([^)]*)\)\s+(.*)$', lines[i])
        if m and m.group(1) in names:
            role = next(x for x in core if x['name'] == m.group(1))['role']
            o = order.get(role, ['d', 'g', 's', 'b'])
            devices[m.group(1)] = {'line': i, 'role': role, 'order': o, 'model_tail': m.group(3), 'term': dict(zip(o, m.group(2).split()))}
    pair = [n for n in devices if not n.startswith('MB')]
    # reference pair: two devices of one type whose gates are driven from the other side
    ref = sorted(pair, key=lambda n: (devices[n]['term']['d'] not in ('OUTP', 'OUTN'), devices[n]['role'], n))[:2]
    p_node, n_node = devices[ref[0]]['term']['d'], devices[ref[1]]['term']['d']

    def save_name(node):
        tbn = ports.get(node)
        return tbn if tbn is not None else '%s.%s' % (inst, node)

    def ac_tail(extra_nodes):
        nodes = sorted({p_node, n_node} | set(extra_nodes))
        return ['acdm ac start=%g stop=%g lin=%d' % (f0 * (1 - span), f0 * (1 + span), points),
                'acf0 ac values=[%.12g]' % f0,
                'save ' + ' '.join(save_name(x) for x in nodes)]
    force = ['LGFORCE_P (0 %s) isource dc=0 mag=1' % p_node, 'LGFORCE_N (0 %s) isource dc=0 mag=-1' % n_node]
    tb_ac_base = [l for l in tb if not l.startswith('dcOpInfo')]
    # 1. DC of the original circuit
    _run(dest / 'dc', tb, circuit, models, solver, env, timeout_s)
    nodes0 = _dc_nodes(dest / 'dc' / 'psf' / 'dcOp.dc')

    def vnode(nodes, n):
        tbn = ports.get(n)
        if tbn is not None:
            return 0.0 if tbn in ('0', 'gnd!') else nodes[tbn]
        return nodes['%s.%s' % (inst, n)]
    # 2. merge a multi-core into one core (ideal wires between in-phase drain nodes)
    drains = sorted({devices[n]['term']['d'] for n in pair})
    merges = []
    if len(drains) > 2:
        others = [d for d in drains if d not in (p_node, n_node)]
        # phase relation from the large-signal steady state (HB fundamental), not from a small-signal probe
        raw = (gen / 'tb.scs').read_text().splitlines()
        tb_hb = [l for l in raw if not (l.split() and l.split()[0] == 'hbnoise') and not l.startswith('save ')
                 and not l.startswith('dcOpInfo')]
        tb_hb.append('save ' + ' '.join(save_name(x) for x in sorted({p_node, n_node} | set(others))))
        _run(dest / 'phase', tb_hb, circuit, models, solver, env, max(timeout_s, 600))
        fd = psf_values(next((dest / 'phase/psf').glob('hb.fd*')))
        pt = min((q for q in fd if q['freq'] > 0), key=lambda q: q['freq'])
        vp, vn = complex(pt[save_name(p_node)]), complex(pt[save_name(n_node)])
        for d in others:
            vd = complex(pt[save_name(d)])
            tgt, vt = min(((p_node, vp), (n_node, vn)), key=lambda t: abs(vd / t[1] - 1))
            ratio = vd / vt
            dv = vnode(nodes0, d) - vnode(nodes0, tgt)
            merges.append({'node': d, 'to': tgt, 'amp_ratio': abs(ratio), 'phase_deg': math.degrees(math.atan2(ratio.imag, ratio.real)),
                           'dc_difference_v': dv})
    # signal-only ideal wire: 1 F is a short at RF and open at DC, so the operating point of each core is kept
    wires = ['LGWIRE_%d (%s %s) capacitor c=1' % (k, m_['node'], m_['to']) for k, m_ in enumerate(merges)]
    base_lines = _inject(lines, top_end, wires)
    shift = len(wires)       # wires are inserted at top_end, device line indexes are unchanged
    # 3. DC of the merged circuit, terminal gm of every core device at f
    _run(dest / 'dc_m', tb, '\n'.join(base_lines) + '\n', models, solver, env, timeout_s) if merges else None
    nodes = _dc_nodes(dest / ('dc_m' if merges else 'dc') / 'psf' / 'dcOp.dc')
    ops = _op_records(next((dest / ('dc_m' if merges else 'dc') / 'psf').glob('dcOpInfo.info*')), names)
    for n, d in devices.items():
        d['v'] = {t: vnode(nodes, d['term'][t]) for t in ('d', 'g', 's', 'b')}
    tg = _terminal_gm(dest / 'termgm', devices, tb, f0, solver, env, timeout_s, models)
    # 4. AC on / off at the drain port; the cancelling sources see an AC-only copy of vgs / vbs
    gates = [devices[n]['term']['g'] for n in ref]
    tb_ac = tb_ac_base + ac_tail(gates)
    top_end_m = top_end + shift
    _run(dest / 'ac_on', tb_ac, '\n'.join(_inject(base_lines, top_end_m, force)) + '\n', models, solver, env, timeout_s)
    off = []
    for i, l in enumerate(base_lines):
        off.append(l)
        for n, d in devices.items():
            if d['line'] != i:
                continue
            t = d['term']
            for kind, ctl, gmv in (('M', t['g'], tg[n]['gm_ext']), ('B', t['b'], tg[n]['gmb_ext'])):
                tag = '%s%s' % (kind, n)
                off += ['E_lg%s (lgx%s 0 %s %s) vcvs gain=1' % (tag, tag, ctl, t['s']),
                        'C_lg%s (lgx%s lgh%s) capacitor c=1' % (tag, tag, tag),
                        'R_lg%s (lgh%s 0) resistor r=1' % (tag, tag),
                        'G_lg%s (%s %s) bsource i=%.12g*v(lgh%s)' % (tag, t['d'], t['s'], -gmv, tag)]
    top_end_off = next(i for i in range(len(off)) if off[i].startswith('ends ' + graph['name']) or (i > top_start and off[i].strip() == 'ends ' + graph['name']))
    _run(dest / 'ac_off', tb_ac, '\n'.join(_inject(off, top_end_off, force)) + '\n', models, solver, env, timeout_s)
    off_nodes = _dc_nodes(dest / 'ac_off' / 'psf' / 'dcOp.dc')
    dc_shift = max(abs(off_nodes[k] - v) for k, v in nodes.items() if k in off_nodes)

    def curve(path):
        return [(q['freq'], q) for q in psf_values(path)]
    con, coff = curve(dest / 'ac_on/psf/acdm.ac'), curve(dest / 'ac_off/psf/acdm.ac')
    vdiff = lambda q: complex(q[save_name(p_node)]) - complex(q[save_name(n_node)])
    # evaluate at exactly f0 (single-point AC); the sweep is kept for the tank peak (review T-0084)
    try:
        qon = psf_values(dest / 'ac_on/psf/acf0.ac')[0]
        qoff = psf_values(dest / 'ac_off/psf/acf0.ac')[0]
    except (OSError, IndexError):
        qon, qoff = _at(con, f0), _at(coff, f0)
    z_on0, z_off0 = vdiff(qon), vdiff(qoff)
    y_on, y_off = 1 / z_on0, 1 / z_off0
    g_loss = y_off.real
    g_neg = g_loss - y_on.real
    # gate-to-drain ratio of the reference pair (1 when the gates sit on the opposite drains)
    ga, gb = gates
    vg = complex(qoff[save_name(ga)]) - complex(qoff[save_name(gb)])
    n_ratio = (-vg / z_off0).real
    peak = max(coff, key=lambda t: abs(vdiff(t[1])))
    for n in ops:
        ops[n].update({'gm_ext_at_f': tg[n]['gm_ext'], 'gmb_ext_at_f': tg[n]['gmb_ext'], 'terminal_v': devices[n]['v'],
                       'drain': devices[n]['term']['d'], 'gate': devices[n]['term']['g']})
    rp = 1 / g_loss if g_loss > 0 else None
    # every device whose drain is on the p side (after merging) contributes; gm_sum = n+p gm of a CMOS core
    p_side = {p_node} | {m_['node'] for m_ in merges if m_['to'] == p_node}
    gm_sum_ext = sum(tg[n]['gm_ext'] for n in pair if devices[n]['term']['d'] in p_side)
    gm_sum_op = sum(float(ops[n]['gm']) for n in pair if devices[n]['term']['d'] in p_side and n in ops)
    warn = [m_ for m_ in merges if not (0.67 < m_['amp_ratio'] < 1.5 and abs(m_['phase_deg']) < 30)]
    res = {'schema': 'vco.loopgain.v3', 'generated_dir': str(gen), 'f_hz': f0,
           'port': [p_node, n_node], 'reference_pair': ref, 'merged': merges,
           'valid': bool(rp) and not warn, 'warning': ('poorly matched merge: %s' % warn) if warn else None,
           'Rp_diff_ohm': rp, 'Zoff_at_f': [z_off0.real, z_off0.imag], 'Zon_at_f': [z_on0.real, z_on0.imag],
           'G_loss_S': g_loss, 'G_neg_S': g_neg, 'margin': g_neg / g_loss if g_loss > 0 else None,
           'gate_drain_ratio': n_ratio, 'gm_sum_ext_S': gm_sum_ext, 'gm_sum_op_S': gm_sum_op,
           'gm_Rp_ext': gm_sum_ext * rp if rp else None, 'gm_Rp_op': gm_sum_op * rp if rp else None,
           'tank_peak': {'f_hz': peak[0], 'abs_ohm': abs(vdiff(peak[1]))}, 'ac_off_dc_shift_v': dc_shift, 'devices': ops,
           'note': ('design metric: gm x Rp_diff at the DRAIN port (target ~4; transformer cores are judged at the drain '
                    'only, the gate/drain ratio n is informational). gm = sum of the devices driving one side (n+p for CMOS, '
                    'terminal gm at f; gm_op = intrinsic dcOpInfo). Rp_diff = drain-port impedance with every core '
                    'device\'s terminal gm cancelled by AC-only sources (gds, device losses, bank, bias kept). margin = '
                    'G_neg/G_loss measured with the real loop (for transformer cores it already contains n). Multi-cores '
                    'are merged by ideal wires between drains that are in phase in HB. All values at the DC operating '
                    'point (startup); a self-biased CMOS core carries far more current there than in steady state.')}
    write_json(dest / 'loopgain.json', res)
    return res


def cli_register(sub):
    c = sub.add_parser('loopgain', help='startup margin at the DC OP: Rp_diff (gm-cancelled DM AC), G_neg, gm*Rp (reference DM method)',
                       description='startup margin at the DC OP: Rp_diff (gm-cancelled DM AC), G_neg, gm*Rp (reference DM method)')
    c.add_argument('generated')
    c.add_argument('--output', required=True)
    c.add_argument('--f-ghz', type=float, help='evaluation frequency (default: measured f0 if you pass it, else the HB guess)')
    c.add_argument('--timeout', type=float, default=300)


def cli_dispatch(args):
    from .cli import EXIT_OK
    if args.command != 'loopgain':
        return None
    return run(args.generated, args.output, f_hz=args.f_ghz * 1e9 if args.f_ghz else None, timeout_s=args.timeout), EXIT_OK
