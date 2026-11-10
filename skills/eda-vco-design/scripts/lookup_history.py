#!/usr/bin/env python3
"""Read a VCO evidence JSONL ledger; never execute its contents or modify it."""
import argparse,json,math
from pathlib import Path

def query(path, architecture=None, version=None, status=None, run=None, input_sha=None, contains=None, conditions=None, limit=5, full=False):
    count=0;selected=[]
    with Path(path).open(encoding='utf-8')as handle:
        for number,line in enumerate(handle,1):
            row=json.loads(line)
            if not isinstance(row,dict):raise ValueError('ledger line %s is not an object'%number)
            pairs=[('architecture',architecture),('version',version),('status',status),('run_id',run),('input_sha256',input_sha)]
            if any(value is not None and row.get(key)!=value for key,value in pairs):continue
            hay=' '.join(str(row.get(k,''))for k in ('path','campaign','run_id','architecture'))
            if contains and contains.lower()not in hay.lower():continue
            actual=row.get('conditions',{})
            if any(key not in actual or actual[key]!=value or (isinstance(actual[key],bool)!=isinstance(value,bool))for key,value in (conditions or {}).items()):continue
            count+=1
            if len(selected)>=limit:continue
            keep=row if full else {key:row.get(key)for key in ('row_number','run_id','architecture','version','campaign','status','adoption_status','structure_id','input_sha256','path','conditions','metrics','source','derived_measurement_of')}
            selected.append(dict(ledger_line=number,record=keep))
    return {'ledger':str(Path(path).absolute()),'matched':count,'returned':len(selected),'records':selected,'scope':'matching existing evidence only; no interpolation, automatic ranking or new simulation'}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('ledger',type=Path);p.add_argument('--architecture');p.add_argument('--version');p.add_argument('--status');p.add_argument('--run');p.add_argument('--input-sha');p.add_argument('--contains');p.add_argument('--condition',action='append',default=[]);p.add_argument('--limit',type=int,default=5);p.add_argument('--full',action='store_true');a=p.parse_args()
    if not 1<=a.limit<=200:p.error('limit must be 1..200')
    conditions={}
    for item in a.condition:
        if '='not in item:p.error('condition must be key=value')
        key,value=item.split('=',1)
        if not key or key in conditions:p.error('empty or duplicate condition key')
        try:value=json.loads(value)
        except ValueError:pass
        if type(value)not in (str,float,int,bool)or isinstance(value,float)and not math.isfinite(value):p.error('condition requires a finite scalar')
        conditions[key]=value
    print(json.dumps(query(a.ledger,a.architecture,a.version,a.status,a.run,a.input_sha,a.contains,conditions,a.limit,a.full),ensure_ascii=False,indent=2,allow_nan=False))
if __name__=='__main__':main()
