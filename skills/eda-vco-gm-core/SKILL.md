---
name: eda-vco-gm-core
description: Diagnose LC VCO core sizing, bias, regeneration margin, NO_OSC and startup/steady-state warnings; distinguish DC gain screens from periodic and transient evidence.
---

# Core bias and startup

Use for NO_OSC, STEADY_WARN and core-related P_HIGH/P_HEADROOM/AMP_LOW. Read the candidate, model domains, controls, result and solver log; record VDD, temperature, output port and frequency. TIMEOUT/SOLVER_ERROR first require log/runtime diagnosis. Check [bank loading](../eda-vco-cap-array/SKILL.md) when amplitude is low.

Set `S=./launch`, `W=/path/to/task-root`; read `flow-status "$W/task" --brief` and `flow-next "$W/task"` for evidence and budget.

## Measure regeneration before changing geometry

1. Check every core model, finger width, nf, multiplier, length and bias against user model limits. Use `Wtotal=Wfinger*nf*m` only after confirming the model's multiplication semantics. Record gm, gds, vgs, vds and loading. Longer channels can reduce flicker while increasing parasitics.
2. Run the same-port startup gain measurement at the observed oscillation frequency:

```sh
$S loopgain <generated-candidate-dir> --output "$W/loopgain" --f-ghz <HB-f0>
```

Read `loopgain.json`: `gm_Rp_ext`, `margin=G_neg/G_loss`, `Rp_diff_ohm`, `merged` and `valid`. The method cancels measured port gm for the loss estimate; transformer gain is observed at the drain port, and dual-core merging follows the HB mode phase. A zero-supply passive result is only an upper bound on loaded Rp. See [calculation scope](../../docs/METHODS.md).

3. Roughly gm·Rp <3 suggests inadequate gain; increase L at fixed f, core strength or useful current. Above roughly 6, compare an excess-gain variant with a smaller core or lower power using measured FoM. A large drop from passive to biased Rp points to core gds/loading. These are single-pair heuristics, not hard pass/fail limits; complementary/multiple-core designs must use the measured same-port margin. Self-biased CMOS may draw much more current at the DC balance point than in periodic operation: DC gain describes startup, not steady-state swing.
4. For NO_OSC inspect the actual startup/mode settings and full return path, not just frequency guess. Check model validity and losses. Use available power headroom, then consider wider core or higher L; width also adds capacitance. The `vdc` knob applies to current-biased variants, `core_width_scale` to voltage-biased cores. Top bias may have the opposite current direction; remeasure power.

## Register and remeasure a change

```sh
$S project hegazi --variant tail --spec "$W/spec.json" --set core_width_scale=1.2 --output "$W/p-next.json"
$S flow-add "$W/task" --project "$W/p-next.json" --reason "measured gain deficit and one bias/core change" --evidence vco-card-procedure-design-loop
$S flow-measure "$W/task" <returned-candidate-id>
$S flow-next "$W/task"
```

For an existing candidate, edit a copy preserving its other parameters rather than resetting them from a template. In project JSON, set `simulation.save_startup_waveform=true` and a suitable `simulation.tstab_ns` when startup evidence is needed; generate and measure a new candidate.

## Check startup separately

Read `psf/hb.tran.pss_hb.tran` when saved; rerun if absent. With `K` set to this skill directory and `PY` to the configured Python, use [startup_check.py](scripts/startup_check.py):

```sh
$PY "$K/scripts/startup_check.py" <result-directory> --pos <positive-node> --neg <negative-node>
```

Inspect initial perturbation, envelope growth, tail-window amplitude/frequency and logs. The helper's default tail spread and t90 thresholds are screening settings; combine them with the waveform and native solver result.

| Observation | Next step |
|---|---|
| Growth followed by stable tail | Retain controls, initial conditions, waveform and logs; verify independent periodic convergence |
| Growth still settling | Extend tstab, rerun and check amplitude/frequency |
| Decay or NO_OSC | Quantify same-port gain/loss, then make one bounded bias/geometry change |
| Missing/truncated waveform or wrong mode | Check saved nodes, mode, settings and logs; startup remains unknown |
| Power limited and extra width ineffective | Examine added parasitics and bank loss; compare [architectures](../eda-vco-architecture/SKILL.md) |

HB success does not prove transient startup; transient screening does not replace periodic convergence. Evidence applies only to the tested code, initial condition and corner. Fair Class-B comparisons require matched tank, core, frequency, power and optimized legal bias choices. Record untested states and return to the [main workflow](../eda-vco-design/SKILL.md).
