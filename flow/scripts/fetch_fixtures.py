#!/usr/bin/env python3
"""Fetch third-party reference files for the parity tests, verifying pinned SHA-256 digests.

The files come from the FlowKit repository at release tag 1.3.2 (example FCS files, ISAC
GatingML 2.0 compliance gates with per-event truth tables, and a FlowJo 10 workspace).
They are NOT committed here: FlowKit's `data/` directory carries no licence statement, and
the ISAC files permit distribution but reserve modification. They are downloaded on demand
into flow/tests/fixtures/ (gitignored) and every file is checked against the digest pinned
in tests/parity/fixtures.sha256.json.

    python scripts/fetch_fixtures.py                 # download + verify
    python scripts/fetch_fixtures.py --update-pins   # (maintainers) rewrite the pin file
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

FLOW = Path(__file__).resolve().parents[1]
DEST = FLOW / "tests" / "fixtures" / "flowkit"
PINS = FLOW / "tests" / "parity" / "fixtures.sha256.json"
BASE = "https://raw.githubusercontent.com/whitews/FlowKit/1.3.2/data/"

# (GatingML file, gate name == truth file suffix). One gate per file, evaluated on data1.fcs.
GML_CASES = [
    ("gml_range_gate.xml", "Range1"),
    ("gml_rect1_gate.xml", "Rectangle1"),
    ("gml_rect2_gate.xml", "Rectangle2"),
    ("gml_poly1_gate.xml", "Polygon1"),
    ("gml_poly2_gate.xml", "Polygon2"),
    ("gml_poly3ns_gate.xml", "Polygon3NS"),
    ("gml_ellipse1_gate.xml", "Ellipse1"),
    ("gml_time_range_gate.xml", "Range2"),
    ("gml_transform_asinh_range1_gate.xml", "ScaleRange1"),
    ("gml_transform_hyperlog_range2_gate.xml", "ScaleRange2"),
    ("gml_transform_linear_range3_gate.xml", "ScaleRange3"),
    ("gml_transform_logicle_range4_gate.xml", "ScaleRange4"),
    ("gml_transform_logicle_range5_gate.xml", "ScaleRange5"),
    ("gml_transform_log_range6_gate.xml", "ScaleRange6"),
    ("gml_matrix_poly4_gate.xml", "Polygon4"),
    ("gml_matrix_rect3_gate.xml", "Rectangle3"),
    ("gml_matrix_rect4_gate.xml", "Rectangle4"),
    ("gml_matrix_rect5_gate.xml", "Rectangle5"),
    ("gml_matrix_transform_asinh_range1c_gate.xml", "ScaleRange1c"),
    ("gml_matrix_transform_hyperlog_range2c_gate.xml", "ScaleRange2c"),
    ("gml_matrix_transform_linear_range3c_gate.xml", "ScaleRange3c"),
    ("gml_matrix_transform_logicle_range4c_gate.xml", "ScaleRange4c"),
]
# Gate types lymphflow does not implement; the tests assert they are *reported* as unsupported.
GML_UNSUPPORTED = ["gml_ellipsoid3d_gate.xml", "gml_quadrant1_gate.xml", "gml_ratio_range1_gate.xml",
                   "gml_boolean_and1_gate.xml"]

EIGHT_COLOUR = [
    "8_color_data_set/8_color_ICS.wsp",
    "8_color_data_set/fcs_files/101_DEN084Y5_15_E01_008_clean.fcs",
    "8_color_data_set/fcs_files/101_DEN084Y5_15_E03_009_clean.fcs",
    "8_color_data_set/fcs_files/101_DEN084Y5_15_E05_010_clean.fcs",
]
OTHER = ["gate_ref/data1.fcs", "100715.fcs", "test_comp_example.fcs"]


def all_paths() -> list[str]:
    paths = list(OTHER) + list(EIGHT_COLOUR)
    for gml, gate in GML_CASES:
        paths += [f"gate_ref/gml/{gml}", f"gate_ref/truth/Results_{gate}.txt"]
    paths += [f"gate_ref/gml/{g}" for g in GML_UNSUPPORTED]
    return paths


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(update_pins: bool = False, quiet: bool = False) -> Path:
    """Download any missing file and verify every file. Returns the fixture directory."""
    pins = json.loads(PINS.read_text()) if PINS.exists() else {}
    new_pins = {}
    for rel in all_paths():
        dest = DEST / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not quiet:
                print(f"fetching {rel}", file=sys.stderr)
            tmp = dest.with_suffix(dest.suffix + ".part")
            with urllib.request.urlopen(BASE + rel, timeout=120) as res, open(tmp, "wb") as out:
                while chunk := res.read(1 << 20):
                    out.write(chunk)
            tmp.replace(dest)
        digest = sha256(dest)
        new_pins[rel] = digest
        if not update_pins:
            if rel not in pins:
                raise SystemExit(f"{rel}: no pinned digest (run with --update-pins after reviewing the file)")
            if pins[rel] != digest:
                dest.unlink()
                raise SystemExit(f"{rel}: SHA-256 mismatch (expected {pins[rel]}, got {digest}); file removed")
    if update_pins:
        PINS.write_text(json.dumps(new_pins, indent=1, sort_keys=True) + "\n")
        if not quiet:
            print(f"wrote {len(new_pins)} pins to {PINS}", file=sys.stderr)
    return DEST


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--update-pins", action="store_true")
    args = ap.parse_args()
    d = fetch(update_pins=args.update_pins)
    print(f"{len(all_paths())} fixture files verified in {d}")
