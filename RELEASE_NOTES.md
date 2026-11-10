# VCO Design Studio v1.0.0

The public release provides a Python 3.11+ desktop and CLI workbench for browsing LC VCO structures and generating circuit files. The UI uses the shared RF-FIELDWORK theme.

All bundled device names, geometry, domain limits and PSF-format samples are fictional software fixtures. No working semiconductor model, measured performance table or host configuration is included. Existing calculation and workflow contracts are exercised with synthetic inputs; optional installed-tool tests require user configuration. Real Cadence, solver and physical signoff validation remain outside this release.

The Class-B files in `data/baselines/class_b/` are rewritten synthetic examples with `provenance.kind: synthetic_example`, not measured baselines. Their component values support software demonstration only.

See README.md for the offline quick start, docs/BINDINGS.md for user configuration and docs/KNOWLEDGE_REVIEW.md for card-level publication decisions.

Agent skills include the public specification-to-delivery workflow and specialist diagnosis, control calibration and qualification criteria. Measurement indexes and failure ledgers come only from user configuration. Optional external integrations are documented in README; the external circuit optimizer uses the public `optimizer` site/action/receipt identifier. Validation of this revision discovered 114 tests: 107 passed and 7 optional installed-tool tests skipped.
