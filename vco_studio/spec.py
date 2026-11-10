"""Task specification schema: one place that defines, validates and normalizes a VCO spec.

A spec is a JSON object. Required: pdk, frequency_band_hz, power_max_mw.
Everything the flow, guidance, evidence lookup and templates need is defined
here, so agents never have to guess field names.
"""
from __future__ import annotations

import math

PDKS = ('demo_a', 'demo_b', 'demo_c', 'demo_d')
PN_OFFSETS = (10000, 100000, 1000000, 10000000)
NOMINAL_VDD = {'demo_a': 1.0, 'demo_b': 1.0, 'demo_c': 1.2, 'demo_d': 1.2}
ENFORCEMENT = ('advisory', 'guided', 'strict')

FIELDS = {
    # name: (type, required, description)
    'pdk': ('str', True, 'user binding id configured in site bindings, existing binding JSON path, or a fictional demo_a..demo_d'),
    'frequency_band_hz': ('band', False, '[f_low, f_high] the adopted codes must cover (Hz). Required unless center_hz + tr_min_pct are given'),
    'center_hz': ('pos', False, 'alternative to a fixed band: measured band center must be within center_tolerance of this (Hz)'),
    'center_tolerance': ('pos', False, 'relative tolerance of the measured band center (default 0.02 = +-2 %)'),
    'power_max_mw': ('pos', True, 'max total power over all adopted codes (VDD + bias + control sources), mW'),
    'pn_targets_dbc_hz': ('pn', False, '{"1000000": -110, ...}; keys must be 1e4/1e5/1e6/1e7 (the offsets HB-noise reports)'),
    'vdd_v': ('pos', False, 'supply; set explicitly for your binding (demo supplies are illustrative)'),
    'temp_c': ('num', False, 'temperature; default 27 (TT stage; user measurements must record temperature)'),
    'main_bits': ('int', False, 'main bank width 1..12 (default: chosen by designer / template)'),
    'aux_bits': ('int', False, 'aux bank width 1..12 for families with an aux register'),
    'max_step_hz': ('pos', False, 'max allowed frequency gap between adjacent adopted codes'),
    'tr_min_pct': ('pos', False, 'min tuning range (f_max-f_min)/center*100 over adopted codes; with center_hz it replaces the fixed band'),
    'budget': ('dict', False, '{"solver_processes": N, "wall_seconds": S}'),
    'allow_aux': ('bool', False, 'whether an independent aux control is acceptable (default true)'),
    'enforcement': ('str', False, 'advisory | guided (default) | strict -- how hard the flow gates are'),
    'evidence_indexes': ('list', False, 'override measured-evidence index files'),
    'comparison_frequency_tolerance': ('pos', False, 'evidence match tolerance (relative), default 0.03'),
    'comparison_power_tolerance': ('pos', False, 'evidence match tolerance (relative), default 0.10'),
    'convergence_tolerances': ('dict', False, '{"frequency_rel":1e-3,"power_rel":1e-2,"pn_abs_db":0.5}'),
    'notes': ('str', False, 'free text'),
    'vdd': ('pos', False, 'legacy alias of vdd_v'),
}


def canonical_pdk(value):
    """Normalize a user-defined binding id, or preserve an existing binding path."""
    if value is None:
        return None
    from pathlib import Path
    text = str(value).strip()
    if Path(text).is_file():
        return text
    from .evidence import canonical_pdk as normalize
    return normalize(text)


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate(spec, strict_unknown=False):
    """Return (normalized_spec, warnings). Raises ValueError listing every problem."""
    if not isinstance(spec, dict):
        raise ValueError('spec must be a JSON object')
    errors, warnings = [], []
    s = dict(spec)
    FIELDS_INTERNAL = {'derived', 'band_mode'}
    for k in list(s):
        if k in FIELDS_INTERNAL:
            s.pop(k)
    for name, (kind, required, _) in FIELDS.items():
        if required and name not in s:
            errors.append('missing required field %s (%s)' % (name, FIELDS[name][2]))
    unknown = sorted(set(s) - set(FIELDS))
    if unknown:
        (errors if strict_unknown else warnings).append('unknown spec fields ignored: %s' % ', '.join(unknown))
    if 'pdk' in s:
        p = canonical_pdk(s['pdk'])
        if p is None:
            errors.append('pdk %r is not one of %s' % (s['pdk'], ', '.join(PDKS)))
        s['pdk'] = p
    if 'frequency_band_hz' not in s and not ('center_hz' in s and 'tr_min_pct' in s):
        errors.append('give frequency_band_hz=[low,high] (cover a fixed band) or center_hz + tr_min_pct (center +- center_tolerance and TR >= tr_min_pct)')
    if 'center_hz' in s and 'tr_min_pct' in s and _finite(s['center_hz']) and _finite(s['tr_min_pct']):
        c, tr = float(s['center_hz']), float(s['tr_min_pct']) / 100
        derived = [c * (1 - tr / 2), c * (1 + tr / 2)]
        if 'frequency_band_hz' in spec and spec['frequency_band_hz'] != derived:
            warnings.append('frequency_band_hz ignored: center_hz + tr_min_pct define the requirement')
        s['frequency_band_hz'] = derived
        s['band_mode'] = 'center_tr'
        warnings.append('band derived from center_hz and tr_min_pct for guidance; freeze checks center and TR, not fixed-band coverage')
    else:
        s.setdefault('band_mode', 'cover')
    if 'center_tolerance' in s and not (_finite(s['center_tolerance']) and 0 < s['center_tolerance'] < 0.5):
        errors.append('center_tolerance must be in (0, 0.5)')
    band = s.get('frequency_band_hz')
    if band is not None:
        if not (isinstance(band, (list, tuple)) and len(band) == 2 and all(_finite(x) for x in band) and 0 < band[0] < band[1]):
            errors.append('frequency_band_hz must be [low, high] with 0 < low < high (Hz)')
        else:
            s['frequency_band_hz'] = [float(band[0]), float(band[1])]
    for name in ('power_max_mw', 'vdd_v', 'vdd', 'max_step_hz', 'tr_min_pct', 'center_hz',
                 'comparison_frequency_tolerance', 'comparison_power_tolerance'):
        if name in s and not (_finite(s[name]) and s[name] > 0):
            errors.append('%s must be a finite positive number' % name)
    if 'temp_c' in s and not _finite(s['temp_c']):
        errors.append('temp_c must be a finite number')
    for name in ('main_bits', 'aux_bits'):
        if name in s:
            v = s[name]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or not 1 <= int(v) <= 12:
                errors.append('%s must be an integer 1..12' % name)
            else:
                s[name] = int(v)
    if 'vdd' in s and 'vdd_v' in s and _finite(s['vdd']) and _finite(s['vdd_v']) and not math.isclose(s['vdd'], s['vdd_v']):
        errors.append('vdd and vdd_v conflict; use vdd_v only')
    if 'vdd' in s and 'vdd_v' not in s and _finite(s['vdd']):
        s['vdd_v'] = s.pop('vdd')
        warnings.append('vdd renamed to vdd_v')
    pn = s.get('pn_targets_dbc_hz')
    if pn is not None:
        if not isinstance(pn, dict) or not pn:
            errors.append('pn_targets_dbc_hz must be a non-empty object {offset_hz: dBc/Hz}')
        else:
            norm = {}
            for k, v in pn.items():
                try:
                    off = int(float(k))
                except (TypeError, ValueError):
                    errors.append('pn_targets_dbc_hz key %r is not a number' % k)
                    continue
                if off not in PN_OFFSETS:
                    errors.append('pn_targets_dbc_hz offset %s unsupported; use one of %s' % (k, ', '.join(map(str, PN_OFFSETS))))
                if not _finite(v) or v >= 0:
                    errors.append('pn_targets_dbc_hz[%s] must be a negative dBc/Hz number' % k)
                norm[str(off)] = v
            s['pn_targets_dbc_hz'] = norm
    if 'enforcement' in s and s['enforcement'] not in ENFORCEMENT:
        errors.append('enforcement must be one of %s' % ', '.join(ENFORCEMENT))
    if 'allow_aux' in s and not isinstance(s['allow_aux'], bool):
        errors.append('allow_aux must be true/false')
    if 'budget' in s:
        b = s['budget']
        if not isinstance(b, dict) or set(b) - {'solver_processes', 'wall_seconds'}:
            errors.append('budget must be {"solver_processes": N, "wall_seconds": S}')
        else:
            for k, v in b.items():
                if not (_finite(v) and v > 0):
                    errors.append('budget.%s must be positive' % k)
    if errors:
        raise ValueError('Invalid spec: ' + '; '.join(errors))
    # defaults
    if s.get('pdk') in NOMINAL_VDD and 'vdd_v' not in s:
        s['vdd_v'] = NOMINAL_VDD[s['pdk']]
        warnings.append('vdd_v defaulted to %s V (PDK nominal)' % s['vdd_v'])
    s.setdefault('temp_c', 27)
    s.setdefault('enforcement', 'guided')
    lo, hi = s['frequency_band_hz']
    s.setdefault('derived', {})
    s['derived'] = {'center_hz': (lo + hi) / 2, 'tr_pct': (hi - lo) / ((lo + hi) / 2) * 100}
    return s, warnings


def describe():
    return {name: {'type': t, 'required': r, 'description': d} for name, (t, r, d) in FIELDS.items()}


EXAMPLE = {
    'pdk': 'demo_a',
    'frequency_band_hz': [9.4e9, 10.6e9],
    'power_max_mw': 3.0,
    'pn_targets_dbc_hz': {'1000000': -110},
    'vdd_v': 1.0,
    'temp_c': 27,
    'budget': {'solver_processes': 30},
    'enforcement': 'guided',
}
