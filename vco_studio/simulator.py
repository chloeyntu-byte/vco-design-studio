"""Bounded private Spectre execution and independent PSF measurement."""
from pathlib import Path
import datetime,json,math,os,re,shutil,time
from .common import read_json,write_json,sha256

def psf_values(path,axis='freq'):
    body=Path(path).read_text(errors='strict').split('\nVALUE\n',1)[1];pts=[];current=None
    for line in body.splitlines():
        z=re.fullmatch(r'"([^"]+)"\s+(.*)',line)
        if not z:continue
        key,val=z.groups()
        if key==axis:current={axis:float(val)};pts.append(current);continue
        if current is None or val=='(':continue
        if val.startswith('('):
            vals=val.strip('()').split()
            if len(vals)==2:current[key]=complex(*map(float,vals))
        else:
            try:current[key]=float(val)
            except ValueError:pass
    return pts

def parse_result(run_dir,project):
    run=Path(run_dir);fd=psf_values(run/'psf/hb.fd.pss_hb');dc=min(fd,key=lambda x:abs(x['freq']));fund=min((x for x in fd if x['freq']>0),key=lambda x:x['freq']);f=fund['freq'];amp=abs(fund['OUTP']-fund['OUTN'])
    p=project['parameters'];sources={'VDD_SRC:p':float(p['vdd']),'VBIAS_SRC:p':float(p['vdc'])}
    for reg,c in project['registers'].items():
        for b in range(c['bits']):sources['V_'+reg+'_'+str(b)+':p']=float(p['vdd'])*((c['value']>>b)&1)
    missing=set(sources)-set(dc)
    if missing:raise ValueError('Missing actual supply current '+str(missing))
    powers={k:-voltage*dc[k].real*1000 for k,voltage in sources.items()};power=sum(powers.values())
    if amp<.05 or power<=0:raise ValueError('No qualified nonzero oscillation or positive total power')
    noise=psf_values(run/'psf/hbnoise.pm.pnoise_hbnoise','relative frequency');metrics={'frequency_hz':f,'power_mw':power,'amplitude_diff_peak_v':amp}
    for ofs in [10000,100000,1000000,10000000]:
        n=min(noise,key=lambda x:abs(x['relative frequency']-ofs))
        if abs(n['relative frequency']-ofs)/ofs>1e-5:raise ValueError('Missing required PN offset '+str(ofs))
        pn=20*math.log10(math.sqrt(2)*n['out']/amp);metrics['pn_'+str(ofs)+'_dbc_hz']=pn;metrics['fom_'+str(ofs)+'_db']=-pn+20*math.log10(f/ofs)-10*math.log10(power)
    log=(run/'spectre.log').read_text(errors='replace');warnings=[]
    if re.search(r'CMI-\d+',log):warnings.append('Model warning: inspect iterative recovery and final device stress')
    if 'SPCRTRF-15310' in log:warnings.append('Transient steady state not detected; HB alone does not prove startup')
    out={'status':'measured','qualification':'periodic_sample_not_full_band','metrics':metrics,'supply_power_mw':powers,'registers':project['registers'],'warnings':warnings}
    try:
        from .noise import contributors
        c=contributors(run/'psf/hbnoise.pm.pnoise_hbnoise',1e6,top=6)
        out['noise_contributors_1m']={'classes':c['classes'],'groups':c['groups']}
    except Exception as exc:  # diagnostic only; never fails a measurement
        out['noise_contributors_1m']={'error':str(exc)[:200]}
    return out

def run(generated_dir,output_dir=None,timeout_s=180,solver=None,env_script=None,cancel_event=None,log=None):
    """Run one bounded HB/PN simulation of a generated design and parse it independently.

    Returns the result dict (status measured|failed); never raises for solver failure,
    raises SolverInterrupted (after killing the solver group) if the caller is interrupted.
    """
    from . import site
    from .runner import run_bounded, wrap_env, SolverInterrupted
    if timeout_s is None or not float(timeout_s) > 0:raise ValueError('timeout must be a positive number of seconds')
    generated=Path(generated_dir).resolve();manifest=read_json(generated/'manifest.json');project=read_json(generated/'project.json')
    for fn,sha in manifest['files'].items():
        if sha256(generated/fn)!=sha:raise ValueError('Generated input changed after manifest: '+fn)
    for fn,sha in manifest.get('model_files',{}).items():
        if sha256(fn)!=sha:raise ValueError('PDK model changed after generation: '+fn)
    solver=solver or project.get('tools',{}).get('spectre') or site.spectre()
    env_script=env_script or project.get('tools',{}).get('native_env') or site.cadence_env()
    if not solver or not Path(solver).is_file():raise FileNotFoundError('Spectre not found; set "spectre" in config/site.json or $VCO_SPECTRE')
    if output_dir is None:output_dir=generated/('simulation-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    dest=Path(output_dir).resolve()
    if dest.exists() and any(dest.iterdir()):raise FileExistsError('Run output already exists: '+str(dest))
    dest.mkdir(parents=True,exist_ok=True)
    for fn in ['circuit.scs','tb.scs','models.scs']:shutil.copy2(generated/fn,dest/fn)
    argv=[str(solver),'-64','tb.scs','+mt=1','-format','psfascii','-raw','psf','+log','spectre.log']
    command=wrap_env(argv,env_script,'studio-env')
    receipt={'schema':'vco.run.v1','generated_dir':str(generated),'graph_hash':manifest['graph_hash'],'argv':argv,'cwd':str(dest),'start_epoch':time.time(),'timeout_s':float(timeout_s),'threads':1,
             'env_script':str(env_script) if env_script and Path(env_script).is_file() else None,
             'input_sha256':{f:sha256(dest/f) for f in ['circuit.scs','tb.scs','models.scs']}}
    if log:log('Spectre started in '+str(dest))
    def started(pid):
        receipt['pid']=pid;write_json(dest/'receipt.json',receipt)
    try:
        outcome=run_bounded(command,dest,timeout_s,dest/'stdout.log',cancel_event=cancel_event,on_start=started)
    except SolverInterrupted:
        receipt.update(exit_code=None,elapsed_s=None,stop_reason='interrupted',stopped=True);write_json(dest/'receipt.json',receipt)
        write_json(dest/'result.json',{'status':'failed','error':'interrupted by caller; solver process group terminated','failure_class':'interrupted','receipt':receipt,'path':str(dest)})
        raise
    receipt.update(exit_code=outcome['exit_code'],elapsed_s=outcome['elapsed_s'],stop_reason=outcome['stop_reason'],stopped=True);write_json(dest/'receipt.json',receipt)
    result={'status':'failed','receipt':receipt,'path':str(dest)}
    try:
        if outcome['stop_reason']=='timeout':
            result['failure_class']='timeout';raise ValueError('Solver timeout after %.0f s (raise --timeout or simplify the analysis)'%float(timeout_s))
        if outcome['stop_reason']=='cancelled':
            result['failure_class']='cancelled';raise ValueError('Solver cancelled')
        if outcome['exit_code']:
            kind=no_startup(dest/'spectre.log')
            if kind=='no_oscillation':
                result['failure_class']='no_oscillation'
                raise ValueError('No oscillation: linear IC found no growing mode and tstab saw no onset (startup gain too low); see %s'%(dest/'spectre.log'))
            if kind=='no_growing_mode':
                result['failure_class']='no_growing_mode'
                raise ValueError('No growing mode at the DC point (linear IC) and HB failed: likely no oscillation (startup gain too low); see %s'%(dest/'spectre.log'))
            result['failure_class']='solver_error';raise ValueError('Solver exited %s; see %s'%(outcome['exit_code'],dest/'spectre.log'))
        result.update(parse_result(dest,project))
    except (ValueError,FileNotFoundError,KeyError,IndexError) as exc:
        result['error']=str(exc)
        if 'failure_class' not in result:
            result['failure_class']='no_oscillation' if 'oscillation' in str(exc) else 'parse_error'
    result['graph_hash']=manifest['graph_hash'];result['physical_identity']=manifest.get('physical_identity');result['conditions']={'vdd':project['parameters']['vdd'],'vdc':project['parameters']['vdc'],'temp_c':project['parameters']['temp_c']};result['evidence']={str(f.relative_to(dest)):sha256(f) for f in [dest/'tb.scs',dest/'circuit.scs',dest/'spectre.log',dest/'psf/hb.fd.pss_hb',dest/'psf/hbnoise.pm.pnoise_hbnoise'] if f.exists()}
    write_json(dest/'result.json',result)
    shutil.rmtree(dest/'tb.ahdlSimDB',ignore_errors=True)   # per-run AHDL compile cache: regenerable, not evidence
    if log:log(result['status']+': '+str(dest/'result.json'))
    return result

def evaluate_candidate(candidate_project,candidate_dir,cancel_event=None,log=None):
    result=run(candidate_dir,timeout_s=candidate_project.get('simulation',{}).get('timeout_s',180),cancel_event=cancel_event,log=log)
    if result['status']!='measured':return {'status':'failed','merit':1e6,'objectives':[1e6,1e6],'total_violation':1e6,'evidence':result,'feasible':False}
    m=result['metrics'];objectives=[-m['fom_100000_db'],-m['fom_1000000_db']];spec=candidate_project.get('spec',{})
    violations=[]
    if spec.get('power_max_mw') is not None and m['power_mw']>spec['power_max_mw']:violations.append('power')
    if spec.get('frequency_band_hz') and not spec['frequency_band_hz'][0]<=m['frequency_hz']<=spec['frequency_band_hz'][1]:violations.append('frequency')
    for ofs,limit in spec.get('pn_targets_dbc_hz',{}).items():
        if m.get('pn_'+str(int(ofs))+'_dbc_hz',float('inf'))>limit:violations.append('pn_'+str(ofs))
    return {'status':'completed','merit':sum(objectives)/2+1000*len(violations),'objectives':objectives,'total_violation':float(len(violations)),'evidence':result,'feasible':not violations,'violations':violations,'scope':'single current main/aux sample; not full-band optimization'}


def _probe_tb(tb_text,stop_ns,maxstep_ps):
    """Testbench for a short startup transient: same circuit/sources/ic, HB and HB-noise removed."""
    keep=[]
    for line in tb_text.splitlines():
        head=line.split()[0] if line.split() else ''
        if head in ('hb','hbnoise','dcOpInfo') or line.startswith('save '):continue
        keep.append(line)
    keep.append('probe tran stop=%gn maxstep=%gp errpreset=liberal annotate=no'%(stop_ns,maxstep_ps))
    keep.append('save OUTP OUTN')
    return '\n'.join(keep)+'\n'


def probe_frequency(generated_dir,output_dir,stop_ns=30,timeout_s=120,guess_hz=None,solver=None,env_script=None):
    """Bounded short transient: estimate the free-running frequency from zero crossings of OUTP-OUTN
    in the last 40 % of the window. Returns {'frequency_hz','amplitude_v','status',...}."""
    from . import site
    from .runner import run_bounded,wrap_env
    generated=Path(generated_dir).resolve();project=read_json(generated/'project.json')
    guess=guess_hz or float(project['parameters'].get('frequency_guess_ghz',10))*1e9
    dest=Path(output_dir).resolve();dest.mkdir(parents=True,exist_ok=False)
    for fn in ['circuit.scs','models.scs']:shutil.copy2(generated/fn,dest/fn)
    maxstep_ps=max(0.2,1e12/(guess*25))
    (dest/'tb.scs').write_text(_probe_tb((generated/'tb.scs').read_text(),stop_ns,maxstep_ps))
    solver=solver or site.spectre();env_script=env_script or site.cadence_env()
    argv=[str(solver),'-64','tb.scs','+mt=1','-format','psfascii','-raw','psf','+log','spectre.log']
    out=run_bounded(wrap_env(argv,env_script,'studio-probe'),dest,timeout_s,dest/'stdout.log')
    receipt={'schema':'vco.probe.v1','generated_dir':str(generated),'argv':argv,'cwd':str(dest),'stop_ns':stop_ns,'maxstep_ps':maxstep_ps,**out}
    write_json(dest/'receipt.json',receipt)
    shutil.rmtree(dest/'tb.ahdlSimDB',ignore_errors=True)
    res={'status':'failed','receipt':receipt}
    try:
        if out['stop_reason'] or out['exit_code']:raise ValueError('probe transient failed (%s)'%(out['stop_reason'] or 'exit %s'%out['exit_code']))
        pts=psf_values(dest/'psf/probe.tran.tran','time')
        t=[p['time'] for p in pts if 'OUTP' in p and 'OUTN' in p];v=[p['OUTP']-p['OUTN'] for p in pts if 'OUTP' in p and 'OUTN' in p]
        if len(t)<50:raise ValueError('too few transient points')
        start=t[0]+0.6*(t[-1]-t[0]);idx=[i for i in range(len(t)) if t[i]>=start]
        seg=[v[i] for i in idx];mean=sum(seg)/len(seg)
        cross=[]
        for a,b in zip(idx,idx[1:]):
            va,vb=v[a]-mean,v[b]-mean
            if va<0<=vb:cross.append(t[a]+(t[b]-t[a])*(-va)/(vb-va))
        amp=(max(seg)-min(seg))/2
        if len(cross)<3 or amp<0.02:raise ValueError('no sustained oscillation in the probe window (amp %.3g V)'%amp)
        period=(cross[-1]-cross[0])/(len(cross)-1)
        res.update(status='measured',frequency_hz=1/period,amplitude_v=amp,cycles=len(cross)-1)
    except (ValueError,FileNotFoundError,KeyError,IndexError,ZeroDivisionError) as exc:
        res['error']=str(exc)
    write_json(dest/'probe.json',res)
    return res


def no_startup(log_path):
    """'no_oscillation' when the HB log shows no linear-IC frequency and no onset in tstab; 'no_growing_mode' when
    only the linear IC found no growing mode (a self-biased CMOS multi-core can still start from the IC kick, so
    this is weaker evidence); None otherwise."""
    try:
        log=Path(log_path).read_text(errors='replace')
    except OSError:
        return None
    lin='Linear IC: Fail to find out initial frequency' in log
    if lin and 'onset of oscillations has not been detected' in log:
        return 'no_oscillation'
    return 'no_growing_mode' if lin else None


def is_guess_failure(result):
    err=(result.get('error') or '')+' '
    if 'Solver exited' in err:
        try:
            log=(Path(result['path'])/'spectre.log').read_text(errors='replace')
            return 'initial frequency is not close to osc frequency' in log
        except OSError:
            return False
    return False


def dc_power_mw(run_dir,project):
    """Supply power from the DC operating point (available even when HB fails)."""
    path=Path(run_dir)/'psf'/'dcOp.dc'
    if not path.is_file():return None
    vals={}
    for line in path.read_text(errors='replace').splitlines():
        m=re.match(r'^"([^"]+)"\s+"I"\s+([-+0-9.eE]+)$',line.strip())
        if m:vals[m.group(1)]=float(m.group(2))
    p=project['parameters'];sources={'VDD_SRC:p':float(p['vdd']),'VBIAS_SRC:p':float(p['vdc'])}
    for reg,c in project['registers'].items():
        for b in range(c['bits']):sources['V_'+reg+'_'+str(b)+':p']=float(p['vdd'])*((c['value']>>b)&1)
    if 'VDD_SRC:p' not in vals:return None
    return sum(-v*vals.get(k,0.0)*1000 for k,v in sources.items())
