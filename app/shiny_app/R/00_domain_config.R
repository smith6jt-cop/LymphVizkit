# ============================================================================
# 00_domain_config.R — LymphVizkit domain / ontology configuration
# ============================================================================

# Null-coalescing operator. Base R gained `%||%` in 4.4.0; the app uses it in
# ~13 files. Define a base-compatible shim here (this file is auto-sourced
# FIRST) so every module can rely on it under older R (this env is 4.3.x).
if (!exists("%||%", mode = "function")) {
  `%||%` <- function(x, y) if (is.null(x)) y else x
}

# ============================================================================
# This file is auto-sourced by Shiny FIRST (alphabetically before 00_globals.R),
# so every other module can read the `DOMAIN` object and its accessors.
#
# PURPOSE: lift every pancreas/islet-specific assumption out of the code and into
# one configurable place. The app was forked from Islet-Explorer-Senior; here we
# retarget it to spleen / lymph-node FOLLICLES with lymphoid defaults, while
# keeping the grouping axis, region scheme, marker panel and phenotype set
# DATA-DRIVEN rather than hardcoded.
#
# OVERRIDE: values below are the built-in defaults. If the `yaml` package is
# installed and `config/lymphoid_default.yml` exists, it is deep-merged on top
# (so a deployment can retarget the app without editing R). Missing yaml pkg or
# file is fine — the inline defaults are authoritative.
#
# NOTE ON PHYSICAL SCHEMA TOKENS: the grouping *column* stays physically named
# "Donor Status" and the region canonical tokens stay follicle_core/band/union —
# these are join keys / schema tokens used in ~40 sites and in the data files.
# Only their DISPLAY (label, levels, order, colors, region display names) is
# configurable here. Keep code + generated data in sync on the physical tokens.
# ============================================================================

DOMAIN_DEFAULT <- list(

  app = list(
    name     = "LymphVizkit",
    title    = "LymphVizkit — Spleen & Lymph Node Follicle Explorer",
    url_path = "lymphvizkit"
  ),

  # ---- Anatomical unit (replaces the `islet` vocabulary) -------------------
  unit = list(
    singular          = "follicle",
    plural            = "follicles",
    Title             = "Follicle",
    key_column        = "follicle_key",     # physical join key column
    id_prefix         = "Follicle",         # -> "Follicle_37"
    id_regex          = "Follicle_\\d+",    # replaces the hardcoded "Islet_\\d+"
    peri_suffix       = "_exp20um",         # GeoJSON expanded-polygon name suffix
    case_id_pad_width = 4,                   # sprintf("%04d", case_id) fallback
    diam_column       = "follicle_diam_um"
  ),

  # micrometers per pixel (GeoJSON polygons are in px, lookups in um)
  pixel_size_um = 0.5078,

  # ---- Grouping axis (replaces the ND<Aab+<T1D disease factor) -------------
  # The physical column is "Donor Status" (kept for join stability); only the
  # display label, level set, order and colors are configurable here.
  grouping = list(
    physical_column = "Donor Status",
    display_label   = "Follicle state",
    levels          = c("Resting", "Reactive", "Involuted"),
    ordered         = TRUE,
    default_palette = "Bright",
    palettes = list(
      "Paul Tol (default)" = c(Resting = "#4477AA", Reactive = "#CC6633", Involuted = "#228833"),
      "Bright"             = c(Resting = "#3366CC", Reactive = "#FF6600", Involuted = "#109618"),
      "Okabe-Ito"          = c(Resting = "#0072B2", Reactive = "#D55E00", Involuted = "#009E73"),
      "Diverging"          = c(Resting = "#2166AC", Reactive = "#B2182B", Involuted = "#1B7837")
    )
  ),

  # ---- Region scheme (replaces islet_core/band/union + 20um peri) ----------
  regions = list(
    items = list(
      list(id = "core",  canonical = "follicle_core",  display = "Germinal center", geojson_class = "Follicle",  is_core = TRUE),
      list(id = "band",  canonical = "follicle_band",  display = "Mantle zone",     geojson_class = NA,          is_core = FALSE),
      list(id = "union", canonical = "follicle_union", display = "Whole follicle",  geojson_class = NA,          is_core = FALSE)
    ),
    expansion = list(
      display       = "Peri-follicle",
      geojson_class = "FollicleExpanded",
      distance_um   = 20
    ),
    # structure overlays drawn on the segmentation map (replaces Nerve/Capillary/Lymphatic)
    structures = list(
      list(class = "Vessel",     display = "Vessel",     color = "#CC0000"),
      list(class = "Sinus",      display = "Sinus",      color = "#00AA00"),
      list(class = "Lymphatic",  display = "Lymphatic",  color = "#0000CC")
    ),
    colors = c(core = "#0066CC", expansion = "#00CCCC")
  ),

  # ---- Markers ------------------------------------------------------------
  markers = list(
    excluded = c("DAPI", "Empty", "Blank"),
    # "follicle-defining markers" replace the INS/GCG/SST hormone fractions.
    # single_col / any_col are the composition-sheet columns that carry the
    # single-positive and any-positive fractions for each defining marker.
    defining = list(
      list(marker = "CD20", single_col = "CD20_single", any_col = "CD20_any"),
      list(marker = "BCL6", single_col = "BCL6_single", any_col = "BCL6_any"),
      list(marker = "CD21", single_col = "CD21_single", any_col = "CD21_any")
    ),
    pseudotime_root = "BCL6",   # replaces INS as the trajectory root anchor
    # the full panel, surfaced in the phenotype-rules modal footer + viewer defaults
    panel = c("CD20", "CD21", "CD23", "CD35", "BCL6", "Ki67", "CD3e", "CD4",
              "CD8a", "FOXP3", "PD1", "CXCR5", "CD68", "CD163", "CD11c", "HLADR",
              "CD138", "CD56", "MPO", "CD31", "CD34", "PDPN", "LYVE1", "SMA",
              "Vimentin", "DAPI")
  ),

  # ---- Phenotypes ---------------------------------------------------------
  phenotypes = list(
    display_order = c("GC B cell", "Mantle B cell", "B cell", "Plasma cell", "FDC",
                      "Tfh cell", "CD4 T cell", "CD8 T cell", "Treg",
                      "Macrophage", "Tingible-body macrophage", "Dendritic cell",
                      "NK cell", "Neutrophil", "Endothelial", "Lymphatic",
                      "Stromal", "Smooth muscle", "Unknown"),
    colors = c(
      "GC B cell"                = "#E63946",
      "Mantle B cell"            = "#F4A261",
      "B cell"                   = "#48CAE4",
      "Plasma cell"              = "#BC4749",
      "FDC"                      = "#6A994E",
      "Tfh cell"                 = "#0096C7",
      "CD4 T cell"               = "#0077B6",
      "CD8 T cell"               = "#023E8A",
      "Treg"                     = "#06A77D",
      "Macrophage"               = "#264653",
      "Tingible-body macrophage" = "#1D3557",
      "Dendritic cell"           = "#2A9D8F",
      "NK cell"                  = "#7209B7",
      "Neutrophil"               = "#E76F51",
      "Endothelial"              = "#F28482",
      "Lymphatic"                = "#84A98C",
      "Stromal"                  = "#9B9B9B",
      "Smooth muscle"            = "#C49792",
      "Unknown"                  = "#CCCCCC"
    ),
    aliases = list(),   # PHENOTYPE_COLORS-form -> CSV-form (none needed for lymphoid default)
    # phenotypes with no CSV rule row — hand-written descriptions
    residuals = list(
      list(name = "Stromal", parent = "all",
           description = "Residual stromal / fibroblast-like cells (Vimentin+), negative for B/T/myeloid markers."),
      list(name = "Unknown", parent = "all",
           description = "Default fallback for cells that match no rule above.")
    )
  ),

  # ---- Optional feature toggles -------------------------------------------
  # Pancreas/T1D-specific UI that has no lymphoid analogue; off by default.
  features = list(
    autoantibody_filter = FALSE   # the T1D islet-autoantibody (GADA/IA2A/...) sidebar filter
  )
)

# ---- Optional YAML overlay -------------------------------------------------
.load_domain <- function(default) {
  cfg <- default
  yml_path <- file.path("config", "lymphoid_default.yml")
  # try relative to app dir and to repo root (../.. from app/shiny_app)
  candidates <- c(yml_path,
                  file.path("..", "..", "config", "lymphoid_default.yml"))
  hit <- Filter(file.exists, candidates)
  if (length(hit) && requireNamespace("yaml", quietly = TRUE)) {
    ov <- tryCatch(yaml::read_yaml(hit[[1]]), error = function(e) NULL)
    if (is.list(ov)) {
      cfg <- utils::modifyList(cfg, ov)
      message("[DOMAIN] Overlaid config from ", hit[[1]])
    }
  }
  cfg
}

DOMAIN <- .load_domain(DOMAIN_DEFAULT)

# ============================================================================
# Accessors — the ONLY supported way to read domain config downstream.
# All are defensive (no %||% dependency; safe if a key is missing).
# ============================================================================
.dm <- function(path, fallback = NULL) {
  x <- DOMAIN
  for (p in path) {
    if (is.list(x) && !is.null(x[[p]])) x <- x[[p]] else return(fallback)
  }
  x
}

# -- unit --
domain_unit_singular <- function() .dm(c("unit", "singular"), "follicle")
domain_unit_plural   <- function() .dm(c("unit", "plural"), "follicles")
domain_unit_Title    <- function() .dm(c("unit", "Title"), "Follicle")
domain_key_col       <- function() .dm(c("unit", "key_column"), "follicle_key")
domain_id_prefix     <- function() .dm(c("unit", "id_prefix"), "Follicle")
domain_id_regex      <- function() .dm(c("unit", "id_regex"), "Follicle_\\d+")
domain_peri_suffix   <- function() .dm(c("unit", "peri_suffix"), "_exp20um")
domain_pad_width     <- function() as.integer(.dm(c("unit", "case_id_pad_width"), 4L))
domain_diam_col      <- function() .dm(c("unit", "diam_column"), "follicle_diam_um")
domain_pixel_size    <- function() as.numeric(.dm("pixel_size_um", 0.5078))

# -- grouping --
domain_group_column  <- function() .dm(c("grouping", "physical_column"), "Donor Status")
domain_group_label   <- function() .dm(c("grouping", "display_label"), "Group")
domain_group_levels  <- function() .dm(c("grouping", "levels"), c("Resting", "Reactive", "Involuted"))
domain_group_ordered <- function() isTRUE(.dm(c("grouping", "ordered"), TRUE))
domain_group_palettes <- function() .dm(c("grouping", "palettes"), list())
domain_group_default_palette <- function() .dm(c("grouping", "default_palette"), "Bright")
domain_group_colors  <- function(palette = domain_group_default_palette()) {
  pals <- domain_group_palettes()
  if (!is.null(pals[[palette]])) return(pals[[palette]])
  if (length(pals)) return(pals[[1]])
  stats::setNames(rep("#888888", length(domain_group_levels())), domain_group_levels())
}
#' Factor helper honoring the configured level set + order
domain_group_factor <- function(x) {
  factor(as.character(x), levels = domain_group_levels(), ordered = domain_group_ordered())
}

# -- regions --
domain_region_items      <- function() .dm(c("regions", "items"), list())
domain_region_canonicals <- function() vapply(domain_region_items(), function(r) r$canonical, character(1))
domain_region_core       <- function() {
  it <- domain_region_items()
  core <- Filter(function(r) isTRUE(r$is_core), it)
  if (length(core)) core[[1]]$canonical else "follicle_core"
}
domain_region_display <- function() {
  it <- domain_region_items()
  stats::setNames(vapply(it, function(r) r$display, character(1)),
                  vapply(it, function(r) r$canonical, character(1)))
}
domain_region_geojson_classes <- function() {
  it <- domain_region_items()
  cls <- vapply(it, function(r) if (is.null(r$geojson_class) || is.na(r$geojson_class)) "" else r$geojson_class, character(1))
  cls[nzchar(cls)]
}
domain_expansion_class <- function() .dm(c("regions", "expansion", "geojson_class"), "FollicleExpanded")
domain_expansion_display <- function() .dm(c("regions", "expansion", "display"), "Peri-follicle")
domain_expansion_um    <- function() as.numeric(.dm(c("regions", "expansion", "distance_um"), 20))
domain_structures      <- function() .dm(c("regions", "structures"), list())
domain_structure_classes <- function() vapply(domain_structures(), function(s) s$class, character(1))

# -- markers --
domain_excluded_markers <- function() .dm(c("markers", "excluded"), c("DAPI"))
domain_defining         <- function() .dm(c("markers", "defining"), list())
domain_defining_markers <- function() vapply(domain_defining(), function(d) d$marker, character(1))
domain_defining_single_cols <- function() vapply(domain_defining(), function(d) d$single_col, character(1))
domain_defining_any_cols    <- function() vapply(domain_defining(), function(d) d$any_col, character(1))
domain_pseudotime_root  <- function() .dm(c("markers", "pseudotime_root"), "BCL6")
domain_marker_panel     <- function() .dm(c("markers", "panel"), character(0))

# -- phenotypes --
domain_phenotype_order  <- function() .dm(c("phenotypes", "display_order"), character(0))
domain_phenotype_colors <- function() {
  cols <- .dm(c("phenotypes", "colors"), NULL)
  if (is.list(cols)) unlist(cols) else cols
}
domain_phenotype_aliases <- function() {
  al <- .dm(c("phenotypes", "aliases"), list())
  if (is.list(al) && length(al)) unlist(al) else character(0)
}
domain_phenotype_residuals <- function() .dm(c("phenotypes", "residuals"), list())

# -- feature toggles --
domain_feature <- function(name, default = FALSE) isTRUE(.dm(c("features", name), default))

message("[DOMAIN] Loaded config for '", .dm(c("app", "name"), "app"),
        "' (unit=", domain_unit_singular(),
        ", groups=", paste(domain_group_levels(), collapse = "/"), ")")
