#!/usr/bin/env python3
"""Expand explicit measured-profile selections and a requested frequency/power/TR grid."""
import argparse
import csv
import json
import sys
from initial_sizing import size, number


def run(config, profiles):
    if profiles.get('schema_version') != 1: raise ValueError('unsupported profile schema')
    if config.get('port_frequency_applicability_confirmed') is not True:
        raise ValueError('confirm applicability of selected Q measurements to modeled port and frequencies')
    note = config.get('q_port_and_frequency_assumption')
    if not isinstance(note, str) or len(note.strip()) < 20:
        raise ValueError('q_port_and_frequency_assumption must explain the port and frequency approximation')
    pdk = config['pdk']; profile = profiles['pdks'][pdk]
    selected = config['profile_selection']
    for key in ('gm_index', 'inductor_index', 'capacitor_index'):
        if isinstance(selected[key], bool) or not isinstance(selected[key], int) or selected[key] < 0:
            raise ValueError('%s must be a nonnegative integer' % key)
    gm = profile['gm_points'][selected['gm_index']]
    ind = profile['passive_points'][selected['inductor_index']]
    cap = profile['passive_points'][selected['capacitor_index']]
    if gm.get('status', 'ok') != 'ok': raise ValueError('selected gm point is not valid')
    if ind.get('kind') != 'inductor' or cap.get('kind') != 'capacitor': raise ValueError('selected passive point kind mismatch')
    if ind.get('y_im_S') is not None and ind['y_im_S'] >= 0: raise ValueError('selected inductor is not inductive')
    if cap.get('y_im_S') is not None and cap['y_im_S'] <= 0: raise ValueError('selected capacitor is not capacitive')
    gm_id = number(gm, 'gm_S') / number(gm, 'id_A')
    ql = number(ind, 'q'); qc = number(cap, 'q')
    base = dict(config['base']); base.update(gm_over_id_per_v=gm_id, inductor_q=ql, capacitor_q=qc)
    cases=[]
    for f in config['frequencies_hz']:
        for p in config['power_budgets_w']:
            for tr in config['fractional_tuning_ranges']:
                d=dict(base); d.update(f_center_hz=f,power_budget_w=p,fractional_tuning_range=tr)
                cases.append({'input': d, 'result': size(d)})
    return {'status':'ideal estimates; not HB, startup, phase-noise or PVT qualification',
            'pdk':pdk, 'q_port_and_frequency_assumption':note, 'selected_profile_records':{'gm':gm,'inductor':ind,'capacitor':cap}, 'cases':cases}


def main():
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument('config_json'); ap.add_argument('profiles_json'); ap.add_argument('--output',required=True); ap.add_argument('--csv',required=True)
    args=ap.parse_args()
    try:
        with open(args.config_json) as f: config=json.load(f)
        with open(args.profiles_json) as f: profiles=json.load(f)
        data=run(config,profiles)
        with open(args.output,'w') as f: json.dump(data,f,indent=2,allow_nan=False); f.write('\n')
        with open(args.csv,'w') as f:
            fields=['pdk','center_hz','power_w','fractional_tr','bits','mode','q_loaded','gm_single_s','rp_ohm','l_differential_h','c_min_f','c_max_f','lsb_f','max_step_hz','feasible','ideal_constraints_satisfied','physical_feasibility','startup_feasibility']
            writer=csv.DictWriter(f,fieldnames=fields); writer.writeheader()
            for case in data['cases']:
                d=case['input']; r=case['result']
                for c in r['candidate_bits']:
                    writer.writerow(dict(pdk=data['pdk'],center_hz=d['f_center_hz'],power_w=d['power_budget_w'],fractional_tr=d['fractional_tuning_range'],bits=c['bits'],mode=c['mode'],q_loaded=r['q_loaded'],gm_single_s=r['gm_single_s'],rp_ohm=r['rp_ohm'],l_differential_h=r['l_differential_h'],c_min_f=r['c_total_at_fmax_f'],c_max_f=r['c_total_at_fmin_f'],lsb_f=c['effective_lsb_f'],max_step_hz=c['max_adjacent_step_hz'],feasible=c['feasible_for_supplied_constraints'],ideal_constraints_satisfied=c['ideal_constraints_satisfied'],physical_feasibility=c['physical_feasibility'],startup_feasibility=c['startup_feasibility']))
    except (ValueError,KeyError,IndexError,TypeError,OSError) as e:
        print('error: %s' % e,file=sys.stderr); return 2
    return 0
if __name__=='__main__': sys.exit(main())
