---
name: eda-vco-initial-sizing
description: Estimate first LC VCO inductance, capacitance, gm, tuning span and bit count, or choose measured seeding versus bounded autosizing after architecture selection.
---

# Initial sizing

Inputs: validated spec, family/variant, user binding/model limits, VDD, temperature, band/TR, power cap, PN offsets and budget. For hand sizing also record core power fraction, same-port Q at f0, target regeneration gain and gm/Id or measured negative conductance. Mark assumptions when data are absent. PN requirements guide selection and later measurement; algebra alone cannot predict compliance.

Use `S=./launch`, `W=/path/to/task-root` and [calculation scope](../../docs/METHODS.md). Power → gm → target gm·Rp → Rp → Q → L/C → bank → swing is one method; `vco_studio/sizing.py` implements the hand calculation.

## Choose a starting path

```sh
$S select --task "$W/task"
$S seed --task "$W/task" --family hegazi --variant tail --output "$W/seed.json"
# Or, with user solver configuration and an explicit budget:
$S autosize --pdk <binding-id> --family hegazi --variant tail --f0-ghz <GHz> --power-mw <mW> --tr <fraction> --bits <N> --budget <N-runs> --output "$W/autosize"
# Or, without a measured seed:
$S plan --spec "$W/spec.json" --q 20 --gain 4 --output "$W/plan.json"
```

The plan's Q and gain above are illustrative assumptions, not measured values. `seed.source_cell=null` means template sizing. A measured seed is rescaled and still needs legal-domain checks and new measurements. Autosize recenters frequency, adjusts power and bank span, measures gain, and compares variants; it has its own simulation accounting, so reconcile it with the task budget first.

Use the plan's gm, Rp, differential L, endpoint C and bit count to construct a project. Inspect template component values before converting absolute targets to scale factors; differential inductance is not automatically the value of each template inductor.

```sh
$S project hegazi --variant tail --spec "$W/spec.json" --set L_scale=1.2 --set main.bits=4 --output "$W/p1.json"
$S flow-add "$W/task" --project "$W/p1.json" --reason "sizing assumptions and first gain test" --evidence vco-card-procedure-design-loop
$S flow-measure "$W/task" <candidate-id>
$S loopgain <generated-candidate-dir> --output "$W/gain" --f-ghz <measured-HB-f0>
$S flow-next "$W/task"
```

## Interpret the first result

| Result | Decision |
|---|---|
| ideal_constraints_satisfied | Algebraic screening only; verify startup, loss, coverage and PN with real models |
| `(fmax-fmin)/(2^N-1) > max_step_hz` | Discrete N-bit coverage is impossible at the requested step without additional controls |
| Fixed/off-state capacitance exceeds high-frequency budget | Rebalance L/bank or architecture; measure full bank admittance |
| Measured gm·Rp below roughly 3 | Increase regeneration or reduce loss; compare L, core width and useful current |
| Measured gm·Rp above roughly 6 | Excess gain is allowed; compare smaller-core/lower-power variants using measured FoM |
| Frequency error with acceptable TR | Adjust L before changing bank span |
| NO_OSC/AMP_LOW | Check same-port negative conductance and loading with [gm-core](../eda-vco-gm-core/SKILL.md) and [cap-array](../eda-vco-cap-array/SKILL.md) |
| PN fails after a legal oscillating design exists | Use [noise-isf](../eda-vco-noise-isf/SKILL.md); an architecture gap goes to [architecture](../eda-vco-architecture/SKILL.md) |

Gain thresholds are design heuristics, not qualification limits. Startup-point DC gain and periodic steady-state loading differ, especially for self-biased complementary cores. Keep each altered geometry as a new candidate and return to the [main workflow](../eda-vco-design/SKILL.md) after measurement.

The optional [initial_sizing.py](scripts/initial_sizing.py) wrapper uses the same calculation implementation; [profile_sweep.py](scripts/profile_sweep.py) explores assumed inputs and [inductor_q.py](scripts/inductor_q.py) checks user inductance/Q data. Their outputs remain estimates until validated.
