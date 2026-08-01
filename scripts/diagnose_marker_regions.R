suppressPackageStartupMessages({
  library(readxl)
  library(dplyr)
  library(stringr)
})

master_path <- file.path("data", "master_results.xlsx")

safe_read_sheet <- function(path, sheet) tryCatch(readxl::read_excel(path, sheet = sheet, guess_max = 100000), error = function(e) NULL)

load_master <- function(path) {
  list(
    markers = safe_read_sheet(path, "Follicle_Markers"),
    targets = safe_read_sheet(path, "Follicle_Targets"),
    comp    = safe_read_sheet(path, "Follicle_Composition"),
    lgals3  = safe_read_sheet(path, "LGALS3")
  )
}

add_follicle_key <- function(df) {
  if (is.null(df) || nrow(df) == 0) return(df)
  if (!"follicle_key" %in% names(df)) df$follicle_key <- NA_character_
  if ("region" %in% names(df)) {
    k <- stringr::str_extract(df$region, "Follicle_\\d+")
    df$follicle_key <- dplyr::coalesce(df$follicle_key, k)
  }
  if ("name" %in% names(df)) {
    k1 <- stringr::str_extract(df$name, "Follicle_\\d+")
    d  <- stringr::str_extract(df$name, "\\d+")
    k2 <- ifelse(!is.na(d), paste0("Follicle_", d), NA_character_)
    df$follicle_key <- dplyr::coalesce(df$follicle_key, k1, k2)
  }
  if ("follicle_id" %in% names(df)) {
    s <- suppressWarnings(as.character(df$follicle_id))
    d <- stringr::str_extract(s, "\\d+")
    k3 <- ifelse(!is.na(d), paste0("Follicle_", d), NA_character_)
    df$follicle_key <- dplyr::coalesce(df$follicle_key, k3)
  }
  df
}

normalize_region <- function(x) {
  # trim and lowercase, then map synonyms
  x0 <- tolower(trimws(as.character(x)))
  dplyr::case_when(
    x0 %in% c("follicle_core", "core") ~ "follicle_core",
    x0 %in% c("follicle_band", "band", "peri-follicle", "peri_follicle", "peri-follicle band", "perifollicle") ~ "follicle_band",
    x0 %in% c("follicle_union", "union", "follicle+20um", "follicle_20um", "follicle +20um", "follicle+20 µm") ~ "follicle_union",
    TRUE ~ x0
  )
}

m <- load_master(master_path)
mk <- m$markers %>% add_follicle_key() %>% dplyr::filter(!is.na(follicle_key)) %>%
  dplyr::mutate(region_type = normalize_region(region_type)) %>%
  dplyr::select(`Case ID`, `Donor Status`, follicle_key, region_type)
if (!is.null(m$lgals3) && nrow(m$lgals3) > 0) {
  g3 <- m$lgals3 %>% add_follicle_key() %>% dplyr::filter(!is.na(follicle_key)) %>%
    dplyr::mutate(region_type = normalize_region(region_type)) %>%
    dplyr::select(`Case ID`, `Donor Status`, follicle_key, region_type)
  mk <- dplyr::bind_rows(mk, g3)
}
mk_regions <- mk %>% dplyr::distinct(`Case ID`, `Donor Status`, follicle_key, region_type)
per_follicle <- mk_regions %>% dplyr::count(`Case ID`, `Donor Status`, follicle_key, name = "n_regions")
missing <- per_follicle %>% dplyr::filter(n_regions < 3)
cat("Follicles missing a marker region type:", nrow(missing), "\n")
if (nrow(missing) > 0) {
  # Identify which region types are present for first few
  det <- mk_regions %>% dplyr::semi_join(missing, by = c("Case ID","Donor Status","follicle_key")) %>%
    dplyr::group_by(`Case ID`,`Donor Status`, follicle_key) %>%
    dplyr::summarise(regions = paste(sort(unique(region_type)), collapse=","), .groups='drop')
  print(head(det, 20))
}