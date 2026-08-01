# Safe wrapper around dplyr::left_join to handle non-data.frame inputs
safe_left_join <- function(x, y, by, context = "join") {
  # Ensure inputs are data.frames
  if (!inherits(x, "data.frame")) {
    x <- tryCatch(as.data.frame(x), error = function(e) {
      stop(sprintf("[safe_left_join] %s: Cannot coerce LHS to data.frame (class: %s)",
                   context, paste(class(x), collapse = "/")))
    })
  }
  if (!inherits(y, "data.frame")) {
    y <- tryCatch(as.data.frame(y), error = function(e) {
      stop(sprintf("[safe_left_join] %s: Cannot coerce RHS to data.frame (class: %s)",
                   context, paste(class(y), collapse = "/")))
    })
  }

  # Perform join with error context
  tryCatch(
    dplyr::left_join(x, y, by = by),
    error = function(e) {
      stop(sprintf("[safe_left_join] %s: %s", context, e$message))
    }
  )
}

# Extract the anatomical-unit key (e.g. "Follicle_37") from region/name/id columns.
# The unit key column, id prefix and id regex all come from DOMAIN config
# (00_domain_config.R), so this works for any configured unit vocabulary.
add_follicle_key <- function(df) {
  if (is.null(df) || nrow(df) == 0) return(df)

  key_col <- domain_key_col()
  id_rx   <- domain_id_regex()
  prefix  <- domain_id_prefix()

  if (!key_col %in% names(df)) df[[key_col]] <- NA_character_

  if ("region" %in% names(df)) {
    key_from_region <- stringr::str_extract(df$region, id_rx)
    df[[key_col]] <- dplyr::coalesce(df[[key_col]], key_from_region)
  }

  if ("name" %in% names(df)) {
    key_from_name <- stringr::str_extract(df$name, id_rx)
    only_digits  <- stringr::str_extract(df$name, "\\d+")
    fallback_name <- ifelse(!is.na(only_digits), paste0(prefix, "_", only_digits), NA_character_)
    df[[key_col]] <- dplyr::coalesce(df[[key_col]], key_from_name, fallback_name)
  }

  id_alt <- paste0(domain_unit_singular(), "_id")  # e.g. "follicle_id"
  if (id_alt %in% names(df)) {
    id_str <- suppressWarnings(as.character(df[[id_alt]]))
    id_digits <- stringr::str_extract(id_str, "\\d+")
    fallback_id <- ifelse(!is.na(id_digits), paste0(prefix, "_", id_digits), NA_character_)
    df[[key_col]] <- dplyr::coalesce(df[[key_col]], fallback_id)
  }

  df
}

compute_diameter_um <- function(area_um2) {
  area_um2 <- suppressWarnings(as.numeric(area_um2))
  ifelse(is.finite(area_um2) & area_um2 > 0, 2 * sqrt(area_um2 / pi), NA_real_)
}
