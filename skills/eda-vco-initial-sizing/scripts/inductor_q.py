#!/usr/bin/env python3
"""Q of one-port (Rs+jwL)||Rp. SI inputs; no implicit coupling reduction."""
import argparse,json,math

def evaluate(l_h, frequencies_hz, rs_ohm=0.0, rp_ohm=None, limit=None):
    if not math.isfinite(l_h) or l_h<=0 or not math.isfinite(rs_ohm) or rs_ohm<0: raise ValueError('L>0 and Rs>=0 required')
    if rp_ohm is not None and (not math.isfinite(rp_ohm) or rp_ohm<=0): raise ValueError('Rp>0 required')
    if limit is not None and (not math.isfinite(limit) or limit<=0): raise ValueError('limit>0 required')
    rows=[]
    for f in frequencies_hz:
        if not math.isfinite(f) or f<=0: raise ValueError('frequencies must be positive and finite')
        z=complex(rs_ohm,2*math.pi*f*l_h)
        if rp_ohm is not None:z=1/(1/z+1/rp_ohm)
        q=z.imag/z.real if z.real>0 else None
        rows.append(dict(frequency_hz=f,z_real_ohm=z.real,z_imag_ohm=z.imag,q=q,within_limit=None if limit is None else q is not None and q<=limit*(1+1e-12)))
    if rs_ohm>0 and rp_ohm is not None:
        x=math.sqrt(rs_ohm*(rp_ohm+rs_ohm));peak=dict(q=rp_ohm/(2*x),frequency_hz=x/(2*math.pi*l_h))
    else:peak=dict(q=None,frequency_hz=None,reason='unbounded across all positive frequencies for ideal single-resistor or lossless model')
    return dict(network='(Rs+jwL)||Rp; absent Rp means open',points=rows,all_positive_frequencies_peak=peak,numeric_comparison_relative_tolerance=1e-12,scope='scalar one-port only; excludes coupled-mode/current-source/device loss and parasitic capacitance')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--l-h',type=float,required=True);p.add_argument('--rs-ohm',type=float,default=0);p.add_argument('--rp-ohm',type=float);p.add_argument('--frequency-hz',type=float,nargs='+',required=True);p.add_argument('--limit',type=float);p.add_argument('--output');a=p.parse_args()
    try:r=evaluate(a.l_h,a.frequency_hz,a.rs_ohm,a.rp_ohm,a.limit)
    except ValueError as e:p.error(str(e))
    text=json.dumps(r,indent=2,allow_nan=False)+'\n'
    if a.output:
        with open(a.output,'w') as f:f.write(text)
    else:print(text,end='')
if __name__=='__main__':main()
