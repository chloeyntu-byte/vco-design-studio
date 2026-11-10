"""Executable adapters to the installed MCO, Cap Array Lab and Z11 engines.

Source tools are imported read-only. Every action requires a generated design,
an explicit new result directory and a receipt; no existing GUI session is used.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
from pathlib import Path
import sys
import threading

def _tool_roots():
    from . import site
    return {name: site.tool_path(name) or Path('/nonexistent/' + name) for name in ('mco', 'caplab', 'z11')}


TOOL_ROOTS = _tool_roots()
_IMPORT_LOCK = threading.RLock()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')


def _module(tool, name):
    root = TOOL_ROOTS[tool].resolve()
    with _IMPORT_LOCK:
        # Disable external tool bytecode writes, and never change their files/config.
        prior = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(root))
        try:
            module = importlib.import_module(name)
            if not Path(module.__file__).resolve().is_relative_to(root):
                raise RuntimeError('Module identity collision: ' + name)
            return module
        finally:
            sys.path.remove(str(root))
            sys.dont_write_bytecode = prior


def _source(module):
    path = Path(module.__file__).resolve()
    return {'module': module.__name__, 'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def available_tools():
    return [{'id': key, 'root': str(root), 'available': root.is_dir(),
             'capability': {'mco': 'native ask/tell optimization with explicit evaluator',
                            'caplab': 'full Y matrix decomposition, resonance interpolation and workbook export',
                            'z11': 'static RLC and mutual inductance one-port solve'}[key]}
            for key, root in TOOL_ROOTS.items()]


def _load(generated_dir):
    root = Path(generated_dir).resolve()
    data = {name: json.loads((root / (name + '.json')).read_text()) for name in ('project', 'graph', 'manifest')}
    if data['graph'].get('schema') != 'vco.graph.v1':
        raise ValueError('Expected a generated vco.graph.v1 design')
    from .common import electrical_hash
    data['graph_hash'] = electrical_hash(data['graph'])
    if data['manifest'].get('graph_hash') not in (None, data['graph_hash']):
        raise ValueError('Generated graph does not match manifest identity')
    data['generated_dir'] = str(root)
    data['input_files'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()
                           if p.is_file() and p.name in ('project.json', 'graph.json', 'manifest.json', 'binding.json', 'circuit.scs', 'registers.json')}
    for name, actual_hash in data['input_files'].items():
        expected_hash = data['manifest'].get('files', {}).get(name)
        if expected_hash is not None and expected_hash != actual_hash:
            raise ValueError('Generated input does not match manifest: ' + name)
    return data


def action_spec(tool, generated_dir, output_dir, **options):
    if tool not in TOOL_ROOTS:
        raise ValueError('Unknown integration: ' + str(tool))
    data = _load(generated_dir)
    return {'schema': 'vco.tool-action.v1', 'tool': tool, 'generated_dir': data['generated_dir'],
            'output_dir': str(Path(output_dir).resolve()), 'source_graph_hash': data['graph_hash'],
            'input_files': data['input_files'], 'options': options}


def _check_cancel(event):
    if event is not None and event.is_set():
        raise InterruptedError('Integration cancelled at a candidate / analysis boundary')


def execute_action(spec, evaluate=None, cancel_event=None, log=None):
    data = _load(spec['generated_dir'])
    if data['graph_hash'] != spec['source_graph_hash'] or data['input_files'] != spec['input_files']:
        raise ValueError('Generated input changed after action preparation')
    _check_cancel(cancel_event)
    output = Path(spec['output_dir'])
    output.mkdir(parents=True, exist_ok=False)
    _write(output / 'action.json', spec)
    receipt = {'schema': 'vco.tool-receipt.v1', 'tool': spec['tool'], 'source_graph_hash': data['graph_hash'],
               'input_identity': _digest(spec), 'output_dir': str(output), 'qualified': False}
    try:
        if log:
            log('Running ' + spec['tool'] + ' on graph ' + data['graph_hash'][:12])
        options = dict(spec.get('options') or {})
        if spec['tool'] == 'mco':
            result = _optimize(data, output, evaluate, cancel_event, log, **options)
        elif spec['tool'] == 'z11':
            result = _z11(data, output, **options)
        elif spec['tool'] == 'caplab':
            result = _caplab(data, output, **options)
        else:
            raise ValueError('Unknown tool')
        _check_cancel(cancel_event)
        receipt.update(status=result.pop('status', 'completed'), **result)
    except Exception as exc:
        receipt.update(status='cancelled' if isinstance(exc, InterruptedError) else 'failed', error=str(exc), error_type=type(exc).__name__)
        _write(output / 'receipt.json', receipt)
        raise
    receipt['result_identity'] = _digest(receipt)
    _write(output / 'receipt.json', receipt)
    if log:
        log(receipt['status'].capitalize() + ' ' + spec['tool'] + ': ' + str(output / 'receipt.json'))
    return receipt


def numeric(value, parameters):
    from .common import evaluate_expression
    answer = float(evaluate_expression(value, parameters))
    if not math.isfinite(answer):
        raise ValueError('Non-finite numeric input')
    return answer


def _passives(data, port, selected_instances=None):
    graph = data['graph']
    nets = {net for inst in graph['instances'] for net in inst.get('nodes', {}).values()}
    if len(port) != 2 or port[0] == port[1] or not set(port).issubset(nets):
        raise ValueError('Select two distinct existing graph nets for the measurement port')
    supported = {'res': ('R', 'r', 1), 'cap': ('C', 'c', 1e15), 'ind': ('L', 'l', 1e12), 'mutual': ('K', 'k', 1)}
    selection = set(selected_instances) if selected_instances is not None else None
    chosen, omitted = [], []
    names = {i['name'] for i in graph['instances']}
    if selection is not None and not selection.issubset(names):
        raise ValueError('Unknown selected passive instance')
    for instance in graph['instances']:
        if instance['role'] not in supported or (selection is not None and instance['name'] not in selection):
            omitted.append(instance['name'])
            continue
        kind, parameter, scale = supported[instance['role']]
        values = instance.get('parameters', {})
        value = numeric(values.get(parameter, values.get('coupling')) if kind == 'K' else values[parameter], graph.get('parameters', {}))
        if kind != 'K' and value <= 0:
            raise ValueError('Passive values must be positive: ' + instance['name'])
        chosen.append({'id': instance['name'], 'kind': kind, 'value': value * scale,
                       'q': values.get('q', 'inf'), 'nodes': instance['nodes'], 'original': instance})
    if not chosen:
        raise ValueError('No passive instances in selected graph')
    selected_names = {p['id'] for p in chosen}
    for p in chosen:
        if p['kind'] == 'K':
            p['l1'] = p['nodes'].get('l1', p['original']['parameters'].get('ind1'))
            p['l2'] = p['nodes'].get('l2', p['original']['parameters'].get('ind2'))
            if not {p['l1'], p['l2']}.issubset(selected_names):
                raise ValueError('Mutual coupling references omitted inductors')
    return chosen, omitted


def _document(data, port, selected_instances=None):
    chosen, omitted = _passives(data, port, selected_instances)
    electric = [p for p in chosen if p['kind'] != 'K']
    electric.append({'id': 'PORT1', 'kind': 'P', 'value': 0, 'q': 0, 'nodes': {'p': port[0], 'n': port[1]}})
    if len({p['id'] for p in electric}) != len(electric):
        raise ValueError('PORT1 instance name is reserved by Z11')
    nets = sorted({p['nodes'][pin] for p in electric for pin in ('p', 'n')})
    ys = {net: 400 + index * 60 for index, net in enumerate(nets)}
    parts, wires, endpoints = [], [], {}
    for index, part in enumerate(electric):
        x = 200 + index * 200
        parts.append({k: part[k] for k in ('id', 'kind', 'value', 'q')} | {'x': x, 'y': 100, 'rotation': 0})
        for pin, dx, terminal in (('p', -40, 0), ('n', 40, 1)):
            net = part['nodes'][pin]
            wires.append([[x + dx, 100], [x + dx, ys[net]]])
            endpoints[(part['id'], terminal)] = net
    maxx = 400 + len(electric) * 200
    wires += [[[0, y], [maxx, y]] for y in ys.values()]
    # Native Z11 accepts nonnegative k and encodes mutual sign by dots.
    # Solve the pairwise polarity constraints, rejecting inconsistent sign cycles.
    signed_supported = bool(getattr(_module('z11', 'network_model_v2'), 'SIGNED_COUPLING', False))
    adjacency = {}
    for part in chosen:
        if part['kind'] == 'K' and part['value'] != 0 and not signed_supported:
            flip = int(part['value'] < 0)
            adjacency.setdefault(part['l1'], []).append((part['l2'], flip))
            adjacency.setdefault(part['l2'], []).append((part['l1'], flip))
    dots = {}
    for start in adjacency:
        if start in dots:
            continue
        dots[start] = 0
        pending = [start]
        while pending:
            current = pending.pop()
            for neighbor, flip in adjacency[current]:
                desired = dots[current] ^ flip
                if neighbor in dots and dots[neighbor] != desired:
                    raise ValueError('Mutual sign cycle cannot be represented by Z11 inductor dots')
                if neighbor not in dots:
                    dots[neighbor] = desired
                    pending.append(neighbor)
    for part in parts:
        if part['kind'] == 'L':
            part['dot'] = dots.get(part['id'], 0)
    for part in chosen:
        if part['kind'] == 'K':
            parts.append({'id': part['id'], 'kind': 'K', 'value': part['value'] if signed_supported else abs(part['value']), 'l1': part['l1'], 'l2': part['l2'], 'x': 0, 'y': 0, 'rotation': 0})
    document = {'parts': parts, 'wires': wires, 'junctions': [], 'studio_source_graph_hash': data['graph_hash']}
    network = _module('z11', 'network_model')
    actual, _ = network.extract_nodes({'parts': [p for p in parts if p['kind'] != 'K'], 'wires': wires})
    # Verify every terminal partition; routing artifacts may never alter topology.
    by_net, reverse = {}, {}
    for endpoint, net in endpoints.items():
        node = actual[endpoint]
        if net in by_net and by_net[net] != node or node in reverse and reverse[node] != net:
            raise RuntimeError('Z11 graphical routing changed graph connectivity')
        by_net[net], reverse[node] = node, net
    return document, omitted


def _z11(data, output, port, frequencies_hz, selected_instances=None):
    model = _module('z11', 'network_model_v2')
    document, omitted = _document(data, port, selected_instances)
    frequencies = [float(f) for f in frequencies_hz]
    if not frequencies or len(frequencies) > 10000 or any(not math.isfinite(f) or f <= 0 for f in frequencies):
        raise ValueError('Provide 1..10000 finite positive frequencies in Hz')
    _write(output / 'network.json', document)
    import numpy as np
    z, ignored = model.solve(document, np.asarray(frequencies) / 1e9)
    rows = [{'frequency_Hz': f, 'Z_real_ohm': float(v.real) if np.isfinite(v) else None,
             'Z_imag_ohm': float(v.imag) if np.isfinite(v) else None} for f, v in zip(frequencies, z)]
    _write(output / 'impedance.json', rows)
    return {'sources': [_source(model), _source(_module('z11', 'network_model'))], 'port': list(port),
            'coupling_convention': getattr(model, 'COUPLING_CONVENTION', 'nonnegative k with inductor dots'),
            'qualification': 'static_passive_projection', 'omitted_instances': omitted, 'rows': rows,
            'ignored_component_count': int(ignored), 'network': str(output / 'network.json'),
            'limitations': ['Linear static RLC/mutual model with constant Q; not HB/PN/startup evidence.',
                            'Active devices and their parasitics are omitted; bias/reference nets are not automatically grounded.']}


def _caplab(data, output, port, samples=None, sample_provenance=None, selected_instances=None):
    if samples is not None and (not sample_provenance or sample_provenance.get('source_graph_hash') != data['graph_hash'] or sample_provenance.get('port') != list(port)):
        raise ValueError('Y sample provenance must bind exact source_graph_hash and port')
    decomposition = _module('caplab', 'caplab.decomposition')
    resonance = _module('caplab', 'caplab.resonance')
    workbook = _module('caplab', 'caplab.workbook')
    devices = _module('caplab', 'caplab.devices')
    model = _module('caplab', 'caplab.model')
    chosen, omitted = _passives(data, port, selected_instances)
    project = model.new_project()
    project.update(workspace=data['project'].get('workspace', ''), library=data['project'].get('target_library', ''),
                   reference={}, private=None, studio_source_graph=data['graph'], studio_registers=data['graph'].get('registers', []))
    project['settings'].update(vdd=str(data['graph'].get('parameters', {}).get('vdd', 1)), vcm='0', bits=0,
                               codes='0', frequencies='1G', decomposition=True)
    ids = {}
    for index, part in enumerate(chosen):
        if part['kind'] == 'K':
            continue
        cell, key, scale = {'R': ('res', 'r', 1), 'C': ('capq', 'c', 1e-15), 'L': ('indq', 'l', 1e-12)}[part['kind']]
        props = {key: str(part['value'] * scale)}
        if part['kind'] in ('L', 'C'):
            props['q'] = str(part['q']) if part['q'] != 'inf' else '1e15'
        node = devices.create_passive(project, cell, 250 + (index % 4) * 220, 180 + (index // 4) * 160, props)
        ids[part['id']] = node['id']
        node['studio_instance'] = part['id']
        for terminal, pin in (('PLUS', 'p'), ('MINUS', 'n')):
            net = part['nodes'][pin]
            node['roles'][terminal] = 'P' if net == port[0] else 'N' if net == port[1] else 'VSS' if net == '0' else 'N_' + net
    for part in chosen:
        if part['kind'] == 'K':
            devices.create_passive(project, 'mind', 200, 100, {'ind1': ids[part['l1']], 'ind2': ids[part['l2']], 'k': str(part['value'])})
    book = workbook.workbook(project)
    book['pages'][0]['name'] = data['graph'].get('name', 'Studio') + ' passive projection'
    book['studio'] = {'source_graph_hash': data['graph_hash'], 'port': port, 'omitted_instances': omitted,
                      'qualification': 'static_passive_projection', 'registers': data['graph'].get('registers', [])}
    result = {'sources': [_source(decomposition), _source(resonance), _source(workbook), _source(devices)],
              'qualification': 'workbook_export_only', 'port': list(port), 'omitted_instances': omitted,
              'workbook_scope': {'kind': 'static_passive_projection', 'omitted_instances': omitted,
                                 'measurement_data_derived_from_projection': False},
              'measurement_scope': None,
              'limitations': ['Workbook circuit drawing is a passive projection; its omitted MOS/subcircuits do not describe the scope of imported measurement data.',
                             'Ideal L/C use Q=1e15 only in workbook visualization; no simulated measurement is invented.',
                             'Resonance is complex Y interpolation, not a new solver point or HB result.']}
    if samples is not None:
        if not sample_provenance or sample_provenance.get('source_graph_hash') != data['graph_hash'] or sample_provenance.get('port') != list(port):
            raise ValueError('Y sample provenance must bind exact source_graph_hash and port')
        if sample_provenance.get('kind') not in ('native_ac', 'native_sp', 'analytic_fixture', 'external_matrix'):
            raise ValueError('Declare sample kind: native_ac/native_sp/analytic_fixture/external_matrix')
        if not samples:
            raise ValueError('Y samples are empty')
        rows, grouped = [], {}
        for index, source in enumerate(samples):
            row = copy.deepcopy(source)
            f = float(row['frequency_Hz'])
            matrix = [complex(float(row['Y' + pair + '_G_S']), float(row['Y' + pair + '_B_S'])) for pair in ('11', '12', '21', '22')]
            split = decomposition.decompose(*matrix, f)
            if split.get('total') is None:
                raise ValueError('Unresolved floating port at sample ' + str(index))
            row.update(split['total'])
            row.update(point=int(row.get('point', row.get('code', 0))), decomposition=split)
            rows.append(row)
            grouped.setdefault(row['point'], []).append(row)
        estimated = {str(code): resonance.find_first_resonance(group) for code, group in grouped.items()}
        book['pages'][0]['name'] = data['graph'].get('name', 'Studio') + ' imported full Y results'
        book['pages'][0]['editor'] = {'tab': 'results'}
        book['pages'][0]['result'] = workbook.cache(rows, _digest(sample_provenance), 'cartesian')
        if len(workbook.cached_rows(book['pages'][0]['result'])) != len(rows):
            raise ValueError('CapLab rejected generated result snapshot')
        _write(output / 'samples.json', samples)
        _write(output / 'sample-provenance.json', sample_provenance)
        _write(output / 'analysis.json', {'rows': rows, 'resonances': estimated})
        measurement_scope = {'kind': sample_provenance['kind'], 'representation': 'supplied_complete_terminal_Y_matrix',
                             'port': list(port), 'source_graph_hash': data['graph_hash'],
                             'source_scope': sample_provenance.get('scope'), 'provenance': sample_provenance,
                             'derived_from_workbook_projection': False}
        book['studio']['measurement_scope'] = measurement_scope
        book['pages'][0]['measurement_scope'] = measurement_scope
        result.update(qualification='matrix_postprocessing_' + sample_provenance['kind'], rows=rows, resonances=estimated,
                      sample_provenance=sample_provenance, measurement_scope=measurement_scope)
        if sample_provenance['kind'] in ('native_ac', 'native_sp'):
            result['limitations'].append('Measurement data are native terminal matrices for the source circuit and conditions documented in sample_provenance; they are independent of the exported passive drawing.')
    workbook.workbook(book)  # Roundtrip using the actual tool loader before export.
    path = output / 'caplab-workbook.json'
    _write(path, book)
    # Dedicated launcher sets CapLab's runtime ROOT before importing its UI.
    launcher = Path(__file__).resolve().parents[1] / 'integrations' / 'caplab_gui.py'
    result.update(workbook=str(path), launch_action={'argv': [sys.executable, str(launcher), '--project', str(path),
                                                            '--runtime', str(output / 'caplab-runtime')],
                                                  'cwd': str(output), 'explicit_project': True, 'auto_execute': False})
    return result


def _optimize(data, output, evaluate, cancel_event, log, variables, budget=12, batch_size=4,
              algorithm='bayesian_optimization', seed=1, parameters=None):
    if not callable(evaluate):
        raise ValueError('MCO requires an explicit project evaluator callback; no default synthetic objective')
    if algorithm not in ('random', 'differential_evolution', 'nsga2', 'bayesian_optimization'):
        raise ValueError('Unsupported MCO algorithm')
    if not 1 <= int(budget) <= 100000 or not 1 <= int(batch_size) <= int(budget):
        raise ValueError('Invalid optimization budget/batch size')
    names = [v['parameter_id'] for v in variables]
    if len(names) != len(set(names)) or not set(names).issubset(data['graph'].get('parameters', {})):
        raise ValueError('Variables must uniquely name existing shared graph parameters')
    module = _module('mco', 'circuit_optimizer.optimizers')
    optimizer = module.PopulationGenerator(variables, algorithm=algorithm, seed=seed, parameters=parameters)
    from .generator import generate
    history = []
    while len(history) < int(budget):
        _check_cancel(cancel_event)
        candidates = optimizer.ask(min(int(batch_size), int(budget) - len(history)))
        evaluated = []
        for values in candidates:
            _check_cancel(cancel_event)
            index = len(history)
            candidate_dir = output / ('candidate-%04d' % index)
            project = copy.deepcopy(data['project'])
            project.setdefault('parameters', {}).update(values)
            manifest = generate(project, candidate_dir)
            observation = evaluate(project, candidate_dir)
            _write(candidate_dir / 'evaluator-observation.json', observation)
            missing = {'status', 'feasible', 'total_violation'} - set(observation)
            if missing:
                raise ValueError('Evaluator must explicitly provide status, feasible and total_violation; missing ' + ', '.join(sorted(missing)))
            status = observation['status']
            if status == 'cancelled':
                raise InterruptedError('Candidate evaluator reported cancellation')
            if status not in ('completed', 'failed', 'not_evaluated'):
                raise ValueError('Evaluator status must be completed, failed, not_evaluated or cancelled')
            feasible = observation['feasible']
            violation = float(observation['total_violation'])
            if type(feasible) is not bool or not math.isfinite(violation) or violation < 0:
                raise ValueError('Evaluator feasible must be bool and total_violation must be finite and nonnegative')
            if status == 'completed' and feasible != (violation == 0):
                raise ValueError('Completed evaluator feasible disagrees with total_violation')
            if status != 'completed' and feasible:
                raise ValueError('Failed or unevaluated candidates cannot be feasible')
            merit, objectives = None, []
            if status == 'completed':
                merit = float(observation['merit'])
                objectives = observation.get('objectives', [merit])
                objectives = [{'score': float(item['score'] if isinstance(item, dict) else item)} for item in objectives]
                if not all(math.isfinite(v) for v in [merit] + [item['score'] for item in objectives]) or not objectives:
                    raise ValueError('Completed evaluator returned invalid objectives')
            evaluation_class = ('feasible' if feasible else 'constraint_failure') if status == 'completed' else ('evaluation_failure' if status == 'failed' else 'not_evaluated')
            row = {'candidate_id': 'candidate-%04d' % index, 'values': values, 'status': status,
                   'evaluation_class': evaluation_class,
                   'metrics': {'merit': merit, 'objectives': objectives, 'total_violation': violation, 'feasible': feasible},
                   'violations': observation.get('violations', []),
                   'evidence': observation.get('evidence', {}), 'manifest': manifest, 'directory': str(candidate_dir)}
            _write(candidate_dir / 'mco-evaluation.json', row)
            history.append(row)
            if status == 'completed':
                evaluated.append(row)
            _write(output / 'history.json', history)
            if log:
                log(row['candidate_id'] + ' ' + evaluation_class + ' merit=' + str(merit))
        optimizer.tell(evaluated)
    completed = [row for row in history if row['status'] == 'completed']
    feasible_rows = [row for row in completed if row['metrics']['feasible']]
    rank = lambda row: (row['metrics']['total_violation'], row['metrics']['merit'])
    best = min(feasible_rows, key=rank) if feasible_rows else None
    best_unqualified = min(completed, key=rank) if completed and not feasible_rows else None
    outcome = 'feasible_candidates_available' if feasible_rows else 'all_completed_candidates_infeasible' if completed else 'no_completed_evaluations'
    return {'sources': [_source(module), _source(_module('mco', 'circuit_optimizer.metrics')), _source(_module('mco', 'circuit_optimizer.engineering'))], 'engine': 'circuit_optimizer.optimizers.PopulationGenerator',
            'status': 'completed' if completed else 'failed', 'outcome': outcome,
            'algorithm': algorithm, 'seed': seed, 'evaluations': len(history), 'best': best, 'best_unqualified': best_unqualified,
            'completed_evaluations': len(completed), 'feasible_evaluations': len(feasible_rows),
            'failed_evaluations': sum(row['status'] == 'failed' for row in history),
            'not_evaluated_count': sum(row['status'] == 'not_evaluated' for row in history),
            'history': str(output / 'history.json'), 'qualification': 'evaluator_dependent_exploration',
            'limitations': ['MCO chooses candidates; evaluator evidence controls electrical validity.',
                            'Optimization success does not qualify phase noise, startup, PVT coverage or final code maps.']}


def optimize(generated_dir, variables, evaluate, output_dir, **options):
    cancel_event, log = options.pop('cancel_event', None), options.pop('log', None)
    return execute_action(action_spec('mco', generated_dir, output_dir, variables=variables, **options),
                          evaluate=evaluate, cancel_event=cancel_event, log=log)


def caplab_analyze(generated_dir, samples, output_dir, port, sample_provenance):
    return execute_action(action_spec('caplab', generated_dir, output_dir, samples=samples, port=port, sample_provenance=sample_provenance))


def export_caplab(generated_dir, output_dir, port, **options):
    return execute_action(action_spec('caplab', generated_dir, output_dir, port=port, **options))


def z11_analyze(generated_dir, output_dir, port, frequencies_hz, **options):
    return execute_action(action_spec('z11', generated_dir, output_dir, port=port, frequencies_hz=frequencies_hz, **options))
