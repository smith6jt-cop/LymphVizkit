# LymphVizkit data contract

The app reads from `data/app_data/` (gitignored except the committed synthetic example).
This document is the schema each file must satisfy. The synthetic generator
`scripts/make_synthetic_follicle_data.py` writes a minimal, schema-correct example so the
app boots via the **Excel-fallback path** (no h5ad / reticulate / DuckDB required).

Physical schema tokens are shared with the code and must stay in sync (they are join keys /
sheet lookups, not display strings):

- Grouping column is physically named **`Donor Status`**; its *values* are the configured
  `DOMAIN$grouping$levels` (lymphoid default: `Resting` / `Reactive` / `Involuted`). The
  display label is configurable (`DOMAIN$grouping$display_label`).
- Region types are **`follicle_core` / `follicle_band` / `follicle_union`** (union is
  synthesized by `prep_data()` when absent).
- Composition fractions use the historical columns **`Ins_single`/`Glu_single`/`Stt_single`**
  (+ `_any`, `Multi_Pos`, `Triple_Neg`, `cells_total`) — retargeted in the UI to the
  follicle-defining markers `DOMAIN$markers$defining` (CD20 / BCL6 / CD21), keeping the
  physical column names.

## `master_results.xlsx` (Excel fallback — primary boot path)

Loaded by `load_master()` in `app/shiny_app/R/data_loading.R`. Four sheets:

| Sheet | Required columns |
|---|---|
| `Follicle_Targets` | `Case ID`, `Donor Status`, `region` (e.g. `Follicle_37_core`), `type` ∈ {`follicle_core`,`follicle_band`}, `class`, `area_um2`, `region_um2`, `area_density`, `count` |
| `Follicle_Markers` | `Case ID`, `Donor Status`, `region`, `region_type` ∈ {`follicle_core`,`follicle_band`}, `marker`, `n_cells`, `pos_count`, `pos_frac` |
| `Follicle_Composition` | `Case ID`, `Donor Status`, `region`, `cells_total`, `Ins_single`, `Glu_single`, `Stt_single`, `Multi_Pos`, `Triple_Neg`, `Ins_any`, `Glu_any`, `Stt_any` |
| `LGALS3` | same shape as `Follicle_Markers` with `marker = "LGALS3"` (optional) |

Notes:
- The follicle key (`follicle_key`, e.g. `Follicle_37`) is extracted from `region` via the
  configured `DOMAIN$unit$id_regex`. The core row's `region_um2` drives
  `follicle_diam_um = 2·sqrt(region_um2/π)`.
- Optional donor columns: `Age`, `Gender`, and `AAb_*` (only used when the
  `autoantibody_filter` feature is enabled — off for lymphoid).

## `cells/{Case}_Follicle_{N}.csv` — per-follicle single cells (drill-down)

Loaded by `load_follicle_cells()` in `drilldown_helpers.R` (filename = `{case_id}_{follicle_key}.csv`).
Columns: `X_centroid`, `Y_centroid` (µm), `phenotype` (a `DOMAIN$phenotypes` name),
`cell_region` ∈ {`core`,`peri`}, `Cell Area`, `Nucleus Area`, and one column per panel marker.

## `follicle_spatial_lookup.csv` — centroids

Loaded by `load_follicle_spatial_lookup()` in `segmentation_helpers.R`.
Required columns: `case_id`, `follicle_key`, `centroid_x_um`, `centroid_y_um` (+ `area_um2`).

## `json/{Case}.geojson` — segmentation boundaries

Read by `sf::st_read()` (requires the optional `sf` package; the viewer self-disables without it).
A QuPath-style `FeatureCollection`; each follicle contributes a core polygon
(`classification.name = "Follicle"`, `name = "Follicle_N"`) and an expanded polygon
(`classification.name = "FollicleExpanded"`, `name = "Follicle_N_exp20um"`). Coordinates are in
**pixels** (µm ÷ `PIXEL_SIZE_UM`).

## `phenotype_rules.csv` — marker → phenotype gating

Parsed by `.parse_phenotype_rules_csv()` in `00_globals.R`. Header-less CSV where row 1 is
`[<ignored>, <ignored>, marker1, marker2, …]` and each data row is
`[parent, phenotype, cell1, cell2, …]` with cell values `pos` / `anypos` / `allneg` / empty.

## Optional enriched `follicle_explorer.h5ad`

Not committed and not required to boot. When present (via the follow-on Python pipeline), it
supplies phenotype proportions (`prop_*`, `peri_prop_*`), donor demographics, neighborhood
metrics, Leiden clustering, and pseudotime — enriching the Plot cell-population selector and the
Spatial / Trajectory tabs, which otherwise render their "data unavailable" states.

## Regenerate the synthetic example

```bash
python scripts/make_synthetic_follicle_data.py        # writes data/app_data/
Rscript scripts/test_shiny_prep.R                     # validates the contract
```
