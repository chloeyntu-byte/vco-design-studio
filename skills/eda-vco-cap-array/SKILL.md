---
name: eda-vco-cap-array
description: Design and diagnose VCO switched-capacitor banks, tuning range, bit count/LSB, on-resistance, off-state bias loading and bank-ac admittance, including AMP_LOW and model-domain failures.
---

# Switched-capacitor banks

Use for insufficient/excessive TR, maximum-step failures, high-code PN degradation, AMP_LOW, excessive tank loss and illegal bank geometry. Initial L/C/gm estimates belong to [initial-sizing](../eda-vco-initial-sizing/SKILL.md). Set `S=./launch`, `W=/path/to/task-root`, `PY` to the configured Python and `K` to this skill directory. Read [bindings](../../docs/BINDINGS.md) for user geometry limits.

## Diagnose the symptom

| Observation | First check |
|---|---|
| AMP_LOW, especially with many cells off | Off-state bias return loading |
| High-code PN_THERMAL is worse than low code | Bank G, switch loss and noise contributions |
| Endpoint sweep does not cover the band | Capacitance ratio and fixed parasitics |
| Center error with acceptable TR | L scaling before changing bank span |
| NO_OSC | Per-code tank G and regeneration |
| Generation domain error or model warning | Actual capacitor/resistor/switch geometry limits |
| Required max_step_hz or bit count | State-count lower bound and measured adjacent gaps |

F_HIGH/F_LOW on an endpoint is expected relative to the center; use the sweep's coverage summary before changing geometry.

## Budget span and bit count without simulation

- For fractional `t=2*(fmax-fmin)/(fmax+fmin)`, total capacitance ratio is `r=((2+t)/(2-t))^2`.
- Fixed C includes core, wiring, protected fixed capacitors and other banks: `r=(Cfix+Con)/(Cfix+Coff)`. Required switched span is at least `(r-1)*(Cfix+Coff)`; changing L alone cannot fix the ratio.
- With fixed L, one mode and weak dispersion, endpoint measurements give `rm=(fhi/flo)^2` and `Cfix=(Con-rm*Coff)/(rm-1)`. This approximation can change with swing and layout parasitics; retain margin and remeasure.
- An N-bit discrete bank covering the band must have maximum gap at least `(fmax-fmin)/(2^N-1)`. For equal-unit binary cells, a first high-edge estimate is `fmax*(1-(1+(r-1)/(2^N-1))^(-1/2))`. Passing this estimate still requires measured sorted-code gaps.
- N bits use `2^N-1` equal unit cells. One more bit at fixed unit size roughly doubles span and off-state load; fixed total span requires smaller units, which may violate model limits.

## Measure biased admittance

`bank-ac` is Studio's headless two-port AC measurement; Cap Array Lab is an optional external postprocessor/viewer, not a replacement solver CLI. Real AC requires user models and authorized solver execution.

```sh
$S bank-ac "$W/task/candidates/candidate_001" --output "$W/bankac" --bank main --codes 0,15 --freq-ghz 3,4,5,6
$PY "$K/scripts/bankac_table.py" "$W/bankac" --at 0:<low-f0-GHz>,15:<high-f0-GHz>
$PY "$K/scripts/bankac_table.py" "$W/bankac" --cfix 0:<low-f0-GHz>,15:<high-f0-GHz> --bits 4
```

These codes/frequencies illustrate a four-bit bank only. Use `2^N-1` as the all-off code, actual register polarity and at least two increasing frequencies covering observed f0. Outputs must be new directories; solver jobs inside a candidate directory count toward the flow budget.

Set `--vcm` for the actual biased port. For a single-ended aux bank whose N terminal is VSS, use the applicable ground-referenced bias and read `p_input=Y11`; a default mid-supply bias can underdrive its switches. Inspect generated connections rather than assuming every aux bank has the same bias.

| Output | Meaning and use |
|---|---|
| diff C/G/R/Q, decomposition.total | Floating P–N equivalent; use C for differential tank budgeting and R=1/G for differential loading |
| p_ground/n_ground | Per-side shunt branches; unavailable for a nonreciprocal matrix, approximate fits are marked |
| p_input (Y11) | P driven with N AC grounded; appropriate for a grounded single-ended bank |
| Q | Valid only for capacitive response with positive G; a negative coupling-branch G alone is not an error |

Do not replace an unavailable per-side value with Y11. `--tank-inductance-h` adds an assumed passive L and omits core/fixed C: its resonance is not a measured VCO frequency. Static AC does not qualify PN/startup; `qualified=false` identifies the postprocessing scope. `caplab-export` exports a passive projection, not measured AC data.

## Separate loss mechanisms

For a cell capacitor Cm feeding bias return Rb, `Gu=(omega*Cm)^2*Rb/(1+(omega*Cm*Rb)^2)`, approximately `1/Rb` when `omega*Cm*Rb >> 1`. With Noff cells, `Gside≈Noff/Rb` and `Rbank,diff≈2*Rb/Noff`. Compare these with measured bank G and same-port tank Rp; they are approximations, not model guarantees.

Increasing Rb can reduce high-code loading but raises low-frequency resistor noise that may upconvert through junction capacitance. Check source contributions at all PN targets. For two series capacitors and switch Ron, `Qon≈2/(omega*Cm*Ron)`; widening the switch reduces Ron but increases Coff and may reduce TR.

| Finding | Candidate change |
|---|---|
| Insufficient TR | Increase bank span within legal geometry, then recenter L |
| Excessive TR | Reduce span or bit count, then recenter L |
| High-code loading | Increase legal bias-return impedance; remeasure noise and all-off state |
| Low-code Ron loss | Increase legal switch strength; remeasure Coff/TR |
| Excessive adjacent gap | Increase usable states if geometry allows; otherwise report the gap |
| Both endpoints poor | Inspect tank/core loss and [gm-core](../eda-vco-gm-core/SKILL.md) |

Change one class of parameters at a time. `bank_cap_scale` scales capacitor geometry; it does not automatically scale switches or bias resistors. Overrides take absolute values after scaling, and generation checks only declared binding domains. Never infer a process rule from a bundled example. If the unit hits its minimum size, fewer bits can reduce span/load; finer steps may then require a topology/device change. Ideal resistors conceal physical loading/noise and must be disclosed as a limitation.

## Return to the flow

```sh
$S project hegazi --variant tail --spec "$W/spec.json" --set bank_cap_scale=1.3 --set L_scale=0.85 --set main.bits=5 --output "$W/p-next.json"
$S flow-add "$W/task" --project "$W/p-next.json" --reason "bank admittance evidence and one loading hypothesis" --evidence vco-card-symptom-amp-low-bank-loading
$S flow-sweep "$W/task" <candidate-id> --codes "$W/codes.json"
$S flow-next "$W/task"
```

Use actual measured scale factors and legal overrides. After enlarging TR, recheck gain at the lowest-frequency code: added C and smaller L can reduce Rp and gm·Rp. Coverage/step acceptance goes to [calibration](../eda-vco-calibration/SKILL.md); auxiliary return mechanisms to [cmr](../eda-vco-cmr/SKILL.md); PN attribution to [noise-isf](../eda-vco-noise-isf/SKILL.md). Freeze and delivery follow the [main workflow](../eda-vco-design/SKILL.md).
