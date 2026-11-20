# VCO Design Studio v1.1.1

**Compare and tune the five structures in your browser: [VCO Structure Lab](https://rf-fieldwork.pages.dev/vco-lab/?lang=en).**
Each structure sits on a tuning bench backed by more than 400 harmonic-balance and phase-noise simulations
(65 nm CMOS, inductor Q = 20): click an element on the schematic, move its knob and watch phase noise, FoM,
alignment maps and the first and second resonance respond. The README now has a gallery of the lab.

Changes in this release:
- `launch topology check` now also verifies that the dot marks on coils agree with the signs of the mutual couplings.
- README gallery and links for the VCO Structure Lab.

Product page and narrated film: https://rf-fieldwork.pages.dev/vco/

---

## Previous release: v1.1.0


v1.1.0 adds a packaged schematic hierarchy, layout-driven native export and a "Topology vs paper" page.

- Every generated design is packaged into a reusable hierarchy with readable instance names and drawn symbols: one-turn winding inductor cells (pins compatible with an EM n-port view), cross-coupled gm, bias, switched-capacitor arrays, units and inverter, and a top symbol that shows the structure.
- Top-level placement comes from user-provided structure packs (`VCO_STUDIO_SCHEMATIC_DIR`; a Class-B example ships in `examples/schematic-pack/`). Without a pack the packaging is derived from the circuit graph and laid out automatically.
- Before any OA is written, a flattened equivalence check (devices, effective parameters, partition of terminals into nets, top-level ports) and a geometric connectivity check (T junctions, no terminal mid-wire, no four-way junctions) must pass.
- Native export lays out with pin geometry probed from the bound masters, writes real wires, labels and symbol shapes, verifies the OA readback and native netlist, and fails on error-severity check markers.
- New commands: `launch schematic describe|check|preview` and `launch topology check|render`.
- The topology page draws each structure in paper style, labels devices with their schematic instance paths, and shows the citation, figure and page of the source paper. Figure images are not distributed (`VCO_STUDIO_PAPER_FIGURES` points at your own crops).

Not included: real structure packs (beyond the Class-B example), real-PDK bindings, real-design knowledge/evidence/lookup data, and the Cadence environment. See CHANGELOG.md for details and README.md for the quick start.

Product page and narrated film: https://rf-fieldwork.pages.dev/vco/

---

## Previous release: v1.0.0

The public release provides a Python 3.11+ desktop and CLI workbench for browsing LC VCO structures and generating circuit files. The UI uses the shared RF-FIELDWORK theme.

All bundled device names, geometry, domain limits and PSF-format samples are fictional software fixtures. No working semiconductor model, measured performance table or host configuration is included. Existing calculation and workflow contracts are exercised with synthetic inputs; optional installed-tool tests require user configuration. Real Cadence, solver and physical signoff validation remain outside this release.

The Class-B files in `data/baselines/class_b/` are rewritten synthetic examples with `provenance.kind: synthetic_example`, not measured baselines. Their component values support software demonstration only.

See README.md for the offline quick start, docs/BINDINGS.md for user configuration and docs/KNOWLEDGE_REVIEW.md for card-level publication decisions.

Agent skills include the public specification-to-delivery workflow and specialist diagnosis, control calibration and qualification criteria. Measurement indexes and failure ledgers come only from user configuration. Optional external integrations are documented in README; the external circuit optimizer uses the public `optimizer` site/action/receipt identifier. Validation of this revision discovered 114 tests: 107 passed and 7 optional installed-tool tests skipped.
