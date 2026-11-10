---
name: eda-vco-architecture
description: Select, compare or upgrade LC VCO architectures when noise, power, tuning range or auxiliary-control constraints limit a design; include a fair Class-B comparison when useful.
---

# Architecture selection and upgrades

Use validated specs and measured candidates from the [main workflow](../eda-vco-design/SKILL.md). Set `S=./launch`, `W=/path/to/task-root`. Keep allowed auxiliary controls, VDD, temperature, frequency band, power cap, PN offsets, bit counts and solver budget explicit. The synthetic Class-B examples are software fixtures, not performance baselines.

## Compare requirements with evidence

For each offset compute `FoM_req = -PN_target + 20*log10(f/offset) - 10*log10(P_max_mW)`. The high band edge is a conservative screen; compare measured points using their actual frequency and power. Final acceptance uses per-adopted-code PN and power, not an extrapolated FoM.

```sh
$S select --task "$W/task"
$S table --pdk <user-binding-id>
$S evidence-query --pdk <user-binding-id> --frequency-hz <f> --power-mw <P> --vdd-v <V> --temp-c <T> --main-bits <N> --offset 1000000
```

Repeat queries at all target offsets. `select` queries nominal temperature evidence; use explicit `evidence-query --temp-c` and `--index` for other conditions. No measured lookup results ship with the release.

- `fom_1m_worst` is the minimum FoM over the table's representative codes. `margin_db = target_PN - predicted_PN`: nonnegative favors that candidate; negative means a demonstrated gap at that cell.
- `cell_distance = abs(ln(f_cell/f)) + 0.5*abs(ln(P_cell/P))`. Large distance or a stricter TR requirement weakens prediction. A failed nearby cell is not proof that the entire family is infeasible.
- If tables are empty, inspect nearby evidence and exclusions. If nothing matches, mark unknown and choose a minimal experiment based on mechanism, with an explicit cost and stopping point.
- `allow_aux=false` excludes the current Hegazi/Murphy/Lim implementations; Guo, Du and Class-B have no independent auxiliary code. Check the actual project registers.

## Mechanism shortlist

| Family/variant | Mechanism and controls | Check before adoption |
|---|---|---|
| hegazi/tail | Explicit tail inductor and bypass capacitor; aux; vdc power control | Tail flicker, return loss and edge-code alignment |
| hegazi/top | Top current source and harmonic return; aux; vdc | Headroom; increasing vdc can reduce current |
| hegazi/voltage | Removes current source, keeps source return; aux; core_width_scale | Supply isolation and far-offset noise |
| murphy/nmos | Coupled differential/common-mode resonance; aux; core_width_scale | Dot polarity, mode selection and modal Q |
| murphy/cmos | Complementary core with transformer return; aux; core_width_scale | Swing clipping and multiple bank losses |
| lim/inverse_f | Drain/gate transformer dual resonance; aux; core_width_scale | Near-offset upconversion and unwanted modes |
| guo/wideband | Fixed broadband head return; no aux; vdc | Return bandwidth and near-offset noise at band edges |
| du/autotracking | Dual complementary cores with shared main control; no aux | Total power/area, tracking and wrong-mode startup |
| class_b/tail or nmos | Conventional cross-coupled reference; no aux | Tail noise, bias and loading must be optimized fairly |

## Continue tuning or change family

| Evidence | Next experiment |
|---|---|
| PN_THERMAL with power headroom | Increase useful current before clipping; `10*log10(P2/P1)` is only a screening estimate |
| PN_THERMAL at power cap | Increase tank impedance at fixed f (L up/C down), reduce bank loss; then compare another family |
| PN_FLICKER with tail contribution | Legal longer tail channel and bypass/return filtering; then compare top/voltage bias or coupled/dual-core alternatives |
| PN_FLICKER in shaped-return designs | Locate source and periodic weighting before choosing another return mechanism |
| Only edge codes fail; AMP_LOW | Measure bank off-state loading and aux alignment before rejecting the family |
| NO_OSC or no aux noise minimum | Check regeneration, wrong mode, full return path and actual control range |
| MODEL_WARN/STEADY_WARN/TIMEOUT/SOLVER_ERROR | Resolve model/steady-state/runtime uncertainty before ranking performance |

A several-dB gap can justify a mechanism comparison after local loss/power issues are addressed; it is not a universal topology limit. Do not transfer a ranking between technologies, temperatures or offsets without measurements.

## Candidate experiment and fair reference

```sh
$S seed --task "$W/task" --family murphy --variant nmos --output "$W/p-next.json"
$S flow-add "$W/task" --project "$W/p-next.json" --reason "evidence, mechanism hypothesis, discriminating test" --evidence vco-card-procedure-structure-upgrade
$S flow-measure "$W/task" <returned-candidate-id>
$S flow-next "$W/task"
```

When a seed has no source, use [initial-sizing](../eda-vco-initial-sizing/SKILL.md). Check its binding, conditions, widths/codes and copied overrides before simulation. Reserve budget for representative controls and independent convergence, not just the middle code.

For Class-B compare matched frequency, power, tank/core conditions and loading. Optimize legal tail length, bias and bypass choices where applicable; an unoptimized noisy tail exaggerates apparent architectural gain. The Class-B tail template has no bypass capacitor and overrides cannot add missing instances; adding branches requires a reviewed topology change. Label custom-netlist results separately from flow-qualified results.

Retain rejected candidates with `flow-mark ... rejected --reason ...`. Record conditions, source IDs, hypothesis, experiment cost, qualification, result and adoption reason in `DECISIONS.md`. Return to the [main workflow](../eda-vco-design/SKILL.md); use [cmr](../eda-vco-cmr/SKILL.md), [noise-isf](../eda-vco-noise-isf/SKILL.md) or [calibration](../eda-vco-calibration/SKILL.md) for the next diagnosis.
