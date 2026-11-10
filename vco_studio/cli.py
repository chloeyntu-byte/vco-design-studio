"""Command line interface of the VCO design system.

Exit codes (stable contract for agents and scripts):
  0   success
  1   operational error (unexpected exception)            -> JSON error on stderr
  2   invalid request (bad arguments, spec, paths, state) -> JSON error on stderr
  3   the solver ran but the measurement failed (status != measured, or any sweep point failed)
  4   solver timeout
  130 interrupted (Ctrl-C / SIGTERM); solver process groups are terminated first
"""
import argparse
import json
import math
import signal
import sys
from pathlib import Path

from .common import read_json, write_json, ROOT

EXIT_OK, EXIT_ERROR, EXIT_INVALID, EXIT_MEASURE_FAILED, EXIT_TIMEOUT, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130


class InvalidRequest(ValueError):
    pass


def _positive_float(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError('not a number: %r' % text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number: %r' % text)
    return value


def _finite_float(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError('not a number: %r' % text)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError('must be finite: %r' % text)
    return value


def _fraction(text):
    value = _positive_float(text)
    if value >= 1:
        raise argparse.ArgumentTypeError('relative tolerance must be in (0, 1): %r' % text)
    return value


def build_parser():
    from .site import version
    p = argparse.ArgumentParser(
        prog='launch',
        description='VCO Design Studio %s — topology catalog, spec-driven design flow, measured evidence. '
                    'Start with: launch quickstart' % version(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('\n', 2)[2])
    p.add_argument('--version', action='version', version=version())
    sub = p.add_subparsers(dest='command', metavar='<command>')
    sub.required = True

    def cmd(name, help_text):
        return sub.add_parser(name, help=help_text, description=help_text)

    cmd('quickstart', 'print the shortest correct command sequence for a new design task')
    cmd('site', 'show the machine profile in use (spectre, python, KB, runs root, evidence indexes)')
    c = cmd('spec-check', 'validate a spec JSON and print the normalized form + warnings')
    c.add_argument('spec')
    cmd('spec-fields', 'list every spec field with type and meaning')
    cmd('catalog', 'list the five topology families and their variants')
    c = cmd('bindings', 'list fictional example bindings; user bindings are configured separately')
    c.add_argument('--pdk')
    cmd('baselines', 'list reference baselines (Class-B)')
    c = cmd('describe', 'describe one family/variant (mechanism, controls, defaults)')
    c.add_argument('family')
    c.add_argument('--variant')

    c = cmd('evidence-query', 'find measured points near a condition; returns matches, peaks, and nearest excluded points with their deltas')
    c.add_argument('--architecture')
    c.add_argument('--variant')
    c.add_argument('--index', action='append', help='evidence index file (default: site evidence_indexes)')
    c.add_argument('--bands', action='append')
    c.add_argument('--pdk')
    c.add_argument('--frequency-hz', type=_positive_float)
    c.add_argument('--power-mw', type=_positive_float)
    c.add_argument('--vdd-v', type=_positive_float)
    c.add_argument('--temp-c', type=_finite_float)
    c.add_argument('--main-bits', type=int)
    c.add_argument('--offset', type=int, default=1000000, choices=[10000, 100000, 1000000, 10000000])
    c.add_argument('--frequency-tolerance', type=_fraction, default=.03)
    c.add_argument('--power-tolerance', type=_fraction, default=.10)
    c.add_argument('--full', action='store_true', help='include every matched point (default: compact summary)')
    c = cmd('evidence-point', 'convert one measured result.json into an evidence point line')
    c.add_argument('result')
    c.add_argument('--pdk', required=True)
    c.add_argument('--real-bank', action='store_true')
    c.add_argument('--output', required=True)

    c = cmd('project', 'create a project JSON for a family/variant on a PDK binding (inherits spec conditions when --spec is given)')
    c.add_argument('family')
    c.add_argument('--variant')
    c.add_argument('--binding', help='user binding JSON path or configured id; demo_a..demo_d are fictional')
    c.add_argument('--spec', help='spec JSON: inherit pdk/vdd/temp/center frequency/bits')
    c.add_argument('--set', action='append', default=[], metavar='KEY=VALUE',
                   help='override parameters.KEY or registers.NAME.bits/value, e.g. --set L_scale=1.2 --set main.bits=5')
    c.add_argument('--output', required=True)
    c = cmd('generate', 'generate netlist/testbench/manifest for a project into a NEW directory')
    c.add_argument('project')
    c.add_argument('--output', required=True)
    c = cmd('simulate', 'run one bounded HB/PN simulation of a generated design (use your scheduler if needed)')
    c.add_argument('generated')
    c.add_argument('--output')
    c.add_argument('--timeout', type=_positive_float, default=300)
    c = cmd('bank-ac', 'switched-bank two-port AC matrix at given codes/frequencies (Cap Lab analysis)')
    c.add_argument('generated')
    c.add_argument('--output', required=True)
    c.add_argument('--bank', default='main')
    c.add_argument('--codes', default='0,15', help='comma list, e.g. 0,15')
    c.add_argument('--freq-ghz', default='5,10,15,20,25', help='comma list of increasing GHz values')
    c.add_argument('--vcm', type=_finite_float)
    c.add_argument('--tank-inductance-h', type=_positive_float)
    c = cmd('representative', 'evaluate one physical design at several explicit control states (worst-case FoM)')
    c.add_argument('generated')
    c.add_argument('--output', required=True)
    c.add_argument('--codes', required=True, help='JSON file: [{"main":0,"aux":3}, ...]')
    c.add_argument('--bank-ac-options')
    c = cmd('optimize', 'bounded MCO parameter search (variables JSON uses parameter ids, see launch spec-fields / docs)')
    c.add_argument('generated')
    c.add_argument('--output', required=True)
    c.add_argument('--variables', required=True)
    c.add_argument('--budget', type=int, default=8)
    c.add_argument('--algorithm', default='bayesian_optimization')
    c.add_argument('--codes')
    c.add_argument('--bank-ac-options')

    c = cmd('flow-init', 'create a design task from a spec (validates spec, records knowledge brief)')
    c.add_argument('--spec', required=True)
    c.add_argument('--workspace', required=True)
    c = cmd('flow-status', 'task state, every result with violations, guidance and next actions')
    c.add_argument('workspace')
    c.add_argument('--brief', action='store_true', help='compact table instead of full JSON')
    c = cmd('flow-next', 'the single next required action for this task (state machine)')
    c.add_argument('workspace')
    c = cmd('flow-add', 'add a candidate project to the task (generates it)')
    c.add_argument('workspace')
    c.add_argument('--project', required=True)
    c.add_argument('--reason', required=True)
    c.add_argument('--evidence', action='append', default=[], help='knowledge/evidence id supporting this choice (repeatable)')
    c = cmd('flow-mark', 'set a candidate disposition (exploring|rejected|adopted) with a reason')
    c.add_argument('workspace')
    c.add_argument('candidate')
    c.add_argument('disposition', choices=['exploring', 'rejected', 'adopted'])
    c.add_argument('--reason', required=True)
    c = cmd('flow-measure', 'simulate the candidate at its current controls (counts against budget)')
    c.add_argument('workspace')
    c.add_argument('candidate')
    c.add_argument('--timeout', type=_positive_float, default=300)
    c = cmd('flow-sweep', 'simulate the candidate at several control states; prints a summary table')
    c.add_argument('workspace')
    c.add_argument('candidate')
    c.add_argument('--codes', required=True, help='JSON file: [{"main":0}, {"main":15}, ...]')
    c.add_argument('--timeout', type=_positive_float, default=300)
    c = cmd('flow-converge', 're-run listed results with tighter HB settings (harmonics/tstab) to create independent convergence evidence')
    c.add_argument('workspace')
    c.add_argument('candidate')
    c.add_argument('--results', required=True, help='JSON list of result.json paths to re-check')
    c.add_argument('--harmonics', type=int, default=21)
    c.add_argument('--tstab-ns', type=_positive_float, default=120)
    c.add_argument('--timeout', type=_positive_float, default=300)
    c = cmd('flow-freeze', 'qualify the adopted control-state grid (coverage, constraints, independent convergence)')
    c.add_argument('workspace')
    c.add_argument('candidate')
    c.add_argument('--results', required=True)
    c.add_argument('--convergence', required=True)

    c = cmd('diagnose', 'environment check (python, spectre, site, families)')
    c.add_argument('--workspace', default='.')
    c = cmd('gui', 'Tk UI (needs DISPLAY)')
    c.add_argument('--workspace')
    c.add_argument('--project')
    c.add_argument('--runtime')
    from . import cli_ext
    cli_ext.register(sub)
    return p


QUICKSTART = """VCO Design Studio — shortest correct path (copy, then edit the spec):

  S=%(root)s/launch ; W=<your writable dir>/<task>
  1. $S spec-fields                      # what a spec may contain
     write $W/spec.json  (required: pdk, frequency_band_hz, power_max_mw; usually pn_targets_dbc_hz)
     $S spec-check $W/spec.json         # validates + shows defaults (vdd, temp 27, enforcement)
  2. $S flow-init --spec $W/spec.json --workspace $W/task
  3. $S select --spec $W/spec.json      # ranks user measurements; empty until evidence is supplied
     $S evidence-query --pdk <pdk> --frequency-hz <f> --power-mw <P> --temp-c 27
  4. $S project <family> --variant <v> --spec $W/spec.json --output $W/p1.json   # inherits pdk/vdd/temp/f0
     ($S seed --spec $W/spec.json --family <family> gives sizing from the nearest measured design)
  5. $S flow-add $W/task --project $W/p1.json --reason "<why>" --evidence <id>
  6. $S flow-measure $W/task candidate_001
     $S flow-next $W/task                # tells you exactly what to do next (diagnosis + knowledge)
  7. sweep codes -> converge -> freeze -> deliver:
     $S flow-sweep $W/task candidate_00N --codes codes.json
     $S flow-converge $W/task candidate_00N --results picked.json
     $S flow-freeze $W/task candidate_00N --results adopted.json --convergence conv.json
     $S deliver $W/task                  # delivery package + interfaces for PVT/MC/layout/post-sim
Exit codes: 0 ok, 2 invalid request, 3 measurement failed, 4 timeout, 130 interrupted.
"""


def _set_overrides(project, items):
    for item in items:
        if '=' not in item:
            raise InvalidRequest('--set expects KEY=VALUE, got %r' % item)
        key, value = item.split('=', 1)
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = value
        if '.' in key:
            reg, field = key.split('.', 1)
            if reg not in project['registers'] or field not in ('bits', 'value'):
                raise InvalidRequest('unknown register override %r (registers: %s; fields: bits, value)' % (key, ', '.join(project['registers'])))
            project['registers'][reg][field] = int(parsed)
        else:
            if key not in project['parameters']:
                raise InvalidRequest('unknown parameter %r; known: %s' % (key, ', '.join(sorted(project['parameters']))))
            project['parameters'][key] = parsed
    return project


def dispatch(args):
    """Return (payload, exit_code)."""
    c = args.command
    if c == 'quickstart':
        return QUICKSTART % {'root': ROOT}, EXIT_OK
    if c == 'site':
        from .site import describe
        return describe(), EXIT_OK
    if c == 'spec-check':
        from .spec import validate
        spec, warnings = validate(read_json(args.spec))
        return {'status': 'valid', 'spec': spec, 'warnings': warnings}, EXIT_OK
    if c == 'spec-fields':
        from .spec import describe, EXAMPLE
        return {'fields': describe(), 'example': EXAMPLE}, EXIT_OK
    if c == 'bindings':
        from .spec import canonical_pdk
        out = read_json(ROOT / 'examples/bindings/index.json')
        for b in out['bindings']:
            b['file'] = str((ROOT / b['file']).resolve()) if not Path(b['file']).is_absolute() else b['file']
        if args.pdk:
            key = canonical_pdk(args.pdk)
            out['bindings'] = [x for x in out['bindings'] if x['pdk'] == key]
            if not out['bindings']:
                out['status'] = 'unknown_pdk'
        return out, EXIT_OK
    if c == 'catalog':
        from .catalog import list_families
        return list_families(), EXIT_OK
    if c == 'baselines':
        from .catalog import list_reference_baselines
        return list_reference_baselines(), EXIT_OK
    if c == 'describe':
        from .catalog import get_variant
        out = get_variant(args.family, args.variant)
        out.pop('graph', None)
        return out, EXIT_OK
    if c == 'evidence-query':
        from .evidence import query, default_indexes, compact
        out = query(args.index if args.index is not None else default_indexes(), pdk=args.pdk,
                    frequency_hz=args.frequency_hz, power_mw=args.power_mw, vdd_v=args.vdd_v, temp_c=args.temp_c,
                    main_bits=args.main_bits, offset=args.offset, frequency_tolerance=args.frequency_tolerance,
                    power_tolerance=args.power_tolerance, band_files=args.bands, architecture=args.architecture,
                    variant=args.variant)
        return (out if args.full else compact(out)), EXIT_OK
    if c == 'evidence-point':
        from .evidence import from_result
        out = from_result(args.result, args.pdk, real_bank=True if args.real_bank else None)
        with Path(args.output).open('x') as f:
            f.write(json.dumps(out, allow_nan=False) + '\n')
        return out, EXIT_OK
    if c == 'project':
        from .catalog import default_project
        if Path(args.output).exists():
            raise FileExistsError('Project already exists: %s (choose a new --output)' % args.output)
        spec = None
        if args.spec:
            from .spec import validate
            spec, _ = validate(read_json(args.spec))
        out = default_project(args.family, args.variant, args.binding, spec=spec)
        out = _set_overrides(out, args.set)
        write_json(args.output, out)
        return out, EXIT_OK
    if c == 'generate':
        from .generator import generate
        return generate(read_json(args.project), args.output), EXIT_OK
    if c == 'simulate':
        from .simulator import run
        out = run(args.generated, args.output, args.timeout)
        return out, _measure_code(out)
    if c == 'bank-ac':
        from .characterize import bank_ac
        try:
            codes = [int(x) for x in args.codes.split(',')]
            freqs = [float(x) * 1e9 for x in args.freq_ghz.split(',')]
        except ValueError:
            raise InvalidRequest('--codes must be integers and --freq-ghz numbers, comma separated')
        return bank_ac(args.generated, args.output, bank=args.bank, codes=codes, frequencies_hz=freqs,
                       vcm=args.vcm, tank_inductance_h=args.tank_inductance_h), EXIT_OK
    if c == 'representative':
        from .representative import run
        out = run(args.generated, args.output, read_json(args.codes), read_json(args.bank_ac_options) if args.bank_ac_options else None)
        return out, (EXIT_OK if out.get('status') == 'completed' else EXIT_MEASURE_FAILED)
    if c == 'optimize':
        from .integrations import optimize
        from .simulator import evaluate_candidate
        evaluator = evaluate_candidate
        if args.codes:
            from .representative import optimizer_evaluator
            evaluator = optimizer_evaluator(read_json(args.codes), read_json(args.bank_ac_options) if args.bank_ac_options else None)
        elif args.bank_ac_options:
            raise InvalidRequest('--bank-ac-options requires --codes')
        if args.budget < 1:
            raise InvalidRequest('--budget must be >= 1')
        return optimize(args.generated, read_json(args.variables), evaluator, args.output, algorithm=args.algorithm,
                        budget=args.budget, batch_size=min(4, args.budget)), EXIT_OK
    if c.startswith('flow-'):
        from . import flow
        if c == 'flow-init':
            return flow.init_task(read_json(args.spec), args.workspace), EXIT_OK
        if c == 'flow-status':
            out = flow.status(args.workspace)
            return (flow.brief_status(out) if args.brief else out), EXIT_OK
        if c == 'flow-next':
            return flow.next_action(args.workspace), EXIT_OK
        if c == 'flow-add':
            return flow.add_candidate(args.workspace, read_json(args.project), args.reason, args.evidence), EXIT_OK
        if c == 'flow-mark':
            return flow.mark(args.workspace, args.candidate, args.disposition, args.reason), EXIT_OK
        if c == 'flow-measure':
            out = flow.measure(args.workspace, args.candidate, args.timeout)
            return out, _measure_code(out)
        if c == 'flow-sweep':
            out = flow.sweep(args.workspace, args.candidate, read_json(args.codes), args.timeout)
            return out, (EXIT_OK if out.get('all_measured') else EXIT_MEASURE_FAILED)
        if c == 'flow-converge':
            out = flow.converge(args.workspace, args.candidate, read_json(args.results), args.harmonics, args.tstab_ns, args.timeout)
            return out, (EXIT_OK if out.get('all_measured') else EXIT_MEASURE_FAILED)
        if c == 'flow-freeze':
            return flow.freeze(args.workspace, args.candidate, read_json(args.results), read_json(args.convergence)), EXIT_OK
    if c == 'diagnose':
        from .site import describe
        from .catalog import list_families
        out = describe()
        out.update({'python_running': sys.version.split()[0], 'workspace': str(Path(args.workspace).resolve()),
                    'families': len(list_families())})
        return out, EXIT_OK
    if c == 'gui':
        from .ui import main as gui
        return {'status': 'closed', 'exit': gui(workspace=args.workspace, project=args.project, runtime=args.runtime)}, EXIT_OK
    from . import cli_ext
    return cli_ext.dispatch(args)


def _measure_code(result):
    if result.get('status') == 'measured':
        return EXIT_OK
    if result.get('failure_class') == 'timeout' or (result.get('receipt') or {}).get('stop_reason') == 'timeout':
        return EXIT_TIMEOUT
    return EXIT_MEASURE_FAILED


def _sigterm(signum, frame):
    raise KeyboardInterrupt('SIGTERM')


def _error(kind, exc, code):
    message = ('missing field or item: %s' % exc) if isinstance(exc, KeyError) else (str(exc) or repr(exc))
    payload = {'status': 'error', 'type': type(exc).__name__, 'message': message, 'exit_code': code}
    if kind:
        payload['kind'] = kind
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    return code


def main(argv=None):
    if sys.version_info < (3, 11):
        print(json.dumps({'status': 'error', 'message': 'Python 3.11+ required; set VCO_STUDIO_PYTHON'}), file=sys.stderr)
        return EXIT_INVALID
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except ValueError:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    from .runner import SolverInterrupted
    try:
        payload, code = dispatch(args)
    except (KeyboardInterrupt, SolverInterrupted) as exc:
        return _error('interrupted', exc, EXIT_INTERRUPTED)
    except StopIteration as exc:
        return _error('invalid_request', ValueError('referenced item not found (check candidate/task ids)'), EXIT_INVALID)
    except (InvalidRequest, ValueError, FileExistsError, FileNotFoundError, KeyError, IsADirectoryError,
            NotADirectoryError, json.JSONDecodeError) as exc:
        return _error('invalid_request', exc, EXIT_INVALID)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        return _error('operational', exc, EXIT_ERROR)
    if isinstance(payload, str):
        print(payload)
    else:
        try:
            print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
        except ValueError as exc:
            return _error('invalid_output', ValueError('result contains NaN/Infinity: %s' % exc), EXIT_ERROR)
    return code
