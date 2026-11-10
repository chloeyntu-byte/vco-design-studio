---
name: eda-vco-noise-isf
description: Noise diagnosis for VCO Design Studio using explicit user inputs.
---

# Noise diagnosis

Use slope only as a triage hint. Compare absolute source contributions at failing offsets, total noise, frequency, power and waveform. Verify improvements across adopted controls.

Commands to inspect (help commands do not launch a solver):

```sh
./launch diagnose-result --help
./launch flow-next --help
```

Use Python 3.11+. See [README](../../README.md) and [binding configuration](../../docs/BINDINGS.md). Solver-dependent actions require user-owned models, a configured runtime and explicit execution authorization. Use bounded jobs, new output directories and receipts. Keep measured evidence separate from synthetic fixtures.
