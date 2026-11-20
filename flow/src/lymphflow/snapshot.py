"""Plain-dict snapshot of an experiment <-> engine objects.

The service builds the snapshot from SQLite; a job subprocess receives it inside its
spec.json and rebuilds the same engine objects without touching the database. Both sides go
through the functions here, so a job evaluates exactly what the service would.
"""
from __future__ import annotations

from pathlib import Path

from .core import comp as comp_mod
from .core import gates as gates_mod
from .core.cache import ArrayCache
from .core.engine import ChannelInfo, GateNode, SampleData, make_dim
from .core.fcs_ingest import parquet_loader
from .core.scales import Scale, make_scale


def scale_from_dict(d: dict) -> Scale:
    return make_scale(d["kind"], d.get("params") or {})


def sample_from_dict(d: dict, events_dir: Path, cache: ArrayCache) -> SampleData:
    comp = comp_mod.CompMatrix.from_json(d["comp"]) if d.get("comp") else None
    return SampleData(
        key=d["sha256"],
        name=d["name"],
        n_events=int(d["n_events"]),
        channels=tuple(
            ChannelInfo(c["idx"], c["col"], c["pnn"], c.get("pns", ""), c["kind"],
                        c.get("pnr"), c.get("vmin"), c.get("vmax"))
            for c in d["channels"]
        ),
        loader=parquet_loader(Path(events_dir) / d["parquet_file"]),
        cache=cache,
        comp=comp,
        comp_state=d["comp_state"],
        comp_enabled=bool(d.get("comp_enabled", True)),
        sample_id=int(d["id"]),
    )


def nodes_for_sample(gates: list[dict], sample_id: int) -> dict[int, GateNode]:
    """Resolve the gate tree for one sample, applying that sample's geometry overrides."""
    by_id = {int(g["id"]): g for g in gates}
    built: dict[int, GateNode] = {}

    def build(gid: int) -> GateNode:
        if gid in built:
            return built[gid]
        g = by_id[gid]
        parent = build(int(g["parent_id"])) if g.get("parent_id") else None
        override = (g.get("overrides") or {}).get(str(sample_id))
        dims = [make_dim(d["pnn"], scale_from_dict(d["scale"]), d.get("comp", "sample")) for d in g["dims"]]
        spec = gates_mod.make_spec(g["kind"], dims, override if override is not None else g["geom"])
        node = GateNode(gid, g["name"], spec, parent, overridden=override is not None)
        built[gid] = node
        return node

    for gid in by_id:
        build(gid)
    return built


def depth_first(gates: list[dict]) -> list[dict]:
    """Gates ordered parent-before-child, siblings by id."""
    children: dict[int, list[dict]] = {}
    for g in sorted(gates, key=lambda g: int(g["id"])):
        children.setdefault(int(g.get("parent_id") or 0), []).append(g)
    out: list[dict] = []

    def walk(pid: int, depth: int) -> None:
        for g in children.get(pid, []):
            out.append({**g, "depth": depth})
            walk(int(g["id"]), depth + 1)

    walk(0, 0)
    return out
