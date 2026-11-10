---
name: eda-vco-cmr
description: Harmonic return for VCO Design Studio using explicit user inputs.
---

# Harmonic return

Define the actual differential/common-mode port and harmonic. Keep fixed networks fixed during tuning sweeps. Examine return phase, damping and mode separation.

Commands to inspect (help commands do not launch a solver):

```sh
./launch describe murphy --variant nmos
./launch z11 --help
```

Use Python 3.11+. See [README](../../README.md) and [binding configuration](../../docs/BINDINGS.md). Solver-dependent actions require user-owned models, a configured runtime and explicit execution authorization. Use bounded jobs, new output directories and receipts. Keep measured evidence separate from synthetic fixtures.
