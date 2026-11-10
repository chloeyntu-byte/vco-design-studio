"""Design flow of the system (docs/METHODS.md section 1) -- single implementation of the hand calculation.

power limit -> gm  ->  gain target (gm*Rp ~ 4, i.e. margin ~2, about full swing) -> Rp  ->  passive Q -> L  ->
f0 and tuning range -> C and bank size.  Inputs/outputs in SI units.  Used by `launch plan`, by the
eda-vco-initial-sizing skill (scripts/initial_sizing.py is a wrapper) and by autosize (gain targets).
The startup gain of a real design is measured with `launch loopgain` (vco_studio/loopgain.py), same definition.
"""
import argparse
import json
import math
import sys


def number(d, name, positive=True, default=None):
    v = d.get(name, default)
    if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v):
        raise ValueError('%s must be a finite number' % name)
    if (positive and v <= 0) or (not positive and v < 0):
        raise ValueError('%s must be %s' % (name, '> 0' if positive else '>= 0'))
    return float(v)


def size(d):
    if 'f_center_hz' in d and 'fractional_tuning_range' in d:
        fc = number(d, 'f_center_hz'); tr = number(d, 'fractional_tuning_range')
        if tr >= 2: raise ValueError('fractional_tuning_range must be < 2')
        fmin, fmax = fc * (1 - tr / 2), fc * (1 + tr / 2)
    else:
        fmin = number(d, 'f_min_hz'); fmax = number(d, 'f_max_hz')
    if fmax <= fmin: raise ValueError('f_max_hz must exceed f_min_hz')
    vdd = number(d, 'vdd_v'); p = number(d, 'power_budget_w')
    fraction = number(d, 'core_power_fraction')
    if fraction > 1: raise ValueError('core_power_fraction must be <= 1')
    margin = number(d, 'regeneration_margin')
    ql = number(d, 'inductor_q'); qc = number(d, 'capacitor_q')
    qothers = d.get('other_parallel_q', [])
    if not isinstance(qothers, list): raise ValueError('other_parallel_q must be a list')
    qs = [ql, qc] + [number({'q': q}, 'q') for q in qothers]
    q = 1.0 / sum(1.0 / x for x in qs)
    f0 = math.sqrt(fmin * fmax); omega = 2.0 * math.pi * f0
    topology = d.get('topology', 'symmetric_nmos_pair')
    if topology == 'symmetric_nmos_pair':
        current = p * fraction / (2.0 * vdd)
        gm = number(d, 'gm_over_id_per_v') * current
        gneg = gm / 2.0
        convention = 'Gneg=gm_single/2; gm_single*Rp=2*margin'
    elif topology == 'measured_port_profile':
        # Explicit same-port measured relation, never infer a multi-core multiplier.
        current = None; gm = None
        gneg = number(d, 'negative_conductance_s')
        convention = 'measured same-port Gneg; gm and current not inferred'
    else: raise ValueError('unsupported topology; provide measured_port_profile for other structures')
    rp = margin / gneg
    ld = rp / (q * omega)
    cmin = 1.0 / ((2 * math.pi * fmax) ** 2 * ld)
    cmax = 1.0 / ((2 * math.pi * fmin) ** 2 * ld)
    fixed = number(d, 'fixed_parasitic_c_f', False, 0)
    bank_min = cmin - fixed; span = cmax - cmin
    off = number(d, 'unit_off_c_f', False, 0)
    min_lsb = number(d, 'min_realizable_lsb_f', False, 0)
    max_step = d.get('max_frequency_step_hz')
    if max_step is not None: max_step = number(d, 'max_frequency_step_hz')
    bits = d.get('bits')
    if bits is None:
        upper = d.get('max_bits_to_compare', 12)
        if isinstance(upper, bool) or not isinstance(upper, int) or not 1 <= upper <= 16: raise ValueError('max_bits_to_compare must be integer 1..16')
        bits = list(range(1, upper + 1))
    elif isinstance(bits, int) and not isinstance(bits, bool): bits = [bits]
    if not isinstance(bits, list) or not bits or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 or n > 16 for n in bits):
        raise ValueError('bits must be an integer or nonempty list of integers in 1..16')
    modes = [('fixed_span', None)]
    if 'unit_on_c_f' in d:
        unit_on = number(d, 'unit_on_c_f')
        if unit_on <= off: raise ValueError('unit_on_c_f must exceed unit_off_c_f')
        modes.append(('fixed_unit_cell', unit_on - off))
    results = []
    for n in bits:
        units = (1 << n) - 1
        for mode, increment in modes:
            lsb = span / units if increment is None else increment
            bank_off = units * off
            # Padding is passive fixed C added after accounting for all cells in off state.
            padding = bank_min - bank_off
            physical_start = fixed + bank_off + max(0.0, padding)
            physical_end = physical_start + units * lsb
            frequencies = [1 / (2 * math.pi * math.sqrt(ld * (physical_start + i * lsb))) for i in range(units + 1)]
            steps = [frequencies[i] - frequencies[i+1] for i in range(units)]
            coverage = physical_start <= cmin * (1 + 1e-12) and physical_end >= cmax * (1 - 1e-12)
            lsb_ok = lsb >= min_lsb
            step_ok = None if max_step is None else max(steps) <= max_step
            bound = (fmax - fmin) / units
            bound_ok = None if max_step is None else bound <= max_step
            results.append({'bits': n, 'mode': mode, 'unit_count': units,
                'coverage_step_lower_bound_hz': bound,
                'coverage_step_bound_ok': bound_ok,
                'coverage_step_bound_assumption': 'at most 2^bits discrete frequencies cover both target endpoints without extra continuous or discrete tuning',
                'effective_lsb_f': lsb, 'total_off_c_f': bank_off, 'additional_padding_c_f': max(0.0, padding),
                'start_total_c_f': physical_start, 'end_total_c_f': physical_end,
                'highest_frequency_hz': frequencies[0], 'lowest_frequency_hz': frequencies[-1],
                'min_adjacent_step_hz': min(steps), 'max_adjacent_step_hz': max(steps),
                'coverage_ok': coverage, 'minimum_lsb_ok': lsb_ok, 'frequency_step_ok': step_ok,
                'ideal_constraints_satisfied': margin > 1 and coverage and lsb_ok and step_ok is not False,
                'feasible_for_supplied_constraints': margin > 1 and coverage and lsb_ok and step_ok is not False,
                'physical_feasibility': 'unverified',
                'startup_feasibility': 'unknown',
                'switch_loss': 'unverified; use measured code-dependent port admittance'})
    return {'qualification': 'analytic_seed_only',
            'feasibility_field_scope': 'ideal_constraints_satisfied and legacy feasible_for_supplied_constraints test supplied algebraic constraints only; never physical feasibility or startup',
            'physical_feasibility': 'unverified', 'startup_feasibility': 'unknown',
            'target_arithmetic_center_hz': (fmin+fmax)/2,
            'evaluation_frequency_hz': f0,
            'unspecified_constraints': [key for key in ('max_frequency_step_hz', 'min_realizable_lsb_f', 'fixed_parasitic_c_f', 'unit_off_c_f') if key not in d],
            'assumptions': ['static weak-dispersion parallel-Q approximation at geometric center',
                'differential L and C use the same floating port; each identical uncoupled grounded arm is Ldiff/2',
                'power budget times fraction is assigned to the core; ideal single-pair current ignores other branches',
                'fixed parasitic C excludes switched-cell off capacitance; ideal code mapping and linear C increments',
                'frequency steps ignore code-dependent loss, frequency dispersion and core/return loading'],
            'f_center_hz': f0, 'q_loaded': q, 'core_power_w': p * fraction,
            'id_per_device_a': current, 'gm_single_s': gm, 'gneg_s': gneg,
            'gm_convention': convention, 'margin_is_above_ideal_threshold': margin > 1,
            'rp_ohm': rp, 'l_differential_h': ld, 'l_each_arm_h': ld / 2,
            'c_total_at_fmax_f': cmin, 'c_total_at_fmin_f': cmax,
            'bank_required_at_fmax_f': bank_min, 'bank_required_at_fmin_f': cmax-fixed,
            'bank_swing_f': span, 'fixed_c_budget_possible': bank_min >= 0,
            'candidate_bits': results}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('input_json'); ap.add_argument('--output')
    args = ap.parse_args()
    try:
        with open(args.input_json) as f: d = json.load(f)
        result = size(d)
        output = json.dumps(result, indent=2, allow_nan=False) + '\n'
        if args.output:
            with open(args.output, 'w') as f: f.write(output)
        else: sys.stdout.write(output)
    except (ValueError, OSError, KeyError, TypeError) as e:
        print('error: %s' % e, file=sys.stderr); return 2
    return 0



GAIN_TARGET = 4.0          # gm*Rp_diff at the drain port (margin ~2); docs/METHODS.md
GAIN_LOW = 3.0             # below: little swing headroom (startup margin < 1.5)
GAIN_EXCESS = 6.0          # above: core oversize for the tank (allowed; compare against a smaller-gain variant)


def gain_class(gm_rp):
    if gm_rp is None:
        return 'unknown'
    if gm_rp < 2.0:
        return 'no_startup_margin'
    if gm_rp < GAIN_LOW:
        return 'low'
    if gm_rp <= GAIN_EXCESS:
        return 'nominal'
    return 'excess'


def swing_limit_v(core_type, vdd):
    """docs/RULES.md (Swing): CMOS core ~VDD, single-type NMOS/PMOS core ~2*VDD differential peak."""
    return (1.0 if core_type == 'cmos' else 2.0) * float(vdd)


def plan(spec, family=None, q=20.0, gain=GAIN_TARGET, gm_over_id=10.0, core_power_fraction=1.0, capacitor_q=None, bits=None):
    """Hand plan for a validated spec (launch plan). Symmetric single pair formulas; CMOS halves the current per
    device type but sums n+p gm on one side (same Rp definition)."""
    lo, hi = spec['frequency_band_hz']
    d = {'f_min_hz': lo, 'f_max_hz': hi, 'power_budget_w': spec['power_max_mw'] * 1e-3, 'vdd_v': spec['vdd_v'],
         'core_power_fraction': core_power_fraction, 'inductor_q': q, 'capacitor_q': capacitor_q or 1e6,
         'regeneration_margin': gain / 2.0, 'gm_over_id_per_v': gm_over_id, 'bits': bits or spec.get('main_bits', 4)}
    if spec.get('max_step_hz'):
        d['max_frequency_step_hz'] = spec['max_step_hz']
    out = size(d)
    out['inputs'] = d
    out['gain_target_gm_rp'] = gain
    out['family'] = family
    out['swing_limits_v'] = {'cmos': swing_limit_v('cmos', spec['vdd_v']), 'nmos_or_pmos': swing_limit_v('single', spec['vdd_v'])}
    out['next'] = ('generate the family template, center f0 with C first (keep L near l_differential_h), measure HB, '
                   'then `launch loopgain` for the real gm*Rp at the drain port and compare with gain_target_gm_rp')
    return out


def cli_register(sub):
    h = 'hand plan of the design flow: power -> gm -> gain target -> Rp -> Q -> L -> C/bank (docs/METHODS.md)'
    c = sub.add_parser('plan', help=h, description=h)
    c.add_argument('--spec', required=True, help='spec.json (validated like spec-check)')
    c.add_argument('--family')
    c.add_argument('--q', type=float, default=20.0, help='tank Q at f0 (magnetic mode Q incl. 1+|k|, <= 30)')
    c.add_argument('--gain', type=float, default=GAIN_TARGET, help='gm*Rp target at the drain port (default 4; excess allowed)')
    c.add_argument('--gm-over-id', type=float, default=10.0, help='assumed gm/Id of the core device (1/V)')
    c.add_argument('--bits', type=int)
    c.add_argument('--output')


def cli_dispatch(args):
    from .cli import EXIT_OK
    from .common import read_json, write_json
    from .spec import validate
    if args.command != 'plan':
        return None
    spec, _ = validate(read_json(args.spec))
    out = plan(spec, family=args.family, q=args.q, gain=args.gain, gm_over_id=args.gm_over_id, bits=args.bits)
    if args.output:
        write_json(args.output, out)
    return out, EXIT_OK
