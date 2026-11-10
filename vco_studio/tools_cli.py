"""CLI entries for local analysis tools that were API-only: Z11 static network projection and the
Cap Array Lab workbook export (bank-ac already runs Cap Lab headlessly on real AC matrices)."""
from pathlib import Path


def cli_register(sub):
    c = sub.add_parser('z11', help='static passive impedance at a tank port (Z11 tool; fast pre-HB screening, not PN evidence)',
                       description='static passive impedance at a tank port (Z11 tool; fast pre-HB screening, not PN evidence)')
    c.add_argument('generated')
    c.add_argument('--output', required=True)
    c.add_argument('--port', default='OUTP,OUTN', help='two node names, e.g. OUTP,OUTN (differential) ')
    c.add_argument('--freq-ghz', required=True, help='comma list, e.g. 8,9,10,11,12 or a range start:stop:step')
    c = sub.add_parser('caplab-export', help='export a Cap Array Lab workbook for the generated design (optional GUI review)',
                       description='export a Cap Array Lab workbook for the generated design (optional GUI review)')
    c.add_argument('generated')
    c.add_argument('--output', required=True)
    c.add_argument('--port', default='OUTP,OUTN')
    _register_diag(sub)


def _register_diag(sub):
    c = sub.add_parser('diagnose-result', help='diagnosis codes + knowledge cards for one result.json (for runs made outside a flow task)',
                       description='diagnosis codes + knowledge cards for one result.json (for runs made outside a flow task)')
    c.add_argument('result')
    c.add_argument('--spec', required=True, help='spec JSON (pdk, band or center_hz+tr_min_pct, power_max_mw, pn targets)')


def _freqs(text):
    if ':' in text:
        a, b, s = (float(x) for x in text.split(':'))
        if s <= 0 or b < a:
            raise ValueError('range must be start:stop:step with step > 0')
        n = int(round((b - a) / s)) + 1
        return [(a + i * s) * 1e9 for i in range(n)]
    return [float(x) * 1e9 for x in text.split(',')]


def cli_dispatch(args):
    from .cli import EXIT_OK
    if args.command == 'z11':
        from .integrations import z11_analyze
        port = args.port.split(',')
        if len(port) != 2:
            raise ValueError('--port needs two node names')
        if Path(args.output).exists() and any(Path(args.output).iterdir()):
            raise FileExistsError('output exists and is not empty')
        out = z11_analyze(args.generated, args.output, port, _freqs(args.freq_ghz))
        return out, EXIT_OK
    if args.command == 'diagnose-result':
        from .common import read_json
        from .spec import validate
        from .advisor import diagnose
        from .knowledge import find_cards
        result = read_json(args.result)
        spec, _ = validate(read_json(args.spec))
        project = None
        gen = (result.get('receipt') or {}).get('generated_dir')
        if gen and (Path(gen) / 'project.json').is_file():
            project = read_json(Path(gen) / 'project.json')
        diag = diagnose(result, spec, project)
        codes = [d['code'] for d in diag]
        lo, hi = spec['frequency_band_hz']
        ctx = {'stage': 'tuning', 'symptoms': codes or None, 'pdk': spec.get('pdk'), 'family': (project or {}).get('family'),
               'frequency_ghz': (lo + hi) / 2e9, 'power_mw': spec.get('power_max_mw')}
        cards = find_cards(**{k: v for k, v in ctx.items() if v})
        return {'diagnosis': diag, 'knowledge': cards,
                'hint': 'inside a design task prefer `launch flow-measure` + `launch flow-next` (budget, receipts, next action)'}, EXIT_OK
    if args.command == 'caplab-export':
        from .integrations import export_caplab
        port = args.port.split(',')
        return export_caplab(args.generated, args.output, port), EXIT_OK
    return None
