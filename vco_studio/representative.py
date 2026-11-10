"""Bounded, actual multi-code HB/PN and optional real-bank AC evaluation."""
import copy
from pathlib import Path
from .common import read_json,write_json

def evaluate(project,candidate_dir,code_map,bank_ac_options=None,cancel_event=None,log=None):
    from .generator import generate
    from .simulator import evaluate_candidate
    from .characterize import bank_ac
    if not code_map:raise ValueError('Explicit nonempty representative control map required')
    for codes in code_map:
        if set(codes)!=set(project['registers']):raise ValueError('Every representative point must specify all physical controls')
    observations=[];results=[]
    for i,codes in enumerate(code_map):
        if cancel_event and cancel_event.is_set():raise InterruptedError('Representative evaluation cancelled')
        p=copy.deepcopy(project)
        for name,value in codes.items():p['registers'][name]['value']=value
        dest=Path(candidate_dir)/'representative'/('point_%03d'%i)
        generate(p,dest);obs=evaluate_candidate(p,dest,cancel_event,log);observations.append(obs)
        results.append(str(Path(obs['evidence']['path'])/'result.json'))
    ac=None
    if bank_ac_options:
        # Caller supplies physical bias and frequency assumptions; do not invent them.
        ac=bank_ac(candidate_dir,Path(candidate_dir)/'representative_bank_ac',cancel_event=cancel_event,log=log,**bank_ac_options)
    failed=any(o['status']!='completed' for o in observations)
    violations=[{'controls':codes,'violations':o.get('violations',[]),'status':o['status']} for codes,o in zip(code_map,observations) if not o['feasible']]
    objectives=[max(o['objectives'][j] for o in observations) for j in range(2)]
    total=sum(o['total_violation'] for o in observations)
    out={'status':'failed' if failed else 'completed','feasible':not violations,'total_violation':total,
         'merit':sum(objectives)/2+1000*total,'objectives':objectives,'violations':violations,
         'evidence':{'result_paths':results,'bank_ac':ac,'controls':code_map},'scope':'worst representative-code FoM; explicit sparse map, not full-band coverage'}
    write_json(Path(candidate_dir)/'representative-evaluation.json',out);return out

def run(generated_dir,output_dir,code_map,bank_ac_options=None):
    from .generator import generate
    p=read_json(Path(generated_dir)/'project.json');generate(p,output_dir)
    return evaluate(p,output_dir,code_map,bank_ac_options)

def optimizer_evaluator(code_map,bank_ac_options=None):
    return lambda project,directory:evaluate(project,directory,code_map,bank_ac_options)
