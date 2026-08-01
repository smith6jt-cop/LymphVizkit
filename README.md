# LymphVizkit

An interactive **R Shiny** application for exploring **spleen and lymph-node follicle** data from
multiplexed imaging (PhenoCycler / CODEX). LymphVizkit is a domain retarget of the
`Islet-Explorer-Senior` architecture: the anatomical unit of analysis is the **follicle**
(germinal center / mantle zone) instead of the pancreatic **islet**, and the app is built around a
**configurable domain layer** so the grouping axis, region scheme, marker panel, and phenotype
rules are data-driven rather than hardcoded.

> Derived from `Islet-Explorer-Senior` (MIT, © 2026 smith6jt-cop).

## What's different from the upstream islet app

Everything pancreas/islet-specific is lifted into a single configuration module
(`app/shiny_app/R/00_domain_config.R`, with a shippable `config/lymphoid_default.yml`) that defines
the `DOMAIN` ontology:

| Concept | Upstream (pancreas / islet) | LymphVizkit (lymphoid / follicle) |
|---|---|---|
| Anatomical unit | islet (`islet_key`, `islet_diam_um`) | **follicle** (`follicle_key`, `follicle_diam_um`) |
| Region scheme | `islet_core` / `islet_band` / `islet_union` + 20 µm peri | **configurable** (default germinal center / mantle / whole follicle + peri-follicle) |
| Grouping axis | fixed `ND < Aab+ < T1D` | **configurable** levels / order / colors (lymphoid default provided) |
| Defining markers | hormone fractions INS / GCG / SST | **follicle-defining markers** (default CD20 / BCL6 / CD21) |
| Pseudotime root | INS | **configurable** (default BCL6) |
| Phenotypes | endocrine + immune | **lymphoid** (B / GC / mantle / plasma / FDC, T-cell subsets, macrophage, DC, …) |

## Application tabs

Six interactive tabs (inherited from the upstream architecture): **Plot**, **Trajectory**,
**Viewer**, **Statistics**, **Spatial**, and an optional **AI Assistant**. Single-cell drill-down
from any follicle shows segmentation boundaries + cell composition.

## Quick start (synthetic example data)

The repository ships a tiny **synthetic** follicle dataset so the app boots without the real
(large, un-committed) data.

```bash
# 1. Install R dependencies
Rscript scripts/install_shiny_deps.R

# 2. Generate the synthetic example dataset into data/app_data/
python scripts/make_synthetic_follicle_data.py

# 3. Run the app (DuckDB/mirai/AI optional; off for the smoke run)
LYMPH_USE_DUCKDB=FALSE LYMPH_USE_MIRAI=FALSE LYMPH_ENABLE_AI=FALSE \
  Rscript -e 'shiny::runApp("app/shiny_app", port = 8080, launch.browser = FALSE)'
```

See [`docs/data_contract.md`](docs/data_contract.md) for the exact data schema each tab expects.

## Data

The app reads from `data/app_data/` (gitignored except the synthetic example):

- `master_results.xlsx` — tabular follicle-level markers / targets / composition (Excel fallback path)
- `follicle_explorer.h5ad` — optional enriched single file (phenotypes, neighborhood, pseudotime)
- `cells/{case}_Follicle_{N}.csv` — per-follicle single-cell tables for drill-down
- `follicle_spatial_lookup.csv` — follicle centroids
- `json/{case}.geojson` — segmentation boundaries
- `phenotype_rules.csv` — marker → phenotype gating rules

## Status & roadmap

**This build:** the app code and data model are fully retargeted to follicles, with a configurable
domain layer and lymphoid defaults, and boot on the synthetic example dataset.

**Follow-on (not in this build):** adapting the Python pipeline (segmentation, phenotyping,
neighborhood metrics, real pseudotime, enriched h5ad) to real spleen/lymph-node PhenoCycler data,
a validated lymphoid marker panel, and production deployment. See `CLAUDE.md` and `docs/` for
architecture and conventions carried over from the upstream app.

## License

MIT — see [LICENSE](LICENSE). Derived from `Islet-Explorer-Senior`, © 2026 smith6jt-cop.
