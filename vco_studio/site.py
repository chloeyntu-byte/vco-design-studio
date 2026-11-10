"""Machine profile and path resolution.

Every host-specific path (Python, Spectre, Cadence env script, knowledge base,
external tools, legacy runtime locations) comes from ONE place:

  1. the JSON file named by $VCO_SITE, else
  2. <repo>/config/site.json, else
  3. built-in neutral defaults (PATH discovery only).

Individual keys can be overridden with environment variables
(VCO_SPECTRE, VCO_CADENCE_ENV, VCO_RUNS_ROOT, VCO_KB_COMMAND).

Data files may store paths as tokens ``@name/rest`` where ``name`` is a key of
``path_map``; legacy absolute paths are remapped through ``legacy_prefixes`` so a
moved installation keeps resolving its evidence.
"""
from __future__ import annotations

import functools
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def version():
    try:
        return (ROOT / 'VERSION').read_text().strip()
    except OSError:
        return 'unknown'


@functools.lru_cache(maxsize=1)
def load():
    path = os.environ.get('VCO_SITE') or str(ROOT / 'config' / 'site.json')
    data = {}
    if Path(path).is_file():
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        data['_file'] = str(Path(path).resolve())
    data.setdefault('path_map', {})
    data['path_map'].setdefault('repo', str(ROOT))
    data.setdefault('legacy_prefixes', {})
    data.setdefault('tools', {})
    data.setdefault('kb', {})
    return data


def reset_cache():
    load.cache_clear()


def resolve(path):
    """Resolve a stored path (token, legacy absolute, or plain) to a usable Path."""
    if path is None:
        return None
    text = str(path)
    s = load()
    if text.startswith('@'):
        name, _, rest = text[1:].partition('/')
        base = s['path_map'].get(name)
        if base is None:
            raise ValueError('Unknown path token @%s (define it in site path_map)' % name)
        return Path(base) / rest if rest else Path(base)
    p = Path(text)
    if p.is_absolute() and not p.exists():
        for old, new in sorted(s['legacy_prefixes'].items(), key=lambda kv: -len(kv[0])):
            if text == old or text.startswith(old.rstrip('/') + '/'):
                target = Path(_expand(new)) / text[len(old.rstrip('/')) + 1:] if text != old else Path(_expand(new))
                if target.exists():
                    return target
    return p


def _expand(value):
    return str(ROOT) if value == '@repo' else str(value).replace('@repo', str(ROOT))


def tokenize(path):
    """Inverse of resolve for writing portable data: longest matching path_map prefix wins."""
    text = str(Path(path).resolve())
    best = None
    for name, base in load()['path_map'].items():
        b = str(Path(base).resolve()) if Path(base).exists() else str(base)
        if text == b or text.startswith(b.rstrip('/') + '/'):
            if best is None or len(b) > len(best[1]):
                best = (name, b)
    if best is None:
        return text
    rest = text[len(best[1]):].lstrip('/')
    return '@' + best[0] + ('/' + rest if rest else '')


def python():
    return os.environ.get('VCO_STUDIO_PYTHON') or load().get('python') or sys.executable


def spectre():
    for cand in (os.environ.get('VCO_SPECTRE'), load().get('spectre'), shutil.which('spectre')):
        if cand and Path(cand).is_file():
            return str(cand)
    return None


def cadence_env():
    cand = os.environ.get('VCO_CADENCE_ENV') or load().get('cadence_env')
    return str(cand) if cand and Path(cand).is_file() else None


def runs_root():
    return Path(os.environ.get('VCO_RUNS_ROOT') or load().get('runs_root') or (Path.cwd() / 'vco-runs'))


def tool_path(name):
    value = load()['tools'].get(name)
    return Path(_expand(value)) if value else None


def kb_command():
    env = os.environ.get('VCO_KB_COMMAND')
    if env:
        return shlex.split(env)
    kb = load().get('kb', {})
    if kb.get('enabled', True) is False:
        return None
    cmd = kb.get('command')
    if not cmd:
        return None
    cmd = [cmd] if isinstance(cmd, str) else list(cmd)
    return cmd if Path(cmd[0]).exists() else None


def evidence_indexes():
    files = load().get('evidence_indexes', [])
    out = []
    for f in files:
        p = resolve(f)
        out.append(p if p.is_absolute() else ROOT / p)
    return out


def evidence_failure_files():
    """Optional user JSONL ledgers of failed attempts, resolved like evidence indexes."""
    out = []
    for file in load().get('evidence_failure_files', []):
        path = resolve(file)
        out.append(path if path.is_absolute() else ROOT / path)
    return out


def describe():
    s = load()
    return {
        'version': version(),
        'program': str(ROOT),
        'site_file': s.get('_file'),
        'site_name': s.get('name'),
        'python': python(),
        'spectre': spectre(),
        'cadence_env': cadence_env(),
        'runs_root': str(runs_root()),
        'kb_command': kb_command(),
        'tools': {k: str(tool_path(k)) for k in s['tools']},
        'evidence_indexes': [str(p) for p in evidence_indexes()],
        'evidence_failure_files': [str(p) for p in evidence_failure_files()],
    }
