---
name: eda-vco-gm-core
description: Core and startup for VCO Design Studio using explicit user inputs.
---

# Core and startup

Check bias, startup gain, amplitude, tank loading and selected mode. A DC gain screen does not replace periodic or transient measurements.

Commands to inspect (help commands do not launch a solver):

```sh
./launch loopgain --help
./launch loopgain --help
```

Use Python 3.11+. See [README](../../README.md) and [binding configuration](../../docs/BINDINGS.md). Solver-dependent actions require user-owned models, a configured runtime and explicit execution authorization. Use bounded jobs, new output directories and receipts. Keep measured evidence separate from synthetic fixtures.
