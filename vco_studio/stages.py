"""Verification interfaces and evidence-preserving layout/delivery packages.

These stages never change task.json, freeze.json or the nominal qualification.
Each invocation keeps its own inputs/results; status.json points at the latest one.
"""
from __future__ import annotations

import copy
import csv
import datetime
import json
import math
import re
import shlex
import shutil
import uuid
from pathlib import Path

from .common import read_json, write_json, sha256, electrical_hash
from .autosize import Executor, BudgetExhausted
from . import flow

OFFSETS = (10000, 100000, 1000000, 10000000)
STAGES = ('pvt', 'mc', 'handoff', 'postsim')
PASSIVE = re.compile(r'(?:res|mim|mom|rtmom|rfres|rfmim|rfrtmom|disres|var|rfmvar|rfind)', re.I)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _path(root, value):
    p = Path(value)
    return p if p.is_absolute() else root / p


def _statuses(root):
    p = root / 'verification/status.json'
    data = read_json(p) if p.is_file() else {}
    for stage in STAGES:
        data.setdefault(stage, {'status': 'not_started', 'updated': None, 'summary': None, 'runs': []})
    return data


def _status(root, stage, status, summary=None, runs=None):
    data = _statuses(root)
    data[stage] = {'status': status, 'updated': _now(), 'summary': summary, 'runs': runs or []}
    write_json(root / 'verification/status.json', data)


def _context(root, candidate=None, require_freeze=False):
    state = read_json(root / 'task.json')
    freeze = read_json(root / 'freeze.json') if (root / 'freeze.json').is_file() else None
    if require_freeze and not freeze:
        raise ValueError('This operation requires freeze.json')
    if freeze:
        if candidate and candidate != freeze['candidate']:
            raise ValueError('--candidate differs from frozen candidate')
        candidate = freeze['candidate']
    if not candidate:
        raise ValueError('Unfrozen task requires --candidate and --codes; results are provisional')
    c = flow._candidate(state, candidate)
    directory = _path(root, c['directory']).resolve()
    manifest = read_json(directory / 'manifest.json')
    for name, expected in manifest['files'].items():
        if sha256(directory / name) != expected:
            raise ValueError('Candidate input changed: ' + name)
    for name, expected in manifest.get('model_files', {}).items():
        if sha256(name) != expected:
            raise ValueError('Candidate model changed: ' + name)
    if freeze and freeze.get('physical_identity') != manifest.get('physical_identity'):
        raise ValueError('Frozen physical identity differs from candidate')
    return state, freeze, c, directory, read_json(directory / 'project.json')


def _codes(value, project, freeze):
    if value in (None, 'adopted', 'endpoints'):
        if not freeze:
            if value != 'endpoints':
                raise ValueError('Unfrozen task requires explicit --codes (file or endpoints)')
            rows = [{k: v['value'] for k, v in project['registers'].items()} for _ in range(2)]
            rows[0]['main'] = 0
            rows[1]['main'] = 2 ** project['registers']['main']['bits'] - 1
        else:
            rows = [{k: v['value'] if isinstance(v, dict) else v for k, v in row.items()}
                    for row in freeze['adopted_control_states']]
            if value == 'endpoints':
                rows = [min(rows, key=lambda r: r['main']), max(rows, key=lambda r: r['main'])]
    else:
        rows = read_json(value)
    flow._validate_code_map(rows, project['registers'])
    complete = []
    for row in rows:
        code = {k: row.get(k, v['value']) for k, v in project['registers'].items()}
        if code not in complete:
            complete.append(code)
    return complete


def _code_key(code):
    return json.dumps(code, sort_keys=True)


def _with_code(project, code):
    p = copy.deepcopy(project)
    for k, v in code.items():
        p['registers'][k]['value'] = v
    return p


def model_sections(path):
    # Ignore commented-out section declarations (including C-style block comments).
    text = Path(path).read_text(errors='replace')
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return set(re.findall(r'^\s*section\s+([^\s/]+)', text, re.M | re.I))


def _section_options(section, corner):
    aliases = {'fs': ('fs', 'fnsp', 'nsp'), 'sf': ('sf', 'snfp')}.get(corner, (corner,))
    if not re.search(r'(^|_)tt(?=_|$)', section):
        return [section] if corner == 'tt' else []
    return [re.sub(r'(^|_)tt(?=_|$)', lambda m: m[1] + a, section) for a in aliases]


def derive_corner(binding, corner, inventory=None):
    """Return an explicit include map; no missing active section is silently retained."""
    if corner not in ('tt', 'ff', 'ss', 'fs', 'sf'):
        raise ValueError('Unknown corner: ' + corner)
    inventory = inventory if inventory is not None else {}
    includes, fallback, missing = [], [], []
    for item in binding.get('model_includes', []):
        row = copy.deepcopy(item)
        path, section = row['path'], row.get('section')
        if not section:
            includes.append(row)
            continue
        if path not in inventory:
            inventory[path] = model_sections(path)
        found = next((s for s in _section_options(section, corner) if s in inventory[path]), None)
        if found is None and corner in ('fs', 'sf') and PASSIVE.search(section) and section in inventory[path]:
            found = section
            fallback.append({'path': path, 'section': section, 'reason': 'passive retained at TT'})
        if found is None:
            missing.append({'path': path, 'original': section, 'attempted': _section_options(section, corner)})
        else:
            row['section'] = found
            includes.append(row)
    return {'corner': corner, 'status': 'unsupported' if missing else 'supported',
            'model_includes': includes, 'fallbacks': fallback, 'missing': missing}


def _limit(count, maximum):
    if maximum < 1:
        raise ValueError('--max-runs must be positive')
    if count > maximum:
        raise ValueError('Plan needs %d runs, exceeds --max-runs %d; reduce corners/temps/vdd/codes/runs or raise the cap' % (count, maximum))


def _invocation(root, stage):
    base = root / 'verification' / stage
    base.mkdir(parents=True, exist_ok=True)
    dest = base / ('run-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
    dest.mkdir()
    return dest


def _summary(root, stage, data, status):
    path = root / 'verification' / stage / 'summary.json'
    write_json(path, data)
    _status(root, stage, status, str(path), data.get('points', []))
    return data


def _metrics(result, spec):
    m = result.get('metrics', {})
    violations = flow.assess_metrics(m, spec) if result.get('status') == 'measured' else ['simulation_failed']
    for ofs in OFFSETS:
        for key in ('pn_%d_dbc_hz' % ofs, 'fom_%d_db' % ofs):
            if not isinstance(m.get(key), (int, float)) or not math.isfinite(m[key]):
                violations.append('missing_or_invalid_' + key)
    return {'status': result.get('status'), 'metrics': m, 'pass': not violations,
            'violations': violations, 'error': result.get('error'), 'result': str(Path(result['path']) / 'result.json')}


def _baseline(root, candidate, freeze):
    rows, cache = {}, {}
    for name in (freeze or {}).get('adopted_results', candidate.get('results', [])):
        path = _path(root, name)
        r = read_json(path)
        if r.get('status') == 'measured':
            directory = _path(root, candidate['directory'])
            manifest = read_json(directory / 'manifest.json')
            flow._verify_result(path, manifest['physical_identity'], sha256(directory / 'circuit.scs'), hash_cache=cache)
            key = _code_key({k: v['value'] for k, v in r['registers'].items()})
            rows[key] = {'metrics': r['metrics'], 'result': str(path)}
    return rows


def _delta(metrics, base):
    if not base or not metrics:
        return None
    b = base['metrics']
    keys = ('frequency_hz', 'power_mw') + tuple('pn_%d_dbc_hz' % o for o in OFFSETS)
    out = {k: metrics[k] - b[k] for k in keys if k in metrics and k in b}
    for k in ('frequency_hz', 'power_mw'):
        if k in out and b[k]:
            out[k + '_pct'] = 100 * out[k] / b[k]
    out['baseline_result'] = base['result']
    return out


def _worst(measured):
    if not measured:
        return {}
    keys = set.intersection(*(set(m) for m in measured))
    return {k: (min(m[k] for m in measured) if k.startswith('fom_') else max(m[k] for m in measured))
            for k in sorted(keys)}


def _mc_worker(args):
    """Private bounded worker invoked *inside* the site's simulation lock."""
    from .runner import run_bounded
    command, timeout = json.loads(args[0]), float(args[1])
    outcome = run_bounded(command, Path.cwd(), timeout, Path('solver.stdout.log'))
    write_json('solver-outcome.json', outcome)
    raise SystemExit(0 if outcome['exit_code'] == 0 and outcome['stop_reason'] is None else 1)


def band_ok(fs, spec):
    """Same band qualification as flow-freeze: center+TR for center_tr specs, edge coverage otherwise."""
    fs = sorted(fs)
    if len(fs) < 2:
        return False
    if spec.get('band_mode') == 'center_tr':
        center = (fs[0] + fs[-1]) / 2
        return (abs(center / spec['center_hz'] - 1) <= spec.get('center_tolerance', 0.02)
                and (fs[-1] - fs[0]) / center * 100 >= spec['tr_min_pct'])
    low, high = spec['frequency_band_hz']
    return fs[0] <= low and fs[-1] >= high


def _aggregate(points, spec):
    groups = {}
    for row in points:
        key = (row.get('corner', 'nominal'), row.get('temp_c'), row.get('vdd'))
        groups.setdefault(key, []).append(row)
    out = []
    low, high = spec['frequency_band_hz']
    for (corner, temp, vdd), rows in groups.items():
        measured = [r['metrics'] for r in rows if r.get('status') == 'measured']
        complete = len(measured) == len(rows)
        fs = sorted(m['frequency_hz'] for m in measured)
        gap = max((b-a for a,b in zip(fs, fs[1:])), default=0)
        covers = bool(complete and fs and band_ok(fs, spec))
        gap_ok = spec.get('max_step_hz') is None or gap <= spec['max_step_hz']
        out.append({'corner': corner, 'temp_c': temp, 'vdd': vdd, 'complete': complete,
                    'frequency_range_hz': [fs[0], fs[-1]] if fs else None, 'covers_spec_band': covers,
                    'max_step_hz': gap, 'step_pass': gap_ok,
                    'pass': covers and gap_ok and all(r.get('pass', False) for r in rows),
                    'worst': _worst(measured)})
    return out


def _pvt(root, state, freeze, candidate, project, codes, corners, temps, vdd_pct, max_runs, dry_run, timeout):
    inventory = {}
    maps = [derive_corner(project['binding'], corner, inventory) for corner in corners]
    supported = [m for m in maps if m['status'] == 'supported']
    _limit(len(supported) * len(temps) * len(vdd_pct) * len(codes), max_runs)
    dest = _invocation(root, 'pvt')
    plan = []
    for cm in supported:
        for temp in temps:
            for pct in vdd_pct:
                vdd = float(project['parameters']['vdd']) * (1 + pct / 100)
                if not math.isfinite(vdd) or vdd <= 0 or not math.isfinite(temp):
                    raise ValueError('Temperature must be finite and supply must be positive and finite')
                for code in codes:
                    plan.append({'corner': cm['corner'], 'temp_c': temp, 'vdd': vdd, 'vdd_pct': pct, 'code': code})
    write_json(dest / 'plan.json', {'corners': maps, 'points': plan, 'solver_runs': len(plan)})
    _status(root, 'pvt', 'prepared' if dry_run else 'running', str(dest / 'plan.json'))
    ex = Executor(dest, max_runs, timeout_s=timeout)
    bases = _baseline(root, candidate, freeze)
    points = []
    from .generator import generate
    for i, row in enumerate(plan):
        p = _with_code(project, row['code'])
        p['binding']['model_includes'] = next(m['model_includes'] for m in maps if m['corner'] == row['corner'])
        p['parameters'].update(temp_c=row['temp_c'], vdd=row['vdd'])
        if dry_run:
            generated = dest / ('d%03d-pvt' % (i + 1))
            generate(p, generated)
            row.update(status='prepared', generated=str(generated))
        else:
            r = ex.measure(p, 'pvt')
            row.update(_metrics(r, state['spec']), generated=r['_generated'])
        points.append(row)
        _status(root, 'pvt', 'prepared' if dry_run else 'running', str(dest / 'plan.json'), points)
    # Exact same-code nominal TT points take precedence over historic nominal samples.
    for row in points:
        if row.get('status') == 'measured' and row['corner'] == 'tt' and row['temp_c'] == project['parameters']['temp_c'] and row['vdd_pct'] == 0:
            bases[_code_key(row['code'])] = {'metrics': row['metrics'], 'result': row['result']}
    for row in points:
        row['delta_tt'] = _delta(row.get('metrics'), bases.get(_code_key(row['code'])))
    groups = [] if dry_run else _aggregate(points, state['spec'])
    complete = bool(points) and all(r.get('status') == 'measured' for r in points) and len(supported) == len(maps)
    result = {'schema': 'vco.verification.pvt.v1', 'candidate': candidate['id'], 'provisional': not bool(freeze),
              'plan': str(dest / 'plan.json'), 'corners': maps, 'points': points, 'condition_groups': groups,
              'pass': None if dry_run else complete and all(g['pass'] for g in groups),
              'scope': 'sampled codes at each separate corner/temperature/supply; no interpolation qualification'}
    result['corner_worst'] = {c: _worst([r['metrics'] for r in points if r['corner'] == c and r.get('status') == 'measured']) for c in corners}
    write_json(dest / 'summary.json', result)
    return _summary(root, 'pvt', result, 'prepared' if dry_run and supported else ('done' if complete else 'failed'))


class _ModifiedExecutor(Executor):
    """Reuse Executor budget/site policy for a netlist transformation before execution."""
    def prepare(self, project, label, transform):
        from .generator import generate
        dest = self.workdir / ('d%03d-%s' % (len(list(self.workdir.glob('d[0-9]*'))) + 1, label))
        generate(project, dest)
        transform(dest)
        manifest = read_json(dest / 'manifest.json')
        for name in manifest['files']:
            manifest['files'][name] = sha256(dest / name)
        manifest['stage_source_physical_identity'] = manifest['physical_identity']
        manifest['physical_identity'] = electrical_hash({'source': manifest['physical_identity'], 'files': manifest['files']})
        manifest['qualification'] = 'modified_stage_inputs_not_nominal_freeze'
        write_json(dest / 'manifest.json', manifest)
        return dest

    def execute(self, dest):
        if self.used >= self.budget:
            raise BudgetExhausted('--max-runs exhausted')
        self.used += 1
        out = dest / 'sim'
        if self.lock_command:
            from .autosize import _launch
            from .runner import run_bounded
            # launch resolves its interpreter from this same site/environment.
            cmd = list(self.lock_command) + [_launch(), 'simulate', str(dest), '--output', str(out), '--timeout', str(self.timeout_s)]
            run_bounded(cmd, dest, self.timeout_s + 7200, dest / 'lock.log')
            try:
                return read_json(out / 'result.json')
            except (OSError, ValueError):
                return {'status': 'failed', 'path': str(out), 'error': 'No result.json after locked stage execution; inspect lock.log'}
        from .simulator import run
        return run(dest, out, self.timeout_s)


def _subckts(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    text = re.sub(r'(?m)//.*$|^\s*\*.*$', '', text)
    text = re.sub(r'\n\s*\+\s*', ' ', text)
    text = re.sub(r'\\\s*\n', ' ', text)
    out = {}
    for match in re.finditer(r'^\s*\.?subckt\s+(\S+)\s+([^\n]*)', text, re.M | re.I):
        name, body = match.groups()
        pins = re.split(r'\bparameters\b|\bparams:', body, flags=re.I)[0].replace('(', ' ').replace(')', ' ').split()
        pins = [x for x in pins if '=' not in x]
        if name in out:
            raise ValueError('Duplicate extracted subckt: ' + name)
        out[name] = pins
    return out


def _postsim(root, state, freeze, candidate, directory, project, codes, netlist, top, max_runs, dry_run, timeout):
    if not netlist:
        raise ValueError('postsim requires --netlist extracted.scs')
    source = Path(netlist).resolve()
    text = source.read_text()
    graph = read_json(directory / 'graph.json')
    expected_name = graph['name']
    top = top or expected_name
    pins = [p['name'] for p in graph['ports']]
    actual = _subckts(text)
    if top != expected_name or actual.get(top) != pins:
        raise ValueError('Extracted top/port mismatch: expected %s %s; selected %s %s; available %s' %
                         (expected_name, pins, top, actual.get(top), sorted(actual)))
    # External includes need an explicit, portable extraction snapshot. Reject ambiguous dependencies.
    if re.search(r'^\s*(?:\.?include|\.lib)\s', text, re.M | re.I):
        raise ValueError('Extracted netlist must be self-contained (flatten/include dependencies before postsim)')
    _limit(len(codes), max_runs)
    dest = _invocation(root, 'postsim')
    ex = _ModifiedExecutor(dest, max_runs, timeout_s=timeout)
    bases = _baseline(root, candidate, freeze)
    points = []
    _status(root, 'postsim', 'prepared' if dry_run else 'running', str(dest))
    for code in codes:
        generated = ex.prepare(_with_code(project, code), 'postsim', lambda d: (d / 'circuit.scs').write_text(text))
        row = {'code': code, 'generated': str(generated)}
        if dry_run:
            row['status'] = 'prepared'
        else:
            row.update(_metrics(ex.execute(generated), state['spec']))
            row['delta_presim'] = _delta(row.get('metrics'), bases.get(_code_key(code)))
        points.append(row)
        _status(root, 'postsim', 'prepared' if dry_run else 'running', str(dest), points)
    groups = [] if dry_run else _aggregate(points, state['spec'])
    data = {'schema': 'vco.verification.postsim.v1', 'candidate': candidate['id'], 'provisional': not bool(freeze),
            'extracted': str(source), 'extracted_sha256': sha256(source), 'top_subckt': top, 'ports': pins,
            'points': points, 'condition_groups': groups, 'pass': None if dry_run else all(g['pass'] for g in groups)}
    write_json(dest / 'comparison.json', data)
    with open(dest / 'comparison.csv', 'w', newline='') as f:
        fields = ['code', 'delta_f_hz', 'delta_p_mw'] + ['delta_pn_%d_db' % o for o in OFFSETS]
        writer = csv.writer(f); writer.writerow(fields)
        for row in points:
            d = row.get('delta_presim') or {}
            writer.writerow([_code_key(row['code']), d.get('frequency_hz'), d.get('power_mw')] + [d.get('pn_%d_dbc_hz' % o) for o in OFFSETS])
    return _summary(root, 'postsim', data, 'prepared' if dry_run else ('done' if all(p.get('status') == 'measured' for p in points) else 'failed'))


def _mc_binding(binding):
    b = copy.deepcopy(binding)
    inventory, selected = {}, []
    for item in b.get('model_includes', []):
        path, section = item['path'], item.get('section', '')
        if path not in inventory:
            inventory[path] = model_sections(path)
        options = _section_options(section, 'mc')
        found = next((s for s in options if s in inventory[path]), None)
        if found:
            selected.append({'path': path, 'original': section, 'section': found})
            item['section'] = found
    return b, selected, {p: sorted(s for s in names if re.match(r'mc(?:_|$)', s, re.I)) for p, names in inventory.items()}


def _mc_tb(dest, runs):
    p = dest / 'tb.scs'
    lines = p.read_text().splitlines()
    analyses, outside = [], []
    for line in lines:
        (analyses if re.match(r'^(?:dcOp|dcOpInfo|hb|hbnoise)\s', line) else outside).append(line)
    if not any(line.startswith('hb ') for line in analyses):
        raise ValueError('Monte Carlo requires an HB/HBnoise testbench')
    p.write_text('\n'.join(outside) + '\nmc1 montecarlo numruns=%d seed=1 variations=all savefamilyplots=yes {\n' % runs +
                 '\n'.join('  ' + line for line in analyses) + '\n}\n')


def _execute_mc(ex, generated, project, runs):
    """MC families have native sample-prefixed PSF names, unlike simulator.run's single HB."""
    from . import site
    from .runner import run_bounded, wrap_env
    from .simulator import parse_result
    _limit(ex.used + runs, ex.budget)
    ex.used += runs
    solver = project.get('tools', {}).get('spectre') or site.spectre()
    if not solver or not Path(solver).is_file():
        raise FileNotFoundError('Monte Carlo requires VCO_SPECTRE/site spectre')
    out = generated / 'sim'; out.mkdir()
    for name in ('circuit.scs', 'tb.scs', 'models.scs'):
        shutil.copy2(generated / name, out / name)
    argv = [str(solver), '-64', 'tb.scs', '+mt=1', '-format', 'psfascii', '-raw', 'psf', '+log', 'spectre.log']
    native = project.get('tools', {}).get('native_env') or site.cadence_env()
    native_command = wrap_env(argv, native, 'studio-mc-env')
    command = native_command
    if ex.lock_command:
        # The lock wait has its own allowance; the worker bounds the solver itself.
        worker = ('import sys; sys.path.insert(0, sys.argv[1]); '
                  'from vco_studio.stages import _mc_worker; _mc_worker(sys.argv[2:])')
        command = list(ex.lock_command) + [site.python(), '-c', worker, str(Path(__file__).resolve().parents[1]),
                                           json.dumps(native_command), str(ex.timeout_s)]
    receipt = {'schema': 'vco.mc.run.v1', 'argv': argv, 'lock_command': ex.lock_command,
               'cwd': str(out), 'generated_dir': str(generated), 'requested_samples': runs,
               'started': _now(), 'input_sha256': {n: sha256(out / n) for n in ('circuit.scs', 'tb.scs', 'models.scs')}}
    write_json(out / 'receipt.json', receipt)
    outcome = run_bounded(command, out, ex.timeout_s + (7200 if ex.lock_command else 0), out / 'stdout.log',
                          on_start=lambda pid: write_json(out / 'process.json', {'pid': pid}))
    if ex.lock_command and (out / 'solver-outcome.json').is_file():
        receipt['lock_outcome'] = outcome
        outcome = read_json(out / 'solver-outcome.json')
    receipt.update(outcome); write_json(out / 'receipt.json', receipt)
    samples = []
    if outcome['exit_code'] == 0 and outcome['stop_reason'] is None:
        for fd in sorted((out / 'psf').rglob('*hb.fd.pss_hb')):
            prefix = fd.name[:-len('hb.fd.pss_hb')]
            noise = fd.with_name(prefix + 'hbnoise.pm.pnoise_hbnoise')
            # An unprefixed PSF is accepted only inside a named MC sample directory.
            if not prefix and fd.parent == out / 'psf':
                continue
            if not noise.is_file():
                continue
            sample = out / 'samples' / ('sample-%04d' % (len(samples) + 1))
            (sample / 'psf').mkdir(parents=True)
            shutil.copy2(fd, sample / 'psf/hb.fd.pss_hb')
            shutil.copy2(noise, sample / 'psf/hbnoise.pm.pnoise_hbnoise')
            shutil.copy2(out / 'spectre.log', sample / 'spectre.log')
            try:
                r = parse_result(sample, project)
            except (ValueError, OSError, KeyError, IndexError) as exc:
                r = {'status': 'failed', 'error': str(exc)}
            r.update(path=str(sample), sample=len(samples) + 1,
                     evidence={str(p): sha256(p) for p in (fd, noise)}, receipt=str(out / 'receipt.json'))
            write_json(sample / 'result.json', r); samples.append(r)
    ok = len(samples) == runs and all(s['status'] == 'measured' for s in samples)
    data = {'status': 'measured' if ok else 'failed', 'samples': samples, 'path': str(out), 'receipt': receipt,
            'error': None if ok else 'Expected %d valid MC samples, found %d; inspect solver log/native family PSF naming' % (runs, len(samples))}
    write_json(out / 'result.json', data)
    return data


def _mc(root, state, freeze, candidate, project, codes, runs, max_runs, dry_run, timeout):
    if runs < 1:
        raise ValueError('--runs must be positive')
    binding, selected, available = _mc_binding(project['binding'])
    if not selected:
        return _summary(root, 'mc', {'candidate': candidate['id'], 'provisional': not bool(freeze), 'points': [],
                         'available_mc_sections': available, 'reason': 'No applicable mc section in binding models. Supply foundry statistical process/mismatch models and an explicit TT-to-MC section mapping; Example nominal corners alone do not configure Monte Carlo.'}, 'not_configured')
    _limit(runs * len(codes), max_runs)
    dest = _invocation(root, 'mc')
    ex = _ModifiedExecutor(dest, max_runs, timeout_s=timeout)
    points = []
    _status(root, 'mc', 'prepared' if dry_run else 'running', str(dest))
    for code in codes:
        p = _with_code(project, code); p['binding'] = binding
        generated = ex.prepare(p, 'mc', lambda d: _mc_tb(d, runs))
        row = {'code': code, 'generated': str(generated), 'requested_samples': runs, 'status': 'prepared'}
        if not dry_run:
            r = _execute_mc(ex, generated, p, runs)
            row.update(status=r['status'], error=r['error'], result=str(Path(r['path']) / 'result.json'))
            row['samples'] = [_metrics(s, state['spec']) for s in r['samples']]
            row['yield_sampled_constraints'] = sum(s['pass'] for s in row['samples']) / runs
        points.append(row)
        _status(root, 'mc', 'prepared' if dry_run else 'running', str(dest), points)
    data = {'schema': 'vco.verification.mc.v1', 'candidate': candidate['id'], 'provisional': not bool(freeze),
            'points': points, 'runs_per_code': runs, 'selected_sections': selected, 'available_mc_sections': available,
            'pass': None if dry_run else all(p['status'] == 'measured' and all(s['pass'] for s in p['samples']) for p in points),
            'analysis': 'Spectre montecarlo around HB/HBnoise; seed=1; variations=all',
            'limitations': ['Prepared syntax is not statistical model qualification.',
                            'HB/HBnoise-in-MC support depends on installed Spectre and model decks. If rejected, validate a PSS/Pnoise oscillator setup and its reader before substituting analyses; no silent nominal fallback.',
                            'Sample yield covers per-code PN/power constraints, not full-band correlated calibration yield.']}
    write_json(dest / 'summary.json', data)
    return _summary(root, 'mc', data, 'prepared' if dry_run else ('done' if all(p['status'] == 'measured' for p in points) else 'failed'))


def verify(task, stage, candidate=None, corners=('tt', 'ff', 'ss', 'fs', 'sf'), temps=(-40, 27, 125),
           vdd_pct=(0, -10, 10), codes=None, max_runs=1000, dry_run=False, runs=100, run=False,
           netlist=None, top_subckt=None, timeout=300):
    root = Path(task).resolve()
    if stage not in ('pvt', 'mc', 'postsim'):
        raise ValueError('Unknown verification stage: ' + stage)
    with flow._task_transaction(root, 'verify-' + stage):
        try:
            _limit(0, max_runs)
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError('--timeout must be positive and finite')
            state, frozen, c, directory, project = _context(root, candidate)
            control = _codes(codes, project, frozen)
            if stage == 'pvt':
                if not corners or not temps or not vdd_pct or len(set(corners)) != len(corners):
                    raise ValueError('Provide nonempty corners/temps/vdd-pct; corners must be unique')
                return _pvt(root, state, frozen, c, project, control, corners, temps, vdd_pct, max_runs, dry_run, timeout)
            if stage == 'postsim':
                return _postsim(root, state, frozen, c, directory, project, control, netlist, top_subckt, max_runs, dry_run, timeout)
            if dry_run and run:
                raise ValueError('--run and --dry-run are mutually exclusive')
            return _mc(root, state, frozen, c, project, control, runs, max_runs, not run, timeout)
        except BaseException as exc:
            prior = _statuses(root)[stage]
            _status(root, stage, 'failed', {'error': str(exc), 'previous_summary': prior['summary']}, prior['runs'])
            raise


def _new_output(path):
    path = Path(path).resolve()
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise FileExistsError('Output must be new or empty: ' + str(path))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _hierarchy(graph):
    byname = {g['name']: g for g in graph.get('subcircuits', []) + [graph]}
    def walk(g, parent, seen):
        if g['name'] in seen:
            raise ValueError('Recursive graph hierarchy: ' + g['name'])
        for inst in g['instances']:
            path = parent + '/' + inst['name']
            yield g, inst, path
            if inst['role'] == 'subckt':
                yield from walk(byname[inst['master']], path, seen | {g['name']})
    yield from walk(graph, graph['name'], set())


def _matching(graph):
    groups = []
    for g in graph.get('subcircuits', []) + [graph]:
        instances = {i['name']: i for i in g['instances']}
        for name, inst in instances.items():
            partner = name[:-1] + ('B' if name.endswith('A') else '2')
            if name[-1:] in ('A', '1') and partner in instances and instances[partner]['role'] == inst['role']:
                other = instances[partner]
                groups.append({'subcircuit': g['name'], 'members': [name, partner], 'rule': '*A/*B or *1/*2',
                               'constraint': 'symmetry; preserve specified geometry/ratio and connectivity',
                               'equal_parameters': inst['parameters'] == other['parameters'], 'review_required': True})
        units = [i['name'] for i in g['instances'] if i.get('master', '').startswith('CT_R3U_')]
        if units:
            groups.append({'subcircuit': g['name'], 'members': units, 'rule': 'CT_R3U_*',
                           'constraint': 'matched unit cells, common orientation, dummy perimeter and symmetric routing', 'review_required': True})
        if g['name'].startswith('CT_R3U_'):
            caps = [i['name'] for i in g['instances'] if i['role'] == 'cap']
            if caps:
                groups.append({'subcircuit': g['name'], 'members': caps, 'rule': 'CT_R3U_* capacitors',
                               'constraint': 'unit geometry and symmetric parasitics', 'review_required': True})
    return groups


def _magnetics(graph, spec):
    fmin, fmax = spec['frequency_band_hz']; f0 = (fmin + fmax) / 2; omega = 2 * math.pi * f0
    inductors, budgets = [], []
    for g in graph.get('subcircuits', []) + [graph]:
        byname = {i['name']: i for i in g['instances']}
        for inst in g['instances']:
            if inst['role'] != 'ind':
                continue
            name = inst['name']; inductance = float(inst['parameters']['l'])
            couplings = [{'instance': i['name'], 'ind1': i['parameters']['ind1'], 'ind2': i['parameters']['ind2'],
                          'k': i['parameters']['coupling']} for i in g['instances']
                         if i['role'] == 'mutual' and name in (i['parameters']['ind1'], i['parameters']['ind2'])]
            rs = byname.get('RS' + name, {}).get('parameters', {}).get('r')
            rp = byname.get('RP' + name, {}).get('parameters', {}).get('r')
            z = complex(rs or 0, omega * inductance)
            if rp:
                z = 1 / (1 / z + 1 / rp)
            q = z.imag / z.real if z.real > 0 else None
            inductors.append({'instance': g['name'] + '/' + name, 'L_h': inductance, 'f0_hz': f0,
                              'q_target_at_f0': q, 'q_basis': 'uncoupled branch with explicit RS/RP; design target, not EM measurement',
                              'series_r_ohm': rs, 'parallel_r_ohm': rp, 'couplings': couplings, 'srf_min_hz_exclusive': 2 * fmax})
            effective = inductance
            paired = name[:-1] + ('B' if name.endswith('A') else 'A')
            mutual = next((c for c in couplings if {c['ind1'], c['ind2']} == {name, paired}), None)
            same = paired in byname and byname[paired]['parameters'].get('l') == inductance
            isolated = len(couplings) == 1 and mutual and same
            if isolated:
                effective *= 1 - mutual['k']
            cap = 1 / (omega * omega * effective)
            budgets.append({'side': g['name'] + '/' + name, 'L_effective_h': effective, 'f0_hz': f0,
                            'tank_total_c_estimate_f': cap, 'allowed_added_c_for_minus_1pct_f': cap * (1 / 0.99 ** 2 - 1),
                            'linear_1pct_budget_f': 0.02 * cap,
                            'basis': 'equal paired differential mode L*(1-k)' if isolated else 'uncoupled branch approximation; multimode network requires port/EM extraction',
                            'review_required': True})
    return inductors, budgets


def handoff(task, output):
    root = Path(task).resolve()
    with flow._task_transaction(root, 'handoff'):
        try:
            state, freeze, c, directory, project = _context(root, require_freeze=True)
            dest = _new_output(output)
            graph = read_json(directory / 'graph.json')
            with open(dest / 'devices.csv', 'w', newline='') as f:
                writer = csv.writer(f); writer.writerow(['instance', 'subcircuit', 'role', 'model', 'geometry', 'nodes'])
                for g, inst, path in _hierarchy(graph):
                    model = inst.get('master') or (inst.get('source_model') if inst.get('ideal') else project['binding']['roles'].get(inst['role'], {}).get('model'))
                    writer.writerow([path, g['name'], inst['role'], model, json.dumps(inst['parameters'], sort_keys=True), json.dumps(inst['nodes'], sort_keys=True)])
            write_json(dest / 'matching.json', {'groups': _matching(graph), 'scope': 'naming-based suggestions; layout engineer must review'})
            inductors, budgets = _magnetics(graph, state['spec'])
            write_json(dest / 'inductors.json', {'inductors': inductors})
            write_json(dest / 'parasitic_budget.json', {'budgets': budgets, 'formula': 'C=1/((2*pi*f0)^2*L); deltaC=C*(1/0.99^2-1)',
                                                      'scope': 'per-side tank estimate; total includes existing device/bank capacitance; not an extra capacitance allowance'})
            write_json(dest / 'pins.json', {'top_subckt': graph['name'], 'ports': graph['ports'],
                                           'port_order': [p['name'] for p in graph['ports']], 'registers': project['registers']})
            for name in ('graph.json', 'circuit.scs', 'project.json'):
                shutil.copy2(directory / name, dest / name)
            shutil.copy2(root / 'freeze.json', dest / 'freeze.json')
            (dest / 'README.md').write_text('# Layout handoff\n\nFrozen nominal candidate: ' + c['id'] + '\n\n'
                'Review naming-derived matching groups and preserve intended device ratios, mutual k signs, pin order and control banks. '
                'Magnetic Q is a target from the lumped branch model. Obtain EM L/Q/SRF and coupling evidence; SRF must exceed twice the maximum specified frequency. '
                'Capacitance budgets are first-order estimates and require extracted same-port/multimode validation.\n\n'
                'Post-layout plan: DRC/LVS, self-contained Spectre PEX with identical top name/ports; replay all adopted codes with the same HB/HBnoise settings; '
                'compare frequency, total supply power and PN at 10k/100k/1M/10M; check full band and step size at each condition, then run PVT/MC as configured.\n\n'
                '```sh\nlaunch verify ' + shlex.quote(str(root)) + ' --stage postsim --netlist extracted.scs\n```\n')
            files = {p.name: sha256(p) for p in dest.iterdir() if p.is_file()}
            data = {'status': 'done', 'candidate': c['id'], 'output': str(dest), 'files': files,
                    'qualification': 'layout interface prepared; no DRC/LVS/PEX or EM execution'}
            write_json(dest / 'handoff.json', data)
            _status(root, 'handoff', 'done', str(dest / 'handoff.json'))
            return data
        except BaseException as exc:
            _status(root, 'handoff', 'failed', {'error': str(exc)})
            raise


def _evidence(root, freeze, directory, candidate):
    paths = (freeze or {}).get('adopted_results', candidate.get('results', []))
    paths = list(dict.fromkeys(list(paths) + (freeze or {}).get('convergence_results', [])))
    verified, cache = [], {}
    manifest = read_json(directory / 'manifest.json')
    for name in paths:
        path = _path(root, name)
        if not freeze and (not path.is_file() or read_json(path).get('status') != 'measured'):
            continue
        row = flow._verify_result(path, manifest['physical_identity'], sha256(directory / 'circuit.scs'), hash_cache=cache)
        verified.append({k: v for k, v in row.items() if k not in ('result', 'settings')})
    if freeze:
        expected = {str(_path(root, r['file']).resolve()): r for r in freeze.get('verified_evidence', [])}
        for row in verified:
            record = expected.get(row['file'])
            if record:
                for k in ('result_sha256', 'receipt_sha256', 'input_sha256', 'raw_evidence_sha256'):
                    if record[k] != row[k]:
                        raise ValueError('Evidence differs from freeze: ' + row['file'] + ' ' + k)
    return verified


def deliver(task, provisional=False, output=None, candidate=None):
    root = Path(task).resolve()
    with flow._task_transaction(root, 'deliver'):
        if not (root / 'freeze.json').is_file() and not provisional:
            raise ValueError('Unfrozen delivery requires --provisional')
        state = read_json(root / 'task.json')
        if candidate and (root / 'freeze.json').is_file():
            raise ValueError('Task is frozen; the delivery uses the frozen candidate')
        if candidate:
            if candidate not in [c['id'] for c in state.get('candidates', [])]:
                raise ValueError('Unknown candidate %r; known: %s' % (candidate, ', '.join(c['id'] for c in state.get('candidates', []))))
            cid = candidate
        else:
            live = [c for c in state.get('candidates', []) if c.get('disposition') != 'rejected']
            cid = (live or state.get('candidates') or [{}])[-1].get('id')
        state, freeze, c, directory, project = _context(root, None if (root / 'freeze.json').is_file() else cid)
        evidence = _evidence(root, freeze, directory, c)
        bases = _baseline(root, c, freeze)
        codes = _codes(None if freeze else 'endpoints', project, freeze)
        if not freeze and bases:
            codes = [json.loads(k) for k in bases]
        # Read the existing workflow API without changing its contract/state.
        workflow_status = flow.status(root)
        verification = _statuses(root)
        cards = set()
        consults = root / 'knowledge/consults.jsonl'
        if consults.is_file():
            for line in consults.read_text().splitlines():
                if line.strip():
                    record = json.loads(line)
                    cards.update(x['id'] if isinstance(x, dict) else x for x in record.get('cards', []))
        decisions = (root / 'DECISIONS.md').read_text() if (root / 'DECISIONS.md').is_file() else 'No DECISIONS.md recorded.'
        unresolved = list(state.get('unresolved', []))
        if not freeze:
            unresolved.append('Unfrozen: delivery uses the selected or latest non-rejected candidate and measured controls (an endpoint plan if no measurements exist).')
        unresolved += ['%s: %s' % (s, verification[s]['status']) for s in STAGES if verification[s]['status'] != 'done']
        for stage_name, row in verification.items():
            if isinstance(row.get('summary'), str) and Path(row['summary']).is_file():
                if read_json(row['summary']).get('pass') is False:
                    unresolved.append(stage_name + ': measured spec/coverage failure (see stage summary)')
        unresolved += list((freeze or {}).get('not_qualified', []))
        dest = _new_output(output or root / 'delivery')
        for name in ('project.json', 'graph.json', 'circuit.scs', 'tb.scs', 'models.scs', 'manifest.json'):
            shutil.copy2(directory / name, dest / name)
        if freeze:
            shutil.copy2(root / 'freeze.json', dest / 'freeze.json')
        write_json(dest / 'evidence.json', {'verified': evidence, 'source_directory': str(directory),
                                           'models': read_json(directory / 'manifest.json').get('model_files', {})})
        write_json(dest / 'flow-status.json', workflow_status)
        write_json(dest / 'verification/status.json', verification)
        for s, row in verification.items():
            p = Path(row['summary']) if isinstance(row.get('summary'), str) else None
            if p and p.is_file():
                write_json(dest / ('verification/' + s + '.json'), read_json(p))
        metric_keys = ['frequency_hz', 'power_mw'] + ['pn_%d_dbc_hz' % o for o in OFFSETS] + ['fom_%d_db' % o for o in OFFSETS]
        with open(dest / 'codes.csv', 'w', newline='') as f:
            writer = csv.writer(f); writer.writerow(list(project['registers']) + metric_keys + ['result'])
            for code in codes:
                base = bases.get(_code_key(code), {})
                writer.writerow([code[k] for k in project['registers']] + [base.get('metrics', {}).get(k) for k in metric_keys] + [base.get('result')])
        nominal = [b['metrics'] for b in bases.values()]
        observed = {k: (min(m[k] for m in nominal) if k.startswith('fom_') else max(m[k] for m in nominal))
                    for k in metric_keys if nominal and all(k in m for m in nominal)}
        fs = [m['frequency_hz'] for m in nominal]
        measured_range = [min(fs), max(fs)] if fs else None
        measured_step = max((b-a for a,b in zip(sorted(fs), sorted(fs)[1:])), default=None)
        commands = {
            'pvt': 'launch verify %s --stage pvt --dry-run --max-runs 1000' % shlex.quote(str(root)),
            'mc': 'launch verify %s --stage mc --runs 100 --dry-run' % shlex.quote(str(root)),
            'handoff': 'launch handoff %s --output /path/to/new-layout-package' % shlex.quote(str(root)),
            'postsim': 'launch verify %s --stage postsim --netlist /path/to/extracted.scs' % shlex.quote(str(root))}
        if not freeze:
            for s in ('pvt', 'mc', 'postsim'):
                commands[s] += ' --candidate %s --codes /path/to/codes.json' % shlex.quote(c['id'])
        header = '# Unfrozen — provisional delivery' if not freeze else '# Frozen TT nominal delivery'
        lines = [header, '', 'Candidate: `%s`; qualification: %s' % (c['id'], (freeze or {}).get('scope', 'provisional, not qualified')), '',
                 '| Specification | Requirement | Measured |', '|---|---|---|',
                 '| Frequency band (Hz) | %s | %s |' % (state['spec']['frequency_band_hz'], measured_range),
                 '| Power (mW) | <= %s | %s |' % (state['spec']['power_max_mw'], observed.get('power_mw', 'unmeasured'))]
        if state['spec'].get('max_step_hz') is not None:
            lines.append('| Maximum adopted frequency step (Hz) | <= %s | %s |' % (state['spec']['max_step_hz'], measured_step))
        for ofs in OFFSETS:
            lines.append('| PN@%s Hz (dBc/Hz) | %s | %s |' %
                         (ofs, state['spec'].get('pn_targets_dbc_hz', {}).get(str(ofs), 'not specified'), observed.get('pn_%d_dbc_hz' % ofs, 'unmeasured')))
        lines += ['', 'Adopted codes: see codes.csv (all four PN/FoM offsets and exact evidence paths).', '',
                  '| Code | f (Hz) | P (mW) | PN@1M |', '|---|---|---|---|']
        for code in codes:
            m = bases.get(_code_key(code), {}).get('metrics', {})
            lines.append('| %s | %s | %s | %s |' % (_code_key(code), m.get('frequency_hz', 'unmeasured'), m.get('power_mw', 'unmeasured'), m.get('pn_1000000_dbc_hz', 'unmeasured')))
        lines += ['', '## Structure decision record', '', decisions, '', '## Consulted knowledge cards', '', ', '.join(sorted(cards)) or 'none recorded', '',
                  '## Verification stages', '', '| Stage | Status | Command |', '|---|---|---|']
        for s in STAGES:
            lines.append('| %s | %s | `%s` |' % (s, verification[s]['status'], commands[s]))
        lines += ['', 'Stage done means execution/results available; inspect each stage summary pass/coverage separately. '
                  'The nominal tb.scs is the candidate testbench; codes.csv records the adopted states to replay. '
                  'Model files and raw PSF remain referenced by original paths and hashes in evidence.json; they are not redistributed.', '',
                  '## Unresolved / unqualified', ''] + ['- ' + str(x) for x in unresolved]
        (dest / 'report.md').write_text('\n'.join(lines) + '\n')
        data = {'schema': 'vco.delivery.v1', 'candidate': c['id'], 'provisional': not bool(freeze), 'created': _now(),
                'spec': state['spec'], 'measured_frequency_range_hz': measured_range, 'measured_max_step_hz': measured_step, 'worst_metrics': observed,
                'codes': codes, 'knowledge_card_ids': sorted(cards), 'unresolved': unresolved, 'verification': verification,
                'commands': commands, 'evidence': evidence, 'source_directory': str(directory),
                'files': {str(p.relative_to(dest)): sha256(p) for p in dest.rglob('*') if p.is_file()}}
        write_json(dest / 'delivery.json', data)
        return {'status': 'done', 'output': str(dest), 'provisional': not bool(freeze), 'manifest': str(dest / 'delivery.json')}


def _csv_numbers(value):
    try:
        values = [float(x) for x in value.split(',')]
    except ValueError:
        raise ValueError('Expected comma-separated finite numbers: ' + value)
    if not values or any(not math.isfinite(x) for x in values) or len(set(values)) != len(values):
        raise ValueError('Expected unique finite values: ' + value)
    return values


def cli_register(sub):
    # add_parser accepts a parser_class only on add_subparsers. The extension shares
    # the parent's parser class, so configure this parser's negative-number matcher.
    p = sub.add_parser('verify', help='prepare/run PVT, Monte Carlo or post-layout verification')
    p._negative_number_matcher = re.compile(r'^-\d+(?:\.\d+)?(?:,-?\d+(?:\.\d+)?)*$')
    p.add_argument('task')
    p.add_argument('--stage', choices=('pvt', 'mc', 'postsim'), required=True)
    p.add_argument('--candidate')
    p.add_argument('--corners', default='tt,ff,ss,fs,sf')
    p.add_argument('--temps', default='-40,27,125')
    p.add_argument('--vdd-pct', default='0,-10,10')
    p.add_argument('--codes', help='adopted (frozen default), endpoints or codes.json')
    p.add_argument('--max-runs', type=int, default=1000)
    p.add_argument('--runs', type=int, default=100, help='MC samples per code (MC defaults to preparation only)')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--dry-run', action='store_true')
    group.add_argument('--run', action='store_true', help='explicitly execute Monte Carlo')
    p.add_argument('--timeout', type=float, default=300)
    p.add_argument('--netlist')
    p.add_argument('--top-subckt')
    p = sub.add_parser('handoff', help='layout handoff package from frozen candidate')
    p.add_argument('task'); p.add_argument('--output', required=True)
    p = sub.add_parser('deliver', help='delivery package with verified evidence hashes')
    p.add_argument('task'); p.add_argument('--provisional', action='store_true'); p.add_argument('--output')
    p.add_argument('--candidate', help='unfrozen delivery: which candidate (default: last non-rejected)')


def cli_dispatch(args):
    from .cli import EXIT_OK, EXIT_MEASURE_FAILED
    if args.command == 'verify':
        kwargs = vars(args).copy(); kwargs.pop('command')
        kwargs['corners'] = tuple(args.corners.split(','))
        kwargs['temps'] = _csv_numbers(args.temps)
        kwargs['vdd_pct'] = _csv_numbers(args.vdd_pct)
        out = verify(**kwargs)
        failed = _statuses(Path(args.task).resolve())[args.stage]['status'] == 'failed' or out.get('pass') is False
        return out, EXIT_MEASURE_FAILED if failed else EXIT_OK
    if args.command == 'handoff':
        return handoff(args.task, args.output), EXIT_OK
    if args.command == 'deliver':
        return deliver(args.task, args.provisional, args.output, getattr(args, 'candidate', None)), EXIT_OK
    return None
