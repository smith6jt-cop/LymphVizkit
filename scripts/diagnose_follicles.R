suppressPackageStartupMessages({
  library(readxl)
  library(dplyr)
  library(stringr)
})

master_path <- file.path("data", "master_results.xlsx")

safe_read_sheet <- function(path, sheet) {
  tryCatch(readxl::read_excel(path, sheet = sheet, guess_max = 100000), error = function(e) NULL)
}

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
  # region-like columns to try
  region_cols <- intersect(c("region", "Region", "follicle_region"), names(df))
  if (length(region_cols) > 0) {
    for (rc in region_cols) {
      key_from_region <- stringr::str_extract(df[[rc]], "Follicle_\\d+")
      df$follicle_key <- dplyr::coalesce(df$follicle_key, key_from_region)
    }
  }
  # name-like columns
  name_cols <- intersect(c("name","Name","follicle_name","follicle"), names(df))
  if (length(name_cols) > 0) {
    for (nc in name_cols) {
      key_from_name <- stringr::str_extract(df[[nc]], "Follicle_\\d+")
      only_digits  <- stringr::str_extract(as.character(df[[nc]]), "\\\\d+")
      fallback_name <- ifelse(!is.na(only_digits), paste0("Follicle_", only_digits), NA_character_)
      df$follicle_key <- dplyr::coalesce(df$follicle_key, key_from_name, fallback_name)
    }
  }
  # id-like columns
  id_cols <- intersect(c("follicle_id","Follicle ID","Follicle_ID","follicleID"), names(df))
  if (length(id_cols) > 0) {
    for (ic in id_cols) {
      id_str <- suppressWarnings(as.character(df[[ic]]))
      id_digits <- stringr::str_extract(id_str, "\\\\d+")
      fallback_id <- ifelse(!is.na(id_digits), paste0("Follicle_", id_digits), NA_character_)
      df$follicle_key <- dplyr::coalesce(df$follicle_key, fallback_id)
    }
  }
  df
}

inspect <- function(df, label) {
  if (is.null(df)) {
    cat("[", label, "] sheet missing or unreadable\n", sep="")
    return()
  }
  cat("[", label, "] nrows=", nrow(df), "\n", sep="")
  cat("cols: ", paste(names(df), collapse=", "), "\n", sep="")
  dd <- add_follicle_key(df)
  na_rows <- sum(is.na(dd$follicle_key))
  cat("follicle_key NA:", na_rows, " (", ifelse(nrow(dd)>0, round(100*na_rows/nrow(dd),2), 0), "%)\n", sep="")
  if (na_rows > 0) {
    cat("Examples with NA follicle_key (first 5):\n")
    ex <- dd %>% filter(is.na(follicle_key)) %>% select(any_of(c("Case ID","Donor Status","region","Region","name","Name","follicle_id","Follicle ID","Follicle_ID"))) %>% head(5)
    print(ex)
  }
  # distinct follicle set size by donor
  if (all(c("Case ID","Donor Status") %in% names(dd))) {
    di <- dd %>% filter(!is.na(follicle_key)) %>% distinct(`Case ID`,`Donor Status`, follicle_key)
    cat("distinct follicles:", nrow(di), "\n")
    per_donor <- di %>% count(`Case ID`,`Donor Status`, name = "n_follicles") %>% arrange(`Donor Status`, `Case ID`)
    print(head(per_donor, 10))
  }
}

m <- load_master(master_path)
inspect(m$comp, "comp")
inspect(m$markers, "markers")
inspect(m$targets, "targets")
if (!is.null(m$lgals3)) inspect(m$lgals3, "lgals3")
