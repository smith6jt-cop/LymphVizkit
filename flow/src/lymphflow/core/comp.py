"""Spillover matrices: parse, validate, classify, apply.

Raw events are never overwritten. Compensation is a derived step whose state is recorded per
sample and shown wherever values are used:

  embedded  a valid matrix was found in the file ($SPILLOVER / SPILL)
  identity  a matrix was found but it is the identity (typical of pre-unmixed spectral files)
  none      no matrix in the file
  invalid   a matrix was found but cannot be applied (names a detector the file lacks,
            malformed, or singular). This is an error state: it is never silently skipped.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np
from flowutils import compensate as _fu_comp

from .deid import SPILL_KEYS


class CompError(ValueError):
    pass


@dataclass(frozen=True)
class CompMatrix:
    detectors: tuple[str, ...]
    matrix: np.ndarray  # n x n float64, rows = fluorochrome, columns = detector

    @property
    def hash(self) -> str:
        payload = json.dumps(
            {"detectors": list(self.detectors), "matrix": [[repr(float(v)) for v in row] for row in self.matrix]},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def to_json(self) -> dict:
        return {"detectors": list(self.detectors), "matrix": self.matrix.tolist(), "hash": self.hash}

    @staticmethod
    def from_json(d: dict) -> "CompMatrix":
        return CompMatrix(tuple(d["detectors"]), np.asarray(d["matrix"], dtype=np.float64))


def find_spill_text(text: dict[str, str]) -> tuple[str | None, str | None]:
    """Return (keyword, value) of the first spillover keyword present, else (None, None)."""
    lowered = {str(k).strip().lower().lstrip("$"): v for k, v in text.items()}
    for key in SPILL_KEYS[:2]:  # 'spillover' (FCS 3.1) then 'spill' (vendor / FCS 3.0)
        value = lowered.get(key)
        if value is not None and str(value).strip():
            return key, str(value)
    return None, None


def parse_spill(text: str) -> CompMatrix:
    try:
        matrix, detectors = _fu_comp.get_spill(text)
    except Exception as exc:  # malformed count, non-numeric cell, wrong length
        raise CompError(f"spillover keyword could not be parsed: {exc}") from exc
    matrix = np.asarray(matrix, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1] or matrix.shape[0] != len(detectors):
        raise CompError("spillover matrix is not square or does not match its header")
    return CompMatrix(tuple(detectors), matrix)


def classify(cm: CompMatrix, pnn_labels: list[str]) -> tuple[str, str]:
    """Return (state, message) for a parsed matrix against a file's PnN labels."""
    if len(set(cm.detectors)) != len(cm.detectors):
        return "invalid", "spillover matrix lists a detector more than once"
    missing = [d for d in cm.detectors if d not in pnn_labels]
    if missing:
        return "invalid", "spillover matrix names detectors absent from the file: " + ", ".join(missing)
    if not np.all(np.isfinite(cm.matrix)):
        return "invalid", "spillover matrix contains non-finite values"
    if np.allclose(cm.matrix, np.eye(len(cm.detectors)), rtol=0, atol=1e-12):
        return "identity", "embedded spillover matrix is the identity; values are used as stored"
    cond = np.linalg.cond(cm.matrix)
    if not np.isfinite(cond) or cond > 1e12:
        return "invalid", f"spillover matrix is singular or ill-conditioned (condition number {cond:.3g})"
    return "embedded", f"embedded {len(cm.detectors)} x {len(cm.detectors)} spillover matrix"


def apply(block: np.ndarray, cm: CompMatrix) -> np.ndarray:
    """Compensate an (n_events x n_detectors) float64 block whose columns follow cm.detectors.

    Delegates to the same FlowUtils routine FlowKit uses (np.linalg.solve(S.T, X.T).T) so
    results are bit-identical to FlowKit's for the same input.
    """
    block = np.ascontiguousarray(block, dtype=np.float64)
    return _fu_comp.compensate(block, cm.matrix, None)
