#!/usr/bin/env python3
"""Real tool smoke with explicitly analytic evaluation, never silicon qualification."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import subprocess
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vco_studio.catalog import default_project
from vco_studio.generator import generate
from vco_studio.integrations import optimize, z11_analyze, caplab_analyze


def main():
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(str(__import__('vco_studio.site', fromlist=['x']).runs_root() / ('validation/tools-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S'))))
    dest.mkdir(parents=True, exist_ok=False)
    project = default_project('murphy', 'nmos')
    manifest = generate(project, dest / 'generated')
    def analytic_evaluator(candidate, directory):
        # Deliberately labelled contract fixture. It exercises real external circuit optimizer search
        # and real shared netlist generation, without claiming HB execution.
        x = candidate['parameters']['L_scale']
        score = (x - 1.13) ** 2
        return {'status': 'completed', 'feasible': True, 'total_violation': 0, 'merit': score, 'objectives': [score], 'evidence': {
            'kind': 'analytic_contract_fixture', 'formula': '(L_scale - 1.13)^2',
            'generated_manifest': str(directory / 'manifest.json'), 'not_hb': True}}
    optimizer = optimize(dest / 'generated', [{'parameter_id': 'L_scale', 'minimum': .7, 'maximum': 1.4}],
                   analytic_evaluator, dest / 'optimizer', budget=16, batch_size=4, seed=17)
    def failed_evaluator(candidate, directory):
        failure = subprocess.run(['/bin/false'], check=False)
        return {'status': 'failed', 'feasible': False, 'total_violation': 0,
                'evidence': {'kind': 'intentional_external_failure_fixture', 'returncode': failure.returncode,
                             'not_hb': True}}
    failed_optimizer = optimize(dest / 'generated', [{'parameter_id': 'L_scale', 'minimum': .9, 'maximum': 1.1}],
                          failed_evaluator, dest / 'optimizer-failure', budget=2, batch_size=1)
    z11 = z11_analyze(dest / 'generated', dest / 'z11', ['OUTP', 'OUTN'], [1e9, 5e9, 10e9, 20e9])
    samples = []
    for frequency, susceptance in [(1e9, -.01), (2e9, .01)]:
        y = complex(.001, susceptance)
        row = {'point': 0, 'frequency_Hz': frequency}
        for pair, v in [('11', y), ('12', -y), ('21', -y), ('22', y)]:
            row['Y' + pair + '_G_S'], row['Y' + pair + '_B_S'] = v.real, v.imag
        samples.append(row)
    caplab = caplab_analyze(dest / 'generated', samples, dest / 'caplab', ['OUTP', 'OUTN'], {
        'source_graph_hash': manifest['graph_hash'], 'port': ['OUTP', 'OUTN'],
        'kind': 'analytic_fixture', 'description': 'Floating reciprocal Y branch, interpolation contract only; not circuit characterization'})
    history = json.loads(Path(optimizer['history']).read_text())
    first_best = min(r['metrics']['merit'] for r in history[:4])
    final_best = optimizer['best']['metrics']['merit']
    summary = {'qualification': 'real_tool_contract_smoke_analytic_inputs', 'optimizer_engine': optimizer['engine'],
               'evaluations': optimizer['evaluations'], 'first_batch_best': first_best, 'final_best': final_best,
               'improved_after_tell': final_best < first_best, 'z11_points': len(z11['rows']),
               'caplab_interpolated_resonance_Hz': caplab['resonances']['0']['frequency_Hz'],
               'source_graph_hash': manifest['graph_hash'], 'directory': str(dest),
               'all_failed_status': failed_optimizer['status'], 'all_failed_best': failed_optimizer['best'],
               'all_failed_completed_count': failed_optimizer['completed_evaluations']}
    (dest / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    if failed_optimizer['status'] != 'failed' or failed_optimizer['best'] is not None or failed_optimizer['completed_evaluations'] != 0:
        raise RuntimeError('Failed evaluator was misqualified')
    if not summary['improved_after_tell'] or abs(summary['caplab_interpolated_resonance_Hz'] - 1.5e9) > 1:
        raise RuntimeError('Tool smoke assertions failed')


if __name__ == '__main__':
    main()
