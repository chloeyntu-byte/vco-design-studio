---
name: eda-vco-calibration
description: Build adopted VCO control LUTs and joint main/aux mappings; verify band coverage, per-code noise/power, maximum frequency step, independent convergence and freeze qualification.
---

# Control calibration and freeze

Use after fixing candidate geometry, when `flow-next` requests endpoint sweeps, convergence or freeze, or when coverage has a step gap. One logical state uses the same physical codes for all offsets. Keep geometry, models, supply and bias fixed across the adopted domain. Changing them creates a new candidate through the [main workflow](../eda-vco-design/SKILL.md).

Inputs: actual task spec, candidate project/registers, result paths, target band or center/TR, power cap, every PN target, optional `max_step_hz`, implementable controls and budget. AMP_LOW goes to [cap-array](../eda-vco-cap-array/SKILL.md); unresolved model/startup/solver failures remain separate from calibration.

## Read back and sample controls

```sh
S=./launch
W=/path/to/task-root
T=$W/task
C=candidate_001
$S spec-check "$W/spec.json"
$S flow-status "$T" --brief
$S flow-next "$T"
# Only for actual four-bit main and aux registers:
printf '%s\n' '[{"main":0,"aux":0},{"main":8,"aux":6},{"main":15,"aux":13}]' > "$W/codes.json"
$S flow-sweep "$T" "$C" --codes "$W/codes.json" > "$W/sweep.json"
$S flow-next "$T"
```

Use `task.json`'s effective spec and the candidate's actual widths; N bits allow 0 through `2^N-1`. Omit absent aux; unspecified controls retain defaults.

- At each representative main, sample several spread aux values, then refine neighboring legal codes. Select using the smallest margin across all target offsets; apply a declared FoM/Pareto rule for tradeoffs.
- Aux may significantly shift frequency, especially in coupled networks. Choose logical states from the joint grid within frequency windows; multiple logical states may share a main value with different aux codes.
- Sort by measured frequency. Check reversals, overlap and maximum adjacent gaps, especially at the high edge where a unit capacitance change has larger frequency effect (`abs(delta_f)≈2*pi^2*L*f^3*delta_C`).
- Measure every adopted state. Reuse a result only when physical inputs and all controls match. Save logical state → physical controls → original result path; retain failures separately.

`flow-sweep.results` lists result paths; `table` gives controls/status/frequency/power/PN/FoM/amplitude/warnings. The summary covers successful points only and rounds values. Final acceptance uses raw `result.json.metrics`, not rounded summaries.

## Acceptance criteria

| Check | Required evidence |
|---|---|
| Domain identity | At least two distinct measured controls on the same physical design, with no unresolved model/measurement failures |
| Fixed band | min(f) at or below band low, max(f) at or above band high |
| Center/TR mode | Requested center tolerance and minimum TR |
| Maximum step | Sorted adopted frequencies satisfy max adjacent gap <= spec.max_step_hz when specified |
| Every code/offset | PN <= target at each requested offset and power <= cap; offset keys are decimal integer strings |
| Full-domain extrema | Maximum PN at each offset, minimum positive-convention FoM and maximum power, each with code/run; FoM uses PN/f/P from the same run |
| Startup and convergence | Numerical repeat checks and startup evidence have separate scopes; warnings remain visible |

No max_step requirement means report measured gaps without inventing a limit. Sparse representative controls do not establish unmeasured-code performance. Fit a dense spectrum before reporting a flicker corner.

## Build path lists and independently converge

`adopted.json` is a JSON array of original result-path strings, not code objects and not the entire sweep output. If the whole successful batch is exactly the final adopted domain:

```sh
$PY - "$W/sweep.json" "$W/adopted.json" <<'PYCODE'
import json, sys
s = json.load(open(sys.argv[1]))
assert s['all_measured'], 'Resolve failed points before adoption'
with open(sys.argv[2], 'w') as f:
    json.dump(s['results'], f, indent=2)
PYCODE
```

Set `PY` to the configured Python. Select the worst PN code for every target offset and maximum-power code into `picked.json`, deduplicating and adding edge/warning states as needed. It has the same path-array format.

```sh
$S flow-converge "$T" "$C" --results "$W/picked.json" --harmonics 21 --tstab-ns 120 > "$W/converge.json"
$S flow-next "$T"
$PY - "$W/converge.json" "$W/conv.json" <<'PYCODE'
import json, sys
s = json.load(open(sys.argv[1]))
assert s['all_measured'], 'Convergence rerun failed; keep task unfrozen'
with open(sys.argv[2], 'w') as f:
    json.dump([p['check'] for p in s['pairs']], f, indent=2)
PYCODE
```

Harmonics/tstab are example rerun settings: choose values that differ from the original and test convergence meaningfully. Default tolerances are frequency_rel=0.001, power_rel=0.01 and pn_abs_db=0.5; explicit task `convergence_tolerances` may override them. Compare all recorded PN offsets. Match each rerun to an adopted result with identical physical identity, widths and controls. Self-reference is not independent convergence. Qualification extends only to tested controls/conditions.

## Freeze or return a gap

```sh
$S flow-freeze "$T" "$C" --results "$W/adopted.json" --convergence "$W/conv.json"
$S flow-next "$T"
$S deliver "$T" --output "$W/delivery"
```

Inspect `freeze.json`: adopted_control_states, frequency_range_hz, max_step_hz, convergence_comparisons, verified_evidence and scope. If freeze fails, correct the identified evidence/constraint problem; retain unfrozen status and use provisional delivery when budget ends.

Near-offset PN varying strongly with aux goes to [cmr](../eda-vco-cmr/SKILL.md)/[noise-isf](../eda-vco-noise-isf/SKILL.md). High-edge gaps or high-code loading go to [cap-array](../eda-vco-cap-array/SKILL.md). Compare competing families using complete adopted LUT extrema under matching conditions. Return lists, mappings, worst codes and gaps to the [main workflow](../eda-vco-design/SKILL.md).
