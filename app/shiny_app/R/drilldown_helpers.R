# ===== Single-Cell Drill-Down Helper Functions =====
# Provides cell-level visualization for the segmentation viewer.
# Loads per-follicle CSV files from data/cells/ and renders cell scatter plots
# over the GeoJSON segmentation base.

# ── Phenotype color palette (from DOMAIN config, 00_domain_config.R) ──────
# Lymphoid default: B-lineage warm, T-lineage cool, myeloid dark, stroma neutral.
PHENOTYPE_COLORS <- domain_phenotype_colors()

# Palette registry + accessor. The domain ships one curated palette; the named
# variants map to it so the existing UI palette selector keeps working. Provide
# per-variant color maps in 00_domain_config.R to differentiate them later.
PHENOTYPE_PALETTES <- list(
  "Original"            = PHENOTYPE_COLORS,
  "High Contrast"       = PHENOTYPE_COLORS,
  "Colorblind Safe"     = PHENOTYPE_COLORS,
  "Maximum Distinction" = PHENOTYPE_COLORS
)

get_phenotype_palette <- function(name = "Original") {
  pal <- PHENOTYPE_PALETTES[[name]]
  if (is.null(pal)) pal <- PHENOTYPE_COLORS
  pal
}

# ── Cell CSV cache (env-based, same pattern as geojson_cache) ──────────
drilldown_cache <- new.env()

# Path to per-follicle cell CSVs
CELLS_DIR <- file.path("..", "..", "data", "app_data", "cells")

#' Check if single-cell drill-down data is available
drilldown_available <- function() {
  dir.exists(CELLS_DIR) && length(list.files(CELLS_DIR, pattern = "\\.csv$")) > 0
}

#' Load single-cell data for a specific follicle
#' @param imageid Case/image ID (e.g., "6505")
#' @param follicle_key Follicle key (e.g., "Follicle_284")
#' @return data.frame with columns: X_centroid, Y_centroid, phenotype, cell_region, + markers
load_follicle_cells <- function(imageid, follicle_key) {
  combined_id <- paste0(imageid, "_", follicle_key)
  cache_key <- combined_id

  if (exists(cache_key, envir = drilldown_cache)) {
    return(get(cache_key, envir = drilldown_cache))
  }

  csv_path <- file.path(CELLS_DIR, paste0(combined_id, ".csv"))
  if (!file.exists(csv_path)) {
    return(NULL)
  }

  cells <- tryCatch({
    df <- read.csv(csv_path, stringsAsFactors = FALSE)
    if (nrow(df) == 0) return(NULL)
    df
  }, error = function(e) {
    message("[DRILLDOWN] Error loading cells for ", combined_id, ": ", e$message)
    NULL
  })

  if (!is.null(cells)) {
    assign(cache_key, cells, envir = drilldown_cache)
    message("[DRILLDOWN] Cached cells for ", combined_id, " (", nrow(cells), " cells)")
  }

  cells
}

#' Render single-cell drill-down plot over GeoJSON base
#' @param info List with case_id, follicle_key, centroid_x, centroid_y
#' @param cells data.frame from load_follicle_cells()
#' @param color_by "phenotype" or a marker column name
#' @param show_peri Logical; if FALSE, only show core cells
#' @return ggplot object
render_follicle_drilldown_plot <- function(info, cells, color_by = "phenotype", show_peri = TRUE,
                                        show_peri_boundary = TRUE, show_structures = TRUE,
                                        palette = PHENOTYPE_COLORS) {
  if (is.null(info) || is.null(cells) || nrow(cells) == 0) return(NULL)

  # Build base plot: GeoJSON boundaries (follicle core always drawn)
  base_plot <- build_segmentation_base_plot(info, show_peri_boundary = show_peri_boundary,
                                             show_structures = show_structures)
  if (is.null(base_plot)) base_plot <- ggplot2::ggplot() + ggplot2::theme_void()

  # Filter cells by region
  if (!show_peri && "cell_region" %in% colnames(cells)) {
    cells <- cells[cells$cell_region == "core", , drop = FALSE]
  }
  if (nrow(cells) == 0) return(base_plot)

  # Convert cell centroids from µm to pixels (matching GeoJSON coordinate space)
  cells$x_px <- cells$X_centroid / PIXEL_SIZE_UM
  cells$y_px <- cells$Y_centroid / PIXEL_SIZE_UM

  if (color_by == "phenotype" && "phenotype" %in% colnames(cells)) {
    # Categorical coloring by phenotype (using fill to avoid colour conflict with structures)
    pheno_present <- sort(unique(cells$phenotype))
    pal <- palette[pheno_present]
    # Fill missing phenotypes with gray
    pal[is.na(pal)] <- "#CCCCCC"

    p <- base_plot +
      ggplot2::geom_point(
        data = cells,
        ggplot2::aes(x = x_px, y = y_px, fill = phenotype),
        shape = 21, colour = "grey30", stroke = 0.3, size = 3.0, alpha = 0.9,
        inherit.aes = FALSE
      ) +
      ggplot2::scale_fill_manual(values = pal, name = "Phenotype") +
      ggplot2::labs(title = paste(info$follicle_key, "- Single Cells"))
  } else if (color_by %in% colnames(cells)) {
    # Continuous coloring by marker expression (using fill)
    cells$marker_val <- suppressWarnings(as.numeric(cells[[color_by]]))

    p <- base_plot +
      ggplot2::geom_point(
        data = cells,
        ggplot2::aes(x = x_px, y = y_px, fill = marker_val),
        shape = 21, colour = "grey30", stroke = 0.3, size = 3.0, alpha = 0.9,
        inherit.aes = FALSE
      ) +
      ggplot2::scale_fill_viridis_c(option = "inferno", name = color_by, na.value = "gray80") +
      ggplot2::labs(title = paste(info$follicle_key, "-", color_by))
  } else {
    # Fallback: just plot cells in gray
    p <- base_plot +
      ggplot2::geom_point(
        data = cells,
        ggplot2::aes(x = x_px, y = y_px),
        color = "gray40", size = 2.5, alpha = 0.7,
        inherit.aes = FALSE
      ) +
      ggplot2::labs(title = paste(info$follicle_key, "- Single Cells"))
  }

  p +
    ggplot2::theme(
      legend.position = "right",
      legend.key.size = ggplot2::unit(0.9, "cm")
    )
}

#' Render phenotype composition summary bar chart
#' @param cells data.frame from load_follicle_cells()
#' @return ggplot object
render_drilldown_summary <- function(cells, palette = PHENOTYPE_COLORS) {
  if (is.null(cells) || nrow(cells) == 0 || !"phenotype" %in% colnames(cells)) {
    return(NULL)
  }

  # Count phenotypes, split by region
  if ("cell_region" %in% colnames(cells)) {
    counts <- as.data.frame(table(cells$phenotype, cells$cell_region),
                            stringsAsFactors = FALSE)
    colnames(counts) <- c("phenotype", "region", "count")
    counts$region <- factor(counts$region, levels = c("core", "peri"))
  } else {
    counts <- as.data.frame(table(cells$phenotype), stringsAsFactors = FALSE)
    colnames(counts) <- c("phenotype", "count")
    counts$region <- "all"
  }

  # Order by total count descending
  total_by_pheno <- tapply(counts$count, counts$phenotype, sum)
  counts$phenotype <- factor(counts$phenotype,
                              levels = names(sort(total_by_pheno, decreasing = TRUE)))

  # Colors
  pheno_present <- levels(counts$phenotype)
  pal <- palette[pheno_present]
  pal[is.na(pal)] <- "#CCCCCC"

  if ("cell_region" %in% colnames(cells) && length(unique(counts$region)) > 1) {
    ggplot2::ggplot(counts, ggplot2::aes(x = phenotype, y = count, fill = phenotype, colour = region)) +
      ggplot2::geom_col(position = "stack", linewidth = 0.5) +
      ggplot2::scale_fill_manual(values = pal, guide = "none") +
      ggplot2::scale_colour_manual(values = c("core" = "grey20", "peri" = "white"), name = "Region") +
      ggplot2::coord_flip() +
      ggplot2::labs(x = NULL, y = "Cell count") +
      theme_follicle() +
      ggplot2::theme(legend.position = "bottom")
  } else {
    ggplot2::ggplot(counts, ggplot2::aes(x = phenotype, y = count, fill = phenotype)) +
      ggplot2::geom_col() +
      ggplot2::scale_fill_manual(values = pal, guide = "none") +
      ggplot2::coord_flip() +
      ggplot2::labs(x = NULL, y = "Cell count") +
      theme_follicle()
  }
}
