from pathlib import Path
import ast, hashlib, json, math, operator, os, tempfile

ROOT = Path(__file__).resolve().parents[1]

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def write_json(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=str(path.parent), delete=False, encoding='utf-8') as out:
        json.dump(data, out, ensure_ascii=False, indent=2, allow_nan=False); out.write('\n'); tmp = out.name
    os.replace(tmp, path)

def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def electrical_hash(graph):
    def strip(obj):
        if isinstance(obj, dict):
            return {k:strip(v) for k,v in obj.items() if k not in ('position','orientation','notes','provenance','description','reference_data')}
        if isinstance(obj, list): return [strip(v) for v in obj]
        return obj
    return hashlib.sha256(json.dumps(strip(graph),sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def evaluate_expression(expr, parameters=None):
    if isinstance(expr,(float,int)): return expr
    parameters = parameters or {}
    import re
    text=str(expr).strip()
    units={'T':1e12,'G':1e9,'M':1e6,'Meg':1e6,'K':1e3,'k':1e3,'m':1e-3,'u':1e-6,'n':1e-9,'p':1e-12,'f':1e-15}
    match=re.fullmatch(r'([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)(Meg|[TGMKkmunpf])?',text)
    if match: return float(match[1])*units.get(match[2],1)
    tree=ast.parse(text,mode='eval')
    ops={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv,ast.Pow:operator.pow}
    def ev(n):
        if isinstance(n,ast.Constant) and isinstance(n.value,(float,int)):return n.value
        if isinstance(n,ast.Name) and n.id in parameters:return evaluate_expression(parameters[n.id],{})
        if isinstance(n,ast.UnaryOp) and isinstance(n.op,(ast.USub,ast.UAdd)):return (-1 if isinstance(n.op,ast.USub) else 1)*ev(n.operand)
        if isinstance(n,ast.BinOp) and type(n.op) in ops:
            a,b=ev(n.left),ev(n.right)
            if isinstance(n.op,ast.Pow) and abs(b)>16:raise ValueError('Exponent exceeds bounded expression domain')
            return ops[type(n.op)](a,b)
        raise ValueError('Unsupported numeric expression: '+text)
    value=ev(tree.body)
    if not math.isfinite(value): raise ValueError('Non-finite parameter')
    return value

def format_number(value):
    return format(float(value),'.15g') if isinstance(value,(float,int)) else str(value)
