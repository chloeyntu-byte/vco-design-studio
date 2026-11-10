---
name: eda-vco-cmr
description: Diagnose second-harmonic/common-mode return, auxiliary-code tuning and harmonic shaping in Hegazi, Murphy, Lim, Guo and Du VCO networks.
---

# Second harmonic and common-mode return

Use after establishing the main tank and operating point. PN_FLICKER can trigger return-path investigation; PN_THERMAL first calls for power/loss checks. A diagnosis code is a clue, not source attribution. Retain candidate identity, actual main/aux widths, legal codes, f0, power, PN offsets, HB waveforms and noise contributions.

Define the actual port, current direction, phase reference and termination. Preserve bias, supplies, fixed branches, mutual dots, other core and loads. Compare only matching conditions. NO_OSC, solver errors and missing steady-state evidence go through the [main workflow](../eda-vco-design/SKILL.md) before mechanism claims.

## Inspect and tune the actual return

Set `S=./launch`, `W=/path/to/task-root`.

```sh
$S flow-next "$W/task"
$S describe murphy --variant nmos
$S kb show vco-card-arch-murphy
```

1. In Hegazi/Murphy/Lim, hold geometry and main code fixed, scan legal aux codes broadly, then refine around feasible minima. This example assumes main=7 and a real four-bit aux register:

```sh
printf '%s\n' '[{"main":7,"aux":0},{"main":7,"aux":7},{"main":7,"aux":15}]' > "$W/codes.json"
$S flow-sweep "$W/task" <candidate-id> --codes "$W/codes.json"
$S flow-next "$W/task"
```

Record result paths, frequency, power, every target PN offset and startup/convergence warnings. If the best tested code lies at the search edge, extend to untested legal neighbors. If it lies at the physical limit, consider a new bank/network candidate. The best aux at mid-main must be rechecked at endpoints and across the adopted grid; joint control mapping belongs to [calibration](../eda-vco-calibration/SKILL.md).

2. At each measured f0 examine second-harmonic voltage/current phasors and modes near 2*f0. Sweep a lossy network using measured complex bank Y. Studio can measure the auxiliary two-port AC matrix:

```sh
$S bank-ac "$W/task/candidates/candidate_001" --bank aux --codes 0,7,15 --freq-ghz 18,20,22 --vcm 0.5 --output "$W/aux-ac"
```

Replace frequencies, codes and common-mode bias with actual conditions. `V2=Z2*I2` applies only to the same port and convention in a linear effective network. Static AC is screening; periodic incremental/harmonic conversion analysis is needed for periodic behavior. An optional Z11 passive solve does not include the whole active oscillator or prove noise improvement.

3. Compare PN and source contributions at similar frequency/power. A 100 kHz–1 MHz drop greater than 25 dB/dec hints at flicker upconversion. Check tail, core, bank and magnetic-loss sources and total closure. A waveform or static impedance peak alone cannot establish PN benefit. Use [noise-isf](../eda-vco-noise-isf/SKILL.md) for signed weighting and independent noise closure.

4. Guo/Du have fixed returns without independent aux in these templates. Sweep the shared main controls and inspect mode tracking. Changes to coupling, compensation or damping create new physical candidates; register them with `flow-add`, remeasure and run `flow-next`.

## Decisions

| Observation | Action |
|---|---|
| PN improves within legal aux range and spec | Refine neighboring codes; check source change, endpoints, startup and convergence |
| Best point at tested edge | Extend legal search; at physical limit, evaluate new range/network |
| 2f peak aligns but PN does not improve | Check mode, phase, damping and periodic source weighting |
| High-code amplitude/PN degrades | Measure full bank Y and bias-return loading with [cap-array](../eda-vco-cap-array/SKILL.md) |
| Frequency jumps or another core behaves abnormally | Check wrong mode, dot polarity, rejection path and supplies; retain failures |

Useful cards: `vco-card-arch-overview`, `vco-card-arch-hegazi`, `vco-card-arch-murphy`, `vco-card-arch-lim`, `vco-card-arch-guo`, `vco-card-arch-du` and `vco-card-arch-classb`. Read their applicability before citing IDs.

Record modes, per-code results, attribution and unverified items in `DECISIONS.md`. Return to the [main workflow](../eda-vco-design/SKILL.md) for convergence, freeze and delivery. The optional [Z11 model checker](scripts/check_z11_model.py) accepts a user `--z11-root` and tests analytic units/finite-Q admittance only.
