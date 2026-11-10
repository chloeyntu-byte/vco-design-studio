---
name: eda-vco-initial-sizing
description: Initial sizing for VCO Design Studio using explicit user inputs.
---

# Initial sizing

Use ideal LC and gain estimates as initial guesses, then apply user geometry limits and recharacterize. Do not treat example dimensions as characterized seeds.

Commands to inspect (help commands do not launch a solver):

```sh
./launch spec-fields
./launch seed --help
```

Use Python 3.11+. See [README](../../README.md) and [binding configuration](../../docs/BINDINGS.md). Solver-dependent actions require user-owned models, a configured runtime and explicit execution authorization. Use bounded jobs, new output directories and receipts. Keep measured evidence separate from synthetic fixtures.
