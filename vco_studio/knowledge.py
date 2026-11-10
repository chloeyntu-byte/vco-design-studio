"""Knowledge access for the design flow.

Two layers, one API:
  * curated cards  -- data/knowledge/cards/*.json in this repository: small, structured, executable
                      (conditions + symptom codes + actions + evidence links). Matched deterministically
                      by structured fields, so the flow can push the right card at the right decision point.
  * knowledge base -- an optional user-configured search/show command for papers, history and raw records.
                      Automatic flow consults retain local cards if this backend is unavailable.

Every consult made through a task is appended to <task>/knowledge/consults.jsonl (a receipt), so
reviews can see what knowledge was used at which stage.
"""
from __future__ import annotations

import json
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from .common import ROOT, read_json

CARD_DIR = ROOT / 'data' / 'knowledge' / 'cards'
CARD_TYPES = ('architecture', 'mechanism', 'symptom', 'tuning', 'pdk_device', 'lookup', 'procedure', 'rule')
LIFECYCLE_BONUS = {'current': 3.0, 'provisional': 0.0}   # reviewed cards outrank drafts


# ----------------------------------------------------------------------------------------- cards
@lru_cache(maxsize=1)
def load_cards():
    cards = {}
    if CARD_DIR.is_dir():
        for f in sorted(CARD_DIR.glob('*.json')):
            data = json.loads(f.read_text(encoding='utf-8'))
            for card in (data if isinstance(data, list) else [data]):
                card['_file'] = str(f.relative_to(ROOT))
                cards[card['id']] = card
    return cards


def reset_cache():
    load_cards.cache_clear()


def validate_card(card):
    errors = []
    for key in ('id', 'type', 'title', 'summary', 'actions', 'applies', 'evidence', 'lifecycle'):
        if key not in card:
            errors.append('missing ' + key)
    if card.get('type') not in CARD_TYPES:
        errors.append('type must be one of ' + ', '.join(CARD_TYPES))
    if not isinstance(card.get('actions', []), list):
        errors.append('actions must be a list')
    if not isinstance(card.get('applies', {}), dict):
        errors.append('applies must be an object')
    if card.get('lifecycle') not in ('current', 'provisional', 'superseded'):
        errors.append('lifecycle must be current|provisional|superseded')
    return errors


def _matches(card, ctx):
    """A card applies when every condition it declares is satisfied by the context (missing ctx = wildcard)."""
    applies = card.get('applies', {})
    score = 0
    for key, allowed in applies.items():
        if key in ('frequency_ghz', 'power_mw'):
            value = ctx.get(key)
            if value is None:
                continue
            lo, hi = allowed
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                return None
            score += 1
            continue
        value = ctx.get(key)
        if value is None:
            if key in ('pdk', 'family', 'symptoms'):
                return None   # specific cards only apply when the context names that pdk/family/symptom
            continue
        allowed_list = allowed if isinstance(allowed, list) else [allowed]
        values = value if isinstance(value, (list, set, tuple)) else [value]
        if not any(v in allowed_list for v in values):
            return None
        score += 2 if key == 'symptoms' else 1
    return score


def find_cards(limit=6, include_superseded=False, **ctx):
    """Structured lookup. ctx keys: stage, symptoms, family, pdk, frequency_ghz, power_mw, type."""
    out = []
    want_type = ctx.pop('type', None)
    for card in load_cards().values():
        if card.get('lifecycle') == 'superseded' and not include_superseded:
            continue
        if want_type and card.get('type') != want_type:
            continue
        score = _matches(card, ctx)
        if score is None:
            continue
        if ctx.get('symptoms') and 'symptoms' not in card.get('applies', {}):
            score -= 1
        score += LIFECYCLE_BONUS.get(card.get('lifecycle'), 0.0)
        out.append((score, card))
    out.sort(key=lambda t: (-t[0], t[1]['id']))
    return [brief(c) for _, c in out[:limit]]


def brief(card):
    return {'id': card['id'], 'type': card['type'], 'title': card['title'], 'summary': card['summary'],
            'actions': card.get('actions', []), 'evidence': card.get('evidence', []),
            'confidence': card.get('confidence'), 'lifecycle': card.get('lifecycle')}


# ----------------------------------------------------------------------------------------- KB
def _kb(args, timeout=None):
    from . import site
    cmd = site.kb_command()
    if not cmd:
        raise RuntimeError('knowledge base not configured (site kb.command)')
    timeout = timeout or float(site.load().get('kb', {}).get('timeout_s', 60))
    r = subprocess.run(cmd + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError('kb %s failed: %s' % (args[0], r.stderr.strip()[-500:]))
    return json.loads(r.stdout)


def kb_show(record_id):
    if record_id in load_cards():
        return load_cards()[record_id]
    return _kb(['show', record_id])


def kb_search(query, mode='keyword', project=None, type=None, limit=5):
    args = ['search', query, '--mode', mode, '--limit', str(limit)]
    if project:
        args += ['--project', project]
    if type:
        args += ['--type', type]
    data = _kb(args, timeout=180 if mode != 'keyword' else None)
    hits = []
    for r in data.get('results', []):
        hits.append({'id': r.get('id'), 'type': r.get('type'), 'title': r.get('title'), 'project': r.get('project'),
                     'excerpt': (r.get('match') or {}).get('excerpt', '')[:300]})
    return hits


def resolve_ids(ids):
    """Which ids exist (cards, KB records, evidence point ids)? Returns {id: bool}."""
    out = {}
    cards = load_cards()
    evidence_ids = None
    for i in ids:
        if i in cards:
            out[i] = True
            continue
        if i.startswith('lookup:') or i.startswith(('demo_a_', 'demo_b_', 'demo_c_', 'demo_d_')):
            from .select import table_results
            key = i.split(':', 1)[-1]
            out[i] = any(r.get('key') == key for r in table_results())
            continue
        if evidence_ids is None:
            evidence_ids = _evidence_ids()
        if i in evidence_ids:
            out[i] = True
            continue
        try:
            rec = _kb(['show', i])
            out[i] = bool(rec and rec.get('id') == i)
        except Exception:
            out[i] = False
    return out


def _evidence_ids():
    from .site import evidence_indexes
    ids = set()
    for p in evidence_indexes():
        try:
            for line in Path(p).read_text().splitlines():
                if line.strip():
                    ids.add(json.loads(line)['id'])
        except OSError:
            pass
    return ids


# ----------------------------------------------------------------------------------------- consults
def consult(task_root, stage, context, query=None, kb_mode='keyword', limit=5):
    """Cards for the structured context (+ optional KB text search); record a receipt in the task."""
    ctx = dict(context)
    cards = find_cards(limit=limit, **ctx)
    kb_hits, kb_error = [], None
    if query:
        try:
            kb_hits = kb_search(query, mode=kb_mode, limit=limit)
        except Exception as exc:
            kb_error = str(exc)[:300]
    receipt = {'time': time.time(), 'stage': stage, 'context': {k: v for k, v in context.items()},
               'query': query, 'cards': [c['id'] for c in cards], 'kb_hits': [h['id'] for h in kb_hits]}
    if task_root is not None:
        path = Path(task_root) / 'knowledge' / 'consults.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(receipt, ensure_ascii=False) + '\n')
    out = {'stage': stage, 'cards': cards, 'kb_hits': kb_hits}
    if kb_error:
        out['kb_error'] = kb_error
    return out


def consult_log(task_root):
    path = Path(task_root) / 'knowledge' / 'consults.jsonl'
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding='utf-8').splitlines() if x.strip()]


# ----------------------------------------------------------------------------------------- CLI
def cli_register(sub):
    c = sub.add_parser('kb', help='knowledge: cards (structured, curated) and an optional user-configured knowledge backend',
                       description='knowledge: cards (structured, curated) and an optional user-configured knowledge backend')
    ks = c.add_subparsers(dest='kb_command', metavar='<kb-command>')
    ks.required = True
    k = ks.add_parser('cards', help='find curated cards by structured context')
    k.add_argument('--stage')
    k.add_argument('--symptom', action='append', dest='symptoms')
    k.add_argument('--family')
    k.add_argument('--pdk')
    k.add_argument('--frequency-ghz', type=float)
    k.add_argument('--power-mw', type=float)
    k.add_argument('--type', choices=CARD_TYPES)
    k.add_argument('--limit', type=int, default=8)
    k.add_argument('--task', help='record a consult receipt in this task workspace')
    k = ks.add_parser('show', help='show a card or KB record by id')
    k.add_argument('id')
    k = ks.add_parser('search', help='text/semantic search in an optional user-configured knowledge backend')
    k.add_argument('query')
    k.add_argument('--mode', default='keyword', choices=['keyword', 'hybrid', 'semantic'])
    k.add_argument('--project')
    k.add_argument('--type')
    k.add_argument('--limit', type=int, default=5)
    k.add_argument('--task')
    k = ks.add_parser('check', help='validate all curated cards')


def cli_dispatch(args):
    if args.command != 'kb':
        return None
    from .cli import EXIT_OK, EXIT_INVALID
    from .spec import canonical_pdk
    if args.kb_command == 'cards':
        ctx = {k: v for k, v in {'stage': args.stage, 'symptoms': args.symptoms, 'family': args.family,
                                 'pdk': canonical_pdk(args.pdk) if args.pdk else None,
                                 'frequency_ghz': args.frequency_ghz, 'power_mw': args.power_mw}.items() if v is not None}
        if args.task:
            out = consult(args.task, args.stage or 'manual', ctx)
            return out, EXIT_OK
        if args.type:
            ctx['type'] = args.type
        return {'cards': find_cards(limit=args.limit, **ctx)}, EXIT_OK
    if args.kb_command == 'show':
        return kb_show(args.id), EXIT_OK
    if args.kb_command == 'search':
        hits = kb_search(args.query, mode=args.mode, project=args.project, type=args.type, limit=args.limit)
        if args.task:
            path = Path(args.task) / 'knowledge' / 'consults.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps({'time': time.time(), 'stage': 'manual-search', 'query': args.query,
                                    'kb_hits': [h['id'] for h in hits]}, ensure_ascii=False) + '\n')
        return {'query': args.query, 'mode': args.mode, 'hits': hits}, EXIT_OK
    if args.kb_command == 'check':
        problems = {cid: validate_card(c) for cid, c in load_cards().items()}
        problems = {k: v for k, v in problems.items() if v}
        return {'cards': len(load_cards()), 'problems': problems}, (EXIT_OK if not problems else EXIT_INVALID)
    return None
