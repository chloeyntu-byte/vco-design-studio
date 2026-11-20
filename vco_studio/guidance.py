"""Mechanism-specific measured comparisons attached to real flow entry points."""
from pathlib import Path

from .catalog import available_variants
from .evidence import default_indexes, query


def noise_actions(project, metrics, spec, current_directory):
    paths = spec.get('evidence_indexes', default_indexes())
    if isinstance(paths, str):
        paths = [paths]
    resolved = [str(Path(current_directory) / path) for path in paths]
    parameters = project['parameters']
    binding = project.get('binding', {})
    pdk = spec.get('pdk') or project.get('pdk') or (binding.get('name') if isinstance(binding, dict) else None)
    failed = [
        int(offset) for offset, target in spec.get('pn_targets_dbc_hz', {}).items()
        if metrics.get('pn_' + str(int(offset)) + '_dbc_hz', float('inf')) > target
    ]
    eligible = available_variants(spec.get('allow_aux', True))
    comparisons = {}
    for offset in failed:
        comparisons[str(offset)] = query(
            resolved, pdk=pdk, frequency_hz=metrics.get('frequency_hz'), power_mw=metrics.get('power_mw'),
            vdd_v=parameters['vdd'], temp_c=parameters['temp_c'], main_bits=project['registers']['main']['bits'],
            offset=offset, frequency_tolerance=spec.get('comparison_frequency_tolerance', .03),
            power_tolerance=spec.get('comparison_power_tolerance', .10), eligible_variants=eligible,
        ) if pdk else {'status': 'unknown', 'reason': 'Explicit PDK identity required', 'matches': []}
    current = (project['family'], project.get('variant'))
    alternatives = eligible - {current}
    measured = dict.fromkeys(
        (row['architecture'], row['variant'])
        for comparison in comparisons.values() for row in comparison.get('matches', [])
        if (row['architecture'], row['variant']) in alternatives
    )
    candidates = [
        {'family': family, 'variant': variant,
         'mechanism': 'Compare tail bias and direct source return at matched frequency, power and tuning controls.',
         'basis': 'matched measured evidence' if measured else 'unmeasured discriminating mechanism candidate',
         'prepare_argv': ['./launch', 'project', family, '--variant', variant, '--binding', '<same-binding.json>',
                          '--output', '<new-project.json>'],
         'evaluate_argv': ['./launch', 'representative', '<new-generated>', '--codes', '<explicit-controls.json>',
                           '--output', '<new-evaluation>']}
        for family, variant in (measured or sorted(alternatives))
    ]
    return {'trigger': 'measured phase-noise violation', 'failed_offsets_hz': failed, 'comparisons': comparisons,
            'mechanism_candidates': candidates,
            'next_action': 'Use matching measured points first. Compare bias variants on the same control map and retune frequency and power into recorded tolerances.',
            'bank_action': {'command': 'bank-ac', 'codes': 'representative measured controls',
                            'frequencies': 'actual fundamental frequencies',
                            'purpose': 'Distinguish bank loading from core and bias noise. Apply the Q gate at f0.'},
            'qualification': 'Nominal comparisons only. Each candidate requires a measurement.'}
