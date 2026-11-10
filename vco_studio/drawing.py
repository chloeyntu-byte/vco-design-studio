"""Electrical graph viewer. Every symbol and named wire comes from the shared graph."""
import tkinter as tk
from tkinter import ttk, font as tkfont

from . import fieldwork_tk as fw
PLOT = fw.tk_plot_colors()
INK = fw.TEXT
WIRE = fw.SERIES[0]
ACCENT = fw.accent('vco')


def graph_views(graph):
    """Return deterministic hierarchical views without changing electrical data."""
    result = [(graph.get('name', 'core'), graph)]
    for child in graph.get('subcircuits', []):
        result.extend(graph_views(child))
    return result


def connectivity(graph):
    nets = {}
    for instance in graph.get('instances', []):
        for terminal, net in instance.get('nodes', {}).items():
            nets.setdefault(str(net), []).append((instance['name'], terminal))
    return nets


class CircuitViewer(ttk.Frame):
    """Scrollable primitive schematic using explicit electrical net labels.

    Labeled wires are deliberately used instead of ambiguous crossed auto-wires.
    Clicking any device highlights its nets and displays exact graph data.
    """
    def __init__(self, parent):
        super().__init__(parent)
        self.font_family = tkfont.nametofont('TkDefaultFont').actual()['family']
        self.graph = {}
        self.views = []
        self.scale_factor = 1.0
        bar = ttk.Frame(self)
        bar.pack(fill='x', pady=(0, 4))
        ttk.Label(bar, text='Hierarchy').pack(side='left')
        self.selection = ttk.Combobox(bar, state='readonly', width=25)
        self.selection.pack(side='left', padx=8)
        self.selection.bind('<<ComboboxSelected>>', self._select)
        ttk.Button(bar, text='−', width=3, command=lambda: self.zoom(.8)).pack(side='left')
        ttk.Button(bar, text='+', width=3, command=lambda: self.zoom(1.25)).pack(side='left')
        ttk.Button(bar, text='Reset', command=self.reset).pack(side='left', padx=4)
        self.count = ttk.Label(bar)
        self.count.pack(side='right')
        holder = ttk.Frame(self)
        holder.pack(fill='both', expand=True)
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(holder, background=PLOT['bg'], highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        xbar = ttk.Scrollbar(holder, orient='horizontal', command=self.canvas.xview)
        ybar = ttk.Scrollbar(holder, command=self.canvas.yview)
        xbar.grid(row=1, column=0, sticky='ew')
        ybar.grid(row=0, column=1, sticky='ns')
        self.canvas.configure(xscrollcommand=xbar.set, yscrollcommand=ybar.set)
        self.canvas.bind('<ButtonPress-2>', lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind('<B2-Motion>', lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        self.source_note = tk.StringVar(value='Explicit graph connectivity; synthetic dimensions require target validation.')
        ttk.Label(self, textvariable=self.source_note, wraplength=700, foreground=fw.WARN).pack(fill='x', pady=(4, 0))
        self.detail = tk.Text(self, height=5, wrap='word', spacing1=2, spacing3=2, bg=fw.RAISED, relief='flat', padx=8, pady=8)
        self.detail.pack(fill='x', pady=(4, 0))
        self._detail('Matching net labels are connected. Select a device to inspect terminals; middle-drag to pan.')

    def set_graph(self, graph):
        self.views = graph_views(graph)
        self.selection.configure(values=[name for name, _ in self.views])
        self.selection.current(0)
        self._select()

    def _select(self, _event=None):
        index = self.selection.current()
        if index < 0:
            return
        self.graph = self.views[index][1]
        self.scale_factor = 1
        self.render()

    def reset(self):
        self.scale_factor = 1
        self.render()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def zoom(self, ratio):
        new = max(.45, min(2.5, self.scale_factor * ratio))
        ratio = new / self.scale_factor
        self.canvas.scale('all', 0, 0, ratio, ratio)
        self.scale_factor = new
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def _detail(self, text):
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', text)
        self.detail.configure(state='disabled')

    def render(self):
        c = self.canvas
        c.delete('all')
        # Electrical graph stays untouched; show active core before passive detail.
        rank = {'nmos': 0, 'pmos': 0, 'ind': 1, 'mutual': 2, 'cap': 3, 'subckt': 4, 'res': 5}
        instances = sorted(self.graph.get('instances', []), key=lambda item: rank.get(item.get('role'), 6))
        nets = connectivity(self.graph)
        self.count.configure(text='{} devices · {} nets'.format(len(instances), len(nets)))
        c.create_text(24, 18, text=self.graph.get('name', 'Circuit'), anchor='nw', fill=INK, font=(self.font_family, 13, 'bold'))
        ports = ', '.join(p['name'] if isinstance(p, dict) else str(p) for p in self.graph.get('ports', []))
        c.create_text(24, 47, text='Ports: ' + ports, width=820, anchor='nw', fill=WIRE, font=(self.font_family, 10))
        columns = 3
        for index, instance in enumerate(instances):
            x, y = 32 + (index % columns) * 280, 102 + (index // columns) * 200
            self._symbol(instance, x, y)
        c.configure(scrollregion=(0, 0, 870, max(360, 104 + ((len(instances) + 2) // 3) * 200)))
        self._detail('Net labels define connections; 0 is global ground. Mutual l1/l2 reference inductor instances.\n' + '\n'.join(str(n) for n in self.graph.get('notes', [])[:2]))

    def _symbol(self, instance, x, y):
        c = self.canvas
        role = instance.get('role', '')
        tag = 'instance:' + instance['name']
        def line(*coords, **kw):
            return c.create_line(*coords, fill=INK, width=2, tags=(tag,), **kw)
        def text(tx, ty, value, **kw):
            return c.create_text(tx, ty, text=value, fill=INK, tags=(tag,), font=(self.font_family, 10), **kw)
        c.create_rectangle(x-8, y-10, x+254, y+174, outline=fw.LINE, fill=fw.PANEL, tags=(tag,))
        text(x, y, instance['name'] + '  ·  ' + role, anchor='nw')
        cx, cy = x+126, y+82
        anchors = {}
        if role in ('nmos', 'pmos'):
            line(cx-10, cy-25, cx-10, cy+25)
            line(cx, cy-25, cx, cy+25)
            line(cx, cy-20, cx+28, cy-20, cx+28, cy-42)
            line(cx, cy+20, cx+28, cy+20, cx+28, cy+42)
            line(cx-10, cy, cx-46, cy)
            line(cx+3, cy, cx+43, cy, arrow='first' if role == 'nmos' else 'last')
            if role == 'pmos':
                c.create_oval(cx-21, cy-5, cx-11, cy+5, fill='white', outline=INK, tags=(tag,))
            anchors = {'d': (cx+28, cy-42, 'n'), 'g': (cx-46, cy, 'w'), 's': (cx+28, cy+42, 's'), 'b': (cx+43, cy, 'e')}
        elif role == 'cap':
            line(cx-42, cy, cx-7, cy)
            line(cx-7, cy-22, cx-7, cy+22)
            line(cx+7, cy-22, cx+7, cy+22)
            line(cx+7, cy, cx+42, cy)
            anchors = {'p': (cx-42, cy, 'w'), 'n': (cx+42, cy, 'e'), 'b': (cx, cy+36, 's')}
            if 'b' in instance.get('nodes', {}):
                line(cx, cy+24, cx, cy+36)
        elif role in ('res', 'ind'):
            line(cx-50, cy, cx-32, cy)
            line(cx+32, cy, cx+50, cy)
            if role == 'res':
                coords = [cx-32, cy]
                for n in range(8):
                    coords.extend((cx-28+n*8, cy + (-10 if n % 2 == 0 else 10)))
                coords.extend((cx+32, cy))
                line(*coords)
            else:
                for n in range(4):
                    c.create_arc(cx-32+n*16, cy-12, cx-16+n*16, cy+12, start=0, extent=180, style='arc', outline=INK, width=2, tags=(tag,))
            anchors = {'p': (cx-50, cy, 'w'), 'n': (cx+50, cy, 'e')}
        elif role in ('vsource', 'isource'):
            c.create_oval(cx-22, cy-22, cx+22, cy+22, outline=INK, width=2, tags=(tag,))
            line(cx, cy-42, cx, cy-22)
            line(cx, cy+22, cx, cy+42)
            if role == 'vsource':
                text(cx, cy-10, '+')
                text(cx, cy+10, '−')
            else:
                line(cx, cy+14, cx, cy-14, arrow='last')
            anchors = {'p': (cx, cy-42, 'n'), 'n': (cx, cy+42, 's')}
        elif role == 'mutual':
            text(cx, cy-10, 'L1  ⇄  L2')
            text(cx, cy+13, 'magnetic coupling')
            anchors = {'l1': (cx-50, cy+35, 'w'), 'l2': (cx+50, cy+35, 'e')}
        else:
            c.create_rectangle(cx-42, cy-30, cx+42, cy+30, outline=INK, width=2, tags=(tag,))
            text(cx, cy, instance.get('master', role), width=80)
            nodes = list(instance.get('nodes', {}))
            for n, terminal in enumerate(nodes):
                left = n % 2 == 0
                anchors[terminal] = (cx+(-60 if left else 60), cy-30+(n//2)*20, 'w' if left else 'e')
                line(cx+(-42 if left else 42), cy-30+(n//2)*20, anchors[terminal][0], anchors[terminal][1])
        for terminal, net in instance.get('nodes', {}).items():
            ax, ay, direction = anchors.get(terminal, (cx, cy+55, 's'))
            dx, dy = {'w': (-6, 0), 'e': (6, 0), 'n': (0, -5), 's': (0, 5)}[direction]
            anchor = {'w': 'e', 'e': 'w', 'n': 's', 's': 'n'}[direction]
            nettag = 'net:' + str(net)
            c.create_oval(ax-2, ay-2, ax+2, ay+2, fill=WIRE, outline=WIRE, tags=(tag, nettag))
            c.create_text(ax+dx, ay+dy, text='{}:{}'.format(terminal, net), anchor=anchor, fill=WIRE, font=(self.font_family, 9), tags=(tag, nettag))
        values = list(instance.get('parameters', {}).items())
        params = ', '.join('{}={}'.format(k, format(v, '.4g') if isinstance(v, (int, float)) else v) for k, v in values[:3])
        if len(values) > 3:
            params += '  … (click)' 
        c.create_text(x+5, y+150, text=params, width=245, anchor='nw', fill=fw.DIM, font=(self.font_family, 9), tags=(tag,))
        c.tag_bind(tag, '<Button-1>', lambda event, item=instance: self.inspect(item))

    def inspect(self, instance):
        for item in self.canvas.find_all():
            if any(tag.startswith('net:') for tag in self.canvas.gettags(item)):
                self.canvas.itemconfigure(item, fill=WIRE)
        nets = connectivity(self.graph)
        rows = []
        for terminal, net in instance.get('nodes', {}).items():
            self.canvas.itemconfigure('net:' + str(net), fill=ACCENT)
            rows.append('{} → {}  [{}]'.format(terminal, net, ', '.join(name+'.'+pin for name, pin in nets[str(net)])))
        self._detail(instance['name'] + ' (' + instance.get('role', '') + ')\n' + '\n'.join(rows) + '\nParameters: ' + str(instance.get('parameters', {})))


def concept_model(variant):
    """Graph-derived overview. Only explicit winding-loss resistor nodes collapse.

    Canonical electrical graph is never modified. The overview omits body and
    digital control wiring and draws magnetic coupling as a distinct relation.
    """
    graph = variant['graph']
    instances = graph['instances']
    collapse = {}
    for item in instances:
        if item['role'] == 'res' and item['name'].startswith('RSL'):
            p, n = item['nodes']['p'], item['nodes']['n']
            if str(n).startswith('zL'):
                collapse[n] = p
            elif str(p).startswith('zL'):
                collapse[p] = n
    components = []
    for item in instances:
        if item['role'] == 'res' and item['name'].startswith(('RSL', 'RPL')):
            continue
        nodes = {k: collapse.get(v, v) for k, v in item.get('nodes', {}).items()}
        components.append(dict(item, nodes=nodes))
    return {'family': variant['family'], 'variant': variant['id'], 'components': components,
            'omitted': ['MOS body wiring', 'digital decoder/control pin wiring', 'explicit winding loss resistors'],
            'collapsed_loss_nodes': collapse}


class ConceptViewer(ttk.Frame):
    """Mechanism overview with actual endpoints and explicit simplification."""
    def __init__(self, parent):
        super().__init__(parent)
        self.family_font = tkfont.nametofont('TkDefaultFont').actual()['family']
        self.model = None
        self.factor = 1.0
        bar = ttk.Frame(self)
        bar.pack(fill='x')
        ttk.Label(bar, text='Concept diagram · solid: electrical branch; dashed: magnetic coupling').pack(side='left')
        ttk.Button(bar, text='Fit view', command=self.fit).pack(side='right')
        ttk.Button(bar, text='+', width=3, command=lambda: self.zoom(1.2)).pack(side='right')
        ttk.Button(bar, text='−', width=3, command=lambda: self.zoom(1/1.2)).pack(side='right')
        holder = ttk.Frame(self)
        holder.pack(fill='both', expand=True)
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(holder, background=PLOT['bg'], highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        xbar = ttk.Scrollbar(holder, orient='horizontal', command=self.canvas.xview)
        ybar = ttk.Scrollbar(holder, command=self.canvas.yview)
        xbar.grid(row=1, column=0, sticky='ew')
        ybar.grid(row=0, column=1, sticky='ns')
        self.canvas.configure(xscrollcommand=xbar.set, yscrollcommand=ybar.set)
        self.canvas.bind('<Map>', lambda event: self.fit())
        self.canvas.bind('<ButtonPress-2>', lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind('<B2-Motion>', lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        self.description = tk.Text(self, height=4, wrap='word', spacing1=3, spacing3=3, padx=8, pady=4, background=fw.RAISED, foreground=fw.TEXT, relief='flat')
        self.description.pack(fill='x', pady=4)
        self.description.configure(state='disabled')

    def set_variant(self, variant):
        self.model = concept_model(variant)
        self.render()
        self.after_idle(self.fit)

    def fit(self):
        if not self.model or self.canvas.winfo_width() < 50:
            return
        self.render()
        factor = min(1.0, max(.4, min((self.canvas.winfo_width()-15)/1020, (self.canvas.winfo_height()-15)/765)))
        self.zoom(factor)
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def zoom(self, ratio):
        new = max(.4, min(2, self.factor*ratio))
        ratio = new/self.factor
        self.canvas.scale('all', 0, 0, ratio, ratio)
        self.factor = new
        for item in self.canvas.find_withtag('label'):
            self.canvas.itemconfigure(item, font=(self.family_font, max(8, int(11*self.factor))))
        for item in self.canvas.find_withtag('title'):
            self.canvas.itemconfigure(item, font=(self.family_font, max(11, int(17*self.factor)), 'bold'))
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def render(self):
        if not self.model:
            return
        import math
        c = self.canvas
        c.delete('all')
        self.factor = 1
        family, variant = self.model['family'], self.model['variant']
        components = self.model['components']
        names = {item['name']: item for item in components}
        wire = fw.SERIES[0]
        mag = fw.SERIES[1]
        main = fw.SERIES[2]
        aux = fw.SERIES[3]
        def text(x, y, value, color=INK, anchor='center', width=None, title=False):
            kw = {'text': value, 'fill': color, 'anchor': anchor, 'font': (self.family_font, 17 if title else 11, 'bold' if title else 'normal'), 'tags': ('title' if title else 'label',)}
            if width:
                kw['width'] = width
            return c.create_text(x, y, **kw)
        def line(*coords, color=wire, width=2, dash=None):
            return c.create_line(*coords, fill=color, width=width, dash=dash or ())
        # These are layout coordinates only. Every component endpoint below is
        # read from the shared graph, including floating MID/T1/T2 and bias nets.
        if family in ('hegazi', 'murphy') and not (family == 'murphy' and variant == 'cmos'):
            upper = 'TOP' if variant == 'top' else 'VDD'
            nodes = {upper:(500,120), 'OUTP':(270,260), 'OUTN':(730,260), 'S':(500,500), 'BIAS':(380,610), 'VSS':(500,710), 'VDC':(130,615)}
            if upper == 'TOP':
                nodes['VDD'] = (500,35)
            mos = {'MN1':(270,430,1), 'MN2':(730,430,1), 'MB':(380,665,1)}
            if variant == 'top':
                mos['MB'] = (500,75,-1)
                nodes['VDC'] = (700,70)
            notes = ('Explicit harmonic return at S; main and aux are independent.' if family == 'hegazi' else 'The coupled network supports differential fundamental and common harmonic modes; aux banks share one control.')
            if family == 'murphy':
                nodes['VSS'] = (500,650)
        elif family == 'murphy':
            nodes = {'VDD':(500,70),'UP':(260,250),'UN':(740,250),'MID':(500,400),'OUTP':(260,550),'OUTN':(740,550),'VSS':(500,725)}
            mos = {'MP1':(260,160,-1),'MP2':(740,160,-1),'MN1':(260,640,1),'MN2':(740,640,1)}
            notes = 'Complementary cores and four windings; MID floats. Main banks share main controls; grounded banks share aux.'
        elif family == 'lim':
            nodes = {'VDD':(260,70),'VSS':(740,720),'DP':(260,240),'DN':(740,240),'MID':(500,395),'OUTP':(260,550),'OUTN':(740,550)}
            mos = {'MP':(260,150,-1),'MN':(740,640,1)}
            notes = 'Drain-gate magnetic feedback; OUTP–OUTN is the fundamental port. Observe the harmonic at drain DP–DN.'
        elif family == 'guo':
            nodes = {'VDD':(750,65),'AUX':(350,65),'HEAD':(500,195),'DP':(260,340),'DN':(740,340),'VDC':(500,455),'OUTP':(260,485),'OUTN':(740,485),'VSS':(500,720)}
            mos = {'MN1':(260,620,1),'MN2':(740,620,1)}
            notes = 'Fixed head branches: LH1→AUX→CH→VDD and LH2→VDD, magnetically coupled. AUX is an internal net; main is the only control.'
        else:
            nodes = {'VDD':(500,65),'OUTP':(260,245),'OUTN':(740,245),'T1':(360,395),'T2':(640,395),'N1':(260,550),'N2':(740,550),'VSS':(500,730)}
            mos = {'MP1':(260,155,-1),'MP2':(740,155,-1),'MN1':(260,640,1),'MN2':(740,640,1)}
            notes = 'Two cores share main controls for tracking. CCA/CCB compensate across cores; RREJ damps the unwanted mode between T1–T2.'
        text(24, 20, family.upper() + ' / ' + variant, anchor='nw', title=True)
        self.description.configure(state='normal')
        self.description.delete('1.0', 'end')
        self.description.insert('1.0', notes + '\nmain: tuning; aux: independent harmonic control where present. Coupling signs and values follow the graph.\nSimplified: bulk terminals, digital wires and coil losses omitted. Crossing lines need a dot to connect. Circuit graph is authoritative.')
        self.description.configure(state='disabled')
        positions = {}
        def branch(item, a, b, offset=0):
            x1,y1=a;x2,y2=b
            dx,dy=x2-x1,y2-y1
            length=max(1,math.hypot(dx,dy));ux,uy=dx/length,dy/length;px,py=-uy,ux
            cx,cy=(x1+x2)/2+px*offset,(y1+y2)/2+py*offset
            # Each repeated branch is separated, with explicit routing to its endpoints.
            start=(cx-ux*28,cy-uy*28);end=(cx+ux*28,cy+uy*28)
            line(x1,y1,*start);line(*end,x2,y2)
            role=item['role'];bank=item.get('master','').split('_')[-1]
            color=main if role=='subckt' and bank=='main' else aux if role=='subckt' else wire
            if role=='ind':
                points=[]
                for i in range(41):
                    t=i/40;along=(t-.5)*56;normal=8*math.sin(t*8*math.pi)
                    points += [cx+ux*along+px*normal,cy+uy*along+py*normal]
                line(*points,color=color)
            elif role in ('cap','subckt'):
                line(*start,cx-ux*5,cy-uy*5,color=color)
                line(cx+ux*5,cy+uy*5,*end,color=color)
                for q in (-5,5):
                    line(cx+ux*q-px*16,cy+uy*q-py*16,cx+ux*q+px*16,cy+uy*q+py*16,color=color,width=3)
                if role=='subckt':
                    c.create_line(cx-ux*18-px*22,cy-uy*18-py*22,cx+ux*18+px*22,cy+uy*18+py*22,arrow='last',fill=color,width=2)
            elif role=='res':
                points=[]
                for i in range(9):
                    along=-28+7*i;normal=0 if i in (0,8) else (7 if i%2 else -7)
                    points += [cx+ux*along+px*normal,cy+uy*along+py*normal]
                line(*points,color=color)
            label=item['name'] + ((' / '+bank) if role=='subckt' else '')
            text(cx+px*28,cy+py*28,label,color=color)
            positions[item['name']] = (cx,cy)
        repeated={}
        for item in components:
            if item['role'] not in ('ind','cap','res','subckt'):
                continue
            pins=('P','N') if item['role']=='subckt' else ('p','n')
            pair=tuple(item['nodes'].get(pin) for pin in pins)
            if not all(net in nodes for net in pair):
                continue
            key=tuple(sorted(pair));index=repeated.get(key,0);repeated[key]=index+1
            offset = index*65
            # Main and aux arrays span the real endpoint pair; no invented returns.
            if family in ('murphy','hegazi') and pair in (('OUTP','OUTN'),('OUTN','OUTP')):
                offset = 45 + index*65
            if family=='du' and item['name'] in ('ICTA','ICTB'):
                offset = 145 if item['name']=='ICTA' else -145
            if family=='du' and item['name'] in ('C0','C0B'):
                offset = 95 if item['name']=='C0' else -95
            if item['name'] == 'CB':
                offset = -80
            if family == 'du' and item['name'] in ('CCA', 'CCB'):
                offset = 65 if item['name'] == 'CCA' else -65
            branch(item,nodes[pair[0]],nodes[pair[1]],offset)
        # Active devices: real gate net is printed at gate lead. A thin routed
        # wire makes the cross-coupling / drain-gate feedback explicit.
        for name,(x,y,orientation) in mos.items():
            item=names.get(name)
            if not item:
                continue
            d,g,s=(item['nodes'][p] for p in ('d','g','s'))
            dpoint=(x,y-orientation*38);spoint=(x,y+orientation*38)
            line(x-8,y-23,x-8,y+23);line(x-17,y-23,x-17,y+23)
            line(x-8,y-18,x,y-18,x,y-38);line(x-8,y+18,x,y+18,x,y+38)
            line(x-17,y,x-50,y)
            if item['role']=='pmos':
                c.create_oval(x-29,y-5,x-19,y+5,fill='white',outline=wire,width=2)
            for net,point in ((d,dpoint),(s,spoint)):
                target=nodes[net]
                line(*point,*target)
            gx,gy=nodes[g]
            # Gate feedback routed on the outside of the associated half-plane.
            route=110 if x<500 else 890
            gate_level = gy - 35 if x < 500 else gy + 35
            line(x-50,y,route,y,route,gate_level,gx,gate_level,gx,gy,color=fw.SERIES[4],width=1)
            text(x+23,y,name+' '+item['role'],anchor='w')
            text(x-50,y-18,'g='+g,color=fw.SERIES[4],anchor='e')
        for item in components:
            if item['role']=='mutual':
                p=item['parameters'];one=positions.get(p.get('ind1'));two=positions.get(p.get('ind2'))
                if one and two:
                    c.create_line(*one,*two,fill=mag,dash=(4,4),width=1)
        for net,(x,y) in nodes.items():
            used=any(net in item.get('nodes',{}).values() for item in components)
            if not used:
                continue
            c.create_oval(x-4,y-4,x+4,y+4,fill=wire,outline=wire)
            text(x+7,y-12,net,color=wire,anchor='sw')
        c.configure(scrollregion=(0,0,1020,765))
