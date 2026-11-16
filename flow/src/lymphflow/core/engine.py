"""Gating engine over lazily loaded columns.

Nothing here touches SQLite or HTTP. A `SampleData` exposes columns by PnN; the engine
evaluates a chain of gates and caches each membership mask under a content hash of
everything that determines it:

    engine version, FCS sha256, active compensation matrix hash,
    gate kind + geometry, each dimension's (PnN, scale hash, comp mode), parent key

so a stale mask cannot be returned after any of those change. Content addressing *is* the
invalidation.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import ENGINE_VERSION
from . import comp as comp_mod
from . import gates as gates_mod
from . import scales as scales_mod
from .cache import ArrayCache
from .gates import Dim, GateSpec
from .scales import Scale


class PanelMismatch(ValueError):
    """A gate or request names a channel the sample does not have."""

    def __init__(self, sample_name: str, missing: list[str]):
        self.sample_name = sample_name
        self.missing = missing
        super().__init__(f"sample '{sample_name}' has no channel(s): {', '.join(missing)}")


class CompensationInvalid(ValueError):
    """The sample's embedded matrix cannot be applied and the user has not resolved it."""


@dataclass(frozen=True)
class ChannelInfo:
    idx: int
    col: str
    pnn: str
    pns: str
    kind: str  # scatter | time | fluoro
    pnr: float | None = None
    vmin: float | None = None
    vmax: float | None = None


@dataclass
class SampleData:
    """Lazy, cached column access for one sample.

    `loader(cols)` returns a dict col -> 1-D array (stored dtype) for the requested Parquet
    column names. Raw values are immutable; compensated and transformed values are derived
    and cached.
    """

    key: str                      # FCS sha256: identity of the event data
    name: str
    n_events: int
    channels: tuple[ChannelInfo, ...]
    loader: Callable[[list[str]], dict[str, np.ndarray]]
    cache: ArrayCache
    comp: comp_mod.CompMatrix | None = None
    comp_state: str = "none"
    comp_enabled: bool = True
    sample_id: int = 0
    _by_pnn: dict[str, ChannelInfo] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._by_pnn = {c.pnn: c for c in self.channels}

    # ---- identity ------------------------------------------------------------
    @property
    def comp_active(self) -> bool:
        return self.comp is not None and self.comp_enabled and self.comp_state == "embedded"

    @property
    def comp_hash(self) -> str:
        return self.comp.hash if self.comp_active else "none"

    def require_usable_comp(self) -> None:
        if self.comp_state == "invalid" and self.comp_enabled:
            raise CompensationInvalid(
                f"sample '{self.name}': the embedded spillover matrix cannot be applied. "
                "Resolve it (switch compensation off for this sample) before gating or plotting."
            )

    def channel(self, pnn: str) -> ChannelInfo:
        try:
            return self._by_pnn[pnn]
        except KeyError:
            raise PanelMismatch(self.name, [pnn]) from None

    def require(self, pnns: list[str]) -> None:
        missing = [p for p in pnns if p not in self._by_pnn]
        if missing:
            raise PanelMismatch(self.name, missing)

    # ---- values --------------------------------------------------------------
    def raw(self, pnn: str) -> np.ndarray:
        ch = self.channel(pnn)
        return self.cache.get_or_compute(("raw", self.key, ch.col), lambda: self.loader([ch.col])[ch.col])

    def _comp_block(self) -> np.ndarray:
        cm = self.comp

        def compute() -> np.ndarray:
            cols = [self.channel(d).col for d in cm.detectors]
            loaded = self.loader(cols)
            block = np.empty((self.n_events, len(cols)), dtype=np.float64)
            for j, c in enumerate(cols):
                block[:, j] = loaded[c]
            return comp_mod.apply(block, cm)

        return self.cache.get_or_compute(("comp", self.key, cm.hash), compute)

    def linear(self, pnn: str, comp: str = "sample") -> np.ndarray:
        """Linear-scale float64 values: compensated when compensation applies to this channel."""
        if comp == "sample":
            self.require_usable_comp()
            if self.comp_active and pnn in self.comp.detectors:
                self.channel(pnn)
                return self._comp_block()[:, self.comp.detectors.index(pnn)]
        raw = self.raw(pnn)
        if raw.dtype == np.float64:
            return raw
        return self.cache.get_or_compute(("raw64", self.key, pnn), lambda: raw.astype(np.float64))

    def scaled(self, pnn: str, scale: Scale, comp: str = "sample") -> np.ndarray:
        """Values in `scale` units (float64)."""
        if scale.is_identity:
            return self.linear(pnn, comp)
        comp_key = self.comp_hash if comp == "sample" else "none"
        return self.cache.get_or_compute(
            ("scaled", self.key, pnn, comp_key, scale.hash),
            lambda: scales_mod.forward(scale, self.linear(pnn, comp)),
        )

    def release(self) -> None:
        self.cache.drop_prefix(("raw", self.key))
        self.cache.drop_prefix(("raw64", self.key))
        self.cache.drop_prefix(("comp", self.key))
        self.cache.drop_prefix(("scaled", self.key))
        self.cache.drop_prefix(("mask", self.key))


@dataclass(frozen=True)
class GateNode:
    """One gate resolved for one sample (override applied), linked to its parent."""

    gate_id: int
    name: str
    spec: GateSpec
    parent: "GateNode | None" = None
    overridden: bool = False

    @property
    def path(self) -> tuple[str, ...]:
        return (self.parent.path if self.parent else ()) + (self.name,)


class Engine:
    def __init__(self, cache: ArrayCache, masks_dir: Path | None = None):
        self.cache = cache
        self.masks_dir = Path(masks_dir) if masks_dir else None
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---- keys ----------------------------------------------------------------
    def gate_key(self, sample: SampleData, node: GateNode | None) -> str:
        if node is None:
            return "root"
        uses_comp = any(d.comp == "sample" for d in node.spec.dims)
        payload = json.dumps(
            [
                ENGINE_VERSION,
                sample.key,
                sample.comp_hash if uses_comp else "none",
                node.spec.kind,
                node.spec.geom_json,
                [d.key() for d in node.spec.dims],
                self.gate_key(sample, node.parent),
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _mask_file(self, sample: SampleData, key: str) -> Path | None:
        if self.masks_dir is None:
            return None
        return self.masks_dir / sample.key[:2] / sample.key / f"{key}.bits"

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    # ---- membership ----------------------------------------------------------
    def mask(self, sample: SampleData, node: GateNode | None) -> np.ndarray | None:
        """Boolean mask for a population; None means the root (all events)."""
        if node is None:
            return None
        sample.require([d.pnn for d in node.spec.dims])
        key = self.gate_key(sample, node)
        cache_key = ("mask", sample.key, key)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        with self._lock_for(sample.key + key):
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached
            path = self._mask_file(sample, key)
            if path is not None and path.is_file():
                packed = np.fromfile(path, dtype=np.uint8)
                if packed.size == (sample.n_events + 7) // 8:
                    m = np.unpackbits(packed, count=sample.n_events).astype(bool)
                    return self.cache.put(cache_key, m)
            parent = self.mask(sample, node.parent)
            cols = [sample.scaled(d.pnn, d.scale, d.comp) for d in node.spec.dims]
            m = gates_mod.membership(node.spec.kind, node.spec.geom, cols, within=parent)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
                np.packbits(m).tofile(tmp)
                tmp.replace(path)
            return self.cache.put(cache_key, m)

    def count(self, sample: SampleData, node: GateNode | None) -> int:
        m = self.mask(sample, node)
        return sample.n_events if m is None else int(np.count_nonzero(m))


def make_dim(pnn: str, scale: Scale, comp: str = "sample") -> Dim:
    if comp not in ("sample", "none"):
        raise gates_mod.GateError("dimension comp must be 'sample' or 'none'")
    return Dim(pnn, scale, comp)
