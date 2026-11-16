"""GatingML 2.0 interchange through FlowKit (optional 'interop' extra).

FlowKit is used here only as a serialiser / parser and, in the parity tests, as an
independent gating oracle. The service's own gating never goes through FlowKit objects.

Export pre-flight guards (FlowKit 1.3.2 behaviours that would otherwise corrupt a file
silently):
  * a gate name is written verbatim as an xs:ID, so names such as "CD4+ T cells" produce XML
    that fails schema validation on re-import -> ids are sanitised and made unique, and the
    id -> original name map is returned;
  * gates are looked up by name without their path, so duplicate names in different branches
    collide -> unique ids also solve this.
"""
from __future__ import annotations

import io
import re

import flowkit as fk
from flowkit import gates as fk_gates
from flowkit import transforms as fk_transforms

from ..core.scales import Scale, make_scale

_ID_BAD = re.compile(r"[^A-Za-z0-9_.\-]")


class Unsupported(ValueError):
    pass


def _xml_id(gate_id: int, name: str) -> str:
    return f"G{gate_id}_" + _ID_BAD.sub("_", name.replace("+", "pos").replace("-", "neg"))[:60]


def _fk_transform(scale: Scale):
    p = scale.p
    if scale.kind == "linear":
        return None if scale.is_identity else fk_transforms.LinearTransform(param_t=p["t"], param_a=p["a"])
    if scale.kind == "log":
        return fk_transforms.LogTransform(param_t=p["t"], param_m=p["m"])
    if scale.kind == "asinh":
        return fk_transforms.AsinhTransform(param_t=p["t"], param_m=p["m"], param_a=p["a"])
    if scale.kind == "logicle":
        return fk_transforms.LogicleTransform(param_t=p["t"], param_w=p["w"], param_m=p["m"], param_a=p["a"])
    if scale.kind == "hyperlog":
        return fk_transforms.HyperlogTransform(param_t=p["t"], param_w=p["w"], param_m=p["m"], param_a=p["a"])
    raise Unsupported(f"scale kind '{scale.kind}' has no GatingML form")


def to_flowkit_strategy(gate_dicts: list[dict], sample_id: int | None = None,
                        comp_detectors: set[str] | None = None):
    """Build a FlowKit GatingStrategy from depth-first ordered gate dicts.

    `comp_detectors`: channels compensated by the samples' embedded matrix. Dimensions on
    those channels (with comp == 'sample') are written with compensation-ref "FCS"; all
    others "uncompensated". Returns (strategy, {xml id: original name}).
    """
    comp_detectors = comp_detectors or set()
    gs = fk.GatingStrategy()
    ids: dict[int, str] = {}
    paths: dict[int, tuple] = {}
    id_map: dict[str, str] = {}
    seen_transforms: set[str] = set()

    for g in gate_dicts:
        gid = int(g["id"])
        xml_id = _xml_id(gid, g["name"])
        ids[gid] = xml_id
        id_map[xml_id] = g["name"]
        parent = int(g.get("parent_id") or 0)
        path = (paths[parent] + (ids[parent],)) if parent else ("root",)
        paths[gid] = path

        geom = g["geom"]
        if sample_id is not None:
            geom = (g.get("overrides") or {}).get(str(sample_id), geom)

        dims_meta = []
        for d in g["dims"]:
            scale = make_scale(d["scale"]["kind"], d["scale"].get("params") or {})
            xform = _fk_transform(scale)
            ref = None
            if xform is not None:
                ref = f"scale_{scale.hash[:12]}"
                if ref not in seen_transforms:
                    gs.add_transform(ref, xform)
                    seen_transforms.add(ref)
            comp_ref = "FCS" if (d.get("comp", "sample") == "sample" and d["pnn"] in comp_detectors) \
                else "uncompensated"
            dims_meta.append((d["pnn"], comp_ref, ref))

        kind = g["kind"]
        if kind in ("rect", "range"):
            dims = [fk.Dimension(pnn, compensation_ref=c, transformation_ref=t,
                                 range_min=geom[f"{ax}0"], range_max=geom[f"{ax}1"])
                    for (pnn, c, t), ax in zip(dims_meta, ("x", "y"))]
            gate = fk_gates.RectangleGate(xml_id, dims)
        elif kind == "polygon":
            dims = [fk.Dimension(pnn, compensation_ref=c, transformation_ref=t) for pnn, c, t in dims_meta]
            gate = fk_gates.PolygonGate(xml_id, dims, geom["vertices"])
        elif kind == "ellipse":
            dims = [fk.Dimension(pnn, compensation_ref=c, transformation_ref=t) for pnn, c, t in dims_meta]
            gate = fk_gates.EllipsoidGate(xml_id, dims, geom["center"], geom["cov"], geom["distance_square"])
        else:
            raise Unsupported(f"gate kind '{kind}' cannot be exported")
        gs.add_gate(gate, path)
    return gs, id_map


def export_gatingml(gate_dicts: list[dict], sample_id: int | None = None,
                    comp_detectors: set[str] | None = None) -> tuple[str, dict[str, str]]:
    gs, id_map = to_flowkit_strategy(gate_dicts, sample_id, comp_detectors)
    buf = io.BytesIO()
    fk.export_gatingml(gs, buf)
    return buf.getvalue().decode("utf-8"), id_map


# ---- FlowKit -> lymphflow (import / parity) ------------------------------------------------

def _scale_from_fk(xform) -> Scale:
    name = type(xform).__name__
    if name == "LinearTransform":
        return make_scale("linear", {"t": xform.param_t, "a": xform.param_a})
    if name == "LogTransform":
        return make_scale("log", {"t": xform.param_t, "m": xform.param_m})
    if name == "AsinhTransform":
        return make_scale("asinh", {"t": xform.param_t, "m": xform.param_m, "a": xform.param_a})
    if name == "LogicleTransform":
        return make_scale("logicle", {"t": xform.param_t, "w": xform.param_w, "m": xform.param_m, "a": xform.param_a})
    if name == "HyperlogTransform":
        return make_scale("hyperlog", {"t": xform.param_t, "w": xform.param_w, "m": xform.param_m, "a": xform.param_a})
    raise Unsupported(f"transform {name} is not supported")


def from_flowkit_strategy(gs, sample_id: str | None = None, sample_comp_refs: set[str] = frozenset({"FCS"})):
    """Convert a FlowKit GatingStrategy to lymphflow gate dicts.

    `sample_comp_refs`: compensation references that mean "the sample's own active matrix".
    Returns (gate dicts with synthetic integer ids, [(gate name, reason)] for gates that
    cannot be represented). A gate whose ancestor is unsupported is itself reported.
    """
    out: list[dict] = []
    unsupported: list[tuple[str, str]] = []
    path_to_id: dict[tuple, int] = {}
    next_id = 1
    for gate_name, gate_path in sorted(gs.get_gate_ids(), key=lambda t: len(t[1])):
        full = tuple(gate_path) + (gate_name,)
        try:
            gate = gs.get_gate(gate_name, gate_path, sample_id=sample_id)
            parent_id = None
            if len(gate_path) > 1:
                if tuple(gate_path) not in path_to_id:
                    raise Unsupported("ancestor gate is unsupported")
                parent_id = path_to_id[tuple(gate_path)]
            cls = type(gate).__name__
            if getattr(gate, "use_complement", False):
                raise Unsupported("complement gates are not supported")
            dims = []
            for dim in gate.dimensions:
                if type(dim).__name__ != "Dimension":
                    raise Unsupported(f"{type(dim).__name__} dimensions are not supported")
                ref = dim.compensation_ref
                if ref in (None, "uncompensated"):
                    comp = "none"
                elif ref in sample_comp_refs:
                    comp = "sample"
                else:
                    raise Unsupported(f"compensation reference '{ref}' is not the sample's own matrix")
                scale = make_scale("linear") if dim.transformation_ref is None \
                    else _scale_from_fk(gs.get_transform(dim.transformation_ref))
                dims.append({"pnn": dim.id, "comp": comp, "scale": {"kind": scale.kind, "params": scale.p}})
            if cls == "RectangleGate":
                if len(dims) == 1:
                    kind, geom = "range", {"x0": gate.dimensions[0].min, "x1": gate.dimensions[0].max}
                elif len(dims) == 2:
                    kind = "rect"
                    geom = {"x0": gate.dimensions[0].min, "x1": gate.dimensions[0].max,
                            "y0": gate.dimensions[1].min, "y1": gate.dimensions[1].max}
                else:
                    raise Unsupported("rectangle gates with more than 2 dimensions are not supported")
            elif cls == "PolygonGate":
                kind, geom = "polygon", {"vertices": [[float(v[0]), float(v[1])] for v in gate.vertices]}
            elif cls == "EllipsoidGate":
                if len(dims) != 2:
                    raise Unsupported("ellipsoid gates with more than 2 dimensions are not supported")
                kind = "ellipse"
                geom = {"center": [float(c) for c in gate.coordinates],
                        "cov": [[float(v) for v in row] for row in gate.covariance_matrix],
                        "distance_square": float(gate.distance_square)}
            else:
                raise Unsupported(f"{cls} is not supported")
        except Unsupported as exc:
            unsupported.append(("/".join(full[1:]), str(exc)))
            continue
        gid = next_id
        next_id += 1
        path_to_id[full] = gid
        out.append({"id": gid, "parent_id": parent_id, "name": gate_name, "kind": kind, "dims": dims,
                    "geom": geom, "overrides": {}, "fk_path": list(gate_path)})
    return out, unsupported
