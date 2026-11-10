---
name: eda-vco-design
description: Start here for LC VCO design, architecture upgrades, tuning, control calibration, freezing and delivery in VCO Design Studio; route diagnostic symptoms to the specialist skills.
---

# VCO design workflow

Run commands from the repository root with `S=./launch`; use a separate writable task directory `W` and Python 3.11+. Read [binding configuration](../../docs/BINDINGS.md) before real EDA execution. The bundled bindings, Class-B baselines and PSF fixtures are synthetic examples, not measured designs. Solver steps below require user-owned models, a configured runtime and authorized execution. Offline browsing and generation do not establish electrical performance.

## Start a task

`$S quickstart` prints the command chain; `$S <command> --help` defines current arguments.

```sh
S=./launch
W=/path/to/new-task
$S spec-fields
# Write W/spec.json: pdk, frequency_band_hz, power_max_mw are required.
# Add pn_targets_dbc_hz, explicit vdd_v/temp_c and a solver_processes budget.
$S spec-check "$W/spec.json"
$S flow-init --spec "$W/spec.json" --workspace "$W/task"
$S select --task "$W/task"
$S seed --task "$W/task" --family hegazi --variant tail --output "$W/p1.json"
$S flow-add "$W/task" --project "$W/p1.json" --reason "mechanism hypothesis and discriminating experiment" --evidence vco-card-procedure-design-loop
$S flow-measure "$W/task" candidate_001
$S flow-next "$W/task"
$S flow-status "$W/task" --brief
```

Use the actual returned candidate ID. Inspect `seed.source_cell`: null means template sizing, not a measured seed. Check binding, supply, temperature, frequency guess, register widths and default codes before measuring. Use a user scheduler or configured `sim_lock_command` when simulation resources are shared.

After each measurement run `flow-next`. Its actions progress through select, measure, improve/fix_failure, sweep_endpoints, converge, freeze and deliver. Record deviations and supporting evidence in the task's `DECISIONS.md`.

## Choose and improve candidates

- Inspect `select.lookup_ranking` and `evidence_nearby` under matching technology, frequency, power and temperature. Shortlist one or two mechanisms. Positive `margin_db` favors a candidate; a negative margin describes the best design found at that cell, not a physical limit. Empty evidence remains unknown; this release supplies no measured ranking.
- For failed PN, use the reported offsets. `PN_FLICKER` uses a 100 kHz to 1 MHz drop greater than 25 dB/dec as a triage hint. Check source contributions and return paths. `PN_THERMAL` calls for power headroom, tank loss and bank loading checks before an architecture change.
- `F_HIGH/F_LOW` suggests LC scaling; distinguish a center error from expected endpoint frequencies. `P_HIGH/P_HEADROOM` suggests a current change. The power knob is family dependent: `vdc` for current-biased variants, `core_width_scale` for voltage-biased cores; verify its direction by measurement.
- Create changed geometry as a new candidate: `$S project hegazi --variant tail --spec "$W/spec.json" --set L_scale=1.3 --set main.bits=5 --output "$W/p2.json"`. When changing an existing project, preserve its other parameters in a copy. Register it with `flow-add` and remeasure.
- Keep discarded candidates: `$S flow-mark "$W/task" candidate_001 rejected --reason "measured gap and evidence"`.
- [Calculation scope](../../docs/METHODS.md): power → gm → target gm·Rp → Rp/Q → L/C → bank → swing. `plan` gives algebraic targets; `autosize` performs bounded solver-based sizing. Neither a template nor a hand calculation proves PN or startup.

## Coverage, convergence, freeze and delivery

```sh
# codes.json contains real register values, e.g. [{"main":0},{"main":15}]
# only for a four-bit main bank; include aux where required.
$S flow-sweep "$W/task" candidate_002 --codes "$W/codes.json"
$S flow-next "$W/task"
$S flow-converge "$W/task" candidate_002 --results "$W/picked.json" --harmonics 21 --tstab-ns 120
$S flow-freeze "$W/task" candidate_002 --results "$W/adopted.json" --convergence "$W/conv.json"
$S deliver "$W/task" --output "$W/delivery"
```

`adopted.json` is an array of original `result.json` paths for the final control domain. `picked.json` selects its worst PN codes at every target offset and maximum-power code, plus endpoints/warnings as needed. `conv.json` contains the independently rerun result paths from `flow-converge` (the `pairs[].check` values). A run cannot validate itself; actual numerical settings must change while physical design and controls match.

Freeze checks measured coverage, per-code power/PN constraints, maximum step when specified, evidence hashes and independent convergence. Inspect `freeze.json` for scope, adopted controls, verified evidence and unqualified items. Sparse endpoints do not establish every-code performance. Nominal freeze does not establish PVT or physical signoff.

For an unfinished task use `deliver --provisional`, optionally `--candidate <id>`, and list remaining gaps. Delivery includes the project, generated files, codes, evidence references/hashes, decisions and verification status. User models/raw PSF remain referenced, not redistributed. Further interfaces are `verify --stage pvt`, `verify --stage mc`, `handoff --output <new-dir>` and `verify --stage postsim --netlist <extracted-netlist>`; inspect plans with `--dry-run` and state which stages actually ran.

## Route symptoms to specialists

| Symptom or decision | Skill |
|---|---|
| Select/compare/upgrade a topology; fair Class-B comparison | [architecture](../eda-vco-architecture/SKILL.md) |
| First L/C/gm, TR and bit-count estimates; seed versus autosize | [initial-sizing](../eda-vco-initial-sizing/SKILL.md) |
| TR, LSB, switch/bias-return loading, high-code AMP_LOW | [cap-array](../eda-vco-cap-array/SKILL.md) |
| NO_OSC, core bias, regeneration margin, startup or STEADY_WARN | [gm-core](../eda-vco-gm-core/SKILL.md) |
| Second harmonic, common-mode return and auxiliary tuning | [cmr](../eda-vco-cmr/SKILL.md) |
| PN_FLICKER/PN_THERMAL, source decomposition, ISF/PPV | [noise-isf](../eda-vco-noise-isf/SKILL.md) |
| Adopted LUT, joint main/aux controls, maximum step and freeze | [calibration](../eda-vco-calibration/SKILL.md) |
| Cards, lookup data, evidence queries and citations | [knowledge](../eda-vco-knowledge/SKILL.md) |

MODEL_WARN, TIMEOUT and SOLVER_ERROR first require model-domain/log/runtime diagnosis; failed jobs alone do not reject an architecture. NO_OSC and an unconverged solve are different findings.

## Evidence and stopping

`select`/`flow-next` push applicable cards and record consults in `task/knowledge/consults.jsonl`. Read their actions; cite resolvable IDs with `flow-add --evidence` and record conditions in `DECISIONS.md`. Strict enforcement requires resolvable evidence. Use `kb cards` for structured lookup and an explicitly configured external backend for `kb search`.

Check `flow-status --brief` for remaining budget. Stop when the adopted domain meets requirements with convergence evidence, when the solver budget is exhausted, or when two consecutive design moves improve the worst-case metric by less than 0.3 dB. In the latter cases deliver the best candidate provisionally with its gaps. This is a workflow stopping rule, not a claim of a global optimum. Do not spend the final verification budget on blind sweeps. Report measured, failed, unknown and unqualified states separately. CLI exits: 0 success, 1 operational error, 2 invalid request, 3 measurement failure, 4 timeout, 130 interruption.
