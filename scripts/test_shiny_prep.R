#!/usr/bin/env Rscript
# Smoke test: source the real app data-loading code and validate that
# prep_data() consumes the (synthetic) Excel dataset correctly, and that the
# DOMAIN config accessors return the expected lymphoid defaults.
#
# Run from the repo root:  Rscript scripts/test_shiny_prep.R
# Requires: readxl, dplyr, stringr, tidyr, jsonlite  (+ the synthetic data:
#   python scripts/make_synthetic_follicle_data.py)

suppressPackageStartupMessages({
  library(readxl); library(dplyr); library(stringr); library(tidyr); library(jsonlite)
})

# Resolve repo root from this script's location, then work from app/shiny_app so
# the app's relative data paths (../../data/app_data/...) resolve.
args <- commandArgs(trailingOnly = FALSE)
file_arg <- sub("^--file=", "", args[grep("^--file=", args)])
repo_root <- if (length(file_arg)) normalizePath(file.path(dirname(file_arg), "..")) else normalizePath(".")
setwd(file.path(repo_root, "app", "shiny_app"))

source("R/00_domain_config.R")
source("R/utils_safe_join.R")
source("R/data_loading.R")

fail <- function(msg) { cat("FAIL:", msg, "\n"); quit(status = 1L) }
ok   <- function(msg) cat("  ok:", msg, "\n")

# ---- DOMAIN config accessors -------------------------------------------------
cat("== DOMAIN config ==\n")
if (!identical(domain_unit_singular(), "follicle")) fail("unit singular != follicle")
ok("unit = follicle")
if (length(domain_group_levels()) < 2) fail("need >= 2 grouping levels")
ok(paste("grouping levels:", paste(domain_group_levels(), collapse = "/")))
if (!length(domain_defining_markers())) fail("no defining markers configured")
ok(paste("defining markers:", paste(domain_defining_markers(), collapse = ", ")))

# ---- add_follicle_key -------------------------------------------------------
k <- add_follicle_key(data.frame(region = c("Follicle_37_core", "Follicle_8_band"),
                                 stringsAsFactors = FALSE))
if (!identical(k[[domain_key_col()]], c("Follicle_37", "Follicle_8")))
  fail("add_follicle_key did not extract the unit key from region names")
ok("add_follicle_key extracts Follicle_37 / Follicle_8")

# ---- load_master + prep_data on the synthetic Excel -------------------------
cat("== load_master + prep_data ==\n")
mp <- file.path("..", "..", "data", "app_data", "master_results.xlsx")
if (!file.exists(mp)) fail(paste0("missing ", mp, " — run scripts/make_synthetic_follicle_data.py"))

master <- load_master(mp)
for (nm in c("markers", "targets", "comp")) {
  if (is.null(master[[nm]]) || !nrow(master[[nm]])) fail(paste0("empty sheet: ", nm))
}
ok("all Excel sheets loaded")

pd <- prep_data(master)
for (nm in c("targets_all", "markers_all", "comp")) {
  if (is.null(pd[[nm]]) || !nrow(pd[[nm]])) fail(paste0("prep_data produced empty ", nm))
}
ok(sprintf("prep_data: targets_all=%dx%d markers_all=%dx%d comp=%dx%d",
           nrow(pd$targets_all), ncol(pd$targets_all),
           nrow(pd$markers_all), ncol(pd$markers_all),
           nrow(pd$comp), ncol(pd$comp)))

if (!"follicle_diam_um" %in% names(pd$comp)) fail("comp lacks follicle_diam_um")
if (!any(is.finite(pd$comp$follicle_diam_um))) fail("follicle_diam_um all non-finite")
ok("comp has finite follicle_diam_um")

# union rows synthesized?
if (!any(pd$targets_all$type == "follicle_union")) fail("no follicle_union rows synthesized")
ok("follicle_union rows synthesized in targets_all")

# grouping values are within the configured level set
grp_vals <- unique(as.character(pd$comp[["Donor Status"]]))
if (!all(grp_vals %in% domain_group_levels()))
  fail(paste("data groups", paste(grp_vals, collapse="/"), "not in config levels"))
ok(paste("comp groups within config levels:", paste(grp_vals, collapse = "/")))

cat("\nPASS: data-loading smoke test\n")
