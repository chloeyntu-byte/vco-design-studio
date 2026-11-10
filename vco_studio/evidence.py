"""Small file-based measured evidence index; no inferred universal bounds."""
import json, math
from pathlib import Path
from .common import read_json, sha256

SCHEMA = 'vco.evidence-point.v1'


class EvidenceUnavailable(OSError):
    """Raw evidence is not reachable here (moved/archived); distinct from a failed audit."""

def canonical_pdk(value):
    """Normalize explicit user identities without guessing technology aliases."""
    import re
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if re.fullmatch(r'[a-z][a-z0-9_-]*', text) else None


def load_points(paths):
    if isinstance(paths, (str, Path)): paths = [paths]
    rows=[]
    for path in paths:
        p=Path(path)
        if not p.is_file(): continue
        for number,line in enumerate(p.read_text().splitlines(),1):
            if not line.strip(): continue
            row=json.loads(line)
            if row.get('schema') != SCHEMA: raise ValueError('Unsupported evidence schema at %s:%d'%(p,number))
            row['pdk_original']=row.get('pdk')
            row['pdk']=canonical_pdk(row.get('pdk')) or row.get('pdk')
            rows.append(row)
    return rows

def default_indexes():
    """Use only explicitly configured user evidence indexes; no bundled measurements."""
    from .site import evidence_indexes
    return evidence_indexes()

def audit_convergence(row,hash_cache=None):
    """Normalize a verified claim without discarding a valid observed sample."""
    claim=row.get('convergence') or {'status':'unknown'}
    if claim.get('status')!='verified':return claim
    from .flow import _verify_result,_compare_convergence,CONVERGENCE_DEFAULTS
    try:
        file=row.get('result_path')
        comparison=claim.get('result_path') or claim.get('comparison_result')
        if not file or not comparison:raise ValueError('Verified convergence requires both actual result paths')
        if Path(file).resolve()==Path(comparison).resolve():raise ValueError('Convergence comparison reuses the observed run')
        expected=claim.get('result_sha256') or claim.get('comparison_sha256')
        if expected and sha256(comparison)!=expected:raise ValueError('Convergence comparison result hash changed')
        primary=read_json(file);manifest=read_json(Path(primary['receipt']['generated_dir'])/'manifest.json')
        circuit=manifest['files']['circuit.scs'];physical=row['physical_id']
        baseline=_verify_result(file,physical,circuit,hash_cache=hash_cache)
        independent=_verify_result(comparison,physical,circuit,hash_cache=hash_cache)
        proof=_compare_convergence(baseline,independent,CONVERGENCE_DEFAULTS,{})
        return dict(claim,status='verified',audit_status='verified',verification=proof)
    except (ValueError,OSError,KeyError,TypeError,IndexError) as exc:
        return {'status':'unknown','audit_status':'invalid_claim','reason':str(exc),'claimed_comparison':claim.get('result_path') or claim.get('comparison_result')}

def audit_point(row,hash_cache=None):
    """Verify source bytes and independently parsed Studio metrics where available."""
    from .site import resolve
    source=row.get('source',{});run=resolve(source.get('run_dir','/nonexistent'))
    if not run.is_dir():raise EvidenceUnavailable('source run directory not present on this host: '+str(run))
    hashes=source.get('hashes',{})
    if not hashes:raise ValueError('Missing source hashes')
    for name,digest in hashes.items():
        path=Path(name) if Path(name).is_absolute() else run/name
        if not path.is_file() or sha256(path)!=digest:raise ValueError('Source hash mismatch: '+str(path))
    file=row.get('result_path')
    if file:
        from .flow import _verify_result
        file=resolve(file);raw=read_json(file);receipt=raw['receipt'];manifest=read_json(resolve(receipt['generated_dir'])/'manifest.json')
        _verify_result(file,row['physical_id'],manifest['files']['circuit.scs'],hash_cache=hash_cache)
        metrics=raw['metrics'];conditions=raw['conditions']
        if row.get('vdd_v')!=conditions['vdd'] or row.get('temp_c')!=conditions['temp_c']:raise ValueError('Point conditions differ from result')
        if row.get('controls')!={k:v['value'] for k,v in raw['registers'].items()}:raise ValueError('Point controls differ from result')
        kind='raw_reparsed_studio_result'
    else:
        audit_file=source.get('audit')
        if not audit_file:raise ValueError('External evidence requires explicit source.audit receipt')
        audit=read_json(audit_file)
        if audit.get('status')!='audited' or audit.get('source',{}).get('hashes')!=hashes or audit.get('physical_id')!=row.get('physical_id'):raise ValueError('External audit is not bound to source hashes')
        metrics=dict(audit['metrics']);metrics.update({'pn_'+str(k)+'_dbc_hz':v for k,v in metrics.get('pn_dbc_hz',{}).items()});kind='external_audit_only'
    for key in ['frequency_hz','power_mw']:
        if not math.isclose(float(row[key]),float(metrics[key]),rel_tol=1e-8,abs_tol=1e-8):raise ValueError('Point metric differs: '+key)
    for offset,pn in row.get('pn_dbc_hz',{}).items():
        if not math.isclose(float(pn),float(metrics['pn_'+str(offset)+'_dbc_hz']),abs_tol=1e-7):raise ValueError('Point PN differs')
        fom=-pn+20*math.log10(row['frequency_hz']/float(offset))-10*math.log10(row['power_mw'])
        if not math.isclose(float(row['fom_db'][offset]),fom,abs_tol=1e-7):raise ValueError('FoM does not close to PN/frequency/power')
    claim=row.get('convergence')
    if isinstance(claim,dict) and claim.get('status')=='verified':
        row['convergence_claim']=dict(claim)
        row['convergence']=audit_convergence(row,hash_cache)
    return kind

def from_result(result_path,pdk,architecture=None,real_bank=None,q_at_f0=None,convergence=None):
    path=Path(result_path).resolve();r=read_json(path);receipt=r.get('receipt',{})
    if r.get('status')=='measured':
        from .flow import _verify_result
        manifest=read_json(Path(receipt['generated_dir'])/'manifest.json')
        _verify_result(path,r['physical_identity'],manifest['files']['circuit.scs'])
    generated=Path(receipt.get('generated_dir',path.parent));p=read_json(generated/'project.json');m=r.get('metrics',{})
    return {'schema':SCHEMA,'id':sha256(path),'pdk':pdk,'architecture':architecture or p['family'],'variant':p.get('variant'),
        'frequency_hz':m.get('frequency_hz'),'power_mw':m.get('power_mw'),'vdd_v':p['parameters']['vdd'],'temp_c':p['parameters']['temp_c'],
        'main_bits':p['registers']['main']['bits'],'controls':{k:v['value'] for k,v in p['registers'].items()},'physical_id':r.get('physical_identity'),
        'pn_dbc_hz':{key.split('_')[1]:v for key,v in m.items() if key.startswith('pn_')},
        'fom_db':{key.split('_')[1]:v for key,v in m.items() if key.startswith('fom_')},'q_at_f0':q_at_f0,'real_bank':real_bank,
        'source':{'run_dir':str(path.parent),'input':str(path.parent/'tb.scs'),'receipt':str(path.parent/'receipt.json'),'hashes':dict(r.get('evidence',{}),**{'result.json':sha256(path)})},
        'result_path':str(path),'status':r['status'],'qualification':r.get('qualification','failed_sample'),'convergence':convergence or {'status':'unknown'},'notes':r.get('warnings',[])}

def query(paths,pdk=None,frequency_hz=None,power_mw=None,vdd_v=None,temp_c=None,main_bits=None,offset=1000000,frequency_tolerance=.03,power_tolerance=.10,architecture=None,band_files=None,variant=None):
    pdk_original=pdk
    if pdk is not None:
        pdk=canonical_pdk(pdk)
        if pdk is None:return {'status':'needs_confirmation','reason':'Unknown PDK alias; select an explicit known PDK identity','pdk_requested':pdk_original,'known_pdk_ids':['demo_a','demo_c','demo_b','demo_d'],'matches':[],'observed_peak':None,'best_observed_worst_band':None,'pareto':[],'excluded':[]}
    for name,value in (('frequency_hz',frequency_hz),('power_mw',power_mw),('vdd_v',vdd_v)):
        if value is not None and (not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0):raise ValueError(name+' must be a finite positive number')
    if temp_c is not None and (not isinstance(temp_c,(int,float)) or not math.isfinite(temp_c)):raise ValueError('temp_c must be finite')
    for name,value in (('frequency_tolerance',frequency_tolerance),('power_tolerance',power_tolerance)):
        if not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<value<1:raise ValueError(name+' must be in (0, 1)')
    if int(offset) not in (10000,100000,1000000,10000000):raise ValueError('offset must be one of 10000, 100000, 1000000, 10000000')
    if architecture is not None:architecture=str(architecture).lower()
    rows=load_points(paths);matched=[];excluded=[];unqualified=[];offset=str(int(offset));hash_cache={};near=[];unavailable=0
    for r in rows:
        reasons=[]
        for key,want in [('pdk',pdk),('architecture',architecture),('variant',variant),('vdd_v',vdd_v),('temp_c',temp_c),('main_bits',main_bits)]:
            if want is not None and r.get(key)!=want:reasons.append(key)
        if r.get('status')!='measured':reasons.append('not_measured')
        if r.get('real_bank') is not True:reasons.append('real_bank_unproven')
        if not r.get('physical_id') or not r.get('source',{}).get('hashes'):reasons.append('missing_identity')
        deltas={}
        for key,want,tolerance in [('frequency_hz',frequency_hz,frequency_tolerance),('power_mw',power_mw,power_tolerance)]:
            value=r.get(key)
            if not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:reasons.append('invalid_'+key);continue
            if want is not None:
                if want<=0:raise ValueError('Query frequency/power must be positive')
                deltas[key]=(value-want)/want
                if abs(deltas[key])>tolerance:reasons.append('unmatched_'+key)
        fom=r.get('fom_db',{}).get(offset)
        if not isinstance(fom,(int,float)) or not math.isfinite(fom):reasons.append('missing_fom')
        if reasons:
            excluded.append({'id':r.get('id'),'reasons':reasons})
            if set(reasons)<= {'unmatched_frequency_hz','unmatched_power_mw'} and (frequency_hz or power_mw):
                near.append({'id':r.get('id'),'architecture':r.get('architecture'),'variant':r.get('variant'),'main_bits':r.get('main_bits'),
                             'f_ghz':round(r['frequency_hz']/1e9,4),'p_mw':round(r['power_mw'],3),'pn_dbc_hz':r.get('pn_dbc_hz',{}).get(offset),
                             'fom_db':fom,'delta_f_pct':round(deltas.get('frequency_hz',0)*100,2),'delta_p_pct':round(deltas.get('power_mw',0)*100,2),
                             'distance':math.hypot(deltas.get('frequency_hz',0)/frequency_tolerance,deltas.get('power_mw',0)/power_tolerance)})
            continue
        try:audit=audit_point(r,hash_cache)
        except EvidenceUnavailable as exc:
            unavailable+=1;excluded.append({'id':r.get('id'),'reasons':['evidence_unavailable: '+str(exc)]});continue
        except (ValueError,OSError,KeyError,TypeError) as exc:
            excluded.append({'id':r.get('id'),'reasons':['evidence_audit_failed: '+str(exc)]});continue
        item=dict(r,match_deltas=deltas,audit_kind=audit)
        q=r.get('q_at_f0')
        if not isinstance(q,(int,float)) or not math.isfinite(q) or not 0<q<=30:
            unqualified.append(dict(item,boundary_exclusion='q_at_f0_missing_or_outside_0_30'));continue
        matched.append(item)
    peak=max(matched,key=lambda r:r['fom_db'][offset]) if matched else None
    groups={}
    for r in matched:
        # Never mix conditions, bank widths or physical circuits into a band.
        key=(r['physical_id'],r.get('pdk'),r.get('vdd_v'),r.get('temp_c'),r.get('main_bits'))
        groups.setdefault(key,[]).append(r)
    bands=[]
    for key,points in groups.items():
        points=sorted(points,key=lambda r:r['frequency_hz']);lo=points[0]['frequency_hz'];hi=points[-1]['frequency_hz'];worst=min(points,key=lambda r:r['fom_db'][offset])
        bands.append({'physical_id':key[0],'point_ids':[r.get('id') for r in points],'controls':[r['controls'] for r in points],
          'frequency_band_hz':[lo,hi],'tr_fraction':2*(hi-lo)/(hi+lo),'max_gap_hz':max([b['frequency_hz']-a['frequency_hz'] for a,b in zip(points,points[1:])] or [0]),
          'sample_minimum_fom_db':worst['fom_db'][offset],'worst_point_id':worst.get('id'),'coverage':'measured_sparse','qualification':'matched observed samples only; no full-band assertion'})
    pareto=[r for r in matched if not any(s['power_mw']<=r['power_mw'] and s['fom_db'][offset]>=r['fom_db'][offset] and (s['power_mw']<r['power_mw'] or s['fom_db'][offset]>r['fom_db'][offset]) for s in matched)]
    qualified_bands,band_rejections=qualify_bands(band_files,matched,offset)
    near.sort(key=lambda x:x['distance'])
    status='measured_matches' if matched else ('evidence_unavailable' if unavailable else 'unknown')
    return {'status':status,'near_misses':near[:8],'evidence_unavailable_count':unavailable,'query':{'pdk':pdk,'pdk_requested':pdk_original,'frequency_hz':frequency_hz,'power_mw':power_mw,'vdd_v':vdd_v,'temp_c':temp_c,'main_bits':main_bits,'offset_hz':int(offset),'frequency_tolerance':frequency_tolerance,'power_tolerance':power_tolerance},
        'matches':matched,'observed_peak':peak,'sample_groups':bands,'best_observed_worst_band':max(qualified_bands,key=lambda b:b['worst_fom_db']) if qualified_bands else None,'qualified_submitted_bands':qualified_bands,'band_rejections':band_rejections,'band_status':'audited submitted domains' if qualified_bands else 'unknown: no complete submitted domain qualified',
        'pareto':pareto,'excluded':excluded,'unqualified_observations':unqualified,'qualification':'Measured conditional feasible samples, not a universal performance limit. Missing cells are unknown; sparse samples do not establish band coverage.'}

def qualify_bands(band_files,matched,offset):
    """Recompute explicitly submitted domains, never infer domains from scattered points."""
    accepted=[];rejected=[];byid={r['id']:r for r in matched}
    for file in band_files or []:
        entries=read_json(file)
        if isinstance(entries,dict):entries=entries.get('bands',[entries])
        for band in entries:
            try:
                ids=band['point_ids'];points=[byid[x] for x in ids]
                if not points or len(set(ids))!=len(ids):raise ValueError('Empty or duplicate points')
                keys={(r['physical_id'],r.get('pdk'),r.get('vdd_v'),r.get('temp_c'),r.get('main_bits')) for r in points}
                if len(keys)!=1 or points[0]['physical_id']!=band['physical_id']:raise ValueError('Mixed physical designs or conditions')
                controls=[r['controls'] for r in points]
                if len({json.dumps(x,sort_keys=True) for x in controls})!=len(controls):raise ValueError('Repeated control state')
                coverage=band['coverage']
                if coverage=='all_main_codes':
                    if {c['main'] for c in controls}!=set(range(2**points[0]['main_bits'])):raise ValueError('Missing main code')
                elif coverage=='adopted_grid':
                    declared=band['adopted_controls']
                    if {json.dumps(c,sort_keys=True) for c in declared}!={json.dumps(c,sort_keys=True) for c in controls}:raise ValueError('Adopted control map differs')
                    if len(points)<2:raise ValueError('One point cannot establish a band')
                else:raise ValueError('Unsupported qualified domain coverage')
                ordered=sorted(points,key=lambda x:x['frequency_hz']);lo=ordered[0]['frequency_hz'];hi=ordered[-1]['frequency_hz']
                if hi<=lo:raise ValueError('No nonzero observed frequency band')
                worst=min(points,key=lambda x:x['fom_db'][offset])
                accepted.append({'physical_id':band['physical_id'],'coverage':coverage,'point_ids':ids,'adopted_controls':controls,'frequency_band_hz':[lo,hi],
                  'tr_fraction':2*(hi-lo)/(hi+lo),'max_gap_hz':max(b['frequency_hz']-a['frequency_hz'] for a,b in zip(ordered,ordered[1:])),
                  'worst_fom_db':worst['fom_db'][offset],'worst_point_id':worst['id'],'source_domain_file':str(file),
                  'qualification':'All explicitly submitted domain samples audited; no continuous-frequency, PVT or independent-convergence claim'})
            except (KeyError,ValueError,TypeError) as exc:rejected.append({'file':str(file),'physical_id':band.get('physical_id'),'reason':str(exc)})
    return accepted,rejected


def _brief_point(r,offset):
    if not r:return None
    return {'id':r.get('id'),'architecture':r.get('architecture'),'variant':r.get('variant'),'main_bits':r.get('main_bits'),
            'controls':r.get('controls'),'f_ghz':round(r['frequency_hz']/1e9,4),'p_mw':round(r['power_mw'],3),
            'pn_dbc_hz':r.get('pn_dbc_hz',{}).get(offset),'fom_db':r.get('fom_db',{}).get(offset),
            'convergence':(r.get('convergence') or {}).get('status'),'qualification':r.get('qualification'),
            'result_path':r.get('result_path'),'notes':r.get('notes') or None}


def compact(out):
    """Agent-sized summary of a query result (use --full for every matched point)."""
    q=out.get('query',{});offset=str(q.get('offset_hz',1000000))
    matches=out.get('matches',[])
    per={}
    for r in matches:
        key=(r.get('architecture'),r.get('variant'))
        if key not in per or r['fom_db'][offset]>per[key]['fom_db'][offset]:per[key]=r
    states={}
    for r in matches:
        states.setdefault((r.get('physical_id'),json.dumps(r.get('controls'),sort_keys=True)),0)
        states[(r.get('physical_id'),json.dumps(r.get('controls'),sort_keys=True))]+=1
    return {'status':out.get('status'),'query':q,
            'counts':{'matched_rows':len(matches),'matched_unique_states':len(states),'excluded':len(out.get('excluded',[])),
                      'evidence_unavailable':out.get('evidence_unavailable_count',0),'q_unqualified':len(out.get('unqualified_observations',[]))},
            'observed_peak':_brief_point(out.get('observed_peak'),offset),
            'best_per_architecture':sorted([_brief_point(r,offset) for r in per.values()],key=lambda x:-(x['fom_db'] or 0)),
            'near_misses':out.get('near_misses',[]),
            'attempted_failed_nearby':failures_near(q.get('pdk'),q.get('frequency_hz'),q.get('power_mw'),
                                                    None,q.get('frequency_tolerance',.03),q.get('power_tolerance',.10)),
            'sample_groups':[{k:g[k] for k in ('physical_id','frequency_band_hz','tr_fraction','sample_minimum_fom_db','coverage')} for g in out.get('sample_groups',[])][:10],
            'best_observed_worst_band':out.get('best_observed_worst_band'),
            'qualification':out.get('qualification'),
            'hint':'unknown != infeasible. Use near_misses (with their deltas) or widen --frequency-tolerance/--power-tolerance; --full prints every point.'}


def failures_near(pdk=None,frequency_hz=None,power_mw=None,architecture=None,frequency_tolerance=.03,power_tolerance=.10):
    """User-configured failed/unmeasured attempts near a condition (attempted != infeasible; read failure_class)."""
    from .site import evidence_failure_files
    out=[]
    for path in evidence_failure_files():
        if not path.is_file():continue
        for line in path.read_text().splitlines():
            if not line.strip():continue
            f=json.loads(line)
            if pdk and canonical_pdk(f.get('pdk'))!=pdk:continue
            if architecture and f.get('architecture')!=architecture:continue
            t=f.get('target') or {}
            fg=t.get('frequency_ghz');pm=t.get('power_mw')
            if frequency_hz and fg and abs(fg*1e9/frequency_hz-1)>max(frequency_tolerance,.10):continue
            if power_mw and pm and abs(pm/power_mw-1)>max(power_tolerance,.25):continue
            if (frequency_hz and not fg):continue
            out.append({'run_dir':f.get('run_dir'),'architecture':f.get('architecture'),'variant':f.get('variant'),'target':t,
                        'failure_class':f.get('failure_class'),'evidence':(f.get('evidence') or '')[:200]})
    return out[:10]
