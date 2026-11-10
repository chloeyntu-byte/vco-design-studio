---
name: eda-vco-noise-isf
description: Triage failed phase noise, flicker upconversion and HBnoise source contributions; use ISF/PPV analysis when periodic weighting is needed to distinguish mechanisms.
---

# Phase-noise diagnosis

Start with source decomposition. Enter ISF/PPV only when spectra do not distinguish mechanisms or the question requires signed periodic weighting. Inputs must identify physical design, models, code, supply, temperature, f0, power, output reference and numerical settings. MODEL_WARN/STEADY_WARN require model/steady-state checks first; NO_OSC goes to [gm-core](../eda-vco-gm-core/SKILL.md), solver errors to logs/runtime diagnosis.

Use actual PSF-ASCII (export binary PSF first): `result.json`, `psf/hbnoise.pm.pnoise_hbnoise`, generated circuit/testbench, and, for single-ended balance, `psf/hb.fd.pss_hb`. Set `S=./launch`, `W=/path/to/task-root`, `PY` to Python 3.11+ and `K` to this skill directory.

## Read status and decompose sources

```sh
$S flow-status "$W/task" --brief
$S flow-next "$W/task"
$S kb cards --stage tuning --symptom PN_FLICKER --task "$W/task"
$PY "$K/scripts/pn_breakdown.py" "$W/run/result.json" --offset 100000 --offset 1000000
$PY "$K/scripts/pn_breakdown.py" "$W/task" --summary
$PY "$K/scripts/pn_breakdown.py" "$W/before/result.json" "$W/after/result.json" --compare
```

Use PN_THERMAL instead when applicable. [pn_breakdown.py](scripts/pn_breakdown.py) also accepts run directories, PSF files and compressed input; `--json` retains the full source table.

- Parse TYPE/TRACE definitions rather than assuming a universal field order. BSIM3 commonly has rd, rs, id, fn, total; BSIM4 differs. Include resistor, inductor and behavioral sources, not only MOS.
- `out` is V/sqrt(Hz); source totals are V^2/Hz. `share=source_total/out^2` and `closure=sum(source_total)/out^2`. The helper's 1% closure tolerance is a parser screen; outside it check missing/correlated sources and output definitions before attribution.
- Check actual instance membership in core/tail/main-bank/aux-bank/magnetic loss groups. Unknown sources are not zero. Flicker fractions above 70%, below 30% and between are triage categories; inspect failing offsets directly.
- Compare absolute source contributions, not just percentages. A source's share can increase while total PN improves. Large single-ended imbalance or asymmetric source weights warrants a connectivity/loading check.
- For differential fundamental peak A, `PN=10*log10(2*out^2/A^2)` should agree with the result. Without A, report contribution spectra only.
- Removing an independent fraction r at fixed periodic state gives upper-bound improvement `-10*log10(1-r)` dB. A circuit change also changes periodic state, so this is not a promised improvement.
- Near-carrier `cornerFreq` corrections and sparse offsets are diagnostic hints. A flicker corner is an asymptote intersection, not a PVT corner; fit a dense spectrum with stated ranges for a quantitative claim.

## Choose one falsifiable change

Let `D=PN(100k)-PN(1M)`. For failing PN, the advisor uses D>25 dB/dec for PN_FLICKER, otherwise PN_THERMAL; the helper may display the negative signed slope. Slope alone does not identify a source.

| Evidence | Experiment |
|---|---|
| D>25 and tail flicker dominates | Legal longer tail channel, bypass/return filtering; inspect [cmr](../eda-vco-cmr/SKILL.md) |
| D>25 and core flicker dominates | Check symmetry, periodic weighting and return; longer L also changes parasitics/startup |
| D near 20 and white tank/core sources dominate | Use power headroom if not clipped; improve measured f0 tank Q/impedance |
| AMP_LOW with bank resistor/switch contribution | Measure biased bank Y and loading with [cap-array](../eda-vco-cap-array/SKILL.md) |
| Slope and source fractions disagree | Follow source evidence at each failing offset; retain mixed-source diagnosis |
| Loss and power fixes leave a substantial gap | Compare [architectures](../eda-vco-architecture/SKILL.md) under the same conditions |

## ISF/PPV when necessary

ISF is impulse sensitivity; PPV is a phase perturbation vector. Source spectra show magnitude contributions but do not alone establish signed conversion weights. Use a separately supplied periodic sensitivity tool or a verified user testbench; no such external tool is bundled or required for basic diagnosis.

Keep internal source directions, phase reference, PSD and correlations. Combine coherent flicker harmonics coherently; sum independent sources/bands by power. Compare a reconstructed spectrum with independent ordinary HBnoise for exactly the same source scope, then list omitted sources and sampling-convergence uncertainty. A partial core-only reconstruction cannot close against total noise without accounting for tail, bank and loss sources. State any symmetry/multiplicity assumptions. If closure fails, check port, sign and normalization before claiming a mechanism.

## Return and verify

Record source table, slope, closure error, hypothesis and matched comparison conditions. Create changed geometry as a new candidate:

```sh
$S flow-add "$W/task" --project "$W/p-next.json" --reason "source evidence, single-change hypothesis, matched test" --evidence vco-card-symptom-pn-flicker
$S flow-measure "$W/task" <candidate-id>
$S flow-next "$W/task"
$S flow-converge "$W/task" <candidate-id> --results "$W/picked.json" --harmonics 21 --tstab-ns 180
```

Use the actual symptom card and candidate ID. New geometry needs its own independent convergence evidence and adopted-code checks; return to the [main workflow](../eda-vco-design/SKILL.md).
