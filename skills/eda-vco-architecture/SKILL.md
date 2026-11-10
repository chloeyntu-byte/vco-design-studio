---
name: eda-vco-architecture
description: Architecture selection for VCO Design Studio using explicit user inputs.
---

# Architecture selection

Compare explicit return filtering, coupled common-mode resonance, inverse-F shaping, fixed broadband return and dual-core tracking. Record allowed controls and a matched Class-B baseline.

Commands to inspect (help commands do not launch a solver):

```sh
./launch catalog
./launch describe hegazi --variant tail
./launch select --help
```

Use Python 3.11+. See [README](../../README.md) and [binding configuration](../../docs/BINDINGS.md). Solver-dependent actions require user-owned models, a configured runtime and explicit execution authorization. Use bounded jobs, new output directories and receipts. Keep measured evidence separate from synthetic fixtures.
