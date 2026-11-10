"""Mechanism-specific measured comparisons attached to real flow entry points."""
from pathlib import Path
from .evidence import query,default_indexes

MECHANISMS={
 'class_b':('nmos','Conventional baseline isolates engineered harmonic-return benefit from core/tank/bank loading.'),
 'murphy':('nmos','Independent main/aux control permits a local differential/common-mode return phase comparison.'),
 'lim':('inverse_f','Inverse-F waveform shaping tests harmonic-voltage timing and device-noise weighting.'),
 'hegazi':('tail','Tail harmonic filtering tests current-source noise transfer and harmonic-return impedance.'),
 'guo':('wideband','Fixed wideband return tests harmonic behavior without per-code auxiliary tuning.'),
 'du':('autotracking','Intrinsic tracking tests whether fixed physical coupling follows the main tuning domain.')}

def noise_actions(project,metrics,spec,current_directory):
    paths=spec.get('evidence_indexes',default_indexes())
    if isinstance(paths,str):paths=[paths]
    resolved=[str(Path(x) if Path(x).is_absolute() else Path(current_directory)/x) for x in paths]
    p=project['parameters'];binding=project.get('binding',{});pdk=spec.get('pdk') or project.get('pdk') or (binding.get('name') if isinstance(binding,dict) else None)
    failed=[int(k) for k,v in spec.get('pn_targets_dbc_hz',{}).items() if metrics.get('pn_'+str(int(k))+'_dbc_hz',float('inf'))>v]
    comparisons={str(offset):query(resolved,pdk=pdk,frequency_hz=metrics.get('frequency_hz'),power_mw=metrics.get('power_mw'),vdd_v=p['vdd'],temp_c=p['temp_c'],main_bits=project['registers']['main']['bits'],offset=offset,frequency_tolerance=spec.get('comparison_frequency_tolerance',.03),power_tolerance=spec.get('comparison_power_tolerance',.10)) if pdk else {'status':'unknown','reason':'Explicit PDK identity required','matches':[]} for offset in failed}
    allow_aux=spec.get('allow_aux',True);measured=[]
    for comparison in comparisons.values():
        for r in comparison.get('matches',[]):
            family=r['architecture']
            if not allow_aux and family in ('murphy','hegazi','lim'):continue
            if family!=project['family'] and family not in measured:measured.append(family)
    proposed=measured or ['class_b']+(['murphy','hegazi'] if allow_aux else ['guo','du'])
    candidates=[]
    for family in proposed:
        if family==project['family'] or family not in MECHANISMS:continue
        variant,mechanism=MECHANISMS[family]
        candidates.append({'family':family,'variant':variant,'mechanism':mechanism,'basis':'matched measured evidence' if measured else 'unmeasured discriminating mechanism candidate',
          'prepare_argv':['./launch','project',family,'--variant',variant,'--binding','<same-binding.json>','--output','<new-project.json>'],
          'evaluate_argv':['./launch','representative','<new-generated>','--codes','<explicit-controls.json>','--output','<new-evaluation>']})
    return {'trigger':'measured phase-noise violation','failed_offsets_hz':failed,'comparisons':comparisons,'mechanism_candidates':candidates,
      'next_action':'Use matching measured points first. For unknown cells, select one discriminating alternative and baseline at a small explicit control map; retune frequency and power into recorded tolerances before ranking.',
      'bank_action':{'command':'bank-ac','codes':'representative measured controls','frequencies':'actual fundamental frequencies','purpose':'Distinguish bank conductance/off-capacitance loading from return/core noise. Q gate applies only at actual f0, never 2f.'},
      'qualification':'candidate actions, not automatic architecture selection; TT nominal only'}
