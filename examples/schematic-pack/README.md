# Example structure pack

A structure pack holds, per variant, `<family>/<variant>.json` (packaging spec: modules, port binding,
symbols, top-level plan) and optionally `<family>/<variant>.ui.json` (paper-style topology drawing).
Point `VCO_STUDIO_SCHEMATIC_DIR` at a folder of packs (several folders separated by `:`). Without a
pack, the program derives the packaging from the circuit graph (magnetic structures, cross-coupled
pairs) and lays the top level out automatically. This folder contains one textbook example
(`class_b/nmos`) to show the format; see `docs/SCHEMATIC.md`.

```sh
VCO_STUDIO_SCHEMATIC_DIR=examples/schematic-pack ./launch schematic check class_b nmos
VCO_STUDIO_SCHEMATIC_DIR=examples/schematic-pack ./launch topology check class_b nmos
```
