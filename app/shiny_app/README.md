# LymphVizkit — Shiny app

Interactive R/Shiny app for exploring **follicle-level** multiplexed-imaging data (PhenoCycler /
CODEX) across a **configurable grouping axis**, plus an embedded image viewer (Avivator/Viv).
Domain assumptions live in `R/00_domain_config.R` (see the repo-root `README.md` and
`docs/data_contract.md`); the lymphoid default groups follicles by `Resting` / `Reactive` /
`Involuted` (physical column `Donor Status`).

- Follicle markers: % positive per follicle vs. size (mean ± SE)
- Follicle targets: density / count vs. size (mean ± SE)
- Follicle composition vs. size (follicle-defining marker fractions)
- Per-size-bin tests across the configured groups (BH-adjusted p-values)

Data source: `../../data/app_data/master_results.xlsx` (Excel fallback) or
`../../data/app_data/follicle_explorer.h5ad`. Generate the committed synthetic example with
`python ../../scripts/make_synthetic_follicle_data.py`.

## Run

Prereqs: R >= 4.2 with `shiny`, `readxl`, `dplyr`, `tidyr`, `stringr`, `ggplot2`, `plotly`, `broom`
(install via `../../scripts/install_shiny_deps.R`).

```r
# from the repository root
shiny::runApp("app/shiny_app")
```

Or from within this directory:

```bash
R -q -e 'shiny::runApp(".", launch.browser = TRUE)'
```

## Notes

- Follicle diameter is derived from CORE area: `Follicle_Targets` rows where `type ==
  "follicle_core"` (core `region_um2`); `follicle_diam_um = 2 * sqrt(core_area / pi)`.
- Region tokens are `follicle_core`, `follicle_band`, `follicle_union` (union synthesized when absent).
- Binning is by diameter (µm); default bin width = 50 µm (adjust in the sidebar).
- Markers use `% positive` (100 × `pos_frac`); targets use `area_density` by class.
- The grouping axis (levels, order, colors, display label) is read from `DOMAIN$grouping` — do not
  hardcode group names.
