#!/usr/bin/env python3
"""Write synthetic FCS files with exactly known populations (same as `python -m lymphflow synthetic`).

    python scripts/make_synthetic_fcs.py OUT_DIR [--files 6] [--events 20000] [--seed 0]

OUT_DIR/truth.json lists every population's exact count per file, the spillover matrix and the
gate thresholds that separate the populations.
"""
import sys

from lymphflow.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(["synthetic", *sys.argv[1:]]))
