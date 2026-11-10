"""Read-only, live OA/CDF discovery and explicit PDK bindings.

Machine paths belong in a selected runtime profile. No PDK files or invoking
workspace are written. Every worker owns a new isolated cds.lib and process group.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def sq(value):
    return json.dumps(str(value), ensure_ascii=True)


def load_profile(profile=None):
    if isinstance(profile, dict):
        return copy.deepcopy(profile)
    from . import site
    path = profile or os.environ.get('VCO_STUDIO_NATIVE_PROFILE') or site.load().get('native_profile')
    if not path:
        raise ValueError('Select a native runtime profile (VCO_STUDIO_NATIVE_PROFILE).')
    return json.loads(site.resolve(path).read_text())


def run_skill(workspace, body, folder, profile=None, timeout=180, cancel_event=None, ocean=False):
    profile = load_profile(profile)
    source = Path(workspace).resolve() / 'cds.lib'
    if not source.is_file():
        raise ValueError('Workspace has no cds.lib: ' + str(source))
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    mapping = folder / 'cds.lib'
    if not mapping.exists():
        mapping.write_text('INCLUDE ' + sq(source) + '\n')
    script, driver, done = [folder / p for p in ('job.il', 'driver.il', 'job.ok')]
    if script.exists() or done.exists():
        raise FileExistsError('Native worker folder is already used: ' + str(folder))
    script.write_text('vcoResult=errset(progn(\n' + body + '\n) t)\nwhen(vcoResult vcoOut=outfile(' + sq(done) + ') fprintf(vcoOut "OK") close(vcoOut))\nif(vcoResult then exit(0) else exit(1))\n')
    driver.write_text('unless(errset(load(' + sq(script) + ') t) exit(1))\n')
    executable = profile['ocean' if ocean else 'virtuoso']
    cmd = [executable, '-nograph', '-nocdsinit', '-cdslib', str(mapping), '-restore', str(driver), '-log', str(folder / 'cadence.log')]
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('CDS_MPS_') or key in ('CDS_LIB_PATH', 'CDS_LOAD_PATH'):
            env.pop(key, None)
    env['LC_ALL'] = 'C'
    env['CDS_LOAD_ENV'] = 'CWD'
    setup = profile.get('environment_script')
    lines = ['set -e', 'vco_cds_set=${CDS_LIC_FILE+x}', 'vco_cds=${CDS_LIC_FILE-}', 'vco_lm_set=${LM_LICENSE_FILE+x}', 'vco_lm=${LM_LICENSE_FILE-}']
    if setup:
        lines.append('source ' + shlex.quote(setup))
    lines += ['vco_restore_var() { printf -v "$1" %s "$2"; export "$1"; }', 'if [[ "$vco_cds_set" == x ]]; then vco_restore_var CDS_LIC_FILE "$vco_cds"; fi', 'if [[ "$vco_lm_set" == x ]]; then vco_restore_var LM_LICENSE_FILE "$vco_lm"; fi']
    if profile.get('prepend_path'):
        lines.append('export PATH=' + shlex.quote(':'.join(profile['prepend_path'])) + ':"$PATH"')
    lines.append('exec ' + shlex.join(cmd))
    wrapper = folder / 'child.sh'
    wrapper.write_text('\n'.join(lines) + '\n')
    started = time.time()
    with (folder / 'stdout.log').open('w') as log:
        proc = subprocess.Popen(['/bin/bash', str(wrapper)], cwd=folder, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError('Native worker cancelled')
                if time.time() - started > timeout:
                    raise TimeoutError('Native worker timeout')
                time.sleep(.15)
        except BaseException:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise
    if proc.returncode or not done.exists():
        tail = (folder / 'stdout.log').read_text(errors='replace')[-5000:]
        raise RuntimeError('Native worker failed; ' + str(folder) + '\n' + tail)
    return {'folder': str(folder), 'log': str(folder / 'stdout.log'), 'returncode': proc.returncode, 'elapsed_seconds': time.time()-started}


def _folder(profile, prefix):
    from . import site
    root = Path(profile.get('runtime_root') or os.environ.get('VCO_STUDIO_RUNTIME') or str(site.runs_root() / 'native'))
    return root / (prefix + '_' + uuid.uuid4().hex[:12])


def discover(workspace, profile=None):
    profile = load_profile(profile)
    folder = _folder(profile, 'discover')
    out = folder / 'libraries.tsv'
    body = 'o=outfile(%s)\nforeach(lib ddGetLibList()\n fprintf(o "LIB\\t%%s\\t%%s\\n" lib~>name lib~>readPath)\n foreach(cell lib~>cells foreach(view cell~>views fprintf(o "VIEW\\t%%s\\t%%s\\t%%s\\n" lib~>name cell~>name view~>name)))\n)\nclose(o)' % sq(out)
    receipt = run_skill(workspace, body, folder, profile)
    libs = {}
    for line in out.read_text().splitlines():
        p = line.split('\t')
        if p[0] == 'LIB':
            libs[p[1]] = {'name': p[1], 'path': p[2], '_cells': {}}
        elif p[0] == 'VIEW':
            libs[p[1]]['_cells'].setdefault(p[2], []).append(p[3])
    for lib in libs.values():
        lib['cells'] = [{'name': key, 'views': sorted(value)} for key, value in sorted(lib.pop('_cells').items())]
    return {'workspace': str(Path(workspace).resolve()), 'cds_lib': str(Path(workspace).resolve() / 'cds.lib'), 'libraries': sorted(libs.values(), key=lambda x:x['name']), 'profile': profile, 'receipt': receipt}


def skill_tokens(text):
    return [a or b for a,b in re.findall(r'"([^"]*)"|([^()\s]+)', text)]


def inspect_masters(workspace, selections, profile=None):
    profile = load_profile(profile)
    folder = _folder(profile, 'masters')
    out = folder / 'masters.tsv'
    body = 'o=outfile(%s)\n' % sq(out)
    for role, selection in selections.items():
        lib, cell, view = selection['library'], selection['cell'], selection.get('view', 'symbol')
        body += '''fprintf(o "MASTER\\t%s\\n")
fprintf(o "LIBPATH\\t%%s\\n" ddGetObj(%s)~>readPath)
cv=dbOpenCellViewByType(%s %s %s "" "r")
unless(cv error("Master is unavailable"))
foreach(term cv~>terminals fprintf(o "PIN\\t%%s\\t%%s\\n" term~>name term~>direction))
cdf=cdfGetCellCDF(ddGetObj(%s %s))
when(cdf
 foreach(p cdf->parameters fprintf(o "CDF\\t%%s\\t%%s\\t%%L\\n" p->name p->paramType p->defValue))
 sim=cdf->simInfo->spectre
 when(sim fprintf(o "SIM\\t%%L\\t%%L\\t%%L\\t%%L\\t%%L\\n" sim->componentName sim->termOrder sim->instParameters sim->propMapping sim->netlistProcedure))
)
dbClose(cv)
''' % (role, sq(lib), sq(lib), sq(cell), sq(view), sq(lib), sq(cell))
    body += 'close(o)'
    receipt = run_skill(workspace, body, folder, profile)
    result, current = {}, None
    for line in out.read_text().splitlines():
        p = line.split('\t')
        if p[0] == 'MASTER':
            current = result[p[1]] = {'pins': [], 'cdf': {}, 'receipt': receipt}
        elif p[0] == 'LIBPATH':
            current['library_path']=p[1]
        elif p[0] == 'PIN':
            current['pins'].append({'name':p[1], 'direction':p[2]})
        elif p[0] == 'CDF':
            raw = '\t'.join(p[3:])
            try:
                val = json.loads(raw)
            except (ValueError, TypeError):
                val = None if raw == 'nil' else raw
            current['cdf'][p[1]] = {'type':p[2], 'default':val}
        elif p[0] == 'SIM':
            current['spectre'] = {'componentName':p[1].strip('"'), 'termOrder':skill_tokens(p[2]), 'instParameters':skill_tokens(p[3]), 'propMapping':p[4], 'netlistProcedure':p[5]}
    return result


PIN_ALIASES = {'d':('D','drain'), 'g':('G','gate'), 's':('S','source'), 'b':('B','bulk','SUB','SUBSTRATE'), 'p':('PLUS','P','PLUS1','A','1'), 'n':('MINUS','N','MINUS1','K','2')}
PARAM_ALIASES = {'l':('l','lr','lt','length'), 'w':('w','wr','wt','width'), 'nf':('nf','nr','fingers'), 'm':('m','mult'), 'r':('r','res'), 'c':('c','cap'), 'coupling':('coupling','k'), 'ind1':('ind1',), 'ind2':('ind2',), 'dc':('vdc','idc','dc'), 'layer':('layer','lay'), 'mimflag':('mimflag',), 'mf':('mf',)}


def _one_alias(names, aliases):
    # Exact canonical spelling takes priority; fuzzy guesses never do.
    if aliases[0] in names:
        return aliases[0]
    matches = [n for n in names if n.lower() in {x.lower() for x in aliases}]
    return matches[0] if len(matches) == 1 else None


def resolve_binding(workspace, roles, name='binding', model_includes=None, profile=None):
    profile = load_profile(profile)
    metadata = inspect_masters(workspace, roles, profile)
    result = {'schema':'vco.binding.v1', 'name':name, 'workspace':str(Path(workspace).resolve()), 'cds_lib':str(Path(workspace).resolve()/'cds.lib'), 'technology':None, 'model_includes':model_includes or [], 'roles':{}, 'unresolved':[], 'profile':profile}
    for role, selection in roles.items():
        info = metadata[role]
        names = [p['name'] for p in info['pins']]
        canonical = ['d','g','s','b'] if role in ('nmos','pmos') else ([] if role == 'mutual' else ['p','n'])
        if role in ('cap','res','ind') and len(names) == 3:
            canonical.append('b')
        terminals = {key:_one_alias(names,PIN_ALIASES[key]) for key in canonical}
        for key, actual in terminals.items():
            if actual is None:
                result['unresolved'].append({'role':role, 'reason':'Ambiguous or missing terminal '+key, 'actual':names})
        if set(filter(None,terminals.values())) != set(names):
            result['unresolved'].append({'role':role, 'reason':'Unmapped master terminals', 'actual':names})
        params = info['cdf']
        parameter_map = {}
        for key, aliases in PARAM_ALIASES.items():
            found = _one_alias(params, aliases)
            if found:
                parameter_map[key] = found
        sim = info.get('spectre', {})
        model = sim.get('componentName')
        if not model or model == 'nil':
            model = params.get('model', {}).get('default')
        if not model:
            result['unresolved'].append({'role':role,'reason':'No Spectre componentName/model in CDF'})
        net_params = sim.get('instParameters', [])
        for key in net_params:
            if key in params:
                parameter_map.setdefault(key,key)
        props = [x for x in skill_tokens(sim.get('propMapping','')) if x != 'nil']
        for left,right in zip(props[::2], props[1::2]):
            if left in PARAM_ALIASES and right in params:
                parameter_map[left]=right
        netmap = {key:actual for key,actual in parameter_map.items() if actual in net_params}
        # CDF simInfo uses propMapping=(nil model model ...); parse pairs.
        props = [x for x in skill_tokens(sim.get('propMapping','')) if x != 'nil']
        for left,right in zip(props[::2], props[1::2]):
            for key,actual in parameter_map.items():
                if actual == right:
                    netmap[key] = left
        required = ('l','w','nf','m') if role in ('nmos','pmos') else ()
        for key in required:
            if key not in netmap:
                result['unresolved'].append({'role':role,'reason':'No verified canonical sizing mapping for '+key})
        if role=='cap' and not ('c' in netmap or 'l' in netmap and ('w' in netmap or 'nf' in netmap)):
            result['unresolved'].append({'role':role,'reason':'No verified capacitance/geometry parameter mapping'})
        if role=='res' and not ('r' in netmap or 'l' in netmap and 'w' in netmap):
            result['unresolved'].append({'role':role,'reason':'No verified resistance/geometry parameter mapping'})
        inverse = {actual:key for key,actual in terminals.items()}
        netorder = [inverse[pin] for pin in sim.get('termOrder',[]) if pin in inverse]
        if len(netorder) != len(terminals):
            result['unresolved'].append({'role':role,'reason':'CDF Spectre termOrder does not cover canonical terminals'})
        seed = {key:params[actual]['default'] for key,actual in parameter_map.items() if actual in params and params[actual]['default'] is not None and key in ('l','w','nf','m','layer','mimflag','mf','r','c') and key in netmap}
        netdefaults = {}
        for key in net_params:
            value=params.get(key,{}).get('default')
            if value is not None and str(value).strip() and not (isinstance(value,str) and ('Par(' in value or value in ('t','nil'))):
                netdefaults[key]=value
        result['roles'][role] = dict(selection, view=selection.get('view','symbol'), model=model, terminals=terminals, netlist_terminal_order=netorder, seed_parameters=seed, netlist_defaults=netdefaults, parameter_map=parameter_map, netlist_parameter_map=netmap, defaults={key:value['default'] for key,value in params.items() if value['default'] is not None}, limits={}, provenance={'source':'live OA master / effective cell CDF', 'metadata':info})
    _apply_profiles(result, model_includes is None)
    result['status'] = 'unresolved' if result['unresolved'] else 'resolved'
    result['identity'] = hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
    return result


def save_binding(binding, path):
    path = Path(path)
    with path.open('x') as stream:
        json.dump(binding,stream,indent=2,ensure_ascii=False)
    return str(path)


def _apply_profiles(binding, auto_models):
    from .common import evaluate_expression
    known=[json.loads(p.read_text()) for p in (ROOT/'native/profiles').glob('pdk-*.json')]
    models=[]
    binding['ideal_roles']={'ind':{'library':'analogLib','cell':'ind','loss':'explicit graph series/parallel resistors'},'fixed_cap':{'library':'analogLib','cell':'cap','scope':'exact fixed capacitors in graph'},'loss_res':{'library':'analogLib','cell':'res','scope':'explicit winding loss only'}}
    for role,b in binding['roles'].items():
        if role=='ind' and (b['library'],b['cell'])!=('analogLib','ind'):
            binding['unresolved'].append({'role':'ind','reason':'PDK magnetic master requires verified same-port inductance/loss/mutual adapter; this graph uses analogLib ind with explicit winding loss.'})
        for key,val in list(b['seed_parameters'].items()):
            try:
                b['seed_parameters'][key]=evaluate_expression(re.sub(r'(?<=\d)K$', 'k', str(val)))
            except (ValueError,TypeError,KeyError,SyntaxError):
                del b['seed_parameters'][key]
        info=b['provenance']['metadata']
        for profile in known:
            if b['library'] != profile['library'] or Path(info.get('library_path','/nonexistent')).resolve()!=Path(profile['library_path']).resolve():
                continue
            rules=profile.get('cells',{}).get(b['cell'],{})
            b['limits']=rules.get('limits',{})
            b['provenance']['domain_source']=rules.get('limits_source')
            if rules.get('limits_source'):
                domain_file=Path(rules['limits_source'])
                if not domain_file.is_file():
                    binding['unresolved'].append({'role':role,'reason':'Declared model-domain evidence is missing: '+str(domain_file)})
                else:
                    b['provenance']['domain_source_sha256']=hashlib.sha256(domain_file.read_bytes()).hexdigest()
            b['provenance']['profile']=profile['name']
            models.extend(profile.get('model_includes',[]))
    if auto_models:
        binding['model_includes']=list({(m['path'],m.get('section','')):m for m in models}.values())
    if any(b['library']!='analogLib' for b in binding['roles'].values()) and not binding['model_includes']:
        binding['unresolved'].append({'role':'models','reason':'No verified model include profile; supply model_includes for selected PDK.'})
    for model in binding['model_includes']:
        if not Path(model['path']).is_file():
            binding['unresolved'].append({'role':'models','reason':'Model include missing: '+model['path']})


def main():
    import argparse
    parser=argparse.ArgumentParser(description='Read actual OA libraries/CDF and resolve device bindings')
    commands=parser.add_subparsers(dest='command',required=True)
    c=commands.add_parser('discover');c.add_argument('workspace');c.add_argument('--profile');c.add_argument('--output')
    c=commands.add_parser('resolve');c.add_argument('workspace');c.add_argument('--roles',required=True);c.add_argument('--name',default='binding');c.add_argument('--model-includes');c.add_argument('--profile');c.add_argument('--output',required=True)
    args=parser.parse_args()
    if args.command=='discover':
        result=discover(args.workspace,args.profile)
    else:
        roles=json.loads(Path(args.roles).read_text())
        includes=json.loads(Path(args.model_includes).read_text()) if args.model_includes else None
        result=resolve_binding(args.workspace,roles,args.name,includes,args.profile)
    if args.output:
        save_binding(result,args.output)
        print(json.dumps({'output':str(Path(args.output).resolve()),'status':result.get('status','discovered'),'unresolved':result.get('unresolved',[])},indent=2,ensure_ascii=False))
    else:
        print(json.dumps(result,indent=2,ensure_ascii=False))


if __name__=='__main__':main()
