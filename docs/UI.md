# Desktop interface

The entry point is `launch gui`; `--runtime` chooses an output directory separate from the program. Load the synthetic example or choose a family to inspect its graph. Changes to conditions are applied to generated graphs after regeneration.

The shared theme is vendored unchanged in `vco_studio/fieldwork_tk.py`. Main and log windows use a header, neutral global actions and product-purple focus. Generate circuit files is the primary design action; native execution is separate. The status chip reports idle, running, completion, warning or error. Background work has a progress indicator and cancellation; errors and task receipts remain in the log. Graphs use the theme Canvas palette.

Screenshots use only synthetic data: empty.png, example.png, running.png and result.png. The running view captures actual background file generation, without a solver. A second size check uses 1280×800. No real EDA session is used.

`vco_studio/ui_fonts.py` matches the kit font preferences without case sensitivity for X servers exposing lowercase family names. The theme source is unchanged. Screenshot capture requires Pillow only for development; normal use does not.
