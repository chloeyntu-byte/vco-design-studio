from copy import deepcopy
from pathlib import Path
from .common import ROOT,read_json

def list_families():
    return deepcopy(read_json(ROOT/'data/catalog.json')['families'])

def list_reference_baselines():
    return [{'id':'class_b','variants':[{'id':'nmos'},{'id':'tail'}],'qualification':'target-PDK measurement required'}]

def with_performance_reference(value):
    value['performance_lookup']={'owner':'shared measured evidence index; no duplicated numeric table',
        'command':['./launch','evidence-query','--architecture',value['family'],'--variant',value['id'],'--pdk','<target-pdk>','--frequency-hz','<actual-f>','--power-mw','<actual-power>','--offset','<offset>'],
        'qualification':'conditional observed peak, never a global physical maximum; retain VDD/temp/bits and convergence'}
    return value

def get_variant(family,variant=None):
    if family=='class_b':
        if variant not in (None,'nmos','tail'):raise ValueError('Unknown reference baseline variant')
        return with_performance_reference(read_json(ROOT/'data/baselines/class_b'/((variant or 'nmos')+'.json')))
    found=next((f for f in list_families() if f['id']==family),None)
    if found is None:raise ValueError('Unknown topology family: '+str(family))
    variant=variant or found['variants'][0]['id']
    if variant not in [v['id'] for v in found['variants']]:raise ValueError('Unknown family variant: '+str(variant))
    return with_performance_reference(read_json(ROOT/'data/topologies'/family/(variant+'.json')))

def resolve_binding_ref(binding):
    """Accept a binding path or a PDK name; return a path string usable by the generator."""
    if binding is None or isinstance(binding, dict):
        return binding
    from . import site
    key = str(binding).strip().lower()
    configured = site.load().get('bindings', {}).get(key)
    if configured:
        return str(site.resolve(configured))
    if key in ('demo_a', 'demo_b', 'demo_c', 'demo_d'):
        return str(ROOT / 'examples/bindings' / (key + '.json'))
    return str(site.resolve(binding))


def default_project(family, variant=None, binding=None, spec=None):
    """Create a project. With a spec (or a PDK binding) the operating conditions are inherited:
    pdk binding, vdd, temp (27 C), HB frequency guess (band center), main/aux widths.
    Without spec/binding the original reference template is returned unchanged (replays)."""
    v = get_variant(family, variant)
    params = deepcopy(v['defaults']['parameters'])
    regs = deepcopy(v['defaults']['registers'])
    inherited = []
    out_notes = []
    if spec is not None and binding is None:
        binding = spec.get('pdk')
    ref = resolve_binding_ref(binding)
    if ref is not None and (spec is not None or not isinstance(ref, dict)):
        from .spec import NOMINAL_VDD, canonical_pdk
        pdk = (spec or {}).get('pdk') or _binding_pdk(ref)
        vdd = (spec or {}).get('vdd_v') or NOMINAL_VDD.get(canonical_pdk(pdk) if pdk else None)
        if vdd is not None and params.get('vdd') != vdd:
            params['vdd'] = float(vdd)
            inherited.append('vdd')
            out_notes.append('vdc (bias) kept from the template; it sets the current: retune it for the power target, or start from `launch seed`')
        temp = (spec or {}).get('temp_c', 27)
        if params.get('temp_c') != temp:
            params['temp_c'] = temp
            inherited.append('temp_c')
    if spec is not None:
        lo, hi = spec['frequency_band_hz']
        params['frequency_guess_ghz'] = round((lo + hi) / 2 / 1e9, 6)
        inherited.append('frequency_guess_ghz')
        for reg, key in (('main', 'main_bits'), ('aux', 'aux_bits')):
            if spec.get(key) and reg in regs:
                bits = int(spec[key])
                regs[reg]['bits'] = bits
                current = int(regs[reg].get('value', 0))
                regs[reg]['value'] = current if current < 2 ** bits else 2 ** (bits - 1)
                inherited.append(key)
    out = {'schema': 'vco.project.v1', 'family': family, 'variant': v['id'], 'binding': ref or 'demo_a',
           'parameters': params, 'registers': regs,
           'simulation': deepcopy(v['defaults'].get('simulation', {})),
           'workspace': '', 'target_library': '', 'target_cell': 'VCO_' + family + '_' + v['id']}
    if inherited:
        out['inherited'] = inherited
    if out_notes:
        out['notes'] = out_notes
    if spec is not None:
        out['pdk'] = spec.get('pdk')
    elif ref is not None:
        out['pdk'] = _binding_pdk(ref)
    return out


def _binding_pdk(ref):
    if isinstance(ref, dict):
        return None
    name = Path(str(ref)).stem
    from .spec import canonical_pdk, PDKS
    key = canonical_pdk(name)
    return key if key in PDKS else None
