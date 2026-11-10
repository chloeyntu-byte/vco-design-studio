"""Small persistent workflow: decisions remain human/agent owned, evidence is checked."""
from pathlib import Path
import datetime,json,subprocess,time,copy,math,re,os,fcntl,functools,inspect
from contextlib import contextmanager
from .common import read_json,write_json,sha256,evaluate_expression
from .catalog import list_families,default_project


@contextmanager
def _task_transaction(workspace,operation,create=False):
    """Serialize mutations of ONE task, including its in-flight solver budget.

    The separate lock inode is never replaced/unlinked. task.json remains an
    atomic snapshot for readers. Process exit closes the non-inherited fd and
    releases flock; unrelated task directories have independent locks. Direct
    simulator/generator calls and external manual edits are outside this lock.
    """
    root=Path(workspace).resolve()
    if create:root.mkdir(parents=True,exist_ok=True)
    if not root.is_dir():raise FileNotFoundError('Task directory does not exist: '+str(root))
    descriptor=os.open(str(root/'.task.lock'),os.O_CREAT|os.O_RDWR|getattr(os,'O_CLOEXEC',0),0o600)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX)
        owner=json.dumps({'pid':os.getpid(),'operation':operation,'acquired_epoch':time.time()}).encode()+b'\n'
        os.ftruncate(descriptor,0);os.write(descriptor,owner)
        try:yield root
        finally:
            os.ftruncate(descriptor,0);os.lseek(descriptor,0,os.SEEK_SET)
            os.write(descriptor,json.dumps({'operation':operation,'released_epoch':time.time(),'pid':None}).encode()+b'\n')
            fcntl.flock(descriptor,fcntl.LOCK_UN)
    finally:os.close(descriptor)


def _locked_mutation(create=False):
    def decorate(function):
        signature=inspect.signature(function)
        @functools.wraps(function)
        def wrapped(*args,**kwargs):
            workspace=signature.bind(*args,**kwargs).arguments['workspace']
            with _task_transaction(workspace,function.__name__,create=create):
                return function(*args,**kwargs)
        return wrapped
    return decorate


def _allocate_directory(parent,prefix,width,registered=()):
    """Reserve a fresh monotonically numbered directory; preserve orphan runs."""
    parent=Path(parent);parent.mkdir(parents=True,exist_ok=True)
    pattern=re.compile(re.escape(prefix)+r'(\d+)$')
    indices=[int(match[1]) for name in list(registered)+[path.name for path in parent.iterdir()]
             if (match:=pattern.fullmatch(str(name)))]
    index=max(indices,default=0 if prefix=='candidate_' else -1)+1
    while True:
        name=prefix+str(index).zfill(width);dest=parent/name
        try:dest.mkdir(exist_ok=False)
        except FileExistsError:index+=1;continue
        return name,dest


METHOD_CARDS=['vco-method-architecture-selection','vco-method-harmonic-mechanisms','vco-method-cap-range-budget']


def _fetch_method_cards(records=METHOD_CARDS):
    """Best-effort knowledge brief; never blocks task creation, never runs inside the task lock."""
    from .knowledge import kb_show
    out=[]
    for record in records:
        try:
            parsed=kb_show(record)
            out.append({'id':record,'status':'retrieved' if parsed and parsed.get('id')==record else 'failed','data':parsed})
        except Exception as exc:  # KB optional
            out.append({'id':record,'status':'unavailable','error':str(exc)[:300]})
    return out


def init_task(spec,workspace):
    from .spec import validate
    spec,warnings=validate(spec)
    root=Path(workspace).resolve()
    if (root/'task.json').exists():raise FileExistsError('Task already exists: %s; use flow-status / flow-next'%root)
    cards=_fetch_method_cards()
    return _init_locked(spec,warnings,cards,root)


@_locked_mutation(create=True)
def _init_locked(spec,warnings,cards,workspace):
    root=Path(workspace).resolve()
    if (root/'task.json').exists():raise FileExistsError('Task already exists: %s; use flow-status / flow-next'%root)
    root.joinpath('knowledge').mkdir(exist_ok=True)
    evidence=[]
    for card in cards:
        row={k:v for k,v in card.items() if k!='data'}
        if card.get('data'):
            write_json(root/'knowledge'/(card['id']+'.json'),card['data']);row['path']='knowledge/'+card['id']+'.json'
        evidence.append(row)
    state={'schema':'vco.task.v2','created':datetime.datetime.now(datetime.timezone.utc).isoformat(),'spec':spec,'spec_warnings':warnings,'evidence':evidence,'candidates':[],'events':[],'stage':'selection','budget':spec.get('budget',{}),'completed':False,'consults':[]}
    write_json(root/'task.json',state)
    write_json(root/'candidate_catalog.json',list_families())
    if not (root/'DECISIONS.md').exists():
        (root/'DECISIONS.md').write_text('# Design decisions\n\nFor every decision write: evidence/knowledge IDs and their conditions -> hypothesis -> this experiment -> keep/reject and why.\nThe catalog is a shortlist source; testing all five families is not required.\n')
    return status(root)


def _candidate(state,candidate_id):
    for row in state['candidates']:
        if row['id']==candidate_id:return row
    known=', '.join(r['id'] for r in state['candidates']) or 'none yet (use flow-add)'
    raise ValueError('Unknown candidate %r; known candidates: %s'%(candidate_id,known))


def _task_constraints(project, spec):
    """Inherit omitted requirements; never silently replace explicit choices."""
    p=copy.deepcopy(project);p.setdefault('parameters',{})
    if isinstance(p.get('registers'),list):p['registers']={r['name']:dict(r) for r in p['registers']}
    p.setdefault('registers',{})
    inherited=[]
    supply=spec.get('vdd',spec.get('vdd_v'))
    if 'vdd' in spec and 'vdd_v' in spec and not math.isclose(float(spec['vdd']),float(spec['vdd_v']),rel_tol=1e-12):
        raise ValueError('Conflicting task vdd and vdd_v')
    if supply is not None:
        supply=float(evaluate_expression(supply))
        if not math.isfinite(supply) or supply<=0:raise ValueError('Task vdd must be finite and positive')
        if 'vdd' in p['parameters'] and not math.isclose(float(evaluate_expression(p['parameters']['vdd'])),supply,rel_tol=1e-12,abs_tol=1e-15):
            raise ValueError('Candidate VDD conflicts with task vdd')
        if 'vdd' not in p['parameters']:inherited.append('vdd')
        p['parameters']['vdd']=supply
    if spec.get('main_bits') is not None:
        bits=spec['main_bits']
        if isinstance(bits,bool) or int(bits)!=float(bits) or not 1<=int(bits)<=12:raise ValueError('Task main_bits must be an integer in 1..12')
        bits=int(bits);reg=p['registers'].get('main',{})
        if isinstance(reg,int):reg={'value':reg}
        if 'bits' in reg and (isinstance(reg['bits'],bool) or float(reg['bits'])!=bits):raise ValueError('Candidate main_bits conflicts with task main_bits')
        if 'bits' not in reg:inherited.append('main_bits')
        reg['bits']=bits
        if 'value' not in reg:
            seed=default_project(p['family'],p.get('variant'))['registers']['main'].get('value',0)
            reg['value']=seed if seed<2**bits else 0
        p['registers']['main']=reg
    temp=spec.get('temp_c')
    if temp is not None:
        current=p['parameters'].get('temp_c')
        if current is None:p['parameters']['temp_c']=temp;inherited.append('temp_c')
        elif not math.isclose(float(current),float(temp)):
            raise ValueError('Candidate temp_c=%s conflicts with task temp_c=%s; create the project with `launch project ... --spec <spec.json>`'%(current,temp))
    spdk=spec.get('pdk');ppdk=p.get('pdk')
    if spdk and ppdk and spdk!=ppdk:raise ValueError('Candidate pdk %s differs from task pdk %s'%(ppdk,spdk))
    if spdk and isinstance(p.get('binding'),str) and p['binding']=='demo_a' and spdk!='demo_a':
        raise ValueError('Candidate uses the reference DEMO_A binding but the task pdk is %s; create the project with --spec or --binding %s'%(spdk,spdk))
    p['spec']=copy.deepcopy(spec)
    return p,inherited


@_locked_mutation()
def add_candidate(workspace,project,reason='',evidence_ids=None):
    root=Path(workspace);state=read_json(root/'task.json')
    if state['completed']:raise ValueError('Frozen task; create a new revision task')
    p,inherited=_task_constraints(project,state['spec'])
    enforcement=state['spec'].get('enforcement','guided')
    if enforcement=='strict':
        if not evidence_ids:raise ValueError('strict enforcement: flow-add needs at least one --evidence id (knowledge card, KB record or evidence point) that supports this candidate')
        from .knowledge import resolve_ids
        missing=[i for i,ok in resolve_ids(evidence_ids).items() if not ok]
        if missing:raise ValueError('strict enforcement: unknown evidence ids: '+', '.join(missing))
    from .generator import generate
    ident,dest=_allocate_directory(root/'candidates','candidate_',3,(row['id'] for row in state['candidates']))
    manifest=generate(p,dest)
    row={'id':ident,'directory':str(dest.resolve()),'graph_hash':manifest['graph_hash'],'family':p['family'],'variant':p.get('variant'),'reason':reason,'evidence_ids':evidence_ids or [],'inherited_task_requirements':inherited,'results':[],'disposition':'exploring'}
    state['candidates'].append(row);state['stage']='characterization';state['events'].append({'action':'add_candidate','id':ident,'time':time.time()});write_json(root/'task.json',state);return row

def _strict_diagnosis_gate(root,state):
    """strict enforcement: after any measurement, the agent must run flow-next (or another consult)
    before launching the next simulation -- diagnosis before action."""
    if state['spec'].get('enforcement')!='strict':return
    times=[e.get('time',0) for e in state.get('events',[]) if e.get('action') in ('measure','code_point','convergence_check')]
    if not times:return
    from .knowledge import consult_log
    consults=[c.get('time',0) for c in consult_log(root)]
    if not consults or max(consults)<max(times):
        raise ValueError('strict enforcement: run `launch flow-next %s` and act on its diagnosis before the next simulation'%root)


@_locked_mutation()
def measure(workspace,candidate_id,timeout_s=180):
    root=Path(workspace);state=read_json(root/'task.json');c=_candidate(state,candidate_id)
    check_budget(state);_strict_diagnosis_gate(root,state)
    from .simulator import run
    result=run(c['directory'],timeout_s=timeout_s);c['results'].append(str(Path(result['path'])/'result.json'));state['events'].append({'action':'measure','id':candidate_id,'result':c['results'][-1],'time':time.time()});write_json(root/'task.json',state)
    recovery=None
    if result.get('status')!='measured':
        recovery=_recover_guess(root,state,c,result,timeout_s)
        if recovery and recovery.get('result'):
            result=recovery.pop('result')
    summary=_row(c['results'][-1])
    summary['violations']=assess_metrics(result['metrics'],state['spec']) if result.get('status')=='measured' else ['simulation_failed']
    out={'summary':summary,'next':'run `launch flow-next %s` for diagnosis, knowledge cards and the next action'%root,
            'result_file':c['results'][-1],'status':result.get('status'),'failure_class':result.get('failure_class'),
            'receipt':{k:result.get('receipt',{}).get(k) for k in ('exit_code','elapsed_s','stop_reason')}}
    if recovery:out['recovery']=recovery
    return out


def _recover_guess(root,state,c,result,timeout_s):
    """HB failed with 'initial frequency is not close to osc frequency': if the DC current is sane, probe the
    free-running frequency with a short transient and re-run HB once with that guess (same physical design:
    frequency_guess_ghz is not part of the physical identity). Both runs count against the budget."""
    from .simulator import is_guess_failure,dc_power_mw,probe_frequency,run
    from .generator import generate
    if not is_guess_failure(result):return None
    project=read_json(Path(c['directory'])/'project.json')
    pdc=dc_power_mw(result['path'],project)
    note={'trigger':'hb_initial_frequency','dc_power_mw':pdc}
    if pdc is not None and pdc<0.3*state['spec']['power_max_mw']:
        note['action']='skipped: DC current far below the power target, most likely no oscillation -- raise the power knob (vdc or core_width_scale)'
        return note
    usage=budget_usage(state);rem=usage.get('solver_processes_remaining')
    if rem is not None and rem<2:
        note['action']='skipped: fewer than 2 solver runs left in the budget';return note
    _,pdir=_allocate_directory(Path(c['directory'])/'probes','probe_',3)
    pdir.rmdir()
    pr=probe_frequency(c['directory'],pdir)
    state['events'].append({'action':'probe','id':c['id'],'result':str(pdir/'probe.json'),'time':time.time()})
    note['probe']={k:pr.get(k) for k in ('status','frequency_hz','amplitude_v','error')}
    if pr.get('status')!='measured':
        note['action']='probe found no sustained oscillation: raise the power knob or check gm margin (vco-card-symptom-no-oscillation)'
        write_json(root/'task.json',state);return note
    p2=dict(project);p2['parameters']=dict(project['parameters'],frequency_guess_ghz=round(pr['frequency_hz']/1e9,6))
    _,dest=_allocate_directory(Path(c['directory'])/'retries','retry_',3)
    generate(p2,dest);r2=run(dest,timeout_s=timeout_s);file=str(Path(r2['path'])/'result.json')
    c['results'].append(file);state['events'].append({'action':'measure','id':c['id'],'result':file,'time':time.time(),'recovery':'probe_guess'})
    write_json(root/'task.json',state)
    note['action']='re-ran HB with the probed frequency guess (%.4f GHz)'%(pr['frequency_hz']/1e9);note['result']=r2
    return note

def budget_usage(state):
    """Count actual solver launches once, excluding aggregate tool receipts."""
    files=set()
    for candidate in state['candidates']:
        files.update(path.resolve() for path in Path(candidate['directory']).rglob('receipt.json'))
        for result in candidate.get('results',[]):
            receipt=Path(result).resolve().parent/'receipt.json'
            if receipt.exists():files.add(receipt)
    runs={}
    unreadable=[]
    for file in sorted(files):
        try:receipt=read_json(file)
        except (OSError,ValueError):unreadable.append(str(file));continue
        if receipt.get('schema')=='vco.run.v1' and receipt.get('pid') is not None:
            ident=('hb',str(Path(receipt['cwd']).resolve()),receipt.get('pid'),receipt.get('start_epoch'))
        elif receipt.get('schema')=='vco.probe.v1':
            ident=('probe',str(Path(receipt['cwd']).resolve()))
        elif all(key in receipt for key in ('run_dir','argv','exit_code','input_sha256','excitation')):
            ident=('ac',str(Path(receipt['run_dir']).resolve()))
        else:continue
        runs[ident]=str(file)
    elapsed=max(0,time.time()-datetime.datetime.fromisoformat(state['created']).timestamp())
    out={'solver_processes':len(runs),'wall_seconds':elapsed,'solver_receipts':list(runs.values()),'scope':'Actual solver receipts under candidate directories or explicitly referenced results; aggregate tool receipts excluded.'}
    limit=state.get('budget',{}).get('solver_processes')
    if limit is not None:out['solver_processes_remaining']=max(0,int(limit)-len(runs))
    if unreadable:out['unreadable_receipts']=unreadable
    return out


def check_budget(state):
    if state.get('completed'):raise ValueError('Frozen task; create a new revision task')
    budget=state.get('budget',{});usage=budget_usage(state);limit=budget.get('solver_processes')
    if limit is not None and usage['solver_processes']>=limit:raise ValueError('Task solver budget exhausted; retain current evidence and submit gaps')
    seconds=budget.get('wall_seconds')
    if seconds is not None and usage['wall_seconds']>=seconds:raise ValueError('Task wall-time budget exhausted')
    return usage

def _validate_code_map(code_map,registers):
    if not isinstance(code_map,list) or not code_map:raise ValueError('codes must be a non-empty JSON list like [{"main":0},{"main":15}]')
    for i,codes in enumerate(code_map):
        if not isinstance(codes,dict) or not codes:raise ValueError('codes[%d] must be an object like {"main":3}'%i)
        for reg,value in codes.items():
            if reg not in registers:raise ValueError('codes[%d]: unknown register %r; this candidate has: %s'%(i,reg,', '.join(registers)))
            bits=int(registers[reg]['bits'])
            if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<2**bits:
                raise ValueError('codes[%d]: %s=%r outside 0..%d'%(i,reg,value,2**bits-1))


def _row(result_file):
    try:r=read_json(result_file)
    except (OSError,ValueError):return {'result':result_file,'status':'unreadable'}
    m=r.get('metrics',{})
    row={'result':result_file,'status':r.get('status'),'controls':{k:v.get('value') for k,v in (r.get('registers') or {}).items()}}
    if r.get('status')=='measured':
        row.update(f_ghz=round(m['frequency_hz']/1e9,6),p_mw=round(m['power_mw'],4),pn_1m=round(m.get('pn_1000000_dbc_hz',float('nan')),2),fom_1m=round(m.get('fom_1000000_db',float('nan')),2),amp_v=round(m.get('amplitude_diff_peak_v',0),3))
        if r.get('warnings'):row['warnings']=r['warnings']
    else:
        row.update(error=r.get('error'),failure_class=r.get('failure_class'))
    return row


@_locked_mutation()
def sweep(workspace,candidate_id,code_map,timeout_s=180):
    root=Path(workspace);state=read_json(root/'task.json');c=_candidate(state,candidate_id)
    from .generator import generate
    from .simulator import run
    base=read_json(Path(c['directory'])/'project.json');results=[];rows=[]
    _validate_code_map(code_map,base['registers'])
    usage=check_budget(state);_strict_diagnosis_gate(root,state)
    remaining=usage.get('solver_processes_remaining')
    if remaining is not None and remaining<len(code_map):
        raise ValueError('Sweep needs %d solver runs but only %d remain in the task budget; submit fewer codes'%(len(code_map),remaining))
    for codes in code_map:
        check_budget(state);p=copy.deepcopy(base)
        for reg,value in codes.items():p['registers'][reg]['value']=int(value)
        _,dest=_allocate_directory(Path(c['directory'])/'code_grid','point_',4)
        generate(p,dest);r=run(dest,timeout_s=timeout_s);file=str(Path(r['path'])/'result.json');c['results'].append(file);results.append(file)
        rows.append(_row(file))
        state['events'].append({'action':'code_point','id':candidate_id,'codes':codes,'result':file,'time':time.time()});write_json(root/'task.json',state)
    measured=[r for r in rows if r.get('status')=='measured']
    summary={}
    if measured:
        fs=[r['f_ghz'] for r in measured];ps=[r['p_mw'] for r in measured];pn=[r['pn_1m'] for r in measured]
        center=(min(fs)+max(fs))/2
        summary={'f_min_ghz':min(fs),'f_max_ghz':max(fs),'center_ghz':center,'tr_pct':(max(fs)-min(fs))/center*100,'p_max_mw':max(ps),'pn_1m_worst':max(pn)}
        lo,hi=state['spec']['frequency_band_hz']
        if state['spec'].get('band_mode')=='center_tr':
            summary['center_ok']=abs(center*1e9/state['spec']['center_hz']-1)<=state['spec'].get('center_tolerance',0.02)
            summary['tr_ok']=summary['tr_pct']>=state['spec']['tr_min_pct']
            summary['covers_band']=summary['center_ok'] and summary['tr_ok']
        else:
            summary['covers_band']=min(fs)*1e9<=lo and max(fs)*1e9>=hi
        summary['power_ok']=max(ps)<=state['spec']['power_max_mw']
    return {'candidate':candidate_id,'results':results,'table':rows,'summary':summary,'all_measured':len(measured)==len(rows),
            'qualification':'measured submitted code map; use flow-converge then flow-freeze for qualification'}


@_locked_mutation()
def converge(workspace,candidate_id,result_files,harmonics=21,tstab_ns=120,timeout_s=300):
    """Re-simulate the same physical design and controls with different HB numerics (independent convergence)."""
    root=Path(workspace);state=read_json(root/'task.json');c=_candidate(state,candidate_id)
    if not isinstance(result_files,list) or not result_files:raise ValueError('results must be a non-empty JSON list of result.json paths')
    from .generator import generate
    from .simulator import run
    remaining=check_budget(state).get('solver_processes_remaining');_strict_diagnosis_gate(root,state)
    if remaining is not None and remaining<len(result_files):raise ValueError('Convergence needs %d runs but only %d remain'%(len(result_files),remaining))
    out=[];rows=[]
    for file in result_files:
        r=read_json(file)
        if r.get('status')!='measured':raise ValueError('Only measured results can be re-checked: '+str(file))
        generated=Path(r['receipt']['generated_dir'])
        p=read_json(generated/'project.json')
        sim=dict(p.get('simulation',{}));
        if int(sim.get('harmonics',15))==int(harmonics) and float(sim.get('tstab_ns',60))==float(tstab_ns):
            raise ValueError('Convergence settings must differ from the original (harmonics/tstab); original uses harmonics=%s tstab_ns=%s'%(sim.get('harmonics',15),sim.get('tstab_ns',60)))
        sim['harmonics']=int(harmonics);sim['tstab_ns']=float(tstab_ns);p['simulation']=sim
        _,dest=_allocate_directory(Path(c['directory'])/'convergence','check_',4)
        generate(p,dest);rr=run(dest,timeout_s=timeout_s);new=str(Path(rr['path'])/'result.json')
        c['results'].append(new);out.append({'original':str(file),'check':new})
        row=_row(new);row['original']=str(file)
        if rr.get('status')=='measured':
            m0=r['metrics'];m1=rr['metrics']
            row['delta']={'frequency_rel':abs(m1['frequency_hz']-m0['frequency_hz'])/m0['frequency_hz'],'power_rel':abs(m1['power_mw']-m0['power_mw'])/m0['power_mw'],
                          'pn_abs_db':max(abs(m1['pn_%d_dbc_hz'%o]-m0['pn_%d_dbc_hz'%o]) for o in (10000,100000,1000000,10000000))}
        rows.append(row)
        state['events'].append({'action':'convergence_check','id':candidate_id,'original':str(file),'result':new,'time':time.time()});write_json(root/'task.json',state)
    return {'candidate':candidate_id,'pairs':out,'table':rows,'all_measured':all(r.get('status')=='measured' for r in rows),
            'note':'Freeze compares these with the adopted results; adopted results file = originals, convergence file = checks. Note: the convergence design directory differs only in HB settings; physical identity is unchanged.'}


@_locked_mutation()
def mark(workspace,candidate_id,disposition,reason):
    root=Path(workspace);state=read_json(root/'task.json');c=_candidate(state,candidate_id)
    if state.get('completed'):raise ValueError('Frozen task; create a new revision task')
    if not reason or not reason.strip():raise ValueError('A reason is required')
    c['disposition']=disposition;c.setdefault('disposition_history',[]).append({'disposition':disposition,'reason':reason,'time':time.time()})
    state['events'].append({'action':'mark','id':candidate_id,'disposition':disposition,'reason':reason,'time':time.time()});write_json(root/'task.json',state)
    return {'candidate':candidate_id,'disposition':disposition}


def assess_metrics(metrics,spec):
    violations=[]
    for key in ('frequency_hz','power_mw'):
        value=metrics.get(key)
        if not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:violations.append('invalid_'+key)
    if 'invalid_power_mw' not in violations and metrics['power_mw']>spec['power_max_mw']:violations.append('power')
    for ofs,limit in spec.get('pn_targets_dbc_hz',{}).items():
        key='pn_'+str(int(ofs))+'_dbc_hz';value=metrics.get(key)
        if not isinstance(value,(int,float)) or not math.isfinite(value):violations.append('missing_or_invalid_'+key)
        elif value>limit:violations.append(key)
    return violations

def status(workspace):
    root=Path(workspace);state=read_json(root/'task.json');report=[]
    for c in state['candidates']:
        results=[]
        try:manifest=read_json(Path(c['directory'])/'manifest.json')
        except (OSError,ValueError):manifest={}
        for file in c['results']:
            try:r=read_json(file)
            except (OSError,ValueError):results.append({'status':'missing_evidence','file':file});continue
            if r.get('physical_identity')!=manifest.get('physical_identity'):results.append({'status':'wrong_candidate','file':file});continue
            violations=assess_metrics(r['metrics'],state['spec']) if r.get('status')=='measured' else ['simulation_failed']
            item={'status':r['status'],'violations':violations,'file':file}
            if any(v.startswith('pn_') or v.startswith('missing_or_invalid_pn_') for v in violations):
                from .guidance import noise_actions
                item['mechanism_actions']=noise_actions(read_json(Path(c['directory'])/'project.json'),r.get('metrics',{}),state['spec'],root)
            results.append(item)
        report.append({'id':c['id'],'family':c['family'],'variant':c['variant'],'results':results,'disposition':c['disposition']})
    if not report:next_actions=['Use retrieved architecture mechanisms and specs to choose a small shortlist; save reasoning in DECISIONS.md.','Create a project via catalog and add candidate; original PDK seed is not a new-spec solution.']
    elif any(any(r.get('violations') for r in c['results']) for c in report):next_actions=['Classify failing sample: domain/startup/loss/coverage/noise before repeated sweeps.','Use bank-ac/Cap Lab for C/G/Q and interval feasibility; external circuit optimizer for bounded parameter search; retrieve mechanism evidence for a new noise/return bottleneck.','Retain alternative mechanism or evidence-based exclusion; reserve final adopted-domain verification budget.']
    else:next_actions=['Obtain actual bank/core/return characterization as relevant; representative samples do not establish full band.','Calibrate realizable main/aux codes, then measure the entire adopted domain and key independent convergence points.']
    return {'workspace':str(root.resolve()),'stage':state['stage'],'completed':state['completed'],'spec':state['spec'],'knowledge':state['evidence'],'budget_usage':budget_usage(state),'candidates':report,'next_actions':next_actions,'qualification':'workflow status, not automatic architecture decision'}

CONVERGENCE_DEFAULTS={'frequency_rel':0.001,'power_rel':0.01,'pn_abs_db':0.5}


def _code_identity(registers):
    if not isinstance(registers,dict) or not registers:raise ValueError('Missing explicit register identity')
    normalized={}
    for name,config in registers.items():
        bits,value=int(config['bits']),int(config['value'])
        if bits!=config['bits'] or value!=config['value'] or bits<1 or not 0<=value<2**bits:raise ValueError('Invalid register identity')
        normalized[name]={'bits':bits,'value':value}
    return json.dumps(normalized,sort_keys=True,separators=(',',':'))


def _solver_settings(tb):
    """Only actual numerical HB/PN settings count, not comments or code changes."""
    settings={}
    keys='reltol|vabstol|iabstol|tstab|maxharms|maxsideband|errpreset|oscmethod|autosteady|fundfreqs'
    for line in tb.splitlines():
        line=line.split('//',1)[0].strip()
        if not line or not re.match(r'^(?:hb|hbnoise|opts)\s',line):continue
        analysis=line.split()[0]
        for key,value in re.findall(r'\b('+keys+r')\s*=\s*(\[[^\]]*\]|[^\s]+)',line):
            text=value.strip('[]')
            try:normalized=float(evaluate_expression(text))
            except (ValueError,TypeError,SyntaxError):normalized=text
            settings[analysis+'.'+key]=normalized
    if not settings:raise ValueError('No auditable HB/PN numerical settings in testbench')
    return settings


def _verify_result(file,physical,circuit_hash,hash_cache=None):
    """Verify existing receipts/raw hashes and re-read metrics; never run a solver."""
    def checked_hash(path):
        path=Path(path).resolve();st=path.stat();key=(str(path),st.st_mtime_ns,st.st_size)
        if hash_cache is None:return sha256(path)
        if key not in hash_cache:hash_cache[key]=sha256(path)
        return hash_cache[key]
    file=Path(file).resolve();r=read_json(file)
    if r.get('status')!='measured':raise ValueError('Unmeasured result '+str(file))
    if r.get('physical_identity')!=physical:raise ValueError('Different geometry, binding, supply, temperature or register width')
    run=Path(r['path']).resolve()
    if file.parent!=run:raise ValueError('Result is not in its recorded run directory')
    receipt=read_json(run/'receipt.json')
    if receipt!=r.get('receipt'):raise ValueError('Result receipt differs from original run receipt')
    if receipt.get('schema')!='vco.run.v1' or receipt.get('exit_code')!=0 or not receipt.get('stopped') or receipt.get('stop_reason'):
        raise ValueError('Missing successful completed solver receipt')
    if Path(receipt['cwd']).resolve()!=run:raise ValueError('Run directory differs from receipt')
    generated=Path(receipt['generated_dir']);manifest=read_json(generated/'manifest.json');project=read_json(generated/'project.json')
    if manifest.get('physical_identity')!=physical or r.get('graph_hash')!=manifest['graph_hash'] or receipt.get('graph_hash')!=manifest['graph_hash']:
        raise ValueError('Result does not match generated input identity')
    if _code_identity(r.get('registers'))!=_code_identity(project['registers']):raise ValueError('Result register code differs from generated project')
    for name,expected in manifest.get('files',{}).items():
        if checked_hash(generated/name)!=expected:raise ValueError('Generated file hash changed: '+name)
    for name,expected in manifest.get('model_files',{}).items():
        if checked_hash(name)!=expected:raise ValueError('Model evidence hash changed: '+name)
    inputs=receipt.get('input_sha256',{})
    if not {'circuit.scs','tb.scs','models.scs'}.issubset(inputs):raise ValueError('Incomplete solver input hash receipt')
    for name,expected in inputs.items():
        if checked_hash(run/name)!=expected or (name in manifest['files'] and expected!=manifest['files'][name]):raise ValueError('Run input hash differs: '+name)
    if inputs['circuit.scs']!=circuit_hash:raise ValueError('Different circuit in result grid')
    evidence=r.get('evidence',{})
    required={'circuit.scs','tb.scs','spectre.log','psf/hb.fd.pss_hb','psf/hbnoise.pm.pnoise_hbnoise'}
    if not required.issubset(evidence):raise ValueError('Missing raw HB/PN evidence hashes')
    for name,expected in evidence.items():
        path=(run/name).resolve()
        if not path.is_relative_to(run) or checked_hash(path)!=expected:raise ValueError('Raw evidence hash changed: '+name)
    from .simulator import parse_result
    measured=parse_result(run,project)
    for key,value in measured['metrics'].items():
        recorded=r.get('metrics',{}).get(key)
        if not isinstance(recorded,(int,float)) or not math.isfinite(recorded) or not math.isclose(recorded,value,rel_tol=1e-10,abs_tol=1e-10):
            raise ValueError('Recorded metric does not match raw evidence: '+key)
    return {'result':r,'file':str(file),'result_sha256':sha256(file),'receipt_sha256':sha256(run/'receipt.json'),
            'code':_code_identity(r['registers']),'settings':_solver_settings((run/'tb.scs').read_text()),
            'input_sha256':inputs,'raw_evidence_sha256':evidence}


def _compare_convergence(baseline,independent,tolerances,spec):
    if baseline['code']!=independent['code']:raise ValueError('Convergence point has no matching adopted control state')
    if baseline['file']==independent['file']:raise ValueError('Convergence evidence reuses the adopted run')
    if baseline['settings']==independent['settings']:raise ValueError('Convergence requires different actual numerical solver settings')
    a,b=baseline['result']['metrics'],independent['result']['metrics']
    errors={'frequency_rel':abs(b['frequency_hz']-a['frequency_hz'])/abs(a['frequency_hz']),
            'power_rel':abs(b['power_mw']-a['power_mw'])/abs(a['power_mw'])}
    pn={key for key in a if key.startswith('pn_') and key.endswith('_dbc_hz')} | {'pn_'+str(int(ofs))+'_dbc_hz' for ofs in spec.get('pn_targets_dbc_hz',{})}
    if not pn:raise ValueError('No PN metric for convergence comparison')
    differences={}
    for key in sorted(pn):
        if key not in a or key not in b or not all(math.isfinite(x) for x in (a[key],b[key])):raise ValueError('Missing convergence PN '+key)
        differences[key]=abs(b[key]-a[key])
    errors['pn_abs_db']=max(differences.values())
    failed=[key for key,value in errors.items() if value>tolerances[key]]
    if failed:raise ValueError('Independent convergence tolerance exceeded: '+', '.join(failed))
    return {'code':json.loads(baseline['code']),'adopted_result':baseline['file'],'independent_result':independent['file'],
            'baseline_settings':baseline['settings'],'independent_settings':independent['settings'],'errors':errors,'pn_errors_db':differences}


@_locked_mutation()
def freeze(workspace,candidate_id,adopted_results,convergence_results=None):
    root=Path(workspace);state=read_json(root/'task.json')
    if state.get('completed'):raise ValueError('Task already frozen; create a new revision')
    c=next(x for x in state['candidates'] if x['id']==candidate_id);spec=state['spec']
    candidate=Path(c['directory']);manifest=read_json(candidate/'manifest.json');physical=manifest.get('physical_identity')
    if not physical:raise ValueError('Candidate predates physical condition identity; regenerate before qualification')
    for name,expected in manifest.get('files',{}).items():
        if sha256(candidate/name)!=expected:raise ValueError('Candidate file changed after generation: '+name)
    _task_constraints(read_json(candidate/'project.json'),spec)
    circuit_hash=sha256(candidate/'circuit.scs')
    adopted=[_verify_result(file,physical,circuit_hash) for file in adopted_results]
    if len(adopted)<2:raise ValueError('Submitted frequency coverage needs more than one point')
    by_code={row['code']:row for row in adopted}
    if len(by_code)!=len(adopted):raise ValueError('Duplicate adopted control state')
    for row in adopted:
        if assess_metrics(row['result']['metrics'],spec):raise ValueError('Adopted point fails PN/power or finite metric checks: '+row['file'])
    from .magnetics import check_q
    graph=read_json(candidate/'graph.json');qchecks=[]
    for row in adopted:
        qc=check_q(graph,row['result']['metrics']['frequency_hz']);qchecks.append({'file':row['file'],**{k:qc[k] for k in ('q_at_f0','limit','k_max','ok')}})
        if not qc['ok']:raise ValueError('Magnetic mode Q %.2f at f0 exceeds the limit %.2f (30*(1+|k|)): %s'%(qc['q_at_f0'],qc['limit'],row['file']))
    fs=sorted(row['result']['metrics']['frequency_hz'] for row in adopted);low,high=spec['frequency_band_hz']
    if spec.get('band_mode')=='center_tr':
        center=(fs[0]+fs[-1])/2;tr=(fs[-1]-fs[0])/center*100;tol=spec.get('center_tolerance',0.02)
        if abs(center/spec['center_hz']-1)>tol:raise ValueError('Measured band center %.6g Hz is outside %.6g Hz +- %.1f %%'%(center,spec['center_hz'],tol*100))
        if tr<spec['tr_min_pct']:raise ValueError('Measured TR %.2f %% < tr_min_pct %.2f %%'%(tr,spec['tr_min_pct']))
    elif fs[0]>low or fs[-1]<high:raise ValueError('Measured endpoints do not cover requested band [%.6g, %.6g] Hz: measured [%.6g, %.6g]; if the requirement is center +- tolerance and TR >= x %%, write the spec with center_hz + tr_min_pct'%(low,high,fs[0],fs[-1]))
    gap=max(b-a for a,b in zip(fs,fs[1:]));maxgap=spec.get('max_step_hz')
    if maxgap is not None and gap>maxgap:raise ValueError('Measured adopted frequency gap exceeds limit')
    if not convergence_results:raise ValueError('Independent convergence evidence not supplied; retain provisional status')
    tolerances=dict(CONVERGENCE_DEFAULTS);overrides=spec.get('convergence_tolerances',{})
    if set(overrides)-set(tolerances):raise ValueError('Unknown convergence tolerance fields')
    tolerances.update(overrides)
    if any(not isinstance(value,(int,float)) or not math.isfinite(value) or value<0 for value in tolerances.values()):raise ValueError('Convergence tolerances must be finite and nonnegative')
    independent=[_verify_result(file,physical,circuit_hash) for file in convergence_results]
    if len({row['file'] for row in independent})!=len(independent):raise ValueError('Duplicate convergence evidence')
    comparisons=[]
    for row in independent:
        if row['code'] not in by_code:raise ValueError('Convergence point has no matching adopted control state')
        if assess_metrics(row['result']['metrics'],spec):raise ValueError('Independent convergence point fails target constraints')
        comparisons.append(_compare_convergence(by_code[row['code']],row,tolerances,spec))
    budget_state=copy.deepcopy(state)
    budget_candidate=next(row for row in budget_state['candidates'] if row['id']==candidate_id)
    budget_candidate['results']=sorted(set(budget_candidate.get('results',[])) | {row['file'] for row in adopted+independent})
    frozen={'schema':'vco.freeze.v1','candidate':candidate_id,'physical_identity':physical,
            'adopted_results':[r['file'] for r in adopted],'convergence_results':[r['file'] for r in independent],
            'adopted_control_states':[json.loads(row['code']) for row in adopted],
            'frequency_range_hz':[fs[0],fs[-1]],'max_step_hz':gap,'magnetic_q_checks':qchecks,'convergence_tolerances':tolerances,'convergence_comparisons':comparisons,
            'verified_evidence':[{key:row[key] for key in ('file','result_sha256','receipt_sha256','input_sha256','raw_evidence_sha256')} for row in adopted+independent],
            'budget_usage':budget_usage(budget_state),'scope':'Only the submitted adopted control-state grid at the recorded nominal conditions; independent convergence is established only for the matched listed states.',
            'not_qualified':['unsubmitted codes','PVT coverage','tank or bank Q','startup','reliability','all-code convergence']}
    write_json(root/'freeze.json',frozen);state['completed']=True;state['stage']='frozen';state['events'].append({'action':'freeze','candidate':candidate_id,'time':time.time(),'scope':frozen['scope']});write_json(root/'task.json',state);return frozen


def next_action(workspace):
    from .advisor import next_action as decide
    root=Path(workspace);state=read_json(root/'task.json')
    return decide(root,state)


def brief_status(report):
    """Compact view: one line per result (agents read this instead of the full JSON)."""
    rows=[]
    for c in report['candidates']:
        for r in c['results']:
            line={'candidate':c['id'],'family':c['family'],'variant':c['variant'],'disposition':c['disposition'],'status':r.get('status'),'violations':r.get('violations'),'file':r.get('file')}
            try:
                data=read_json(r['file']);m=data.get('metrics',{})
                if m:line.update(f_ghz=round(m['frequency_hz']/1e9,4),p_mw=round(m['power_mw'],3),pn_1m=round(m.get('pn_1000000_dbc_hz',0),2),fom_1m=round(m.get('fom_1000000_db',0),2),controls={k:v.get('value') for k,v in data.get('registers',{}).items()})
            except (OSError,ValueError,KeyError):pass
            rows.append(line)
    return {'stage':report['stage'],'completed':report['completed'],'budget':{k:report['budget_usage'].get(k) for k in ('solver_processes','solver_processes_remaining')},'results':rows,'next':'run flow-next for the required next step'}
