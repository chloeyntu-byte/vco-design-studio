---
name: eda-vco-design
description: VCO design entry for VCO Design Studio using explicit user inputs.
---

# VCO design entry

Read requirements, inspect the catalog and choose a mechanism. Use user bindings and evidence; never infer performance from the examples.

Commands to inspect (help commands do not launch a solver):

```sh
./launch catalog
./launch spec-check examples/spec.json
./launch describe murphy --variant nmos
```

Use Python 3.11+. See [README](../../README.md) and [binding configuration](../../docs/BINDINGS.md). Solver-dependent actions require user-owned models, a configured runtime and explicit execution authorization. Use bounded jobs, new output directories and receipts. Keep measured evidence separate from synthetic fixtures.
