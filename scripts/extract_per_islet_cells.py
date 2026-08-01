#!/usr/bin/env python3
"""
Extract per-follicle single-cell data for the Shiny drill-down viewer.

For each qualified follicle (≥20 core cells), outputs a CSV file containing all
associated cells (core + peri-follicle) with spatial coordinates, phenotype,
region label, cell morphology, and 31 protein marker expressions.

Output: data/cells/{imageid}_Follicle_{N}.csv (~1,024 files)

Usage:
    python scripts/extract_per_follicle_cells.py
    python scripts/extract_per_follicle_cells.py --input path/to/single_cell.h5ad --output-dir data/cells
"""

import argparse
import os
import re
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.join(SCRIPT_DIR, "..")

DEFAULT_SC_H5AD = os.path.join(
    PROJECT_ROOT, "single_cell_analysis",
    "CODEX_scvi_BioCov_phenotyped_newDuctal.h5ad"
)
DEFAULT_LYMPH_H5AD = os.path.join(
    PROJECT_ROOT, "follicle_analysis", "follicles_core_fixed.h5ad"
)
DEFAULT_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "data", "cells")


def parse_parent(parent_str):
    """Parse Parent column to extract follicle name and region.
    Returns (follicle_name, region) or (None, None) for non-follicle cells.
    """
    if not isinstance(parent_str, str):
        return None, None
    m = re.match(r"^(Follicle_\d+)(_exp20um)?$", parent_str)
    if m:
        follicle_name = m.group(1)
        region = "peri" if m.group(2) else "core"
        return follicle_name, region
    return None, None


def extract_cells(sc_path, follicle_path, output_dir):
    """Extract per-follicle cell CSVs from single-cell H5AD."""
    import anndata as ad

    print("=" * 60)
    print("Extracting Per-Follicle Cell Data")
    print("=" * 60)

    # ── 1. Load qualified follicle list ──────────────────────────────────────
    print(f"\n1. Loading qualified follicles: {follicle_path}")
    follicle_adata = ad.read_h5ad(follicle_path)
    qualified = follicle_adata.obs[["imageid", "base_follicle_id"]].copy()
    qualified["imageid"] = qualified["imageid"].astype(str)
    qualified["base_follicle_id"] = qualified["base_follicle_id"].astype(str)
    qualified["combined_follicle_id"] = (
        qualified["imageid"] + "_" + qualified["base_follicle_id"]
    )
    qualified_set = set(qualified["combined_follicle_id"])
    print(f"   Qualified follicles: {len(qualified_set)}")

    # ── 2. Load single-cell H5AD ─────────────────────────────────────────
    print(f"\n2. Loading single-cell H5AD: {sc_path}")
    sc = ad.read_h5ad(sc_path, backed="r")
    var_names = list(sc.var_names)
    print(f"   Total cells: {sc.shape[0]:,}")
    print(f"   Markers ({len(var_names)}): {', '.join(var_names[:10])}...")

    # ── 3. Parse cell-follicle assignments ──────────────────────────────────
    print("\n3. Parsing cell-follicle assignments...")
    obs = sc.obs[["imageid", "Parent", "phenotype",
                  "X_centroid", "Y_centroid",
                  "Cell Area", "Nucleus Area"]].copy()
    obs["imageid"] = obs["imageid"].astype(str)
    obs["Parent"] = obs["Parent"].astype(str)
    obs["phenotype"] = obs["phenotype"].astype(str)

    parsed = obs["Parent"].apply(parse_parent)
    obs["follicle_name"] = [p[0] for p in parsed]
    obs["cell_region"] = [p[1] for p in parsed]

    # Filter to follicle cells only
    follicle_mask = obs["follicle_name"].notna()
    obs_follicle = obs[follicle_mask].copy()
    obs_follicle["combined_follicle_id"] = (
        obs_follicle["imageid"] + "_" + obs_follicle["follicle_name"]
    )

    # Filter to qualified follicles
    obs_follicle = obs_follicle[obs_follicle["combined_follicle_id"].isin(qualified_set)].copy()
    print(f"   Cells in qualified follicles: {len(obs_follicle):,}")

    # ── 4. Extract expression matrix for these cells ─────────────────────
    print("\n4. Extracting expression data (this may take a minute)...")
    # Get the integer indices of the follicle cells
    cell_indices = np.where(follicle_mask.values)[0]
    # Further filter to qualified
    qualified_mask = obs.loc[follicle_mask, "combined_follicle_id_temp" if False else "imageid"].index  # use obs_follicle index
    cell_idx_list = obs_follicle.index

    # Read X for these cells - chunked to manage memory
    # Since backed mode, slice to get the expression
    chunk_size = 50000
    idx_array = np.array([sc.obs_names.get_loc(i) for i in cell_idx_list])
    idx_array.sort()

    expr_chunks = []
    for start in range(0, len(idx_array), chunk_size):
        end = min(start + chunk_size, len(idx_array))
        chunk_idx = idx_array[start:end]
        chunk_data = sc.X[chunk_idx, :].toarray() if hasattr(sc.X[chunk_idx, :], 'toarray') else np.array(sc.X[chunk_idx, :])
        expr_chunks.append(chunk_data)
        print(f"   Read chunk {start//chunk_size + 1}: cells {start}-{end}")

    expr_matrix = np.vstack(expr_chunks)

    # Build expression DataFrame aligned with obs_follicle
    # Re-sort obs_follicle to match idx_array order
    idx_to_pos = {idx: pos for pos, idx in enumerate(idx_array)}
    obs_follicle_sorted = obs_follicle.loc[[sc.obs_names[i] for i in idx_array]].copy()

    expr_df = pd.DataFrame(expr_matrix, columns=var_names, index=obs_follicle_sorted.index)

    # ── 5. Write per-follicle CSVs ──────────────────────────────────────────
    print(f"\n5. Writing per-follicle CSVs to {output_dir}")
    os.makedirs(output_dir, exist_ok=True)

    # Combine obs + expression
    cell_data = pd.concat([
        obs_follicle_sorted[["X_centroid", "Y_centroid", "phenotype", "cell_region",
                           "Cell Area", "Nucleus Area"]],
        expr_df
    ], axis=1)
    cell_data["combined_follicle_id"] = obs_follicle_sorted["combined_follicle_id"]

    n_written = 0
    n_skipped = 0
    total_cells = 0
    for cid, group in cell_data.groupby("combined_follicle_id"):
        # Use combined_follicle_id as filename
        fname = f"{cid}.csv"
        fpath = os.path.join(output_dir, fname)
        out = group.drop(columns=["combined_follicle_id"])
        out.to_csv(fpath, index=False)
        n_written += 1
        total_cells += len(out)

    print(f"\n6. Summary:")
    print(f"   Files written: {n_written}")
    print(f"   Total cells extracted: {total_cells:,}")
    print(f"   Avg cells per follicle: {total_cells / max(n_written, 1):.0f}")
    print(f"   Output directory: {output_dir}")

    # Check total size
    total_size = sum(
        os.path.getsize(os.path.join(output_dir, f))
        for f in os.listdir(output_dir) if f.endswith(".csv")
    )
    print(f"   Total size: {total_size / (1024*1024):.1f} MB")

    print(f"\n{'='*60}")
    print("DONE")
    print(f"{'='*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract per-follicle single-cell CSVs for drill-down viewer"
    )
    parser.add_argument(
        "--input", default=DEFAULT_SC_H5AD,
        help="Path to single-cell H5AD (phenotyped)"
    )
    parser.add_argument(
        "--follicles", default=DEFAULT_LYMPH_H5AD,
        help="Path to follicles_core_fixed.h5ad (qualified follicle list)"
    )
    parser.add_argument(
        "--output-dir", default=DEFAULT_OUTPUT_DIR,
        help="Output directory for per-follicle CSVs"
    )
    args = parser.parse_args()

    for path, label in [
        (args.input, "single-cell H5AD"),
        (args.follicles, "follicles_core_fixed.h5ad"),
    ]:
        if not os.path.exists(path):
            print(f"ERROR: {label} not found: {path}")
            sys.exit(1)

    extract_cells(args.input, args.follicles, args.output_dir)
