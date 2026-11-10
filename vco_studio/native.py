"""Build fresh private OA hierarchy and require physical-connectivity readback."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import re
from .pdk import ROOT, sq, run_skill, load_profile, inspect_masters

IDEAL = {
 'cap':{'cell':'cap','model':'capacitor','terminals':{'p':'PLUS','n':'MINUS'},'parameter_map':{'c':'c','m':'m'}},
 'res':{'cell':'res','model':'resistor','terminals':{'p':'PLUS','n':'MINUS'},'parameter_map':{'r':'r','m':'m'}},
 'ind':{'cell':'ind','model':'inductor','terminals':{'p':'PLUS','n':'MINUS'},'parameter_map':{'l':'l','r':'r','m':'m'}},
 'mutual':{'cell':'mind','model':'mutual_inductor','terminals':{},'parameter_map':{'coupling':'k','ind1':'ind1','ind2':'ind2','l1':'ind1','l2':'ind2'}},
 'vsource':{'cell':'vsource','model':'vsource','terminals':{'p':'PLUS','n':'MINUS'},'parameter_map':{'dc':'vdc','type':'srcType'}},
 'isource':{'cell':'isource','model':'isource','terminals':{'p':'PLUS','n':'MINUS'},'parameter_map':{'dc':'idc','type':'srcType'}},
}


def role_binding(inst, binding):
    role = inst['role']
    if inst.get('ideal') or role in ('mutual','vsource','isource'):
        if role not in IDEAL:
            raise ValueError('No ideal primitive for '+role)
        return dict(IDEAL[role], library='analogLib', view='symbol', defaults={})
    if role not in binding.get('roles',{}):
        raise ValueError('Missing PDK binding for role '+role)
    resolved = binding['roles'][role]
    if not all(resolved.get(k) for k in ('library','cell','terminals')):
        raise ValueError('Native binding requires live master/CDF discovery for '+role)
    if any(value is None for value in resolved.get('terminals',{}).values()):
        raise ValueError('Unresolved PDK terminals for '+role)
    return resolved


def _name(value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
        raise ValueError('Unsafe OA identifier: '+str(value))
    return value


def _graphs(graph):
    entries = {g['name']:g for g in graph.get('subcircuits',[])}
    entries[graph['name']] = graph
    ordered, visiting, visited = [], set(), set()
    def visit(g):
        if g['name'] in visiting:
            raise ValueError('Recursive hierarchy: '+g['name'])
        if g['name'] in visited:
            return
        visiting.add(g['name'])
        for inst in g.get('instances',[]):
            if inst['role']=='subckt':
                if inst['master'] not in entries:
                    raise ValueError('Missing subcircuit '+inst['master'])
                visit(entries[inst['master']])
        visiting.remove(g['name']); visited.add(g['name']); ordered.append(g)
    visit(graph)
    return ordered


def _native_value(value, params):
    value = str(value)
    # Parent parameters are explicit inherited OA expressions.
    if params:
        pattern = r'\b('+'|'.join(re.escape(k) for k in sorted(params,key=len,reverse=True))+r')\b'
        value = re.sub(pattern,lambda m:'pPar("'+m.group(1)+'")',value)
    return value


def build_script(graph, binding, target, folder):
    lib, top = _name(target['library']), _name(target['cell'])
    graphs = _graphs(graph)
    mapping = {g['name']:(top if g is graph else top+'__'+_name(g['name'])) for g in graphs}
    libpath = Path(target.get('library_path', Path(folder)/'oa'/lib)).resolve()
    body = (ROOT/'native/helpers.il').read_text()
    body += '\nwhen(ddGetObj('+sq(lib)+') error("Refusing pre-existing target library"))\n'
    body += 'unless(ddCreateLib('+sq(lib)+' '+sq(libpath)+') error("Cannot create private library"))\n'
    expected = []
    for g in graphs:
        cell = mapping[g['name']]
        body += 'when(ddGetObj(%s %s) error("Refusing existing target cell"))\ncv=dbOpenCellViewByType(%s %s "schematic" "schematic" "w")\nunless(cv error("Cannot create schematic"))\n' % (sq(lib),sq(cell),sq(lib),sq(cell))
        names=set()
        child_ports=[len(next(x for x in graphs if x['name']==i['master']).get('ports',[])) for i in g.get('instances',[]) if i['role']=='subckt']
        y_pitch=max(5.0,max(child_ports,default=0)*0.5+2.0)
        for index,inst in enumerate(g.get('instances',[])):
            _name(inst['name'])
            if inst['name'] in names:
                raise ValueError('Duplicate instance '+inst['name'])
            names.add(inst['name'])
            if inst['role']=='subckt':
                sub = next(x for x in graphs if x['name']==inst['master'])
                b={'library':lib,'cell':mapping[inst['master']],'view':'symbol','terminals':{p['name']:p['name'] for p in sub.get('ports',[])},'parameter_map':{k:k for k in sub.get('parameters',{})}}
            else:
                b=role_binding(inst,binding)
            # Individual isolated tiles avoid accidental geometric shorting; wire
            # labels establish graph nets. Relative ordering remains deterministic.
            x,y = (index % 8)*5.0, -(index//8)*y_pitch
            orient=inst.get('orientation','R0')
            if orient not in ('R0','R90','R180','R270','MX','MY','MXR90','MYR90'):
                raise ValueError('Invalid orientation '+orient)
            body += 'master=dbOpenCellViewByType(%s %s %s "" "r")\nunless(master error("Missing bound master"))\ni=dbCreateInst(cv master %s list(%s %s) %s)\nunless(i error("Cannot create instance"))\n' % (sq(b['library']),sq(b['cell']),sq(b.get('view','symbol')),sq(inst['name']),x,y,sq(orient))
            if set(inst.get('nodes',{})) != set(b['terminals']):
                raise ValueError('Terminal coverage differs for '+inst['name']+': graph '+str(inst.get('nodes',{}))+' binding '+str(b['terminals']))
            for key,net in inst.get('nodes',{}).items():
                actual=b['terminals'][key]
                net='gnd!' if net=='0' else net
                body += 'vcoStub(cv i %s %s)\n' % (sq(actual),sq(net))
                expected.append([cell,inst['name'],actual,net])
            for key,value in inst.get('parameters',{}).items():
                actual=b.get('parameter_map',{}).get(key)
                if actual is None and key in b.get('provenance',{}).get('metadata',{}).get('cdf',{}):
                    actual=key
                if actual is None:
                    # Never silently ignore a sizing/physical parameter.
                    raise ValueError('Unmapped parameter '+key+' for '+inst['name']+' ('+b['cell']+')')
                value=_native_value(value,g.get('parameters',{}) if g is not graph else {})
                body += 'dbReplaceProp(i %s "string" %s)\n' % (sq(actual),sq(value))
        for index,p in enumerate(g.get('ports',[])):
            direction=p.get('direction','inputOutput')
            if direction not in ('input','output','inputOutput'):
                raise ValueError('Invalid port direction '+direction)
            body += 'vcoPort(cv %s %s list(-4 %s))\n' % (sq(p['name']),sq(direction),-index*1.25)
        body += 'schCheck(cv)\nunless(dbSave(cv) error("Check/save failed"))\ndbClose(cv)\n'
        body += 'cv=dbOpenCellViewByType(%s %s "symbol" "schematicSymbol" "w")\nmaster=dbOpenCellViewByType("basic" "sympin" "symbol" "" "r")\n' % (sq(lib),sq(cell))
        height=max(1,len(g.get('ports',[]))*.25)
        body += 'dbCreateRect(cv list("device" "drawing") list(list(-1 %s) list(1 %s)))\n' % (-height,height)
        for index,p in enumerate(g.get('ports',[])):
            y=height-.25-index*.5
            body += 'schCreateSymbolPin(cv master %s %s list(-1.5 %s) "R0")\ndbCreateLine(cv list("device" "drawing") list(list(-1.5 %s) list(-1 %s)))\n' % (sq(p['name']),sq(p.get('direction','inputOutput')),y,y,y)
        body += 'dbCreateLabel(cv list("device" "label") list(0 0) %s "centerCenter" "R0" "roman" 0.1)\ndbReplaceProp(cv "instNamePrefix" "string" "I")\nunless(dbSave(cv) error("Symbol save failed"))\ndbClose(cv)\n' % sq(g['name'])
        if g.get('parameters'):
            body += 'cdf=cdfCreateBaseCellCDF(ddGetObj(%s %s))\n' % (sq(lib),sq(cell))
            for key,value in g['parameters'].items():
                _name(key)
                body += 'cdfCreateParam(cdf ?name %s ?type "string" ?defValue %s ?parseAsCEL "yes" ?parseAsNumber "yes")\n' % (sq(key),sq(value))
            body += 'cdfSaveCDF(cdf)\n'
    body += 'o=outfile(%s)\n' % sq(Path(folder)/'connectivity.tsv')
    for g in graphs:
        cell=mapping[g['name']]
        body += 'cv=dbOpenCellViewByType(%s %s "schematic" "" "r")\nunless(cv error("Reopen failed"))\nforeach(inst cv~>instances foreach(it inst~>instTerms fprintf(o "NET\\t%%s\\t%%s\\t%%s\\t%%s\\n" %s inst~>name it~>name it~>net~>name)))\nforeach(term cv~>terminals fprintf(o "PORT\\t%%s\\t%%s\\t%%s\\n" %s term~>name term~>direction))\ndbClose(cv)\ncv=dbOpenCellViewByType(%s %s "symbol" "" "r")\nforeach(term cv~>terminals fprintf(o "SYMBOL\\t%%s\\t%%s\\t%%s\\n" %s term~>name term~>direction))\ndbClose(cv)\n' % (sq(lib),sq(cell),sq(cell),sq(cell),sq(lib),sq(cell),sq(cell))
    body += 'close(o)\n'
    return body,expected,mapping,str(libpath)


def _verify_readback(folder, expected, graph, mapping):
    rows=[line.split('\t') for line in (Path(folder)/'connectivity.tsv').read_text().splitlines()]
    actual=sorted(row[1:] for row in rows if row[0]=='NET')
    if actual != sorted(expected):
        missing=[r for r in expected if r not in actual]
        extra=[r for r in actual if r not in expected]
        raise RuntimeError('OA physical connectivity mismatch: missing='+str(missing[:10])+' extra='+str(extra[:10]))
    ports=sorted([mapping[g['name']],p['name'],p.get('direction','inputOutput')] for g in _graphs(graph) for p in g.get('ports',[]))
    for kind in ('PORT','SYMBOL'):
        if sorted(row[1:] for row in rows if row[0]==kind) != ports:
            raise RuntimeError(kind+' hierarchy interface differs from graph')
    return {'instance_terminals':len(actual), 'ports':len(ports), 'hierarchy_cells':len(mapping), 'exact_match':True}


def export(graph, binding, target, output_dir, execute=False, cancel_event=None):
    target=dict(target,workspace=target.get('workspace') or binding.get('workspace'))
    ind=binding.get('roles',{}).get('ind')
    if ind and (ind.get('library'),ind.get('cell'))!=('analogLib','ind'):
        raise ValueError('Selected PDK inductor needs a verified magnetic/mutual adapter; refusing silent ideal substitution')
    if binding.get('unresolved'):
        raise ValueError('PDK binding unresolved: '+json.dumps(binding['unresolved']))
    folder=Path(output_dir).resolve()
    folder.mkdir(parents=True,exist_ok=True)
    if (folder/'native-request.json').exists():
        raise FileExistsError('Native export already exists; select a fresh output directory')
    body,expected,mapping,libpath=build_script(graph,binding,target,folder)
    if Path(libpath).exists():
        raise FileExistsError('Refusing existing private library path '+libpath)
    from .common import electrical_hash
    identity=electrical_hash({'graph':graph,'binding':binding})
    receipt={'schema':'vco.native.v1','status':'prepared','target':dict(target,library_path=libpath),'source_identity':identity,'source_graph_hash':electrical_hash(graph),'hierarchy':mapping,'expected_connections':len(expected),'limitations':['Seed circuit; no performance qualification implied.']}
    (folder/'native-request.json').write_text(json.dumps(receipt,indent=2))
    (folder/'build.il').write_text(body)
    (folder/'expected-connectivity.json').write_text(json.dumps(expected,indent=2))
    if not execute:
        return receipt
    profile=load_profile(target.get('profile') or binding.get('profile'))
    workspace=target.get('workspace') or binding['workspace']
    try:
        Path(libpath).parent.mkdir(parents=True,exist_ok=True)
        receipt['build']=run_skill(workspace,body,folder/'build-worker',profile,cancel_event=cancel_event,timeout=600)
        receipt['readback']=_verify_readback(folder,expected,graph,mapping)
        netfolder=folder/'netlist-worker'
        netfolder.mkdir()
        (netfolder/'cds.lib').write_text('INCLUDE '+sq(Path(workspace).resolve()/'cds.lib')+'\nDEFINE '+target['library']+' '+sq(libpath)+'\n')
        netbody="envSetVal(\"asimenv.startup\" \"projectDir\" 'string %s)\nsimulator('spectre)\ndesign(%s %s \"schematic\")\nresultsDir(%s)\n" % (sq(netfolder/'simulation'),sq(target['library']),sq(target['cell']),sq(netfolder/'simulation'))
        netbody += 'envOption(\'switchViewList list("spectre" "cmos_sch" "schematic" "veriloga") \'stopViewList list("spectre"))\n'
        for key,value in graph.get('parameters',{}).items():
            netbody += 'desVar(%s %s)\n' % (sq(key),sq(value))
        if binding.get('model_includes'):
            pairs=' '.join('list('+sq(m['path'])+' '+sq(m.get('section',''))+')' for m in binding['model_includes'])
            netbody += 'modelFile('+pairs+')\n'
        netbody += 'net=createNetlist(?recreateAll t)\nunless(net error("Native Spectre netlisting failed"))\no=outfile(%s) fprintf(o "%%L\\n" net) close(o)\n' % sq(netfolder/'netlist-path.txt')
        receipt['netlisting']=run_skill(workspace,netbody,netfolder,profile,cancel_event=cancel_event,ocean=True,timeout=600)
        netlists=list(netfolder.glob('**/netlist'))+list(netfolder.glob('**/input.scs'))
        files=[p for p in netlists if p.is_file()]
        if not files:
            raise RuntimeError('Native netlister returned without a readable netlist')
        receipt['netlists']=[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
        main_netlist=netfolder/'simulation'/target['cell']/'spectre/schematic/netlist/netlist'
        receipt['netlist_verification']=verify_netlist(main_netlist,graph,binding,mapping)
        receipt['status']='verified'
        receipt['profile']=profile
        receipt['workspace_entry']=prepare_workspace(receipt,profile,folder/'workspace')
    except BaseException as exc:
        receipt['status']='failed';receipt['error']=str(exc)
        (folder/'native-receipt.json').write_text(json.dumps(receipt,indent=2))
        raise
    (folder/'native-receipt.json').write_text(json.dumps(receipt,indent=2))
    return receipt


def verify_netlist(path, graph, binding, mapping):
    """Compare native emitted devices, ordered nets, models and explicit values."""
    from .common import evaluate_expression
    text=Path(path).read_text().replace('\\\n',' ')
    headers={}
    actual={}
    current=None
    for raw in text.splitlines():
        line=raw.strip()
        if line.startswith('// Cell name:'):
            current=line.split(':',1)[1].strip();actual.setdefault(current,{})
        if line.startswith('subckt '):
            tokens=line.split();headers[tokens[1]]=tokens[2:]
        if not line or line.startswith(('//','subckt ','ends ')):
            continue
        match=re.match(r'^(\w+)\s+(?:\(([^)]*)\)\s+)?([\w]+)(?:\s+(.*))?$',line)
        if match and current in mapping.values():
            name,nets,model,params=match.groups()
            values=dict(re.findall(r'(\w+)\s*=\s*(\([^)]*\)|[^\s]+)',params or ''))
            actual[current][name]={'nets':(nets or '').split(),'model':model,'parameters':values}
    total=0
    for g in _graphs(graph):
        cell=mapping[g['name']]
        if set(actual.get(cell,{})) != {i['name'] for i in g['instances']}:
            raise RuntimeError('Native netlist instance coverage mismatch for '+cell)
        for inst in g['instances']:
            a=actual[cell][inst['name']]
            if inst['role']=='subckt':
                model=mapping[inst['master']]
                order=headers.get(model)
                if order is None:
                    raise RuntimeError('Missing native subcircuit '+model)
                wanted=[inst['nodes'][p] for p in order]
                params=inst.get('parameters',{})
            else:
                b=role_binding(inst,binding);model=b['model']
                order=b.get('netlist_terminal_order')
                if order is None:
                    raw=b.get('provenance',{}).get('metadata',{}).get('spectre',{}).get('termOrder')
                    reverse={v:k for k,v in b['terminals'].items()}
                    order=[reverse[x] for x in raw] if raw else list(b['terminals'])
                wanted=[inst['nodes'][p] for p in order]
                param_map=b.get('netlist_parameter_map',{}) if not inst.get('ideal') and inst['role'] not in ('mutual','vsource','isource') else {}
                params={param_map.get(k,k):v for k,v in inst.get('parameters',{}).items()}
            wanted=['0' if v=='gnd!' else v for v in wanted]
            if a['model']!=model or a['nets']!=wanted:
                raise RuntimeError('Native netlist model/nets mismatch '+cell+'/'+inst['name']+': '+str(a))
            for key,value in params.items():
                if key not in a['parameters']:
                    raise RuntimeError('Native netlist dropped parameter '+cell+'/'+inst['name']+'/'+key)
                rendered=a['parameters'][key]
                try:
                    lhs=float(evaluate_expression(rendered));rhs=float(evaluate_expression(value))
                    equal=abs(lhs-rhs)<=max(abs(rhs)*1e-9,1e-30)
                except (ValueError,TypeError,KeyError,SyntaxError):
                    equal=str(value)==rendered
                if not equal:
                    raise RuntimeError('Native netlist parameter differs '+cell+'/'+inst['name']+'/'+key+': '+str(value)+' -> '+rendered)
            total+=1
    return {'instances':total,'models_nodes_parameters_match':True,'path':str(path)}


def prepare_workspace(receipt, profile=None, output_dir=None, workspace=None, library_path=None):
    """Create a self-contained, read-only GUI entry without editing shared cds.lib.

    For a moved OA library pass its new path, a destination PDK workspace and
    destination runtime profile. This prepares access; it does not requalify PDKs.
    """
    import shlex
    receipt_file=Path(receipt).resolve() if isinstance(receipt,(str,Path)) else None
    if receipt_file:
        receipt=json.loads(receipt_file.read_text())
    target=receipt['target']
    if receipt.get('status')!='verified':
        raise ValueError('Only a verified native receipt can produce a GUI entry')
    profile=load_profile(profile or receipt.get('profile'))
    source=Path(workspace or target['workspace']).resolve()/'cds.lib'
    libpath=Path(library_path or target['library_path']).resolve()
    if not source.is_file() or not (libpath/'cdsinfo.tag').is_file():
        raise ValueError('Destination workspace cds.lib or private OA library is unavailable')
    if output_dir is None:
        if receipt_file is None:
            raise ValueError('output_dir is required for an in-memory receipt')
        output_dir=receipt_file.parent/'workspace'
    folder=Path(output_dir).resolve();folder.mkdir(parents=True,exist_ok=True)
    mapping=folder/'cds.lib';script=folder/'open.il';launcher=folder/'open-virtuoso.sh'
    files={mapping:'INCLUDE '+sq(source)+'\nDEFINE '+_name(target['library'])+' '+sq(libpath)+'\n'}
    files[script]='''procedure(vcoOpenGenerated()
 let((w)
  w=deOpenCellView(%s %s "schematic" "schematic" nil "r")
  unless(w error("Could not open generated private schematic"))
  printf("VCO_STUDIO_OPEN %%L\\n" w)
 ))
hiEnqueueCmd("vcoOpenGenerated()")
''' % (sq(target['library']),sq(target['cell']))
    lines=['#!/bin/bash','set -e','if [[ -z "${DISPLAY:-}" ]]; then printf "Set DISPLAY to your existing desktop before opening Virtuoso.\\n" >&2; exit 2; fi','vco_cds_set=${CDS_LIC_FILE+x}','vco_cds=${CDS_LIC_FILE-}','vco_lm_set=${LM_LICENSE_FILE+x}','vco_lm=${LM_LICENSE_FILE-}']
    if profile.get('environment_script'):
        lines.append('source '+shlex.quote(profile['environment_script']))
    lines+=['vco_restore_var() { printf -v "$1" %s "$2"; export "$1"; }', 'if [[ "$vco_cds_set" == x ]]; then vco_restore_var CDS_LIC_FILE "$vco_cds"; fi','if [[ "$vco_lm_set" == x ]]; then vco_restore_var LM_LICENSE_FILE "$vco_lm"; fi','export CDS_LOAD_ENV=CWD']
    if profile.get('prepend_path'):
        lines.append('export PATH='+shlex.quote(':'.join(profile['prepend_path']))+':"$PATH"')
    lines+=['for vco_var in ${!CDS_MPS_@}; do unset "$vco_var"; done','unset CDS_LIB_PATH CDS_LOAD_PATH','cd '+shlex.quote(str(folder)),'exec '+shlex.join([profile['virtuoso'],'-nocdsinit','-cdslib',str(mapping),'-restore',str(script),'-log',str(folder/'gui.log')])]
    files[launcher]='\n'.join(lines)+'\n'
    for path,content in files.items():
        if path.exists():
            if path.read_text()!=content:
                raise FileExistsError('Refusing to overwrite existing workspace entry '+str(path))
        else:
            with path.open('x') as stream:stream.write(content)
    return {'workspace':str(folder),'cds_lib':str(mapping),'open_command':'bash '+shlex.quote(str(launcher)),'mode':'read-only','library_path':str(libpath),'migration_note':'External PDK/model dependencies are not copied; regenerate and verify after a technology change.'}


def main():
    import argparse
    parser=argparse.ArgumentParser(description='Private OA export and standalone Virtuoso entry')
    commands=parser.add_subparsers(dest='command',required=True)
    c=commands.add_parser('export');c.add_argument('generated');c.add_argument('--library',required=True);c.add_argument('--cell',required=True);c.add_argument('--workspace');c.add_argument('--output',required=True);c.add_argument('--profile');c.add_argument('--execute',action='store_true')
    c=commands.add_parser('workspace');c.add_argument('receipt');c.add_argument('--output');c.add_argument('--workspace');c.add_argument('--library-path');c.add_argument('--profile')
    args=parser.parse_args()
    if args.command=='export':
        generated=Path(args.generated);graph=json.loads((generated/'graph.json').read_text());binding=json.loads((generated/'binding.json').read_text())
        target={'library':args.library,'cell':args.cell,'workspace':args.workspace or binding['workspace']}
        if args.profile:target['profile']=args.profile
        result=export(graph,binding,target,args.output,args.execute)
    else:
        result=prepare_workspace(args.receipt,args.profile,args.output,args.workspace,args.library_path)
    print(json.dumps(result,indent=2,ensure_ascii=False))


if __name__=='__main__':main()
