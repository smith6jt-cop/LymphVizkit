## LymphVizkit — coding agent quickstart

Purpose: Interactive R Shiny app to explore **follicle-level** multiplexed-imaging measurements by a **configurable grouping axis** and follicle size, plus a lightweight embedded image viewer (Avivator/Viv) for local OME-TIFFs. This is a **domain retarget** of the upstream pancreas/islet app to spleen / lymph-node follicles; all domain assumptions live in `app/shiny_app/R/00_domain_config.R` (the `DOMAIN` object, read via `domain_*()` accessors). **Do NOT hardcode islet/pancreas terms (`"ND"`, `"Islet_"`, `"INS"`, hormone fractions, etc.).**

Big picture
- App entrypoint: `app/shiny_app/app.R` (single-file Shiny app). Data come from `data/master_results.xlsx` with sheets: `Follicle_Markers`, `Follicle_Targets`, `Follicle_Composition`, and optional `LGALS3`.
- Core server data flow:
  1) `load_master()` reads sheets via readxl.
  2) `prep_data()` normalizes and joins into three tables: `targets_all`, `markers_all`, `comp`, and computes follicle diameter (µm) from Follicle_Targets CORE area: `follicle_diam_um = 2*sqrt(core_region_um2/pi)`.
  3) App UI tabs use these tables for plots, stats, QA, and the embedded viewer.
- The grouping axis is CONFIGURABLE via `DOMAIN$grouping` (physical join column `Donor Status`): levels, order, and colors come from `domain_group_levels()` / `domain_group_colors()`. Lymphoid default: `Resting` < `Reactive` < `Involuted`. Never hardcode group names or colors.

Project-specific conventions you must preserve
- Keying: All joins/grouping use (`Case ID`, `Donor Status`, `follicle_key`). `follicle_key` is derived by `add_follicle_key()` from `region` like `Follicle_200_core`, or `name`/`follicle_id` fallback.
- Region labels are normalized to exact tokens: `follicle_core`, `follicle_band`, `follicle_union`. Do not introduce new spellings.
- Autoantibody (AAb) filter is a pancreas/T1D-specific feature gated behind `DOMAIN$features$autoantibody_filter` (off for lymphoid). When enabled it filters within the autoantibody-positive group only; otherwise the columns/inputs are absent and the filter is a no-op.
- Synthesis rules (important):
  - Targets: If union rows are missing, synthesize counts only as `union = core + band`. Do NOT fabricate `region_um2`, `area_um2`, or `area_density` for union.
  - Markers: If union rows are missing, synthesize `n_cells` and `pos_count` as sums (compute `pos_frac` from them). If band missing but core+union exist, backfill band = union − core (clamp to non-negative). Reattach AAb flags after synthesis.
- Normalization options for plotting: `none`, `global z-score`, `robust per-donor` (median/MAD; MAD×1.4826). Apply consistently in both main plot and distribution plots.
- Stats tab: Global model is `value ~ donor_status + follicle_diam_um`; pairwise t-tests on residuals from `value ~ follicle_diam_um` with BH adjustment. Respect the configured factor order from `domain_group_levels()`.

Embedded viewer (Avivator/Viv)
- Static bundle lives under `app/shiny_app/www/avivator/`. Install via `scripts/install_avivator.sh` (mirrors public site; or builds if source present and Node+pnpm are available).
- Optional channel mapping from `app/shiny_app/Channel_names` or `Channel_names.txt` lines like `INS (C26)`. `build_channel_config_b64()` encodes a config sent to the viewer; it highlights INS (red), GCG (blue), SST (yellow), DAPI (grey) if present.
- Local OME-TIFFs can be served by setting `LOCAL_IMAGE_ROOT` or placing files in `local_images/` (resource path `/local_images`).

Developer workflows
- Install R deps: run `scripts/install_shiny_deps.R` (adds CRAN pkgs; Bioconductor extras are optional and not required for current app features).
- Run the app:
  - VS Code task: “Run Shiny app to reproduce issues”.
  - Or shell: `R -q -e "shiny::runApp('app/shiny_app', launch.browser=FALSE)"`.
- Build `data/master_results.xlsx` from TSVs: `python scripts/build_master_excel.py` (expects `data/results/*.tsv` and `CODEX_Pancreas_Donors.xlsx`; writes `data/master_results.xlsx`).
- Alternate donor compilation: `python scripts/compile_donors.py --input donors.zip --output out.xlsx [--donor-map-xlsx CODEX_Pancreas_Donors.xlsx]`.

Data expectations (examples)
- Follicle_Targets: `region` like `Follicle_64_core`, `class`, `region_um2`, `area_um2`, `area_density`, `count`.
- Follicle_Markers/LGALS3: `region` or `region_type`, `marker`, `n_cells`, `pos_count`, `pos_frac`.
- Composition: `cells_total`, `Ins_any`, `Glu_any`, `Stt_any` (fractions computed as 100×value/cells_total in app).
- Donor metadata: `Case ID` (string; zero-padded), `Donor Status` in {ND, Aab+, T1D}, optional AAb columns.

Quality checks and diagnostics
- QA tab enforces the “3× regions” rule per donor: markers/targets should have 3× distinct region rows vs composition follicles. Console emits NA audits via `audit_na()` on load.

When making changes
- Keep the three prepared tables’ schema and keys stable (`follicle_key`, region tokens, donor ordering). If you add metrics, thread them through `raw_df_base()` → `plot_df()` → `summary_df()` and update labels/metric selectors accordingly.

Questions to confirm with maintainers
- Provide a sample `CODEX_Pancreas_Donors.xlsx` layout and minimal `data/results/*.tsv` examples if contributing data pipelines.