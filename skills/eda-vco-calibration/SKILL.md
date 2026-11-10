---
name: eda-vco-calibration
description: Control calibration for VCO Design Studio using explicit user inputs.
---

# Control calibration

Calibrate every adopted main/aux pair on the same physical design. Check endpoints, adjacent steps and independent convergence. Sparse samples cannot prove full-band coverage.

Commands to inspect (help commands do not launch a solver):

```sh
./launch flow-sweep --help
./launch flow-converge --help
```

Use Python 3.11+. See [README](../../README.md) and [binding configuration](../../docs/BINDINGS.md). Solver-dependent actions require user-owned models, a configured runtime and explicit execution authorization. Use bounded jobs, new output directories and receipts. Keep measured evidence separate from synthetic fixtures.
