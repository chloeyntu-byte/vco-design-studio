"""Paper-style flat topology drawings that correspond one-to-one to the packaged schematic.

Each variant has `data/schematic/<family>/<variant>.ui.json`: device glyphs placed like the
paper figure, wires, rail symbols and labels.  Every glyph names the schematic object it
stands for (`ref`, e.g. "GM/MN1", "L_TANK", "CT_MAIN", "L_XFMR/LDA", "C0"); its terminal nets
are looked up in the packaged graph, and the drawing is checked with the same connectivity
extractor as the Virtuoso plan (schematic.connectivity_check).  So the UI drawing cannot show
a connection the schematic does not have, and every top-level schematic object appears.
Coordinates are canvas pixels (y down), on a 10 px grid.
"""
from __future__ import annotations
import copy
import json
import math
from pathlib import Path

from .common import ROOT, read_json
from . import schematic as S

PAPERS = ROOT / 'data' / 'papers'


def _shown_path(path):
    path = Path(path)
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return path.name


def figure_dir():
    """Folder with the user's own crops of the source figures (not distributed)."""
    import os
    env = os.environ.get('VCO_STUDIO_PAPER_FIGURES')
    return Path(env).expanduser() if env else PAPERS / 'figures'

# glyph terminal offsets (x, y) before mirroring/rotation; y down
GLYPHS = {
    'nmos': {'d': (0, -30), 'g': (-30, 0), 's': (0, 30)},
    'pmos': {'s': (0, -30), 'g': (-30, 0), 'd': (0, 30)},
    'cap': {'a': (0, -20), 'b': (0, 20)},
    'array': {'a': (0, -20), 'b': (0, 20)},
    'res': {'a': (0, -30), 'b': (0, 30)},
    'coil': {'a': (0, -40), 'b': (0, 40)},
    'ind_ct': {'P': (-70, 0), 'N': (70, 0), 'CT': (0, -30)},
    'vdd': {'a': (0, 0)},
    'gnd': {'a': (0, 0)},
    'pin': {'a': (0, 0)},
    'kbar': {},
}
DEFAULT_TERMS = {
    'nmos': {'d': 'd', 'g': 'g', 's': 's'}, 'pmos': {'d': 'd', 'g': 'g', 's': 's'},
    'cap': {'a': 'p', 'b': 'n'}, 'res': {'a': 'p', 'b': 'n'}, 'array': {'a': 'P', 'b': 'N'},
    'coil': {'a': 'p', 'b': 'n'}, 'ind_ct': {'P': 'P', 'N': 'N', 'CT': 'CT'},
}


def _rot(dx, dy, rot, mirror):
    if mirror:
        dx = -dx
    for _ in range((rot // 90) % 4):
        dx, dy = -dy, dx
    return dx, dy


def glyph_pins(g):
    out = {}
    for t, (dx, dy) in GLYPHS[g['g']].items():
        dx, dy = _rot(dx, dy, g.get('rot', 0), g.get('mirror', False))
        out[t] = (g['at'][0] + dx, g['at'][1] + dy)
    return out


# ---------------------------------------------------------------- net resolution

def _top_inst(packaged, name):
    return next((i for i in packaged['instances'] if i['name'] == name), None)


def resolve_ref(packaged, ref):
    """Return {graph terminal: top net} for a ref like 'GM/MN1', 'L_TANK', 'C0', 'L_XFMR/LDA'."""
    subs = {s['name']: s for s in packaged['subcircuits']}
    parts = ref.split('/')
    inst = _top_inst(packaged, parts[0])
    if inst is None:
        raise KeyError('No top-level instance ' + parts[0])
    if len(parts) == 1:
        return {k: str(v) for k, v in inst['nodes'].items()}
    sub = subs[inst['master']]
    member = next((i for i in sub['instances'] if i['name'] == parts[1]), None)
    if member is None:
        raise KeyError('No member %s in %s' % (parts[1], sub['name']))
    ports = {p['name'] for p in sub['ports']}

    def up(net):
        net = str(net)
        return str(inst['nodes'][net]) if net in ports else parts[0] + '.' + net
    nodes = dict(member['nodes'])
    if member['role'] == 'ind':
        # a winding glyph stands for series loss + winding + parallel loss: use the outer ends
        rs = next((i for i in sub['instances'] if i['name'] == 'RSL' + member['name'][1:]), None)
        if rs is not None and rs['nodes']['n'] == nodes['p']:
            nodes['p'] = rs['nodes']['p']
    return {k: up(v) for k, v in nodes.items()}


def drawing_cell(spec, packaged):
    """Turn a UI drawing spec into a connectivity_check cell."""
    insts = []
    for g in spec['glyphs']:
        pins = glyph_pins(g)
        if g['g'] in ('vdd', 'gnd', 'pin', 'kbar'):
            continue
        nets = resolve_ref(packaged, g['ref'])
        terms = g.get('terms', DEFAULT_TERMS[g['g']])
        nodes = {}
        for gt, xy in pins.items():
            gterm = terms.get(gt)
            if gterm is None:
                raise KeyError('%s: drawn terminal %s of %s has no schematic terminal mapping' % (g.get('id', g['ref']), gt, g['g']))
            if gterm not in nets:
                raise KeyError('%s: %s has no terminal %s (has %s)' % (g.get('id', g['ref']), g['ref'], gterm, sorted(nets)))
            nodes[gt] = nets[gterm]
        insts.append({'name': g.get('id', g['ref']), 'nodes': nodes, 'pins': {t: pins[t] for t in nodes}})
    labels = list(spec.get('labels', []))
    for g in spec['glyphs']:
        if g['g'] in ('vdd', 'gnd', 'pin'):
            labels.append({'net': g['net'], 'xy': list(glyph_pins(g)['a'])})
    wires = [{'net': w.get('net'), 'pts': [list(p) for p in w['pts']]} for w in spec.get('wires', [])]
    # rail symbols and pins sit on a wire end: treat them as labels on that wire
    return {'instances': insts, 'wires': wires, 'labels': labels, 'pins': []}


def coverage(spec, packaged):
    """Every top-level schematic object must appear (whole, or through its members)."""
    refs = [g['ref'] for g in spec['glyphs'] if 'ref' in g]
    subs = {s['name']: s for s in packaged['subcircuits']}
    missing = []
    for inst in packaged['instances']:
        name = inst['name']
        if name in refs:
            continue
        members = [r.split('/', 1)[1] for r in refs if r.startswith(name + '/')]
        if not members:
            missing.append(name)
            continue
        sub = subs.get(inst.get('master'))
        if sub:
            need = [i['name'] for i in sub['instances'] if i['role'] in ('nmos', 'pmos', 'ind', 'cap')]
            lack = [m for m in need if m not in members]
            if lack:
                missing.append(name + '/' + ','.join(lack))
    dup = sorted({r for r in refs if refs.count(r) > 1})
    return {'ok': not missing and not dup, 'missing': missing, 'duplicates': dup}


def ui_spec_path(family, variant):
    return S.pack_file(family, variant, '.ui.json') or S.SPEC_DIR / family / (variant + '.ui.json')


def load_ui(family, variant):
    p = ui_spec_path(family, variant)
    return read_json(p) if p.is_file() else None


def packaged_for(family, variant, graph=None):
    from .catalog import get_variant
    graph = graph or get_variant(family, variant)['graph']
    packaged, full = S.package_variant(graph, S.load_spec(family, variant, graph))
    return packaged, full


def check_ui(family, variant, spec=None):
    spec = spec or load_ui(family, variant)
    packaged, _ = packaged_for(family, variant)
    cell = drawing_cell(spec, packaged)
    conn = S.connectivity_check(cell)
    cov = coverage(spec, packaged)
    return {'ok': conn['ok'] and cov['ok'], 'connectivity': conn, 'coverage': cov}


def papers_for(family, variant):
    idx = read_json(PAPERS / 'index.json')
    out = []
    for p in idx['papers']:
        if p['family'] != family:
            continue
        for f in p['figures']:
            if variant in f.get('variants', []) or not f.get('variants'):
                out.append(dict(f, citation=p['citation'], doi=p['doi'], paper=p['id'],
                                path=str(figure_dir() / f['file'])))
    return out


# ---------------------------------------------------------------- drawing primitives

def draw_ops(spec, packaged=None):
    """Abstract draw list: ('line', pts, style) ('oval', box, style) ('text', xy, text, style) ('poly', pts, style)."""
    ops = []

    def L(pts, **st):
        ops.append(('line', [tuple(p) for p in pts], st))

    def T(xy, text, **st):
        ops.append(('text', tuple(xy), text, st))
    for g in spec['glyphs']:
        x0, y0 = g['at']
        rot, mir = g.get('rot', 0), g.get('mirror', False)
        tag = g.get('ref') or g.get('net')

        def P(dx, dy):
            dx, dy = _rot(dx, dy, rot, mir)
            return (x0 + dx, y0 + dy)

        def l(*pts, **st):
            L([P(*p) for p in pts], tag=tag, **st)
        k = g['g']
        if k in ('nmos', 'pmos'):
            l((-8, -20), (-8, 20), w=3)
            l((-15, -15), (-15, 15), w=2)
            l((-30, 0), (-15, 0) if k == 'nmos' else (-21, 0))
            if k == 'pmos':
                c = P(-18, 0)
                ops.append(('oval', (c[0] - 3, c[1] - 3, c[0] + 3, c[1] + 3), {'tag': tag}))
            l((-8, -12), (0, -12), (0, -30))
            l((-8, 12), (0, 12), (0, 30))
            if k == 'nmos':
                l((-6, 12), (-1, 8))
                l((-6, 12), (-1, 16))
            else:
                l((-2, -12), (-7, -8))
                l((-2, -12), (-7, -16))
        elif k in ('cap', 'array'):
            l((0, -20), (0, -4))
            l((0, 4), (0, 20))
            l((-14, -4), (14, -4), w=3)
            l((-14, 4), (14, 4), w=3)
            if k == 'array':
                l((-16, 14), (16, -14))
                l((16, -14), (8, -13))
                l((16, -14), (15, -6))
        elif k == 'res':
            pts = [(0, -30), (0, -18)]
            for i in range(6):
                pts.append(((7 if i % 2 == 0 else -7), -15 + i * 6))
            pts += [(0, 18), (0, 30)]
            l(*pts)
        elif k == 'coil':
            l((0, -40), (0, -24))
            l((0, 24), (0, 40))
            side = -1 if g.get('bump', 'right') == 'left' else 1
            for i in range(4):
                c = -18 + i * 12
                pts = [(side * 7 * math.sin(math.pi * t / 10), c - 6 * math.cos(math.pi * t / 10)) for t in range(11)]
                l(*pts)
            if g.get('dot'):
                d = P(-side * 8, -26 if g['dot'] == 'a' else 26)
                ops.append(('oval', (d[0] - 3, d[1] - 3, d[0] + 3, d[1] + 3), {'tag': tag, 'fill': True}))
        elif k == 'ind_ct':
            for sgn in (-1, 1):
                l((sgn * 70, 0), (sgn * 58, 0))
                for i in range(4):
                    c = sgn * (52 - i * 12)
                    pts = [(c - 6 * math.cos(math.pi * t / 10), -7 * math.sin(math.pi * t / 10)) for t in range(11)]
                    l(*pts)
                l((sgn * 10, 0), (sgn * 4, 0))
            l((-4, 0), (4, 0))
            l((0, 0), (0, -30))
        elif k == 'kbar':
            n = g.get('length', 60)
            l((-3, -n / 2), (-3, n / 2), w=2)
            l((3, -n / 2), (3, n / 2), w=2)
            if g.get('text'):
                T(P(8, -n / 2), g['text'], anchor='w', color='#8a4f9e', tag=tag, small=True)
            continue
        elif k == 'vdd':
            l((-12, 0), (12, 0), w=3)
            T(P(0, -12), g['net'], anchor='s', color='#a43a1d', tag=tag)
            continue
        elif k == 'gnd':
            l((0, 0), (0, 6))
            l((-12, 6), (12, 6), w=2)
            l((-7, 11), (7, 11))
            l((-3, 16), (3, 16))
            continue
        elif k == 'pin':
            c = P(0, 0)
            ops.append(('oval', (c[0] - 4, c[1] - 4, c[0] + 4, c[1] + 4), {'tag': tag, 'fill': True}))
            T((c[0] + (8 if not g.get('left') else -8), c[1]), g['net'], anchor='w' if not g.get('left') else 'e', color='#a43a1d', tag=tag)
            continue
        lab = g.get('label', g.get('ref', ''))
        lx, ly = g.get('label_at', (0, 0)) if 'label_at' in g else (None, None)
        if lx is None:
            off = {'nmos': (8, 0), 'pmos': (8, 0), 'cap': (18, 0), 'array': (20, 0), 'res': (12, 0), 'coil': (14, 0), 'ind_ct': (0, 16)}[k]
            if mir and k in ('nmos', 'pmos'):
                off = (-8, 0)
            px, py = (x0 + (off[0] if not mir else off[0]), y0 + off[1]) if k not in ('nmos', 'pmos') else (x0 + off[0], y0 + off[1])
            anchor = 'n' if k == 'ind_ct' else ('e' if (mir and k in ('nmos', 'pmos')) else 'w')
        else:
            px, py = x0 + lx, y0 + ly
            anchor = g.get('anchor', 'w')
        T((px, py), lab, anchor=anchor, color='#7a4a10', tag=tag, small=True)
    for w in spec.get('wires', []):
        L(w['pts'], tag='net:' + str(w.get('net')), wire=True, net=w.get('net'))
    # junction dots where a wire vertex meets the middle of another wire or 3+ ends meet
    ends = {}
    segs = []
    for w in spec.get('wires', []):
        pts = [tuple(p) for p in w['pts']]
        for p in pts:
            ends[p] = ends.get(p, 0) + 1
        segs += list(zip(pts, pts[1:]))
    for p, n in ends.items():
        mid = any(S._on_segment(p, a, b) and p != a and p != b for a, b in segs)
        if n >= 3 or mid:
            ops.append(('oval', (p[0] - 3.5, p[1] - 3.5, p[0] + 3.5, p[1] + 3.5), {'fill': True, 'wire': True}))
    for lab in spec.get('labels', []):
        T((lab['xy'][0] + 4, lab['xy'][1] - 4), lab['net'], anchor='sw', color='#1e6b8a', tag='net:' + lab['net'])
    for t in spec.get('texts', []):
        T(tuple(t['xy']), t['text'], anchor=t.get('anchor', 'w'), color=t.get('color', '#55687c'), small=True, note=True)
    return ops


def render_png(spec, path, scale=2.0, title=None):
    from PIL import Image, ImageDraw, ImageFont
    ops = draw_ops(spec)
    W, H = spec.get('size', [1000, 760])
    img = Image.new('RGB', (int(W * scale), int(H * scale)), '#fbfcfe')
    d = ImageDraw.Draw(img)

    def font(sz):
        for f in ('/usr/share/fonts/wqy-microhei/wqy-microhei.ttc', '/usr/share/fonts/dejavu/DejaVuSans.ttf'):
            try:
                return ImageFont.truetype(f, int(sz * scale))
            except OSError:
                pass
        return ImageFont.load_default()
    for op in ops:
        if op[0] == 'line':
            st = op[2]
            col = '#2f6f92' if st.get('wire') else '#22344a'
            d.line([(x * scale, y * scale) for x, y in op[1]], fill=col, width=int(st.get('w', 2) * scale * (1.0 if not st.get('wire') else 0.9)))
        elif op[0] == 'oval':
            b = op[1]
            st = op[2]
            d.ellipse([v * scale for v in b], outline='#22344a', fill='#2f6f92' if st.get('fill') else '#fbfcfe', width=int(1.5 * scale))
    for op in ops:
        if op[0] != 'text':
            continue
        (x, y), text, st = op[1], op[2], op[3]
        f = font(11 if st.get('small') else 12)
        tw, th = d.textbbox((0, 0), text, font=f)[2:]
        a = st.get('anchor', 'w')
        dx = {'w': 0, 'e': -tw, 'n': -tw / 2, 's': -tw / 2, 'sw': 0, 'center': -tw / 2}.get(a, 0)
        dy = {'w': -th / 2, 'e': -th / 2, 'n': 0, 's': -th, 'sw': -th, 'center': -th / 2}.get(a, -th / 2)
        d.text((x * scale + dx, y * scale + dy), text, fill=st.get('color', '#22344a'), font=f)
    if title:
        d.text((10, 8), title, fill='#22344a', font=font(14))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return str(path)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='Paper-style topology drawing: check / render')
    ap.add_argument('cmd', choices=['check', 'render'])
    ap.add_argument('family')
    ap.add_argument('variant')
    ap.add_argument('--spec')
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    spec = read_json(a.spec) if a.spec else load_ui(a.family, a.variant)
    r = check_ui(a.family, a.variant, spec)
    if a.cmd == 'render':
        r['png'] = render_png(spec, a.out or '/tmp/topology.png', title='%s / %s' % (a.family, a.variant))
    print(json.dumps(r, indent=1, ensure_ascii=False))
    return 0 if r['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())


# ---------------------------------------------------------------- Tk viewer

def _tk_viewer_class():
    import tkinter as tk
    from tkinter import ttk, font as tkfont

    from . import fieldwork_tk as fw
    plot = fw.tk_plot_colors()
    remap = {'#22344a': fw.TEXT, '#2f6f92': fw.SERIES[0], '#7a4a10': fw.DIM, '#a43a1d': fw.accent('vco'),
             '#1e6b8a': fw.SERIES[0], '#8a4f9e': fw.SERIES[4], '#55687c': fw.DIM, '#fbfcfe': plot['bg'], '#bd5b24': fw.accent('vco')}

    def col(c):
        return remap.get(c, c)

    class TopologyViewer(ttk.Frame):
        """Paper-style drawing (left) + source figure and citation (right) + click details."""
        def __init__(self, parent):
            super().__init__(parent)
            self.family = self.variant = None
            self.spec = None
            self.packaged = None
            self.factor = 1.0
            self._img = None
            self.font_family = tkfont.nametofont('TkDefaultFont').actual()['family']
            bar = ttk.Frame(self)
            bar.pack(fill='x', pady=(0, 4))
            self.title = ttk.Label(bar, text='', font=(self.font_family, 11, 'bold'))
            self.title.pack(side='left')
            ttk.Button(bar, text='Fit', command=self.fit).pack(side='right')
            ttk.Button(bar, text='+', width=3, command=lambda: self.zoom(1.2)).pack(side='right')
            ttk.Button(bar, text='-', width=3, command=lambda: self.zoom(1 / 1.2)).pack(side='right')
            self.notes = ttk.Label(self, text='', foreground=fw.DIM, wraplength=820, justify='left')
            self.notes.pack(fill='x')
            panes = ttk.Panedwindow(self, orient='horizontal')
            panes.pack(fill='both', expand=True)
            left = ttk.Frame(panes)
            right = ttk.Frame(panes, width=360)
            panes.add(left, weight=3)
            panes.add(right, weight=1)
            self._panes = panes
            self._sash_done = False
            panes.bind('<Configure>', self._initial_sash)
            self.canvas = tk.Canvas(left, background=col('#fbfcfe'), highlightthickness=0)
            self.canvas.pack(fill='both', expand=True)
            self.canvas.bind('<Configure>', lambda e: self.fit())
            self.canvas.bind('<ButtonPress-2>', lambda e: self.canvas.scan_mark(e.x, e.y))
            self.canvas.bind('<B2-Motion>', lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
            figbar = ttk.Frame(right)
            figbar.pack(fill='x')
            ttk.Label(figbar, text='Source figure').pack(side='left')
            self.figure_box = ttk.Combobox(figbar, state='readonly', width=34)
            self.figure_box.pack(side='left', padx=6, fill='x', expand=True)
            self.figure_box.bind('<<ComboboxSelected>>', lambda e: self._show_figure())
            self.figure = tk.Canvas(right, background=fw.PLOT_BG, highlightthickness=1, highlightbackground=fw.LINE, height=300)
            self.figure.pack(fill='both', expand=True, pady=4)
            self.figure.bind('<Configure>', lambda e: self._show_figure())
            self.citation = tk.Text(right, height=8, wrap='word', relief='flat', background=fw.RAISED, foreground=fw.TEXT, padx=8, pady=4, font=(self.font_family, 9))
            self.citation.pack(fill='x')
            self.detail = tk.Text(self, height=3, wrap='word', relief='flat', background=fw.RAISED, foreground=fw.TEXT, padx=8, pady=4, font=(self.font_family, 9))
            self.detail.insert('1.0', 'Select a device to see its schematic instance path, module cell and terminal nets.')
            self.detail.pack(fill='x', pady=(4, 0))
            self.figures = []

        def _initial_sash(self, event):
            # paper panel takes ~30 % (180..420 px); re-applied when the window is resized
            if event.width > 300 and getattr(self, '_last_w', None) != event.width:
                self._last_w = event.width
                right = int(min(420, max(180, event.width * 0.30)))
                self._panes.sashpos(0, event.width - right)

        def set_variant(self, family, variant, graph=None):
            self.family, self.variant = family, variant
            self.spec = load_ui(family, variant)
            try:
                self.packaged, _ = packaged_for(family, variant, graph)
            except Exception:
                self.packaged = None
            self.figures = papers_for(family, variant)
            self.title.configure(text='%s / %s - topology after the source paper (labels = generated schematic instance paths)' % (family, variant))
            names = ['%s  %s  p.%s' % (f['paper'], f['label'], f['pdf_page']) for f in self.figures]
            self.figure_box.configure(values=names)
            if self.spec and self.spec.get('figure'):
                idx = next((i for i, f in enumerate(self.figures) if f['file'] == self.spec['figure']), 0)
            else:
                idx = 0
            if names:
                self.figure_box.current(idx)
            else:
                self.figure_box.set('(reference baseline, no source figure)')
            self.notes.configure(text='\n'.join(t['text'] for t in (self.spec or {}).get('texts', [])))
            self.render()
            self.after_idle(self.fit)
            self._show_figure()

        def render(self):
            c = self.canvas
            c.delete('all')
            self.factor = 1.0
            if not self.spec:
                c.create_text(20, 20, anchor='nw', text='No topology drawing installed for this variant. Structure packs are user-provided: set VCO_STUDIO_SCHEMATIC_DIR (see examples/schematic-pack).', fill=fw.DIM, width=700)
                return
            for op in draw_ops(self.spec):
                st = op[-1]
                tags = (st['tag'],) if st.get('tag') else ()
                if op[0] == 'line':
                    pts = [v for p in op[1] for v in p]
                    c.create_line(*pts, fill=col('#2f6f92') if st.get('wire') else col('#22344a'), width=st.get('w', 2) if not st.get('wire') else 2, tags=tags + (('wire',) if st.get('wire') else ('dev',)), capstyle='round')
                elif op[0] == 'oval':
                    c.create_oval(*op[1], outline=col('#22344a'), fill=col('#2f6f92') if st.get('fill') else col('#fbfcfe'), tags=tags)
                elif op[0] == 'text' and st.get('note'):
                    continue
                elif op[0] == 'text':
                    c.create_text(*op[1], text=op[2], anchor=st.get('anchor', 'w'), fill=col(st.get('color', '#22344a')),
                                  font=(self.font_family, 10 if st.get('small') else 11), tags=tags + ('label',))
            for g in self.spec['glyphs']:
                if g.get('ref'):
                    c.tag_bind(g['ref'], '<Button-1>', lambda e, g=g: self.inspect(g))
            c.configure(scrollregion=(0, 0) + tuple(self.spec.get('size', [1000, 760])))

        def fit(self):
            if not self.spec or self.canvas.winfo_width() < 50:
                return
            W, H = self.spec.get('size', [1000, 760])
            f = max(.35, min(2.0, min((self.canvas.winfo_width() - 10) / W, (self.canvas.winfo_height() - 10) / H)))
            self.render()
            self.zoom(f)

        def zoom(self, ratio):
            new = max(.3, min(3, self.factor * ratio))
            r = new / self.factor
            self.canvas.scale('all', 0, 0, r, r)
            self.factor = new
            for item in self.canvas.find_withtag('label'):
                self.canvas.itemconfigure(item, font=(self.font_family, max(7, int(10 * self.factor))))
            bb = self.canvas.bbox('all')
            if bb:
                self.canvas.configure(scrollregion=bb)

        def inspect(self, g):
            text = g['ref']
            if self.packaged is not None:
                try:
                    nets = resolve_ref(self.packaged, g['ref'])
                    top = g['ref'].split('/')[0]
                    inst = next(i for i in self.packaged['instances'] if i['name'] == top)
                    where = ('module cell %s' % inst['master']) if inst['role'] == 'subckt' else ('top-level %s' % inst['role'])
                    text = '%s  -  %s\nterminal -> top-level net: %s' % (g['ref'], where, ', '.join('%s->%s' % kv for kv in nets.items()))
                except Exception as exc:
                    text = '%s (%s)' % (g['ref'], exc)
            self.detail.configure(state='normal')
            self.detail.delete('1.0', 'end')
            self.detail.insert('1.0', text)
            self.detail.configure(state='disabled')
            for item in self.canvas.find_withtag('dev'):
                self.canvas.itemconfigure(item, fill=col('#22344a'))
            for item in self.canvas.find_withtag(g['ref']):
                if self.canvas.type(item) == 'line':
                    self.canvas.itemconfigure(item, fill=col('#bd5b24'))

        def _show_figure(self):
            self.figure.delete('all')
            self.citation.configure(state='normal')
            self.citation.delete('1.0', 'end')
            i = self.figure_box.current()
            if not self.figures or i < 0:
                self.citation.insert('1.0', 'Reference baseline (conventional cross-coupled LC VCO); no source figure.')
                self.citation.configure(state='disabled')
                return
            f = self.figures[i]
            shown = False
            if Path(f['path']).is_file():
                try:
                    from PIL import Image, ImageTk
                    im = Image.open(f['path'])
                    w, h = max(50, self.figure.winfo_width() - 8), max(50, self.figure.winfo_height() - 8)
                    im.thumbnail((w, h))
                    self._img = ImageTk.PhotoImage(im)
                    self.figure.create_image(w / 2 + 4, h / 2 + 4, image=self._img)
                    shown = True
                except Exception as exc:
                    self.figure.create_text(12, 12, anchor='nw', fill=fw.WARN, text='Cannot show the figure: %s' % exc)
            if not shown:
                self.figure.create_text(12, 12, anchor='nw', fill=fw.DIM, width=max(150, self.figure.winfo_width() - 24),
                                        text='Figure images are not distributed. Put your own crop of this figure at\n%s\n(or set VCO_STUDIO_PAPER_FIGURES to a folder with it).' % _shown_path(f['path']))
            jp = (', journal page %s' % f['journal_page']) if f.get('journal_page') else ''
            self.citation.insert('1.0', '%s - %s (PDF page %s%s)\n\n%s\nDOI %s\nThe paper and its figures remain under the publisher\'s copyright.' % (
                f['label'], f['caption'], f['pdf_page'], jp, f['citation'], f['doi']))
            self.citation.configure(state='disabled')

    return TopologyViewer


def TopologyViewer(parent):
    return _tk_viewer_class()(parent)


def cli_register(sub):
    h = 'paper-style topology drawing of a variant: check it against the packaged schematic or render a PNG'
    c = sub.add_parser('topology', help=h, description=h)
    c.add_argument('action', choices=['check', 'render'])
    c.add_argument('family')
    c.add_argument('variant')
    c.add_argument('--spec', help='drawing file (default data/schematic/<family>/<variant>.ui.json)')
    c.add_argument('--out', help='PNG path for render')


def cli_dispatch(args):
    if args.command != 'topology':
        return None
    from .cli import EXIT_OK, EXIT_INVALID
    spec = read_json(args.spec) if args.spec else load_ui(args.family, args.variant)
    if spec is None:
        raise ValueError('no topology drawing for %s/%s' % (args.family, args.variant))
    r = check_ui(args.family, args.variant, spec)
    if args.action == 'render':
        if not args.out:
            raise ValueError('render needs --out')
        r['png'] = render_png(spec, args.out, title='%s / %s' % (args.family, args.variant))
    return r, (EXIT_OK if r['ok'] else EXIT_INVALID)
