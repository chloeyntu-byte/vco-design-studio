"""VCO Design Studio: isolated, asynchronous Tk front end to the shared APIs."""
from __future__ import annotations
from . import fieldwork_tk as fw
from .ui_fonts import normalize_family_case
import copy
import datetime as dt
import inspect
import json
import os
from pathlib import Path
import queue
import threading
import traceback
import uuid
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, font as tkfont
from .drawing import CircuitViewer, ConceptViewer

INSTALL_ROOT = Path(__file__).resolve().parent.parent


def runtime_root(workspace=None, runtime=None):
    root = Path(runtime).expanduser().resolve() if runtime else ((Path(workspace).expanduser().resolve() / 'vco-design-studio_runtime') if workspace else INSTALL_ROOT.with_name(INSTALL_ROOT.name + '_runtime'))
    if root == INSTALL_ROOT or INSTALL_ROOT in root.parents or root in INSTALL_ROOT.parents:
        raise ValueError('Runtime and program directories must be separate.')
    return root


def save_json(path, value):
    """Atomic project/config write with timestamp backup; no lost-write temp names."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
        backup = path.with_name(path.name + '.bak-' + stamp)
        if backup.exists():
            raise FileExistsError('Backup already exists; retry in a moment: ' + str(backup))
        backup.write_bytes(path.read_bytes())
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path, schema):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('schema') != schema:
        raise ValueError('Unexpected file type; expected ' + schema)
    return value


def register_definitions(variant):
    definitions = variant.get('graph', {}).get('registers', [])
    result = {}
    for definition in definitions:
        if not definition.get('controllable', True):
            continue
        role = definition.get('role', '')
        name = definition.get('name', '')
        bank = 'aux' if role in ('aux', 'harmonic', 'auxiliary', '2f') or 'aux' in name.lower() else 'main'
        result.setdefault(bank, definition)
    # Fixed Guo and intrinsic Du tracking never gain an independent aux control.
    family = variant.get('family', variant.get('graph', {}).get('family'))
    if family in ('guo', 'du'):
        result.pop('aux', None)
    return result


class StudioApp:
    def __init__(self, root, workspace=None, project=None, runtime=None):
        from . import catalog
        self.root = root
        self.runtime = runtime_root(workspace, runtime)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.window_id = uuid.uuid4().hex[:12]
        self.log_path = self.runtime / 'logs' / ('ui-' + self.window_id + '.log')
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.job_token = 0
        self.revision = 0
        self.generated_revision = None
        self.generated_dir = None
        self.project_file = None
        self.file_identity = None
        self.binding = {'schema': 'vco.binding.v1', 'roles': {}, 'status': 'unresolved', 'unresolved': [{'reason': 'No device binding selected'}]}
        self.discovery = None
        self.current_variant = {}
        self.extra_project = {}
        self.families = catalog.list_families()
        self.family_id = self.families[0]['id']
        self.workspace = tk.StringVar(value=str(Path(workspace).resolve()) if workspace else '')
        self.target_library = tk.StringVar()
        self.target_cell = tk.StringVar(value='vco_design')
        self.output_root = tk.StringVar(value=str(self.runtime / 'runs'))
        self.variant_id = tk.StringVar()
        self.status = tk.StringVar(value='Ready; devices unbound')
        self.binding_status = tk.StringVar(value='Unresolved: load a binding or discover workspace devices')
        self.parameter_vars = {}
        self.register_vars = {}
        self.role_vars = {}
        self._loading = False
        self._build()
        self._choose_family(self.family_id)
        self._show_empty_state()
        if project is not None:
            if isinstance(project, dict):
                self.apply_project(project)
            else:
                self.load_project(project)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_id = self.root.after(80, self._poll)
        self.root.bind('<Destroy>', self._destroyed, add='+')
        self._log('INFO', 'Session ' + self.window_id + '; new independent session.')

    def _build(self):
        r = self.root
        r.title('VCO Design Studio — New project')
        r.geometry('1600x1000+0+0')
        r.minsize(1280, 800)
        fw.apply(r, product='vco')
        normalize_family_case(r)
        self.font_family = r.fw_fonts['body'][0]
        fw.header(r, 'VCO Design Studio', 'LC topology and circuit workbench',
                  right=lambda bar: [
                      ttk.Button(bar, text='Open project', command=self.load_project).pack(side='left', padx=4),
                      ttk.Button(bar, text='Save project', command=self.save_project).pack(side='left', padx=4)
                  ]).pack(fill='x')
        frame = ttk.Frame(r, padding=16)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        familybar = ttk.Frame(frame)
        familybar.grid(row=1, column=0, sticky='ew', pady=(12, 8))
        self.family_buttons = {}
        for n, family in enumerate(self.families):
            familybar.columnconfigure(n, weight=1)
            button = ttk.Button(familybar, text=family['name'], style='TButton', command=lambda fid=family['id']: self._choose_family(fid))
            button.grid(row=0, column=n, sticky='ew', padx=(0, 4))
            self.family_buttons[family['id']] = button
        self.summary = ttk.Label(frame, wraplength=1150, style='Dim.TLabel')
        self.summary.grid(row=2, column=0, sticky='ew', pady=(0, 8))
        panes = ttk.Panedwindow(frame, orient='horizontal')
        panes.grid(row=3, column=0, sticky='nsew')
        self.panes = panes
        left = ttk.Frame(panes, width=360, style='Panel.TFrame')
        right = ttk.Frame(panes)
        panes.add(left, weight=0)
        panes.add(right, weight=1)
        self.settings = ttk.Notebook(left)
        self.settings.pack(fill='both', expand=True)
        design_wrap, design = self._scrollable(self.settings)
        pdk_wrap, pdkpage = self._scrollable(self.settings)
        self.settings.add(design_wrap, text='Design')
        self.settings.add(pdk_wrap, text='Device binding')
        ttk.Label(design, text='Topology variant').pack(anchor='w')
        self.variant_box = ttk.Combobox(design, state='readonly', width=32)
        self.variant_box.pack(fill='x', pady=(4, 12))
        self.variant_box.bind('<<ComboboxSelected>>', self._choose_variant)
        self.parameters_frame = ttk.LabelFrame(design, text='Operating point', padding=8)
        self.parameters_frame.pack(fill='x', pady=4)
        self.register_frame = ttk.LabelFrame(design, text='Tuning controls', padding=8)
        self.register_frame.pack(fill='x', pady=4)
        target = ttk.LabelFrame(design, text='New output target', padding=8)
        target.pack(fill='x', pady=4)
        for row, (label, var) in enumerate([('Library', self.target_library), ('Cell', self.target_cell), ('Output folder', self.output_root)]):
            ttk.Label(target, text=label).grid(row=row, column=0, sticky='w', pady=4)
            ttk.Entry(target, textvariable=var, width=24).grid(row=row, column=1, sticky='ew', pady=4)
        target.columnconfigure(1, weight=1)
        ttk.Label(design, text='Dimensions are synthetic seeds. Validate user devices,\nstartup, noise and tuning coverage before adoption.', wraplength=310, foreground=fw.WARN).pack(fill='x', pady=8)
        self.netlist_button = ttk.Button(left, text='Generate circuit files', command=lambda: self.generate(False), style='Primary.TButton')
        self.netlist_button.pack(fill='x', pady=4)
        self.native_button = ttk.Button(left, text='Build in native workspace', command=lambda: self.generate(True))
        self.native_button.pack(fill='x', pady=4)
        ttk.Label(pdkpage, text='Workspace containing cds.lib').pack(anchor='w')
        ttk.Entry(pdkpage, textvariable=self.workspace).pack(fill='x', pady=4)
        discoverbar = ttk.Frame(pdkpage)
        discoverbar.pack(fill='x')
        ttk.Button(discoverbar, text='Browse workspace', command=self.choose_workspace).pack(side='left')
        self.discover_button = ttk.Button(discoverbar, text='Discover devices', command=self.discover_pdk)
        self.discover_button.pack(side='right')
        self.roles_frame = ttk.Frame(pdkpage)
        self.roles_frame.pack(fill='x', pady=8)
        ttk.Label(self.roles_frame, text='Role').grid(row=0, column=0, sticky='w')
        ttk.Label(self.roles_frame, text='library / cell / view').grid(row=0, column=1, sticky='w')
        for i, role in enumerate(('nmos', 'pmos', 'cap', 'res', 'ind')):
            ttk.Label(self.roles_frame, text=role).grid(row=i+1, column=0, sticky='nw', pady=4)
            rf = ttk.Frame(self.roles_frame)
            rf.grid(row=i+1, column=1, sticky='ew', pady=4)
            variables = [tk.StringVar() for _ in range(3)]
            widgets = []
            for j, var in enumerate(variables):
                box = ttk.Combobox(rf, textvariable=var, width=(14 if j == 0 else 12))
                box.grid(row=0 if j < 2 else 1, column=j if j < 2 else 0, sticky='ew', padx=(0, 4), pady=4)
                widgets.append(box)
                box.bind('<<ComboboxSelected>>', lambda e, role=role: self._role_options(role))
                var.trace_add('write', lambda *a: self._binding_dirty())
            variables[2].set('symbol')
            self.role_vars[role] = (variables, widgets)
        self.roles_frame.columnconfigure(1, weight=1)
        ttk.Label(pdkpage, text='res binds bank bias resistors. Primitive inductors use\nanalogLib/ind; other magnetic models need an adapter.', wraplength=310, foreground=fw.DIM).pack(fill='x', pady=4)
        ttk.Label(pdkpage, text='Model files (path | section, one per line)').pack(anchor='w', pady=(4, 0))
        self.model_text = tk.Text(pdkpage, height=3, width=30, wrap='none')
        self.model_text.pack(fill='x', pady=4)
        self.model_text.bind('<<Modified>>', self._models_changed)
        self.resolve_button = ttk.Button(pdkpage, text='Resolve terminals and CDF', command=self.resolve_binding)
        self.resolve_button.pack(fill='x', pady=4)
        bindbar = ttk.Frame(pdkpage)
        bindbar.pack(fill='x')
        ttk.Button(bindbar, text='Load binding', command=self.load_binding).pack(side='left')
        ttk.Button(bindbar, text='Save binding', command=self.save_binding).pack(side='right')
        ttk.Label(pdkpage, textvariable=self.binding_status, wraplength=315, foreground=fw.WARN).pack(fill='x', pady=8)
        self.pages = ttk.Notebook(right)
        self.pages.pack(fill='both', expand=True, padx=(12, 0))
        self.viewer = CircuitViewer(self.pages)
        self.pages.add(self.viewer, text='Circuit graph')
        self.guide_pages = ttk.Notebook(self.pages)
        self.pages.add(self.guide_pages, text='Architecture guide')
        self.concept = ConceptViewer(self.guide_pages)
        self.guide_pages.add(self.concept, text='Concept diagram')
        self.guide = self._text_page(self.guide_pages, 'Mechanism and qualification')
        tools_page = ttk.Frame(self.pages, padding=12)
        self.pages.add(tools_page, text='Analysis and outputs')
        ttk.Label(tools_page, text='Analysis uses the generated graph and a new output folder.', wraplength=600).pack(anchor='w')
        toolbar = ttk.Frame(tools_page)
        toolbars = [('Analyze passive port', 'z11'), ('Export bank workbook', 'caplab'), ('Optimize current controls', 'optimizer')]
        toolbuttons = []
        for label, tool in toolbars:
            button = ttk.Button(toolbar, text=label, command=lambda tool=tool: self.run_tool(tool))
            button.pack(side='left', padx=(0, 4))
            toolbuttons.append(button)
        toolbar.pack(fill='x', pady=8)
        self.tool_buttons = toolbuttons
        livebar = ttk.Frame(tools_page)
        livebar.pack(fill='x', pady=(0, 8))
        for label, tool in [('Measure HB and noise', 'simulate'), ('Measure bank admittance', 'bank_ac')]:
            button = ttk.Button(livebar, text=label, command=lambda tool=tool: self.run_tool(tool))
            button.pack(side='left', padx=(0, 4))
            self.tool_buttons.append(button)
        ttk.Label(tools_page, text='Optimization measures selected controls; full tuning coverage requires a separate sweep.', wraplength=600, foreground=fw.WARN).pack(anchor='w', pady=4)
        opt = ttk.Frame(tools_page)
        opt.pack(fill='x', pady=4)
        self.opt_parameter = tk.StringVar(value='core_width_scale')
        self.opt_low = tk.StringVar(value='0.9')
        self.opt_high = tk.StringVar(value='1.1')
        self.opt_budget = tk.StringVar(value='4')
        ttk.Combobox(opt, textvariable=self.opt_parameter, values=['core_width_scale', 'L_scale', 'bank_cap_scale', 'aux_cap_scale', 'loss_scale', 'k_scale'], state='readonly', width=20).grid(row=0, column=0, columnspan=2, sticky='w')
        for column, (label, var) in enumerate([('Minimum', self.opt_low), ('Maximum', self.opt_high), ('Sample budget', self.opt_budget)]):
            ttk.Label(opt, text=label).grid(row=1, column=column*2, sticky='w', pady=4)
            ttk.Entry(opt, textvariable=var, width=7).grid(row=1, column=column*2+1, padx=4)
        ac = ttk.Frame(tools_page)
        ac.pack(fill='x', pady=4)
        self.ac_bank = tk.StringVar(value='main')
        self.ac_codes = tk.StringVar(value='0,15')
        self.ac_frequencies = tk.StringVar(value='5,10,15,20,25')
        ttk.Label(ac, text='AC bank / codes').grid(row=0, column=0, sticky='w')
        self.ac_bank_box = ttk.Combobox(ac, textvariable=self.ac_bank, values=['main'], state='readonly', width=7)
        self.ac_bank_box.grid(row=0, column=1, padx=4)
        ttk.Entry(ac, textvariable=self.ac_codes, width=20).grid(row=0, column=2)
        ttk.Label(ac, text='AC frequency (GHz)').grid(row=1, column=0, sticky='w', pady=4)
        ttk.Entry(ac, textvariable=self.ac_frequencies, width=30).grid(row=1, column=1, columnspan=2, sticky='ew')
        ttk.Label(tools_page, text='Passive projections are separate from measured AC. Two excitations per code preserve the full admittance matrix.', wraplength=600, foreground=fw.WARN).pack(anchor='w', pady=(4, 12))
        self.artifacts = tk.Text(tools_page, wrap='word', spacing1=2, spacing3=2, height=15, padx=8, pady=8)
        self.artifacts.pack(fill='both', expand=True)
        self.artifacts.configure(state='disabled')
        foot = ttk.Frame(frame)
        foot.grid(row=4, column=0, sticky='ew', pady=(8, 4))
        self.status_chip = fw.StatusChip(foot)
        self.status_chip.pack(side='left', padx=8, pady=4)
        self.status.trace_add('write', self._status_changed)
        self._status_changed()
        self.cancel_button = ttk.Button(foot, text='Cancel task', command=self.cancel, state='disabled')
        self.cancel_button.pack(side='right')
        ttk.Button(foot, text='Clear failure', command=self.clear_failure).pack(side='right', padx=4)
        ttk.Button(foot, text='Diagnose', command=self.diagnose).pack(side='right', padx=4)
        ttk.Button(foot, text='Open log', command=self.show_log).pack(side='right', padx=4)
        self.progress = ttk.Progressbar(frame, mode='determinate', style='Horizontal.TProgressbar')
        self.progress.grid(row=5, column=0, sticky='ew')
        self.log_text = tk.Text(frame, height=4, wrap='word', bg=fw.RAISED, fg=fw.TEXT, relief='flat', padx=8, pady=4)
        self.log_text.grid(row=6, column=0, sticky='ew', pady=(4, 0))
        self.log_text.configure(state='disabled')
        for var in (self.workspace, self.target_library, self.target_cell, self.output_root):
            var.trace_add('write', lambda *a: self._changed())
        self.workspace.trace_add('write', lambda *a: self._binding_dirty())

    def _status_changed(self, *_args):
        message = self.status.get()
        lower = message.lower()
        state = ('error' if any(word in lower for word in ('failed', 'failure:', 'error:')) else
                 'running' if self.busy else
                 'warn' if any(word in lower for word in ('unresolved', 'cancel', 'unbound')) else
                 'done' if any(word in lower for word in ('generated', 'saved', 'loaded', 'completed', 'returned')) else 'idle')
        self.status_chip.set(state, message)
        self.status_chip._msg.configure(wraplength=max(320, self.root.winfo_width()-600))

    def _show_empty_state(self):
        self.viewer.canvas.delete('all')
        self.viewer.count.configure(text='')
        self.viewer._detail('Choose a family or load the synthetic example to inspect connections.')
        self.empty_state = ttk.Frame(self.viewer, padding=24, style='Panel.TFrame')
        self.empty_state.place(relx=.5, rely=.42, anchor='center')
        ttk.Label(self.empty_state, text='Explore an LC VCO topology', style='Title.TLabel').pack(pady=8)
        ttk.Label(self.empty_state, text='Load a synthetic example or choose a family above.',
                  style='PanelDim.TLabel').pack(pady=8)
        ttk.Button(self.empty_state, text='Load example', style='Primary.TButton',
                   command=self.load_example).pack(pady=16)

    def load_example(self):
        if self.busy:
            return
        from .catalog import default_project
        self.apply_project(default_project('murphy', 'nmos', 'demo_a'))
        self.empty_state.place_forget()
        self.root.title('VCO Design Studio — Synthetic example')
        self.status.set('Loaded synthetic example; no simulation evidence')

    def _scrollable(self, parent):
        wrapper = ttk.Frame(parent)
        canvas = tk.Canvas(wrapper, width=345, height=1, highlightthickness=0)
        scroll = ttk.Scrollbar(wrapper, command=canvas.yview)
        scroll.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        content = ttk.Frame(canvas, padding=16, style='Panel.TFrame')
        fw.section(content, 'Configuration').pack(anchor='w', pady=(0, 8))
        window = canvas.create_window(0, 0, anchor='nw', window=content)
        previous = [None]
        def content_configure(event):
            dimensions = (event.width, event.height)
            if previous[0] != dimensions:
                previous[0] = dimensions
                canvas.configure(scrollregion=canvas.bbox('all'))
        content.bind('<Configure>', content_configure)
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.bind('<Button-4>', lambda e: canvas.yview_scroll(-3, 'units'))
        canvas.bind('<Button-5>', lambda e: canvas.yview_scroll(3, 'units'))
        return wrapper, content

    def _text_page(self, notebook, title):
        frame = ttk.Frame(notebook)
        text = tk.Text(frame, wrap='word', spacing1=2, spacing3=2, padx=16, pady=12, relief='flat', width=50)
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        text.configure(state='disabled')
        notebook.add(frame, text=title)
        return text

    def _set_text(self, widget, text):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', text)
        widget.configure(state='disabled')

    def _changed(self):
        if not self._loading:
            self.revision += 1
            if hasattr(self, 'viewer'):
                self.viewer.source_note.set('Template or last generated graph; regenerate to apply edited parameters.')

    def _models_changed(self, _event=None):
        if self.model_text.edit_modified():
            self._binding_dirty()
            self.model_text.edit_modified(False)

    def _binding_dirty(self):
        if self._loading:
            return
        self._changed()
        self.binding['status'] = 'unresolved'
        self.binding['unresolved'] = [{'reason': 'Binding inputs changed; resolve master terminals and CDF again'}]
        self.binding_status.set('Unresolved: inputs changed; resolve terminals and CDF again')

    def _choose_family(self, family_id):
        if self.busy:
            return
        if hasattr(self, 'empty_state'):
            self.empty_state.place_forget()
        self.family_id = family_id
        family = next(f for f in self.families if f['id'] == family_id)
        for fid, button in self.family_buttons.items():
            button.configure(style='SelectedFamily.TButton' if fid == family_id else 'TButton', text=('● ' if fid == family_id else '') + next(f['name'] for f in self.families if f['id'] == fid))
        self.summary.configure(text=family.get('summary', ''))
        self.variants = family['variants']
        self.variant_box.configure(values=[v.get('name', v['id']) for v in self.variants])
        self.variant_box.current(0)
        self._choose_variant()

    def _choose_variant(self, _event=None):
        if self.busy:
            return
        from . import catalog
        index = self.variant_box.current()
        if index < 0:
            return
        variant = self.variants[index]
        self.variant_id.set(variant['id'])
        self.current_variant = catalog.get_variant(self.family_id, variant['id'])
        self.current_variant.setdefault('family', self.family_id)
        if not self._loading:
            self.extra_project.pop('overrides', None)
        self.viewer.set_graph(self.current_variant['graph'])
        self.concept.set_variant(self.current_variant)
        self._build_fields()
        self._update_guide()
        self._changed()

    def _build_fields(self):
        from . import catalog
        for frame in (self.parameters_frame, self.register_frame):
            for widget in frame.winfo_children():
                widget.destroy()
        self.parameter_vars = {}
        defaults = self.current_variant.get('defaults', {})
        try:
            project = catalog.default_project(self.family_id, self.variant_id.get())
        except AttributeError:
            project = defaults
        parameters = project.get('parameters', defaults.get('parameters', {}))
        labels = [('vdd', 'Supply VDD (V)'), ('vdc', 'Bias VDC (V)'), ('temp_c', 'Temperature (°C)'), ('frequency_guess_ghz', 'Frequency guess (GHz)')]
        for row, (key, label) in enumerate(labels):
            if key not in parameters:
                continue
            var = tk.StringVar(value=str(parameters[key]))
            self.parameter_vars[key] = var
            ttk.Label(self.parameters_frame, text=label).grid(row=row, column=0, sticky='w', pady=4)
            ttk.Entry(self.parameters_frame, textvariable=var, width=13).grid(row=row, column=1, sticky='e', pady=4)
            var.trace_add('write', lambda *a: self._changed())
        self.extra_parameters = {k: v for k, v in parameters.items() if k not in self.parameter_vars}
        self.register_vars = {}
        definitions = register_definitions(self.current_variant)
        defaults_regs = project.get('registers', {})
        for row, (bank, definition) in enumerate(definitions.items()):
            default = defaults_regs.get(bank, {})
            bits = tk.StringVar(value=str(default.get('bits', definition.get('bits', 4))))
            value = tk.StringVar(value=str(default.get('value', definition.get('default', 0))))
            self.register_vars[bank] = (bits, value)
            ttk.Label(self.register_frame, text='Main tuning' if bank == 'main' else 'Auxiliary tuning').grid(row=row*2, column=0, sticky='w', pady=4)
            fields = ttk.Frame(self.register_frame)
            fields.grid(row=row*2+1, column=0, columnspan=2, sticky='w')
            ttk.Label(fields, text='bits').pack(side='left')
            ttk.Spinbox(fields, textvariable=bits, from_=1, to=12, width=5).pack(side='left', padx=(4, 12))
            ttk.Label(fields, text='code').pack(side='left')
            ttk.Entry(fields, textvariable=value, width=9).pack(side='left', padx=4)
            for var in (bits, value):
                var.trace_add('write', lambda *a: self._changed())
        if hasattr(self, 'ac_bank_box'):
            self.ac_bank_box.configure(values=list(definitions))
            self.ac_bank.set('main')
            main_bits = int(self.register_vars.get('main', (tk.StringVar(value='4'),))[0].get())
            self.ac_codes.set('0,' + str(2**main_bits-1))
        if 'aux' not in definitions:
            ttk.Label(self.register_frame, text='No auxiliary control; fixed or intrinsic tracking network', wraplength=290, foreground=fw.DIM).grid(row=8, column=0, sticky='w', pady=(8, 0))

    def _update_guide(self):
        v = self.current_variant
        rows = [v.get('name', self.variant_id.get()), '', v.get('description', '')]
        for key, label in [('features', 'Features'), ('connections', 'Connectivity'), ('calibration', 'Calibration'), ('reference_data', 'Reference conditions'), ('provenance', 'Provenance')]:
            rows.extend(['', label])
            value = v.get(key)
            if value is None:
                rows.append('Not supplied.')
            elif isinstance(value, str):
                rows.append(value)
            else:
                rows.append(json.dumps(value, ensure_ascii=False, indent=2))
        rows.extend(['', 'Synthetic examples are not performance evidence for a target device binding.'])
        self._set_text(self.guide, '\n'.join(rows))

    def collect_project(self):
        from . import catalog
        try:
            project = catalog.default_project(self.family_id, self.variant_id.get(), binding=self.binding)
        except AttributeError:
            project = {'schema': 'vco.project.v1', 'family': self.family_id, 'variant': self.variant_id.get()}
        project.update(copy.deepcopy(self.extra_project))
        project.update({'schema': 'vco.project.v1', 'family': self.family_id, 'variant': self.variant_id.get(), 'binding': copy.deepcopy(self.binding), 'workspace': self.workspace.get(), 'target_library': self.target_library.get(), 'target_cell': self.target_cell.get(), 'output_root': self.output_root.get()})
        project['parameters'] = copy.deepcopy(self.extra_parameters)
        for name, variable in self.parameter_vars.items():
            value = float(variable.get())
            if not __import__('math').isfinite(value):
                raise ValueError(name + ' must be finite')
            project['parameters'][name] = value
        project['registers'] = {}
        for bank, (bits_var, value_var) in self.register_vars.items():
            bits, value = int(bits_var.get()), int(value_var.get())
            if not 1 <= bits <= 12 or not 0 <= value < 2**bits:
                raise ValueError(bank + ' requires 1–12 bits and a code within range')
            project['registers'][bank] = {'bits': bits, 'value': value}
        return project

    def apply_project(self, project):
        if project.get('schema') != 'vco.project.v1':
            raise ValueError('Expected a vco.project.v1 project')
        self._loading = True
        try:
            self._choose_family(project['family'])
            match = next(i for i, v in enumerate(self.variants) if v['id'] == project['variant'])
            self.variant_box.current(match)
            self._choose_variant()
            for key, value in project.get('parameters', {}).items():
                if key in self.parameter_vars:
                    self.parameter_vars[key].set(str(value))
                else:
                    self.extra_parameters[key] = value
            for bank, values in project.get('registers', {}).items():
                if bank not in self.register_vars:
                    raise ValueError('Topology has no independent register ' + bank)
                self.register_vars[bank][0].set(str(values['bits']))
                self.register_vars[bank][1].set(str(values.get('value', 0)))
            for key in ('workspace', 'target_library', 'target_cell', 'output_root'):
                if key in project:
                    getattr(self, key).set(project[key])
            self.extra_project = {key: copy.deepcopy(value) for key, value in project.items() if key not in ('schema', 'family', 'variant', 'binding', 'parameters', 'registers', 'workspace', 'target_library', 'target_cell', 'output_root')}
            self._apply_binding(project.get('binding', self.binding))
        finally:
            self._loading = False
        self._changed()

    def load_project(self, path=None):
        if self.busy:
            return
        path = path or filedialog.askopenfilename(title='Open project', filetypes=[('VCO project', '*.json')], parent=self.root)
        if not path:
            return
        try:
            self.apply_project(read_json(path, 'vco.project.v1'))
            self.project_file = Path(path)
            self.root.title('VCO Design Studio — ' + Path(path).name)
            self.file_identity = self.project_file.stat().st_mtime_ns
            self._log('INFO', 'Loaded project: ' + str(path))
        except Exception as error:
            self._error(error)

    def save_project(self):
        try:
            project = self.collect_project()
            path = filedialog.asksaveasfilename(title='Save project', defaultextension='.json', initialfile=self.project_file.name if self.project_file else 'project.json', parent=self.root)
            if not path:
                return
            path = Path(path)
            if self.project_file == path and path.exists() and self.file_identity != path.stat().st_mtime_ns:
                raise RuntimeError('Project changed in another window. Save under a new name or reload.')
            save_json(path, project)
            self.project_file = path
            self.root.title('VCO Design Studio — ' + Path(path).name)
            self.file_identity = path.stat().st_mtime_ns
            self._log('INFO', 'Saved project: ' + str(path))
        except Exception as error:
            self._error(error)

    def choose_workspace(self):
        if self.busy:
            return
        path = filedialog.askdirectory(title='Choose workspace', parent=self.root)
        if path:
            self.workspace.set(path)

    def discover_pdk(self):
        workspace = self.workspace.get().strip()
        if not workspace:
            self._error(ValueError('Select a workspace before discovery'))
            return
        from . import pdk
        self._start('Discover devices', lambda cancel, log: pdk.discover(workspace), self._discovered)

    def _discovered(self, result):
        self.discovery = result
        libs = [lib['name'] for lib in result.get('libraries', [])]
        for role, (_variables, widgets) in self.role_vars.items():
            widgets[0].configure(values=libs)
            self._role_options(role)
        self.binding_status.set('Found {} libraries; select devices and resolve the binding.'.format(len(libs)))
        self._set_text(self.artifacts, json.dumps(result, ensure_ascii=False, indent=2))

    def _role_options(self, role):
        if not self.discovery:
            return
        variables, widgets = self.role_vars[role]
        lib = next((v for v in self.discovery.get('libraries', []) if v['name'] == variables[0].get()), {})
        cells = lib.get('cells', [])
        widgets[1].configure(values=[v['name'] for v in cells])
        cell = next((v for v in cells if v['name'] == variables[1].get()), {})
        widgets[2].configure(values=cell.get('views', ['symbol']))

    def resolve_binding(self):
        from . import pdk
        workspace = self.workspace.get().strip()
        roles = {role: dict(zip(('library', 'cell', 'view'), (var.get().strip() for var in values))) for role, (values, _widgets) in self.role_vars.items() if any(var.get().strip() for var in values[:2])}
        includes = []
        for row in self.model_text.get('1.0', 'end').splitlines():
            if row.strip():
                path, _, section = row.strip().partition('|')
                include = {'path': path.strip()}
                if section.strip():
                    include['section'] = section.strip()
                includes.append(include)
        if not workspace:
            self._error(ValueError('Provide a workspace'))
            return
        self._start('Resolve terminals and CDF', lambda cancel, log: pdk.resolve_binding(workspace, roles, model_includes=includes), self._apply_binding)

    def _apply_binding(self, binding):
        if not isinstance(binding, dict):
            from .generator import resolve_project
            binding = resolve_project({'family': self.family_id, 'variant': self.variant_id.get(), 'binding': binding})['binding']
        self.binding = copy.deepcopy(binding)
        old_loading = self._loading
        self._loading = True
        try:
            for role, (variables, _widgets) in self.role_vars.items():
                value = binding.get('roles', {}).get(role, {})
                for var, key in zip(variables, ('library', 'cell', 'view')):
                    var.set(value.get(key, 'symbol' if key == 'view' else ''))
            self.model_text.delete('1.0', 'end')
            self.model_text.insert('1.0', '\n'.join(row['path'] + (' | ' + row['section'] if row.get('section') else '') for row in binding.get('model_includes', [])))
            self.model_text.edit_modified(False)
            self.root.update_idletasks()
        finally:
            self._loading = old_loading
        unresolved = binding.get('unresolved', [])
        if unresolved or binding.get('status') != 'resolved':
            self.binding_status.set('Unresolved: ' + '; '.join(str(row.get('role', '')) + ' ' + str(row.get('reason', row)) if isinstance(row, dict) else str(row) for row in unresolved)[:1000])
        else:
            self.binding_status.set('Terminals and CDF resolved; generate and validate before adoption.')
        self._changed()

    def load_binding(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title='Load device binding', filetypes=[('JSON', '*.json')], parent=self.root)
        if path:
            try:
                binding = read_json(path, 'vco.binding.v1')
                if binding.get('workspace'):
                    self.workspace.set(binding['workspace'])
                self._apply_binding(binding)
                self._log('INFO', 'Loaded binding: ' + path)
            except Exception as error:
                self._error(error)

    def save_binding(self):
        path = filedialog.asksaveasfilename(title='Save device binding', defaultextension='.json', initialfile='binding.json', parent=self.root)
        if path:
            try:
                save_json(path, self.binding)
                self._log('INFO', 'Saved binding: ' + path)
            except Exception as error:
                self._error(error)

    def generate(self, execute_native=False):
        try:
            project = self.collect_project()
            if execute_native and (not project['workspace'] or not project['target_library'] or not project['target_cell']):
                raise ValueError('Provide a workspace, new library and cell for native construction')
            if self.binding.get('unresolved') or self.binding.get('status') != 'resolved':
                raise ValueError('Binding unresolved; resolve terminals, CDF and model mapping first')
            if self.binding.get('workspace') and Path(self.binding['workspace']).resolve() != Path(project['workspace']).resolve():
                raise ValueError('Workspace differs from the binding; resolve it again')
            output = Path(project['output_root']).expanduser().resolve() / (dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + self.window_id + '-' + uuid.uuid4().hex[:6])
            if output == INSTALL_ROOT or INSTALL_ROOT in output.parents:
                raise ValueError('Choose an output folder outside the program directory')
            revision = self.revision
        except Exception as error:
            self._error(error)
            return
        def work(cancel, log):
            from . import generator, native
            log('Generating circuit: ' + str(output))
            if cancel.is_set():
                raise InterruptedError('Cancelled')
            manifest = generator.generate(copy.deepcopy(project), output)
            if cancel.is_set():
                return {'output': str(output), 'manifest': manifest, 'cancelled': True, 'revision': revision}
            graph = json.loads((output / 'graph.json').read_text(encoding='utf-8'))
            binding = json.loads((output / 'binding.json').read_text(encoding='utf-8'))
            if not project['workspace'] or not project['target_library']:
                return {'output': str(output), 'manifest': manifest, 'revision': revision}
            target = {'workspace': project['workspace'], 'library': project['target_library'], 'cell': project['target_cell']}
            kwargs = {'execute': execute_native}
            if 'cancel_event' in inspect.signature(native.export).parameters:
                kwargs['cancel_event'] = cancel
            log('Building in native workspace' if execute_native else 'Exporting native build scripts')
            receipt = native.export(graph, binding, target, output, **kwargs)
            return {'output': str(output), 'manifest': manifest, 'native': receipt, 'revision': revision}
        self._start('Circuit generation' + (' / native build' if execute_native else ''), work, self._generated)

    def _generated(self, result):
        self.generated_dir = Path(result['output'])
        self.generated_revision = result['revision']
        graph_path = self.generated_dir / 'graph.json'
        if graph_path.exists():
            self.viewer.set_graph(json.loads(graph_path.read_text(encoding='utf-8')))
            self.viewer.source_note.set('Generated graph: ' + self.generated_dir.name + '; performance requires measurement.')
        self._set_text(self.artifacts, json.dumps(result, ensure_ascii=False, indent=2))
        self._log('INFO', 'Outputs: ' + str(self.generated_dir))
        if result.get('cancelled'):
            self.status.set('Cancelled remaining stages; generated files retained')
        elif 'native' not in result:
            self.status.set('Circuit files generated; provide a workspace to export native scripts')
        elif result.get('native', {}).get('status') == 'verified':
            self.status.set('Native connectivity and netlist verified; performance unqualified')
        else:
            self.status.set('Circuit and native scripts generated; native build not executed')

    def run_tool(self, tool):
        if self.generated_dir is None or self.generated_revision != self.revision:
            self._error(ValueError('Generate the current design first; existing outputs use earlier inputs'))
            return
        generated = self.generated_dir
        output = self.runtime / 'tools' / (tool + '-' + uuid.uuid4().hex[:12])
        try:
            parameters = self.collect_project().get('parameters', {})
            options = {}
            if tool == 'z11':
                f0 = float(parameters.get('frequency_guess_ghz', 10)) * 1e9
                options = {'port': ['OUTP', 'OUTN'], 'frequencies_hz': [f0 * factor for factor in (.5, .75, 1, 1.25, 1.5, 2)]}
            elif tool == 'caplab':
                options = {'port': ['OUTP', 'OUTN']}
            elif tool == 'optimizer':
                low, high, budget = float(self.opt_low.get()), float(self.opt_high.get()), int(self.opt_budget.get())
                if not 0 < low < high or not 1 <= budget <= 100000:
                    raise ValueError('Provide ordered positive bounds and an integer sample budget')
                parameter = self.opt_parameter.get()
                if parameter == 'aux_cap_scale' and 'aux' not in self.register_vars:
                    raise ValueError('Select a parameter present in this topology; no independent auxiliary bank exists')
                options = {'variables': [{'parameter_id': parameter, 'minimum': low, 'maximum': high}], 'budget': budget, 'batch_size': min(4, budget), 'algorithm': 'bayesian_optimization'}
            elif tool == 'bank_ac':
                options = {'bank': self.ac_bank.get(), 'codes': [int(v.strip()) for v in self.ac_codes.get().split(',')], 'frequencies_hz': [float(v.strip())*1e9 for v in self.ac_frequencies.get().split(',')]}
        except Exception as error:
            self._error(error)
            return
        def work(cancel, log):
            from . import integrations
            if tool == 'simulate':
                from .simulator import run
                return run(generated, output_dir=output, cancel_event=cancel, log=log)
            if tool == 'bank_ac':
                from .characterize import bank_ac
                return bank_ac(generated, output, cancel_event=cancel, log=log, **options)
            evaluate = None
            if tool == 'optimizer':
                from .simulator import evaluate_candidate
                def evaluate(project, candidate_dir):
                    kwargs = {}
                    signature = inspect.signature(evaluate_candidate).parameters
                    if 'cancel_event' in signature:
                        kwargs['cancel_event'] = cancel
                    if 'log' in signature:
                        kwargs['log'] = log
                    return evaluate_candidate(project, candidate_dir, **kwargs)
            spec = integrations.action_spec(tool, generated, output, **options)
            return integrations.execute_action(spec, evaluate=evaluate, cancel_event=cancel, log=log)
        self._start(tool, work, self._tool_completed)

    def _tool_completed(self, result):
        self._set_text(self.artifacts, json.dumps(result, ensure_ascii=False, indent=2))
        status = result.get('status')
        if status in ('failed', 'cancelled'):
            self.status.set('Task failed or cancelled: ' + str(result.get('outcome', result.get('error', status))))
        elif status == 'measured':
            self.status.set('Selected controls measured; full tuning coverage unqualified')
        elif result.get('tool') == 'optimizer' and result.get('best') is None:
            self.status.set('Optimization completed without a feasible candidate')
        else:
            self.status.set('Analysis completed; inspect qualification in the receipt')

    def _start(self, label, work, done):
        if self.busy:
            self._log('WARN', 'A task is active. Wait for completion or cancel it.')
            return
        self.busy = True
        self.job_token += 1
        token = self.job_token
        revision = self.revision
        self.cancel_event = threading.Event()
        self.cancel_button.configure(state='normal')
        for button in [self.netlist_button, self.native_button, self.discover_button, self.resolve_button] + list(self.family_buttons.values()) + self.tool_buttons:
            button.configure(state='disabled')
        self.variant_box.configure(state='disabled')
        self.status.set(label + ' running')
        self.progress.configure(mode='indeterminate')
        self.progress.start(15)
        self._log('INFO', label + ' started; inputs frozen.')
        def worker():
            try:
                result = work(self.cancel_event, lambda text: self.events.put(('log', token, str(text))))
                self.events.put(('done', token, (result, done, revision)))
            except BaseException as error:
                self.events.put(('error', token, (str(error), traceback.format_exc())))
        self.worker = threading.Thread(target=worker, name='vco-ui-' + str(token), daemon=True)
        self.worker.start()

    def _poll(self):
        try:
            while True:
                kind, token, payload = self.events.get_nowait()
                if token != self.job_token:
                    continue
                if kind == 'log':
                    self._log('INFO', payload)
                else:
                    self.busy = False
                    self.progress.stop()
                    self.progress.configure(mode='determinate', value=0)
                    self.cancel_button.configure(state='disabled')
                    for button in [self.netlist_button, self.native_button, self.discover_button, self.resolve_button] + list(self.family_buttons.values()) + self.tool_buttons:
                        button.configure(state='normal')
                    self.variant_box.configure(state='readonly')
                    if kind == 'error':
                        self.status.set('Cancelled' if self.cancel_event.is_set() else 'Failure: ' + payload[0])
                        self._log('ERROR', payload[1])
                    else:
                        result, done, revision = payload
                        self.status.set('Cancelled; task returned' if self.cancel_event.is_set() else 'Task returned; inspect its receipt')
                        if revision != self.revision:
                            self._log('WARN', 'Inputs changed while running; receipt retained without applying stale results.')
                            self._set_text(self.artifacts, json.dumps(result, ensure_ascii=False, indent=2))
                        else:
                            try:
                                done(result)
                            except Exception as error:
                                self._error(error)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.poll_id = self.root.after(80, self._poll)

    def cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.status.set('Cancel requested; waiting for task processes')
            self._log('WARN', 'Cancel requested for this session.')

    def clear_failure(self):
        if not self.busy:
            self.status.set('Ready; previous errors retained in the log')

    def diagnose(self):
        details = {'schema': 'vco.ui.diagnostic.v1', 'window_id': self.window_id, 'workspace': self.workspace.get(), 'runtime': str(self.runtime), 'install_root': str(INSTALL_ROOT), 'python': __import__('sys').executable, 'tk': self.root.tk.call('info', 'patchlevel'), 'busy': self.busy, 'binding_status': self.binding.get('status', 'unresolved'), 'unresolved': self.binding.get('unresolved', []), 'generated_dir': str(self.generated_dir) if self.generated_dir else None, 'log': str(self.log_path)}
        self._set_text(self.artifacts, json.dumps(details, ensure_ascii=False, indent=2))
        self.pages.select(2)
        self._log('INFO', 'Diagnostics displayed; inspect missing configuration.')

    def _error(self, error):
        self.status.set('Failure: ' + str(error))
        self._log('ERROR', str(error))

    def _log(self, level, text):
        line = '{} [{}] {}\n'.format(dt.datetime.now().strftime('%H:%M:%S'), level, text)
        with self.log_path.open('a', encoding='utf-8') as stream:
            stream.write(line)
        bottom = self.log_text.yview()[1] >= .98
        self.log_text.configure(state='normal')
        self.log_text.insert('end', line)
        self.log_text.configure(state='disabled')
        if bottom:
            self.log_text.see('end')

    def show_log(self):
        window = tk.Toplevel(self.root)
        window.title('VCO Design Studio — Log')
        fw.apply(window, product='vco')
        normalize_family_case(window)
        fw.header(window, 'VCO Design Studio', 'Session log').pack(fill='x')
        window.geometry('900x500')
        text = tk.Text(window, wrap='word')
        text.pack(fill='both', expand=True)
        bar = ttk.Frame(window)
        bar.pack(fill='x')
        level = tk.StringVar(value='All')
        picker = ttk.Combobox(bar, textvariable=level, values=['All', 'ERROR', 'WARN', 'INFO'], state='readonly', width=10)
        picker.pack(side='left')
        def refresh(*args):
            lines = self.log_path.read_text(encoding='utf-8').splitlines()
            if level.get() != 'All':
                lines = [line for line in lines if '[' + level.get() + ']' in line]
            self._set_text(text, '\n'.join(lines))
        def copy_log():
            window.clipboard_clear()
            window.clipboard_append(text.get('1.0', 'end-1c'))
        def save_log():
            path = filedialog.asksaveasfilename(parent=window, defaultextension='.log')
            if path:
                Path(path).write_text(text.get('1.0', 'end-1c'), encoding='utf-8')
        picker.bind('<<ComboboxSelected>>', refresh)
        ttk.Button(bar, text='Refresh', command=refresh).pack(side='left')
        ttk.Button(bar, text='Copy', command=copy_log).pack(side='left')
        ttk.Button(bar, text='Save visible log', command=save_log).pack(side='left')
        ttk.Label(bar, text=str(self.log_path)).pack(side='right')
        refresh()

    def _destroyed(self, event):
        if event.widget is self.root:
            if getattr(self, 'poll_id', None):
                try:
                    self.root.after_cancel(self.poll_id)
                except tk.TclError:
                    pass
                self.poll_id = None
            self.job_token += 1
            self.cancel_event.set()

    def close(self):
        if self.busy:
            self.cancel()
            self._log('WARN', 'Wait for cancellation before closing.')
            return
        self.root.destroy()


def main(workspace=None, project=None, runtime=None):
    root = tk.Tk()
    StudioApp(root, workspace=workspace, project=project, runtime=runtime)
    root.mainloop()
