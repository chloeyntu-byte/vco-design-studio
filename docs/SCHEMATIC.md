# Schematic packaging, symbols and top-level plans

The simulation graph (`data/topologies/...`) stays flat at the top and is never edited for
drawing. `vco_studio/schematic.py` packages it into a reusable hierarchy for Virtuoso and the
UI, following a conventional Class-B VCO cell hierarchy (top → gm core, capacitor array → unit →
inverter, inductor, tail):

| level | cell (suffix after the top cell name) | instance name at top | contents |
|---|---|---|---|
| top | `<TOP>` | — | module symbols + fixed caps / damping resistors, real wires |
| inductor | `IND_TANK`, `IND_TAIL`, `IND_XFMR`, `IND_HEAD` | `L_TANK`, `L_TAIL`, `L_XFMR`, `L_HEAD` | windings, series/parallel loss, all mutual couplings of that magnetic structure |
| gm | `GM`, `GM_N`, `GM_P` | `GM`, `GM_N`, `GM_P` | core transistors |
| bias | `BIAS` | `ISRC` | current-source device and its bypass cap |
| array | `CT_MAIN`, `CT_AUX` | `CT_MAIN`, `CT_AUX`, `CT_DM`, `CT_CMP` … | binary units `U<bit>_<k>` |
| unit | `CTU_MAIN`, `CTU_AUX` | `U<bit>_<k>` | `MSW`, `RBP`/`RBN`, `CP`/`CN`, `INV` |
| inverter | `INV` | `INV` | `MP`, `MN` |

One magnetic structure is one inductor cell: mutual couplings never cross a cell boundary
(Spectre `mutual_inductor` needs both windings in one scope). Inductor cells are drawn as
one-turn winding outlines (SNPFit style) so the symbol can later be swapped for an EMX/S-parameter
`nport` view with the same port names. An inductor cell may expose an internal floating node
(e.g. the Lim/Murphy-CMOS transformer centre `MID`) as a port for that purpose.

Electrical identity: `schematic.equivalence()` flattens the packaged hierarchy and the
electrical graph (with effective hierarchy parameters) and requires the same devices and
parameters, the same partition of device terminals into nets (internal nets stay scoped, so a
missing port is caught) and the same terminals on every top-level port. Native export refuses a
non-equivalent packaging, lays out with the pin geometry probed from the bound masters (and
their bound view), and fails on any error-severity Check marker.

Register widths: array symbols keep P/N/VDD/VSS in place for 1–9 bits; when the widths differ
from a plan, its bit routing is replaced by automatic stubs, pins are added in a free column
and the top symbol's bit pins are regenerated in the drawn column.

## Structure packs

Per-variant packaging, symbols, top-level plans and topology drawings live in **structure packs**:
`<family>/<variant>.json` and `<family>/<variant>.ui.json`. Packs are user-provided and are not
part of this repository; point `VCO_STUDIO_SCHEMATIC_DIR` at one or more pack folders (separated by
`:`), searched before `data/schematic/`. `examples/schematic-pack/` contains one textbook example
(`class_b/nmos`).

Without a pack the packaging is derived from the circuit graph: every magnetic structure (windings
joined by couplings, with their series/parallel loss) becomes an inductor cell, every cross-coupled
transistor pair a gm cell, arrays keep their own cells; module symbols are generic and the top
level is laid out automatically with label stubs. The same equivalence and connectivity checks apply.

Other files:

- `data/schematic/master_symbols.json` — pin positions of the analogLib/basic masters. Other
  masters get placeholder geometry for previews; native export probes the masters of the resolved
  binding and lays out with their real pin positions.
- `data/papers/index.json` — source figures (paper, figure, physical PDF page, crop box) of the five
  families. Images are not distributed; put your own crops in `data/papers/figures/` or set
  `VCO_STUDIO_PAPER_FIGURES`. The papers and their figures remain under their publishers'
  copyright; this repository's licence does not cover them.

## Commands

```bash
./launch schematic describe <family> <variant> [--spec F]   # nets + pin offsets
./launch schematic check    <family> <variant> [--spec F]   # equivalence + connectivity
./launch schematic preview  <family> <variant> [--spec F] --out DIR [--all]
./launch topology  check    <family> <variant>              # UI drawing vs packaged schematic
```

`check` extracts nets from the plan geometry with the schematic editor's rules: wire endpoints
and vertices join what they touch, including the middle of another wire (T junction); a device
or pin terminal lying in the middle of a wire is an error; crossings without a vertex do not
connect; a label names the wire group it lies on. Every terminal that no wire reaches gets an
automatic 0.25 stub and net label (used for control bits and repeated rails).

## Top plan format

```json
"top": {
  "instances": {"L_TANK": {"xy": [0, 3], "orient": "R0"}, "...": {}},
  "wires":  [{"net": "OUTP", "pts": [[-0.25, 1.875], [-0.25, 1.0], [-2.0, 1.0]]}],
  "labels": [{"net": "VDD", "xy": [0, 4.25]}],
  "pins":   {"OUTP": {"xy": [6, 1], "side": "R"}, "VDC": {"xy": [-6, -3], "side": "L"}},
  "notes":  ["why this arrangement"]
}
```

All coordinates are multiples of 0.0625 (schematic grid). Orientations: R0 R90 R180 R270 MX MY
MXR90 MYR90 (Cadence semantics; `MY` mirrors x). A pin with `side: L` gets a 0.5 stub to the
right unless a wire already ends on it.

Explicit symbol format (`symbols.<CELL>` or `top_symbol`):

```json
{"shapes": [{"t": "line", "pts": [[x,y],...]}, {"t": "rect", "box": [x0,y0,x1,y1]},
            {"t": "ellipse", "box": [...]}, {"t": "arc", "box": [...], "start": -90, "stop": 90},
            {"t": "poly", "pts": [...]},
            {"t": "label", "xy": [x,y], "text": "P", "h": 0.05, "just": "centerLeft", "layer": "pin"},
            {"t": "label", "xy": [x,y], "text": "[@instanceName]", "layer": "instance"}],
 "pins": {"P": [x, y], "N": [x, y]}}
```

Pins must sit on the grid and match the cell ports exactly.

## Drawing rules

1. Reproduce the paper figure's arrangement at the top level: magnetic structure on top (or as
   the paper places it), active core below, arrays across the nodes they load, bias/tail at
   the bottom centre; P side left, N side right, mirror-symmetric about the vertical axis.
2. Higher potential up, VSS down. Real wires for the oscillation nodes, tank, centre taps and
   tail/source nodes; labels for repeated rails and control buses.
3. Pins at the margins: controls and VDC on the left, outputs on the right, VDD top-left, VSS
   bottom-left.
4. No different-net T junctions, no terminal in mid-wire, no four-way junctions (offset one branch), as few crossings as possible.
5. The top symbol shows the family's structural signature; supplies top/bottom, controls left,
   outputs right.
