---
name: eda-vco-knowledge
description: Retrieve VCO cards, user measurement indexes and lookup data at design decision points; cite applicable evidence and preserve unknown/qualification states rather than treating examples as measurements.
---

# Knowledge and evidence at decision points

Set `S=./launch`, `W=/path/to/task-root`. Four sources answer different questions:

| Source | Question | Commands |
|---|---|---|
| Curated cards | What action fits this stage, symptom and family? | `kb cards --stage <stage> --symptom <code> --family <family>`; `kb show <card-id>` |
| User measured lookup table | What has been demonstrated near this technology/frequency/power? | `select --task <task>`; `table --pdk <binding-id> --results <user-results.jsonl>`; `seed --task <task> --family <family> --output <new-project.json>` |
| User evidence indexes | Which measured/failed points match the conditions and why were others excluded? | `evidence-query --index <measurements.jsonl> --pdk <binding-id> --frequency-hz <f> --power-mw <P> --temp-c <T>` |
| Optional external knowledge backend | Papers, mechanism explanations and user history | `kb search "<symptom and mechanism>" --mode hybrid --task <task>`; `kb show <record-id>` |

No measured lookup or performance evidence ships with this release. Configure `VCO_SITE` with `evidence_indexes` and, optionally, `evidence_failure_files`; missing files yield unknown/no nearby attempts. Configure `kb.command` or `VCO_KB_COMMAND` for an external backend implementing the CLI search/show contract. Local cards remain usable without it; automatic consults retain a backend error instead of blocking the flow. Direct backend searches require that configuration.

## Consult throughout the flow

```sh
$S flow-next "$W/task"
$S kb cards --stage tuning --symptom PN_FLICKER --task "$W/task"
$S kb show vco-card-symptom-pn-flicker
$S evidence-query --pdk <binding-id> --frequency-hz <f> --power-mw <P> --vdd-v <V> --temp-c <T> --offset 100000
```

After every measurement, inspect the pushed card actions and diagnostic codes. During selection, inspect lookup distance, margin and conditions. A negative margin shows a nearby demonstrated gap, not a universal bound. When cards are insufficient, query the configured backend with symptom/mechanism terms and retain its source identity and applicability. Backend-specific archival filters are not part of Studio's CLI contract.

`task/knowledge/consults.jsonl` records stage, context, card IDs and backend hit IDs. Use `--task` for manual searches/cards when a receipt is needed. A receipt records consultation; it does not itself validate a quantitative claim.

## Cite and qualify

Write `source/card/evidence ID → matching conditions → adopted conclusion → proposed change` in `DECISIONS.md`, and pass resolvable IDs to `flow-add --evidence`. Strict mode requires resolvable evidence. Card IDs support a procedure or mechanism, not an invented measured result.

Quantitative claims need actual results, raw-source hashes, model/bias conditions, widths/codes and qualification scope. Query `near_misses` and exclusions before widening tolerances. A failed point requires its failure class and target conditions; an absent point remains unknown. Observed peak is not a physical limit. A historical seed is a starting point, not acceptance evidence.

For a user JSONL history ledger, [lookup_history.py](../eda-vco-design/scripts/lookup_history.py) filters existing rows without running simulations or ranking unmeasured designs. Inspect `--help` and filter exact stored conditions; do not infer aliases or field meanings from a record name.

## Preserve reusable findings

When a verified finding changes future decisions, draft a structured card with `id`, `type`, `title`, `applies`, `summary`, `actions`, `evidence`, `confidence` and `lifecycle`. Conditions may include stage, symptoms, family, pdk, frequency_ghz and power_mw. Use explicit provisional/current/superseded lifecycle and preserve contradictory evidence.

Keep private results and identifiers outside the public repository. Publication requires a sanitized card and review of its source claims; do not copy model data or private run paths. Run `$S kb check` after an authorized card edit. This CLI reads cards and queries a backend; it does not provide a database-write command.

Return the selected evidence IDs, relevant conditions, uncertainty and next experiment to the [main workflow](../eda-vco-design/SKILL.md).
