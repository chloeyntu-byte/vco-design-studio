#!/usr/bin/env python3
"""Check current Z11 V2 one-port units and finite-Q admittance against algebra."""
import argparse,json,sys
p=argparse.ArgumentParser();p.add_argument('--z11-root',required=True);p.add_argument('--output');a=p.parse_args()
sys.path.insert(0,a.z11_root)
import numpy as np
import network_model_v2 as nm
f=np.array([16.,20.,24.]); w=2*np.pi*f*1e9
parts=[dict(id='PORT1',kind='P',x=0,y=0,rotation=1),dict(id='L1',kind='L',x=120,y=0,rotation=1,value=1000.,q=12.),dict(id='C1',kind='C',x=240,y=0,rotation=1,value=60.,q=40.)]
d=dict(parts=parts,wires=[[[0,-40],[240,-40]],[[0,40],[240,40]]],junctions=[])
z,ignored=nm.solve(d,f)
zl=w*1e-9/12+1j*w*1e-9;zc=1/(w*60e-15*40)-1j/(w*60e-15)
expected=1/(1/zl+1/zc)
assert ignored==0 and np.allclose(z,expected,rtol=1e-10,atol=1e-10)
r={'status':'analytic V2 one-port test passed; not PDK or PN verification','frequency_GHz':f.tolist(),'real_ohm':z.real.tolist(),'imag_ohm':z.imag.tolist(),'max_relative_error':float(np.max(np.abs((z-expected)/expected)))}
t=json.dumps(r,indent=2)
if a.output:
 with open(a.output,'w') as out:out.write(t+'\n')
print(t)
