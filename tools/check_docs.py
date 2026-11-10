"""Check local Markdown links and documented help commands without an EDA runtime."""
import pathlib,re,subprocess,os
root=pathlib.Path(__file__).resolve().parents[1]
errors=[]
for p in list(root.glob('*.md'))+list((root/'docs').glob('*.md'))+list((root/'skills').rglob('*.md')):
    for ref in re.findall(r'\]\(([^)]+)\)',p.read_text()):
        if '://' not in ref and not ref.startswith('#') and not (p.parent/ref.split('#')[0]).exists():
            errors.append(str(p.relative_to(root))+': missing '+ref)
for command in ('catalog','describe','generate','bindings','diagnose','spec-fields','spec-check','kb','flow-next','select','seed','table','plan','autosize','quickstart','flow-init','flow-add','flow-measure','flow-status','flow-mark','flow-freeze','deliver','verify','handoff','site','caplab-export','optimize','flow-sweep','flow-converge','bank-ac','z11','loopgain','diagnose-result','gui'):
    p=subprocess.run([str(root/'launch'),command,'--help'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=os.environ.copy())
    if p.returncode:errors.append(command+': invalid documented command')
if errors:raise SystemExit('\n'.join(errors))
print('Documentation links and CLI help: PASS')
