---
name: eda-vco-cap-array
description: Switched capacitor banks for VCO Design Studio using explicit user inputs.
---

# Switched capacitor banks

Inspect on-resistance, off-state loading, bias return and full biased admittance. Use only user device domains. Distinguish differential and single-ended loads.

Commands to inspect (help commands do not launch a solver):

```sh
./launch bank-ac --help
```

Use Python 3.11+. See [README](../../README.md) and [binding configuration](../../docs/BINDINGS.md). Solver-dependent actions require user-owned models, a configured runtime and explicit execution authorization. Use bounded jobs, new output directories and receipts. Keep measured evidence separate from synthetic fixtures.
