# Public release validation

Validation used Python 3.11.15, Tk 8.6 and NumPy on Linux (CentOS 7 / glibc 2.17).

- `tools/check.sh`: 112 tests passed, no skips when compatible external tool roots were supplied in a private read-only test profile. No real EDA solver ran.
- Without optional external tools: seven installed-tool tests are explicitly skipped. The remaining tests cover generation, controls, geometry domains, immutable manifests, evidence integrity, concurrent state updates, cancellation, synthetic PSF replay and GUI creation.
- `launch kb check`: 29 cards, no problems. Card-by-card publication decisions are in KNOWLEDGE_REVIEW.md.
- `tools/check_docs.py`: local links and documented CLI help passed.
- The release scanner and an independent sensitive-term search reported zero findings; no allowlist was required.
- The shared theme file is byte-identical to the supplied theme. Product-side font fallback handles lowercase X font family names.
- Screenshots: empty, example, running, result and compact. The running state uses actual background file generation, never a simulated circuit. At 1280×800 the main notebook occupies 67.9% of the window width; the full log remains within the window. At 1600×1000 the main notebook exceeds 60% of the width.

Native construction, electrical performance and signoff were not validated. Optional adapters were tested only against existing compatible analysis modules with synthetic inputs. Generated examples define no semiconductor models.
