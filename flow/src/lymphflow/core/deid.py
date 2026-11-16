"""FCS TEXT keyword allow-list.

An FCS TEXT segment routinely carries operator names, institution, sample/patient ids,
acquisition dates and vendor free text. FlowIO lower-cases keys and strips every '$', so a
standard `$OP` and a vendor `OP` are indistinguishable; a deny-list therefore cannot be
complete. Only keywords matched here are ever stored. Everything else is dropped at ingest
and only the *count* of dropped keys is recorded.
"""
from __future__ import annotations

import re

# Per-parameter keywords: name, stain, range, amplification, gain, bits, voltage.
_PARAM_RE = re.compile(r"^p\d+[nsregbv]$")

# Structural / instrument-model keywords with no personal or institutional content.
_EXACT = frozenset({"tot", "par", "datatype", "byteord", "mode", "timestep", "cyt", "vol"})

# Acquisition timing: kept only when the deployment opts in (LYMPH_FLOW_KEEP_ACQ_TIMES).
_TIMES = frozenset({"date", "btim", "etim"})

# Spillover is parsed into a compensation matrix, not stored as a keyword.
SPILL_KEYS = ("spillover", "spill", "comp")


def filter_keywords(text: dict[str, str], keep_times: bool = False) -> tuple[dict[str, str], int]:
    """Return (kept keywords, number of dropped keywords)."""
    kept: dict[str, str] = {}
    dropped = 0
    for raw_key, value in text.items():
        key = str(raw_key).strip().lower().lstrip("$")
        if key in SPILL_KEYS:
            continue
        if _PARAM_RE.match(key) or key in _EXACT or (keep_times and key in _TIMES):
            kept[key] = str(value)
        else:
            dropped += 1
    return kept, dropped
