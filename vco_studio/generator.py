"""Generate one immutable design from the shared electrical graph."""
import copy, json, math, re
from pathlib import Path
from .common import ROOT,read_json,write_json,sha256,electrical_hash,evaluate_expression,format_number
from .catalog import get_variant,default_project
from .site import resolve as resolve_path


# HB start-up initial conditions per family: node -> (DC reference, sign of the 0.5 mV kick) in the oscillating mode
IC_RULES={
    'guo':{'OUTP':('vdc',1),'OUTN':('vdc',-1),'DUT.DP':('vdd',-1),'DUT.DN':('vdd',1)},
    'du':{'OUTP':('cm',1),'OUTN':('cm',-1),'DUT.N2':('cm',1),'DUT.N1':('cm',-1)},
}

def resolve_project(project):
    base=default_project(project['family'],project.get('variant'));base.update({k:v for k,v in project.items() if k not in ['parameters','registers']})
    base['parameters'].update(project.get('parameters',{}))
    incoming=project.get('registers',{})
    if isinstance(incoming,list):incoming={r['name']:r for r in incoming}
    unknown=set(incoming)-set(base['registers'])
    if unknown:raise ValueError('This topology has no independent controls '+str(sorted(unknown)))
    for reg,val in incoming.items():
        if isinstance(val,int):val={'value':val}
        base['registers'][reg].update(val)
    binding=base['binding']
    if isinstance(binding,str):
        from .catalog import resolve_binding_ref
        binding=read_json(resolve_binding_ref(binding))
    base['binding']=binding
    return base

def make_graph(project):
    p=resolve_project(project);v=get_variant(p['family'],p['variant']);g=copy.deepcopy(v['graph']);par=p['parameters']
    for key in ['L_scale','core_width_scale','bank_cap_scale','aux_cap_scale','loss_scale','k_scale']:
        par[key]=evaluate_expression(par[key])
        if par[key]<=0:raise ValueError(key+' must be positive')
    graphs=g['subcircuits']+[g];byname={x['name']:x for x in graphs}
    for reg,conf in p['registers'].items():
        bits=int(conf['bits']);value=int(conf.get('value',0))
        if not 1<=bits<=12:raise ValueError('Register width must be 1..12; >12 requires explicit implementation sizing')
        if not 0<=value<2**bits:raise ValueError(reg+' register value outside width')
        bank=byname['CT_R3_'+reg];pins=['B'+str(i) for i in range(bits-1,-1,-1)]
        bank['ports']=[{'name':n,'direction':'inputOutput'} for n in ['N','P']+pins+['VDD','VSS']]
        bank['instances']=[{'name':'I%d_%d'%(bit,j),'role':'subckt','master':'CT_R3U_'+reg,'nodes':{'N':'N','P':'P','SW':'B'+str(bit),'VDD':'VDD','VSS':'VSS'},'parameters':{},'position':[4*bit,4*j]} for bit in range(bits) for j in range(2**bit)]
        old=[x['name'] for x in g['ports']];start=next(i for i,n in enumerate(old) if n.startswith(reg+'_B'))
        old=[n for n in old if not n.startswith(reg+'_B')];old[start:start]=[reg+'_'+n for n in pins]
        g['ports']=[{'name':n,'direction':'inputOutput'} for n in old]
        for inst in g['instances']:
            if inst.get('master')==bank['name']:
                inst['nodes']={k:val for k,val in inst['nodes'].items() if not re.fullmatch(r'B\d+',k)}
                inst['nodes'].update({pin:reg+'_'+pin for pin in pins})
        record=next(x for x in g['registers'] if x['name']==reg);record.update(bits=bits,default=value,pins_msb_first=[reg+'_'+n for n in pins])
    migration=[]
    source_binding=read_json(ROOT/'examples/bindings/demo_a.json')
    source_model_paths={str(resolve_path(x['path']).resolve()) for x in source_binding.get('model_includes',[])}
    target_model_paths={str(resolve_path(x['path']).resolve()) for x in p['binding'].get('model_includes',[])}
    model_source_changed=target_model_paths!=source_model_paths
    for sub in graphs:
        for inst in sub['instances']:
            if inst.get('ideal') or inst['role']=='subckt':continue
            binding=p['binding'].get('roles',{}).get(inst['role'],{})
            if model_source_changed and binding.get('model')==inst.get('source_model') and binding.get('geometry_policy') not in ('target_seed','preserve_verified'):
                raise ValueError('Same model name from different model files requires explicit geometry_policy target_seed or preserve_verified: '+inst['role'])
            if model_source_changed and binding.get('model')==inst.get('source_model') and binding.get('geometry_policy')=='preserve_verified':
                migration.append({'instance':sub['name']+'/'+inst['name'],'policy':'preserve_verified','from_model':inst.get('source_model'),'to_model':binding['model'],'qualification':'caller explicitly preserved geometry across model sources; target-domain/performance requires evidence'})
            if binding.get('model') and (binding['model']!=inst.get('source_model') or binding.get('geometry_policy')=='target_seed'):
                seed=binding.get('seed_parameters')
                if not seed:raise ValueError('Different target model requires characterized/default seed: '+inst['role'])
                before=copy.deepcopy(inst['parameters'])
                # Canonical geometry is resolved once; UI/native/netlist see the same migrated devices.
                if inst['role'] in ['cap','res']:
                    after={k:evaluate_expression(v) for k,v in seed.items()}
                    if 'm' in before and 'm' in binding.get('netlist_parameter_map',{}):after['m']=before['m']
                else:
                    after={k:evaluate_expression(v) for k,v in seed.items()}
                    for k in ['l','w','nf','m']:
                        if binding.get('geometry_policy')!='target_seed' and k in before and k in binding.get('netlist_parameter_map',{}):after[k]=before[k]
                    for k,lim in binding.get('limits',{}).items():
                        if k in after and isinstance(lim,list) and len(lim)==2:
                            lo,hi=lim
                            if lo is not None:after[k]=max(after[k],lo)
                            if hi is not None:after[k]=min(after[k],hi)
                inst['parameters']=after;inst['target_model']=binding['model']
                migration.append({'instance':sub['name']+'/'+inst['name'],'from_model':inst.get('source_model'),'to_model':binding['model'],'from_parameters':before,'to_parameters':after,'qualification':'explicit target seed; only declared limits checked, model-domain/performance equivalence not established'})
    for graph in graphs:
        for inst in graph['instances']:
            vals=inst['parameters'];role=inst['role']
            if graph is g:
                if role in ['nmos','pmos']:
                    vals['w']*=par['core_width_scale']
                    wmin=(p['binding'].get('roles',{}).get(role,{}).get('limits',{}).get('w') or [None,None])[0]
                    if wmin and vals['w']<wmin*(1-1e-12) and vals.get('nf',1)>1:
                        # below the finger-width limit: fewer fingers at the same total width
                        total=vals['w']*vals['nf'];nf=max(1,math.floor(total/wmin));vals['nf']=float(nf);vals['w']=total/nf
                    wmax=(p['binding'].get('roles',{}).get(role,{}).get('limits',{}).get('w') or [None,None])[1]
                    if wmax and vals['w']>wmax*(1+1e-12) and 'nf' in vals:
                        # keep the finger width legal: scale the finger count instead (same total width)
                        total=vals['w']*vals['nf'];nf=math.ceil(total/wmax);vals['nf']=float(nf);vals['w']=total/nf
                    nfmax=(p['binding'].get('roles',{}).get(role,{}).get('limits',{}).get('nf') or [None,None])[1]
                    if nfmax and vals.get('nf',0)>nfmax and 'm' in p['binding'].get('roles',{}).get(role,{}).get('netlist_parameter_map',{}):
                        # beyond the finger limit: parallel multiplier m (same total width)
                        total=vals['w']*vals['nf']*vals.get('m',1);mult=math.ceil(vals['nf']/nfmax);nf=math.ceil(vals['nf']/mult)
                        vals['m']=float(vals.get('m',1)*mult);vals['nf']=float(nf);vals['w']=total/(nf*vals['m'])
                if role=='ind':vals['l']*=par['L_scale']
                if role=='res' and inst['name'].startswith('RSL'):vals['r']*=par['L_scale']*par['loss_scale']
                if role=='res' and inst['name'].startswith('RPL'):vals['r']*=par['L_scale']/par['loss_scale']
                if role=='mutual':vals['coupling']*=par['k_scale']
            if graph['name'].startswith('CT_R3U_') and role=='cap':
                scale=par['aux_cap_scale'] if graph['name'].endswith('_aux') else par['bank_cap_scale']
                
                if 'l' in vals and 'w' in vals:
                    vals['l']*=math.sqrt(scale);vals['w']*=math.sqrt(scale)
                elif 'l' in vals:vals['l']*=scale
                elif scale!=1:raise ValueError('Target capacitor has no supported geometric scale; use a validated explicit override')
    for path,over in p.get('overrides',{}).items():
        if '/' not in str(path):raise ValueError('override key must be "<subcircuit>/<instance>", got %r; subcircuits: %s'%(path,', '.join(sorted(byname))))
        sub,instname=path.split('/',1)
        if sub not in byname:raise ValueError('override %r: unknown subcircuit %r; known: %s'%(path,sub,', '.join(sorted(byname))))
        graph=byname[sub];inst=next((x for x in graph['instances'] if x['name']==instname),None)
        if inst is None:raise ValueError('override %r: unknown instance %r in %s; known: %s'%(path,instname,sub,', '.join(x['name'] for x in graph['instances'])))
        if not isinstance(over,dict):raise ValueError('override %r must map parameter names to values'%path)
        if inst.get('ideal') and inst['role']=='cap' and inst['parameters'].get('c')==50e-15 and 'c' in over and evaluate_expression(over['c'])!=50e-15:raise ValueError('Protected fixed 50fF must remain unchanged')
        inst['parameters'].update({k:evaluate_expression(val) for k,val in over.items()})
    g['migration']=migration
    g['parameters']=copy.deepcopy(par);g['binding_identity']=electrical_hash(p['binding']);g['notes']+=['Reference dimensions are a seed; any migration/scaling invalidates prior performance and LUT qualification.']
    return g,p

def validate_graph(graph,binding):
    errors=[];warnings=[];children=graph.get('subcircuits',[]);names={x['name']:x for x in children+[graph]}
    if binding.get('unresolved'):errors.append('Unresolved device binding: '+json.dumps(binding['unresolved'],ensure_ascii=False))
    ind=binding.get('roles',{}).get('ind',{})
    if ind and (ind.get('library'),ind.get('cell'))!=('analogLib','ind'):
        errors.append('Coupled primitive inductors require analogLib/ind; a PDK magnetic model needs an explicitly validated adapter')
    for sub in children+[graph]:
        seen=set();inds={x['name']:x for x in sub['instances'] if x['role']=='ind'}
        for inst in sub['instances']:
            role=inst['role'];p=inst['parameters'];label=sub['name']+'/'+inst['name']
            if inst['name'] in seen:errors.append(label+': duplicate instance')
            seen.add(inst['name'])
            if role=='subckt':
                if inst['master'] not in names:errors.append(label+': missing master')
                elif set(inst['nodes'])!={x['name'] for x in names[inst['master']]['ports']}:errors.append(label+': hierarchy terminal mismatch')
            elif role=='mutual':
                if abs(float(p['coupling']))>=1:errors.append(label+': abs(k) must be <1')
                if p['ind1'] not in inds or p['ind2'] not in inds:errors.append(label+': coupling references missing inductors')
            elif not inst.get('ideal'):
                b=binding.get('roles',{}).get(role)
                if not b or not b.get('model'):errors.append(label+': missing model binding for '+role);continue
                limits=b.get('limits',binding.get('limits',{}).get(role,{}))
                if b.get('model')!=inst.get('source_model'):
                    warnings.append(label+': migrated model; use target defaults and recharacterize')
                for key,lim in limits.items():
                    if key not in p or not isinstance(lim,(tuple,list)) or len(lim)!=2:continue
                    val=float(p[key]);lo,hi=lim
                    if (lo is not None and val<lo*(1-1e-12)) or (hi is not None and val>hi*(1+1e-12)):errors.append(label+': '+key+' outside declared model domain')
                if 'nf' in p and (p['nf']<1 or p['nf']!=int(p['nf'])):errors.append(label+': nf must be positive integer')
            for key in ['c','r','l','w','m']:
                if key in p and isinstance(p[key],(float,int)) and p[key]<=0:errors.append(label+': '+key+' must be positive')
        if len(inds)>1:
            import numpy as np
            order=list(inds);mat=np.eye(len(order))
            for inst in sub['instances']:
                if inst['role']=='mutual':
                    p=inst['parameters']
                    if p['ind1'] not in inds or p['ind2'] not in inds:continue
                    a,b=order.index(p['ind1']),order.index(p['ind2']);mat[a,b]=mat[b,a]=p['coupling']
            if np.linalg.eigvalsh(mat).min()<-1e-10:errors.append(sub['name']+': nonphysical mutual-inductance matrix')
    return {'valid':not errors,'errors':errors,'warnings':warnings,'scope':'graph/declared parameter domains, not startup/noise/PVT qualification'}

def emit_circuit(graph,binding):
    graphs=graph.get('subcircuits',[])+[graph];byname={g['name']:g for g in graphs};lines=[]
    primitive={'res':'resistor','cap':'capacitor','ind':'inductor','mutual':'mutual_inductor','vsource':'vsource','isource':'isource'}
    for g in graphs:
        lines.append('subckt '+g['name']+' '+' '.join(p['name'] for p in g['ports']))
        for inst in g['instances']:
            role=inst['role'];p=inst['parameters'];nodes=inst['nodes']
            if role=='subckt':
                model=inst['master'];order=[x['name'] for x in byname[model]['ports']];outparams=p
            elif inst.get('ideal') or role in ['vsource','isource']:
                model=primitive[role];order=['p','n'] if role!='mutual' else [];outparams=p
            else:
                b=binding['roles'][role];model=b['model'];order=b.get('netlist_terminal_order',list(nodes))
                mapping=b.get('netlist_parameter_map',b.get('parameter_map',{}));outparams=copy.deepcopy(b.get('netlist_defaults',b.get('defaults',{})))
                # Geometry of a different capacitor/resistor model must use its characterized target seed.
                transfer=p
                for key,val in transfer.items():
                    if key in mapping:outparams[mapping[key]]=val
                    elif key in b.get('passthrough_parameters',[]):outparams[key]=val
                if model==inst.get('source_model') and b.get('geometry_policy')!='target_seed':
                    for key in ['sigma','mismatchflag','mimflag','mf']:
                        if key in p:outparams[mapping.get(key,key)]=p[key]
            ns=' ('+' '.join(nodes[x] for x in order)+')' if order else ''
            lines.append(inst['name']+ns+' '+model+' '+ ' '.join(k+'='+format_number(v) for k,v in outparams.items()))
        lines.append('ends '+g['name']);lines.append('')
    return '\n'.join(lines)

def emit_tb(graph,project):
    p=project['parameters'];vdd=float(p['vdd']);vdc=float(p['vdc']);settings=project.get('simulation',{})
    save_startup=settings.get('save_startup_waveform',False)
    if type(save_startup) is not bool:raise ValueError('simulation.save_startup_waveform must be a boolean')
    saveinit='yes' if save_startup else 'no'
    lines=['simulator lang=spectre','global 0','include "models.scs"','include "circuit.scs"','parameters VDD='+format_number(vdd)+' VDC='+format_number(vdc),
           'VDD_SRC (VDD 0) vsource dc=VDD','VBIAS_SRC (VDC 0) vsource dc=VDC']
    saved=['OUTP','OUTN','VDD_SRC:p','VBIAS_SRC:p'];pinmap={'VSS':'0'}
    for reg,conf in project['registers'].items():
        pname=reg.upper()+'_CODE';lines.append('parameters '+pname+'='+str(int(conf['value'])))
        for bit in range(int(conf['bits'])):
            node=reg+'_B'+str(bit);inst='V_'+reg+'_'+str(bit)
            lines.append(inst+' ('+node+' 0) vsource dc=VDD*(floor('+pname+'/'+str(2**bit)+')-2*floor('+pname+'/'+str(2**(bit+1))+'))')
            saved.extend([node,inst+':p'])
    lines.append('DUT ('+' '.join(pinmap.get(x['name'],x['name']) for x in graph['ports'])+') '+graph['name'])
    temp=p.get('temp_c',27);harms=int(settings.get('harmonics',15));tstab=settings.get('tstab_ns',60);guess=p['frequency_guess_ghz']
    lines.append('opts options temp='+str(temp)+' tnom=27 reltol='+str(settings.get('reltol',1e-4))+' save=selected')
    reference=get_variant(project['family'],project['variant'])
    cm=vdc if project['family']=='guo' else vdd*.5 if project['family'] in ['lim','murphy','du'] and project['variant']!='nmos' else vdd
    # start-up kick in the oscillating mode, centred on each node's DC bias (docs/METHODS.md §4). Fixed template
    # numbers are not used: the bias moves during sizing (Guo gate = vdc), and a multi-core must be kicked in its
    # oscillating mode (Du: N2 with OUTP, N1 with OUTN), otherwise the DC-consistent IC excites only a decaying mode.
    rule=IC_RULES.get(project['family'])
    if rule:
        ref={'cm':cm,'vdd':vdd,'vdc':vdc}
        lines.append('ic '+' '.join(node+'='+format_number(ref[base]+sign*.0005) for node,(base,sign) in rule.items()))
    else:
        initial=settings.get('initial_conditions',{})
        if initial:
            lines.append('ic '+' '.join(k.replace('I22.','DUT.')+'='+format_number(val) for k,val in initial.items()))
        else:lines.append('ic OUTP='+format_number(cm+.0005)+' OUTN='+format_number(cm-.0005))
    lines.extend(['dcOp dc','dcOpInfo info what=oppoint where=rawfile',
        'hb (OUTP OUTN) hb oscic=lin tstab='+str(tstab)+'n saveinit='+saveinit+' autosteady=yes fundfreqs=['+str(guess)+'G] maxharms=['+str(harms)+'] errpreset=conservative oscmethod=onetier restart=yes annotate=status tstabmethod=gear2only',
        'hbnoise (OUTP OUTN) hbnoise sweeptype=relative relharmvec=[1] start=10k stop=10M dec=1 maxsideband='+str(harms)+' noiseout=[pm] annotate=status',
        'save '+' '.join(saved)])
    return '\n'.join(lines)+'\n'

def generate(project,output_dir):
    """Generate one immutable design directory atomically (all checks first, then a single rename)."""
    import os,shutil,uuid
    dest=Path(output_dir).expanduser().resolve()
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):raise FileExistsError('Output must be a new or empty directory: '+str(dest))
    graph,project=make_graph(project);binding=project['binding'];validation=validate_graph(graph,binding)
    if not validation['valid']:raise ValueError('Design validation failed: '+'; '.join(validation['errors']))
    includes=[]
    for item in binding.get('model_includes',[]):
        path=str(resolve_path(item['path']))
        if '"' in path or '\n' in path:raise ValueError('Invalid model filename')
        if not Path(path).is_file():raise FileNotFoundError('Model file in binding does not exist: '+path)
        includes.append('include "'+path+'"'+(' section='+item['section'] if item.get('section') else ''))
    if not includes:raise ValueError('No explicit model includes in binding')
    circuit=emit_circuit(graph,binding);tb=emit_tb(graph,project)
    model_files={path:sha256(path) for path in sorted({str(resolve_path(x['path']).resolve()) for x in binding['model_includes']})}
    dest.parent.mkdir(parents=True,exist_ok=True)
    tmp=dest.parent/('.gen-'+dest.name+'-'+uuid.uuid4().hex[:8])
    tmp.mkdir()
    try:
        write_json(tmp/'project.json',project);write_json(tmp/'graph.json',graph);write_json(tmp/'binding.json',binding);write_json(tmp/'registers.json',graph['registers']);write_json(tmp/'validation.json',validation);write_json(tmp/'migration.json',graph.get('migration',[]))
        (tmp/'circuit.scs').write_text(circuit)
        (tmp/'tb.scs').write_text(tb)
        (tmp/'models.scs').write_text('\n'.join(includes)+'\n')
        (tmp/'README.md').write_text('# '+project['family']+' / '+project['variant']+'\n\nGenerated from one shared graph. main and aux are independent static bit inputs; bank switches/inverters are PDK devices. No auxiliary register exists for fixed/auto-tracking families.\n\nModel includes and transistor geometry are seed values; native binding and current-condition AC/HB/noise verification are separate. Fixed 50 fF is preserved. TB bias source is explicit and power must include VDD, bias and active control sources. A changed bit width invalidates historical LUT/performance.\n\nUse the Studio `simulate` command for isolated execution; never overwrite this evidence directory.\n')
        files={f.name:sha256(f) for f in tmp.iterdir() if f.is_file()}
        physical_parameters={k:v for k,v in project['parameters'].items() if k!='frequency_guess_ghz'}
        condition={'parameters':physical_parameters,'register_widths':{k:v['bits'] for k,v in project['registers'].items()},'binding':binding}
        physical=electrical_hash({'circuit_sha256':files['circuit.scs'],'conditions':condition,'model_files':model_files})
        manifest={'schema':'vco.generated.v1','graph_hash':electrical_hash(graph),'physical_identity':physical,'model_files':model_files,'binding_hash':electrical_hash(binding),'family':project['family'],'variant':project['variant'],'output_dir':str(dest),'files':files,'qualification':'generated_not_simulation_qualified','provenance':graph['provenance']}
        write_json(tmp/'manifest.json',manifest)
        if dest.exists():dest.rmdir()
        os.rename(tmp,dest)
    except BaseException:
        shutil.rmtree(tmp,ignore_errors=True);raise
    return manifest
