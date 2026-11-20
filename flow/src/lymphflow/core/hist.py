"""Server-side binning. Raw events never leave the server; the browser receives bin counts.

Events outside the display window are clamped into the edge bins (they "pile up" on the
axis, as in conventional cytometry software) so the plotted total always equals the
population count. Events with a non-finite coordinate cannot be placed and are reported
separately as `n_nonfinite`.
"""
from __future__ import annotations

import json
import struct

import numpy as np


def _bin_index(v: np.ndarray, lo: float, hi: float, nb: int) -> np.ndarray:
    idx = ((v - lo) * (nb / (hi - lo))).astype(np.int64)
    np.clip(idx, 0, nb - 1, out=idx)
    return idx


def hist2d(x: np.ndarray, y: np.ndarray, mask: np.ndarray | None,
           xwin: tuple[float, float], ywin: tuple[float, float], nb: int = 256) -> tuple[np.ndarray, int, int]:
    """Return (counts[nb, nb] uint32 with row = y bin, n_shown, n_nonfinite)."""
    finite = np.isfinite(x) & np.isfinite(y)
    sel = finite if mask is None else (finite & mask)
    n_pop = x.shape[0] if mask is None else int(np.count_nonzero(mask))
    xs, ys = x[sel], y[sel]
    flat = _bin_index(ys, ywin[0], ywin[1], nb) * nb + _bin_index(xs, xwin[0], xwin[1], nb)
    counts = np.bincount(flat, minlength=nb * nb).astype(np.uint32).reshape(nb, nb)
    n_shown = int(xs.shape[0])
    return counts, n_shown, n_pop - n_shown


def off_scale(v: np.ndarray, mask: np.ndarray | None, win: tuple[float, float]) -> dict:
    """How many of the population's events lie outside the display window on this axis.

    Those events are drawn on the plot edge. A gate drawn strictly inside the plot does not
    contain them, so the count is surfaced to the user rather than left implicit.
    """
    vs = v if mask is None else v[mask]
    return {"below": int(np.count_nonzero(vs < win[0])), "above": int(np.count_nonzero(vs > win[1]))}


def hist1d(x: np.ndarray, mask: np.ndarray | None, win: tuple[float, float], nb: int = 256) -> tuple[np.ndarray, int, int]:
    finite = np.isfinite(x)
    sel = finite if mask is None else (finite & mask)
    n_pop = x.shape[0] if mask is None else int(np.count_nonzero(mask))
    xs = x[sel]
    counts = np.bincount(_bin_index(xs, win[0], win[1], nb), minlength=nb).astype(np.uint32)
    n_shown = int(xs.shape[0])
    return counts, n_shown, n_pop - n_shown


def pack_frame(header: dict, counts: np.ndarray) -> bytes:
    """[uint32 LE header length][UTF-8 JSON header, space-padded][uint32 LE counts].

    The header is padded so the counts start on a 4-byte boundary and the browser can view
    them as a Uint32Array without copying.
    """
    raw = json.dumps(header, separators=(",", ":")).encode()
    raw += b" " * ((-(4 + len(raw))) % 4)
    return struct.pack("<I", len(raw)) + raw + np.ascontiguousarray(counts, dtype="<u4").tobytes()


def unpack_frame(buf: bytes) -> tuple[dict, np.ndarray]:
    (n,) = struct.unpack_from("<I", buf, 0)
    header = json.loads(buf[4:4 + n].decode())
    counts = np.frombuffer(buf, dtype="<u4", offset=4 + n)
    return header, counts
