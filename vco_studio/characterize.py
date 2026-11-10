"""Real switched-bank AC matrices handed to the installed Cap Array Lab engine."""
from pathlib import Path
import shutil
from .common import read_json,write_json,sha256,format_number
from .simulator import psf_values

def bank_ac(generated_dir,output_dir,bank='main',codes=None,frequencies_hz=None,vcm=None,tank_inductance_h=None,timeout_s=180,cancel_event=None,log=None):
    source=Path(generated_dir).resolve();p=read_json(source/'project.json');g=read_json(source/'graph.json');manifest=read_json(source/'manifest.json')
    for name,expected in manifest['files'].items():
        if sha256(source/name)!=expected:raise ValueError('Generated input changed after manifest: '+name)
    for name,expected in manifest.get('model_files',{}).items():
        if sha256(name)!=expected:raise ValueError('PDK model changed after generation: '+name)
    if bank not in p['registers']:raise ValueError('No such physical bank '+bank)
    conf=p['registers'][bank];bits=int(conf['bits']);codes=codes if codes is not None else [0,2**bits-1]
    if not codes or any(int(c)!=c or not 0<=c<2**bits for c in codes):raise ValueError('Invalid bank codes')
    f=frequencies_hz or [x*1e9 for x in [5,10,15,20,25]]
    if len(f)<2 or any(v<=0 for v in f) or sorted(set(f))!=list(f):raise ValueError('At least two increasing unique positive frequencies required')
    vdd=p['parameters']['vdd'];vcm=float(vcm if vcm is not None else vdd/2)
    dest=Path(output_dir).resolve();dest.mkdir(parents=True,exist_ok=False);samples=[];receipts=[]
    from . import site
    from .runner import run_bounded,wrap_env
    solver=p.get('tools',{}).get('spectre') or site.spectre();env=p.get('tools',{}).get('native_env') or site.cadence_env()
    if not solver:raise FileNotFoundError('Spectre not found; set "spectre" in config/site.json or $VCO_SPECTRE')
    if timeout_s is None or not float(timeout_s)>0:raise ValueError('timeout must be positive')
    for code in codes:
        cols=[]
        for excite in [0,1]:
            if cancel_event and cancel_event.is_set():raise InterruptedError('Bank AC cancelled')
            run=dest/('code_%d_exc_%d'%(code,excite));run.mkdir()
            for name in ['circuit.scs','models.scs']:shutil.copy2(source/name,run/name)
            lines=['simulator lang=spectre','global 0','include "models.scs"','include "circuit.scs"',
                   'VDD_SRC (VDD 0) vsource dc='+str(vdd),
                   'VP (P 0) vsource dc='+str(vcm)+' mag='+str(1-excite),
                   'VN (N 0) vsource dc='+str(vcm)+' mag='+str(excite)]
            for i in range(bits):lines.append('VB%d (B%d 0) vsource dc=%s'%(i,i,vdd*((code>>i)&1)))
            lines.append('BANK (N P '+' '.join('B'+str(i) for i in range(bits-1,-1,-1))+' VDD 0) CT_R3_'+bank)
            if tank_inductance_h:
                resistance=2*3.141592653589793*max(f)*float(tank_inductance_h)/25
                lines.extend(['LTANK (P XL) inductor l='+str(tank_inductance_h),'RTANK (XL N) resistor r='+str(resistance)])
            lines.extend(['opts options temp='+str(p['parameters']['temp_c'])+' reltol=1e-5','ac ac values=['+' '.join(format_number(v) for v in f)+']','save P N VP:p VN:p','sopts options save=selected'])
            (run/'input.scs').write_text('\n'.join(lines)+'\n')
            argv=[solver,'-64','input.scs','+mt=1','-format','psfascii','-raw','psf','+log','spectre.log']
            outcome=run_bounded(wrap_env(argv,env,'studio-ac'),run,timeout_s,run/'stdout.log',cancel_event=cancel_event,poll_s=.15)
            exit_code=outcome['exit_code'];reason=outcome['stop_reason']
            receipt={'argv':argv,'source_graph_hash':manifest['graph_hash'],'code':code,'excitation':excite,'exit_code':exit_code,'elapsed_s':outcome['elapsed_s'],'stop_reason':reason,'input_sha256':sha256(run/'input.scs'),'run_dir':str(run)};write_json(run/'receipt.json',receipt);receipts.append(receipt)
            if reason or exit_code:raise RuntimeError('Bank AC failed (%s): %s'%(reason or 'exit '+str(exit_code),run))
            data=psf_values(run/'psf/ac.ac');cols.append(data)
            if log:log('Cap Lab matrix acquired code %d, excitation %d'%(code,excite))
        if len(cols[0])!=len(cols[1]):raise ValueError('AC excitation frequency identities differ')
        for a,b in zip(*cols):
            if a['freq']!=b['freq']:raise ValueError('AC matrix frequency mismatch')
            y11=-a['VP:p'];y21=-a['VN:p'];y12=-b['VP:p'];y22=-b['VN:p']
            samples.append({'point':code,'frequency_Hz':a['freq'],'Y11_G_S':y11.real,'Y11_B_S':y11.imag,'Y12_G_S':y12.real,'Y12_B_S':y12.imag,'Y21_G_S':y21.real,'Y21_B_S':y21.imag,'Y22_G_S':y22.real,'Y22_B_S':y22.imag,bank+'_code':code})
    write_json(dest/'matrix_samples.json',samples)
    provenance={'source_graph_hash':manifest['graph_hash'],'port':['OUTP','OUTN'],'kind':'native_ac','bank':bank,'vcm_v':vcm,'vdd_v':vdd,'temperature_c':p['parameters']['temp_c'],'model_hash':sha256(source/'models.scs'),'runs':receipts,'scope':'complete current PDK bank, ideal static control; '+('explicit assumed differential series-RL tank' if tank_inductance_h else 'bank only, no tank/core'),'tank_inductance_h':tank_inductance_h}
    write_json(dest/'provenance.json',provenance)
    from .integrations import caplab_analyze
    result=caplab_analyze(source,samples,dest/'caplab',port=['OUTP','OUTN'],sample_provenance=provenance)
    write_json(dest/'result.json',result);return result
