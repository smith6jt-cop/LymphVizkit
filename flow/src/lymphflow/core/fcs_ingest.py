"""FCS -> Parquet event store.

One immutable Parquet file per sample holding the *preprocessed, uncompensated,
untransformed* values (FlowIO's gain / log-amplifier / timestep preprocessing, the same call
FlowKit makes), one column per parameter. A column is stored as float32 only when that is
lossless for every value; otherwise float64. The FCS file itself is not kept.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import flowio
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import comp as comp_mod
from .deid import filter_keywords

ROW_GROUP = 262_144
_MAGIC = (b"FCS2.0", b"FCS3.0", b"FCS3.1", b"FCS3.2")


class FcsError(ValueError):
    pass


@dataclass
class ParsedChannel:
    idx: int
    col: str
    pnn: str
    pns: str
    kind: str
    pnr: float | None
    vmin: float | None
    vmax: float | None
    dtype: str


@dataclass
class IngestResult:
    sha256: str
    fcs_version: str
    n_events: int
    n_params: int
    n_nonfinite: int
    dtype: str                      # 'float32' if every column is float32, else 'float64' / 'mixed'
    channels: list[ParsedChannel]
    keywords: dict[str, str]        # allow-listed only
    n_keywords_dropped: int
    comp_state: str
    comp_message: str
    comp: comp_mod.CompMatrix | None
    warnings: list[str] = field(default_factory=list)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _open_flowdata(path: Path, warnings: list[str]) -> flowio.FlowData:
    """Strict parse first; retry leniently on the offset errors some vendors' files trigger."""
    try:
        return flowio.FlowData(str(path))
    except Exception as strict_exc:  # FlowIO raises several unrelated exception types
        try:
            fd = flowio.FlowData(str(path), ignore_offset_error=True, ignore_offset_discrepancy=True)
        except Exception:
            raise FcsError(f"not a readable FCS file: {strict_exc}") from strict_exc
        warnings.append(f"parsed with offset tolerance (strict parse failed: {strict_exc})")
        return fd


def _float_or_none(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def ingest_fcs(path: Path, parquet_path: Path, keep_times: bool = False) -> IngestResult:
    path = Path(path)
    with open(path, "rb") as fh:
        magic = fh.read(6)
    if magic not in _MAGIC:
        raise FcsError("file does not start with an FCS2.0/3.0/3.1 header")

    warnings: list[str] = []
    fd = _open_flowdata(path, warnings)
    text = fd.text
    if str(text.get("mode", "L")).strip().upper() != "L":
        raise FcsError(f"unsupported $MODE '{text.get('mode')}': only list-mode (L) data is supported")
    if str(text.get("nextdata", "0")).strip() not in ("0", ""):
        warnings.append("file contains more than one data set; only the first was read")

    n_params = int(fd.channel_count)
    events = fd.as_array(preprocess=True)  # float64, (n_events, n_params)
    if events.ndim != 2 or events.shape[1] != n_params:
        raise FcsError("event data does not match $PAR")
    n_events = int(events.shape[0])
    if n_events == 0:
        raise FcsError("file contains no events")
    if n_events != int(fd.event_count):
        warnings.append(f"$TOT says {fd.event_count} events but {n_events} were read")

    pnn_labels = [fd.channels[i + 1]["pnn"] for i in range(n_params)]
    if len(set(pnn_labels)) != len(pnn_labels):
        raise FcsError("file has duplicate $PnN channel names")
    scatter = set(fd.scatter_indices)
    time_index = fd.time_index

    finite_rows = np.isfinite(events).all(axis=1)
    n_nonfinite = int(n_events - np.count_nonzero(finite_rows))

    arrays: dict[str, pa.Array] = {}
    channels: list[ParsedChannel] = []
    for i in range(n_params):
        col = f"p{i + 1:03d}"
        values = events[:, i]
        as32 = values.astype(np.float32)
        lossless = np.array_equal(as32.astype(np.float64), values, equal_nan=True)
        stored = as32 if lossless else values
        arrays[col] = pa.array(stored)
        finite = values[np.isfinite(values)]
        meta = fd.channels[i + 1]
        kind = "time" if i == time_index else ("scatter" if i in scatter else "fluoro")
        channels.append(
            ParsedChannel(
                idx=i, col=col, pnn=pnn_labels[i], pns=str(meta.get("pns", "") or ""), kind=kind,
                pnr=_float_or_none(meta.get("pnr")),
                vmin=float(finite.min()) if finite.size else None,
                vmax=float(finite.max()) if finite.size else None,
                dtype=str(stored.dtype),
            )
        )
    del events

    keywords, n_dropped = filter_keywords(text, keep_times=keep_times)

    comp, comp_state, comp_message = None, "none", "no spillover matrix in the file"
    spill_key, spill_text = comp_mod.find_spill_text(text)
    if spill_text is not None:
        try:
            comp = comp_mod.parse_spill(spill_text)
            comp_state, comp_message = comp_mod.classify(comp, pnn_labels)
        except comp_mod.CompError as exc:
            comp, comp_state, comp_message = None, "invalid", str(exc)
        comp_message = f"${spill_key.upper()}: {comp_message}"

    sha = file_sha256(path)
    parquet_path = Path(parquet_path)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(arrays)
    table = table.replace_schema_metadata(
        {
            "lymphflow": json.dumps(
                {
                    "sha256": sha,
                    "n_events": n_events,
                    "channels": [{"col": c.col, "pnn": c.pnn, "pns": c.pns, "kind": c.kind} for c in channels],
                }
            )
        }
    )
    tmp = parquet_path.with_suffix(".tmp")
    # Float event data is high-cardinality: dictionary encoding only bloats it (measured: 41 %
    # larger than the FCS). Byte-stream-split + zstd is ~19 % smaller than the FCS and reads faster.
    pq.write_table(table, tmp, row_group_size=ROW_GROUP, compression="zstd",
                   use_dictionary=False, use_byte_stream_split=True)
    tmp.replace(parquet_path)

    dtypes = {c.dtype for c in channels}
    return IngestResult(
        sha256=sha,
        fcs_version=str(fd.version),
        n_events=n_events,
        n_params=n_params,
        n_nonfinite=n_nonfinite,
        dtype=dtypes.pop() if len(dtypes) == 1 else "mixed",
        channels=channels,
        keywords=keywords,
        n_keywords_dropped=n_dropped,
        comp_state=comp_state,
        comp_message=comp_message,
        comp=comp,
        warnings=warnings,
    )


def parquet_loader(parquet_path: Path):
    """Return loader(cols) -> {col: ndarray} reading only the requested columns."""
    parquet_path = Path(parquet_path)

    def load(cols: list[str]) -> dict[str, np.ndarray]:
        table = pq.read_table(parquet_path, columns=list(cols))
        return {c: table.column(c).to_numpy() for c in cols}

    return load
