# Changelog

## v1.1.0

- Schematic packaging (`vco_studio/schematic.py`, `docs/SCHEMATIC.md`): every generated design is packaged into a reusable hierarchy with readable instance names and drawn symbols — one-turn winding inductor symbols whose pins stay compatible with an EM n-port view, cross-coupled gm, bias, switched-capacitor arrays, units and inverter — and a top-level plan. Per-variant packaging, plans and topology drawings come from user-provided structure packs (`VCO_STUDIO_SCHEMATIC_DIR`, example in `examples/schematic-pack/`); without a pack the packaging is derived from the circuit graph and laid out automatically. A flattened-netlist equivalence check and a geometric connectivity check run before any OA is written.
- Native export uses the packaged hierarchy, lays out with the pin geometry probed from the masters (and bound views) of the resolved binding, writes real wires, labels and symbol shapes, verifies the OA readback and the native netlist, and fails on error-severity schematic check markers.
- The equivalence check compares effective (hierarchy-resolved) parameters and the partition of terminals into nets, so an unconnected module port or a changed override is reported. Register widths other than the drawn plan get automatic bit pins and stubs; array symbols keep their power and signal pins in place for 1-9 bits.
- New commands: `launch schematic describe|check|preview` and `launch topology check|render`.
- New "Topology vs paper" page: a paper-style drawing (from a structure pack) whose device labels are schematic instance paths (checked against the packaged schematic), beside the citation, figure number and page of the source paper (`data/papers/index.json`). Figure images are not distributed; point `VCO_STUDIO_PAPER_FIGURES` at your own crops.

## v1.0.0

- First public release with five topology families, CLI and agent workflow skills.
- Apply the RF-FIELDWORK VCO theme, dark circuit plots, explicit status feedback and an offline example entry.
- Replace machine profiles, device bindings, measured evidence and private reference geometry with user configuration and synthetic fixtures.
- Document offline versus user-provided EDA capabilities in English and Chinese.
