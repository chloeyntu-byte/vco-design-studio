# Desktop interface

The entry point is `launch gui`; `--runtime` chooses an output directory separate from the program. Load the synthetic example or choose a family to inspect its graph. Changes to conditions are applied to generated graphs after regeneration.

The shared theme is vendored unchanged in `vco_studio/fieldwork_tk.py`. Main and log windows use a header, neutral global actions and product-purple focus. Generate circuit files is the primary design action; native execution is separate. The status chip reports idle, running, completion, warning or error. Background work has a progress indicator and cancellation; errors and task receipts remain in the log. Graphs use the theme Canvas palette.

Screenshots use only synthetic data: empty.png, example.png, running.png and result.png. The running view captures actual background file generation, without a solver. A second size check uses 1280×800. No real EDA session is used.

`vco_studio/ui_fonts.py` matches the kit font preferences without case sensitivity for X servers exposing lowercase family names. The theme source is unchanged. Screenshot capture requires Pillow only for development; normal use does not.

## Topology vs paper

The second page draws the selected variant flat (drawings come from a user-provided structure pack, see SCHEMATIC.md), the way its source paper draws it (no module boxes). Every device label is the instance path in the generated schematic (`GM/MN1`, `L_XFMR/LDA`, `CT_MAIN`); selecting a device shows its module cell and the top-level net of each terminal. `./launch topology check <family> <variant>` extracts the drawing's nets with the same rules as the schematic plan and requires every top-level schematic instance to appear, so the drawing cannot show a connection the schematic lacks. The right panel gives the citation, figure number, PDF page and caption from `data/papers/index.json`; figure images are not distributed (set `VCO_STUDIO_PAPER_FIGURES` to a folder with your own crops). See [SCHEMATIC.md](SCHEMATIC.md) for the packaged hierarchy and top-level plans. Screenshot: topology.png (taken with the maintainer's local structure pack loaded; the pack is not part of this repository). Generated Virtuoso views from the same local packs: virtuoso-top-hegazi-tail.png, virtuoso-top-lim.png, virtuoso-top-du.png (top levels), virtuoso-symbol-top.png (top symbol) and virtuoso-symbol-transformer.png (one-turn transformer symbol).
