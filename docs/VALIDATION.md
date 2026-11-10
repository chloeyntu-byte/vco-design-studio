# Public release validation

Validation used Python 3.11.15, Tk 8.6 and NumPy on Linux (CentOS 7 / glibc 2.17).

- `DISPLAY=:92 VCO_STUDIO_PYTHON=<installed-python-3.11> ./tools/check.sh`: 114 tests discovered, 107 passed and **7 skipped** (optional installed-tool contracts: 3 Z11, 1 Cap Array Lab and 3 external circuit optimizer tests). No failures or errors; no real EDA solver ran. The two added regressions cover configured measurement/failure ledgers and absent-data/knowledge-backend fallback.
- This run used no private site profile or external tool roots. The seven installed-tool tests were not validated in this environment; no zero-skip result is claimed. Without DISPLAY, three additional GUI tests skip (10 total).
- `launch kb check`: 29 cards, no problems. Card-by-card publication decisions are in KNOWLEDGE_REVIEW.md.
- `tools/check_docs.py`: local links and documented CLI help passed, including the restored flow, freeze, delivery, sizing and integration commands. Nine agent skills retain symptom routing, measurement decisions and qualification steps without private evidence or host references.
- Offline CLI smoke: quickstart, flow-init, select, seed, flow-add, flow-next, flow-status, provisional deliver and missing-data evidence-query passed. The generated delivery report uses English fields; no solver ran.
- Skill scalar frontmatter, code fences and local links passed for all nine skills. The optional canonical skill validator could not run because PyYAML is absent; no dependency was installed.
- The release scanner and an independent sensitive-term search reported zero findings; no allowlist was required.
- The shared theme file is byte-identical to the supplied theme. Product-side font fallback handles lowercase X font family names.
- Screenshots: empty, example, running, result and compact. The running state uses actual background file generation, never a simulated circuit. At 1280×800 the main notebook occupies 67.9% of the window width; the full log remains within the window. At 1600×1000 the main notebook exceeds 60% of the width.

Native construction, electrical performance and signoff were not validated. Installed external adapters were not exercised in this run; their seven tests skipped as listed above. Remaining tests use synthetic inputs and explicit fixtures. Generated examples define no semiconductor models.
