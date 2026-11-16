"""Parity against three independent references.

1. ISAC GatingML 2.0 compliance gates on data1.fcs, with the published per-event truth tables.
2. FlowKit's own evaluation of the same gates (same primitives, different engine).
3. A FlowJo 10 workspace (3 samples, 14 gates each): per-event identity with FlowKit, and the
   population counts FlowJo itself stored in the workspace file.

Reference files are fetched with pinned SHA-256 digests (scripts/fetch_fixtures.py); nothing
third-party is committed. Requires the optional 'interop' extra (FlowKit).
"""
import warnings
import xml.etree.ElementTree as ET

import numpy as np
import pytest

fk = pytest.importorskip("flowkit", reason="optional 'interop' extra not installed")

import fetch_fixtures  # noqa: E402
from conftest import load_sample  # noqa: E402
from lymphflow import snapshot as snap  # noqa: E402
from lymphflow import synthetic  # noqa: E402
from lymphflow.core import comp as comp_mod  # noqa: E402
from lymphflow.core import scales  # noqa: E402
from lymphflow.core.cache import ArrayCache  # noqa: E402
from lymphflow.core.engine import Engine  # noqa: E402
from lymphflow.interop import gatingml as gml  # noqa: E402

pytestmark = pytest.mark.network
warnings.filterwarnings("ignore")


def comp_refs(gs, sample_id=None) -> set[str]:
    refs = set()
    for name, path in gs.get_gate_ids():
        try:
            dims = gs.get_gate(name, path, sample_id=sample_id).dimensions
        except Exception:
            continue
        refs |= {d.compensation_ref for d in dims if getattr(d, "compensation_ref", None) not in (None, "uncompensated")}
    return refs


def use_strategy_matrix(sdata, gs, refs):
    """Give the sample the single spectrum matrix the gating strategy references."""
    (ref,) = refs
    m = gs.get_comp_matrix(ref)
    sdata.comp = comp_mod.CompMatrix(tuple(m.detectors), np.asarray(m.matrix, dtype=np.float64))
    sdata.comp_state = "embedded"


# ---- 1 + 2: ISAC compliance gates -------------------------------------------------------------------
@pytest.fixture(scope="module")
def data1(flowkit_fixtures):
    return flowkit_fixtures / "gate_ref" / "data1.fcs", fk.Sample(str(flowkit_fixtures / "gate_ref" / "data1.fcs"))


@pytest.mark.parametrize("gml_file,gate", fetch_fixtures.GML_CASES, ids=[g for _, g in fetch_fixtures.GML_CASES])
def test_isac_gate_matches_truth_table_and_flowkit(flowkit_fixtures, data1, tmp_path, gml_file, gate):
    fcs_path, fk_sample = data1
    gs = fk.parse_gating_xml(str(flowkit_fixtures / "gate_ref" / "gml" / gml_file))
    refs = comp_refs(gs)
    engine = Engine(ArrayCache(), None)
    sdata, _ = load_sample(fcs_path, tmp_path, engine.cache)
    if refs and refs != {"FCS"}:
        use_strategy_matrix(sdata, gs, refs)
    gates, unsupported = gml.from_flowkit_strategy(gs, sample_comp_refs=refs or {"FCS"})
    assert not unsupported
    node = next(n for n in snap.nodes_for_sample(gates, 0).values() if n.name == gate)
    mine = engine.mask(sdata, node)
    truth = np.loadtxt(flowkit_fixtures / "gate_ref" / "truth" / f"Results_{gate}.txt").astype(bool)
    assert mine.shape == truth.shape == (13367,)
    assert np.array_equal(mine, truth), f"{int(mine.sum())} events vs {int(truth.sum())} in the ISAC truth table"
    assert np.array_equal(mine, gs.gate_sample(fk_sample).get_gate_membership(gate))


def test_isac_cases_cover_every_gate_kind_and_scale(flowkit_fixtures):
    """Guards against the suite passing vacuously on a narrow subset."""
    kinds, scale_kinds, comp_modes = set(), set(), set()
    for gml_file, _ in fetch_fixtures.GML_CASES:
        gs = fk.parse_gating_xml(str(flowkit_fixtures / "gate_ref" / "gml" / gml_file))
        gates, _ = gml.from_flowkit_strategy(gs, sample_comp_refs=comp_refs(gs) or {"FCS"})
        for g in gates:
            kinds.add(g["kind"])
            scale_kinds |= {d["scale"]["kind"] for d in g["dims"]}
            comp_modes |= {d["comp"] for d in g["dims"]}
    assert kinds == {"rect", "range", "polygon", "ellipse"}
    assert scale_kinds == {"linear", "log", "asinh", "logicle", "hyperlog"}
    assert comp_modes == {"none", "sample"}


@pytest.mark.parametrize("gml_file", fetch_fixtures.GML_UNSUPPORTED)
def test_unsupported_gate_types_are_reported_not_misread(flowkit_fixtures, gml_file):
    gs = fk.parse_gating_xml(str(flowkit_fixtures / "gate_ref" / "gml" / gml_file))
    gates, unsupported = gml.from_flowkit_strategy(gs)
    assert unsupported and all(reason for _, reason in unsupported)
    assert len(gates) + len(unsupported) == len(gs.get_gate_ids())


# ---- 3: FlowJo workspace ---------------------------------------------------------------------------------
def flowjo_stored_counts(wsp_path) -> dict:
    """{(sample name, (population path...)): count} as written by FlowJo into the .wsp file."""
    strip = lambda tag: tag.split("}")[-1]  # noqa: E731
    out = {}
    for el in ET.parse(wsp_path).getroot().iter():
        if strip(el.tag) != "Sample":
            continue
        node = next(c for c in el if strip(c.tag) == "SampleNode")

        def walk(parent, path, name=node.get("name")):
            for sub in parent:
                if strip(sub.tag) != "Subpopulations":
                    continue
                for pop in sub:
                    if strip(pop.tag) == "Population":
                        here = path + (pop.get("name"),)
                        out[(name, here)] = int(pop.get("count"))
                        walk(pop, here)

        walk(node, ())
    return out


@pytest.fixture(scope="module")
def eight_colour(flowkit_fixtures):
    base = flowkit_fixtures / "8_color_data_set"
    fcs = sorted((base / "fcs_files").glob("*.fcs"))
    ws = fk.Workspace(str(base / "8_color_ICS.wsp"), fcs_samples=[str(p) for p in fcs])
    ws.analyze_samples(use_mp=False)
    return ws, fcs, flowjo_stored_counts(base / "8_color_ICS.wsp")


def test_flowjo_workspace_gates_match_flowkit_per_event_and_flowjo_counts(eight_colour, tmp_path):
    ws, fcs, flowjo = eight_colour
    engine = Engine(ArrayCache(), None)
    n_gates, large, small = 0, [], []
    for path in fcs:
        sid = path.name
        gs = ws.get_gating_strategy(sid)
        refs = comp_refs(gs, sid)
        sdata, r = load_sample(path, tmp_path, engine.cache)
        assert r.n_events > 280_000
        use_strategy_matrix(sdata, gs, refs)                 # the workspace's own matrix
        gates, unsupported = gml.from_flowkit_strategy(gs, sample_id=sid, sample_comp_refs=refs)
        assert not unsupported and len(gates) == 14
        nodes = snap.nodes_for_sample(gates, 0)
        for g in gates:
            mine = engine.mask(sdata, nodes[g["id"]])
            theirs = ws.get_gate_membership(sid, g["name"], tuple(g["fk_path"]))
            assert np.array_equal(mine, theirs), (sid, g["name"])
            n_gates += 1
            stored = flowjo[(sid, tuple(g["fk_path"][1:]) + (g["name"],))]
            count = int(mine.sum())
            (large if stored >= 1000 else small).append((abs(count - stored), stored, sid, g["name"]))
        sdata.release()
    assert n_gates == 42 and len(large) + len(small) == 42
    # FlowJo's stored counts are NOT reproduced exactly - by this engine or by FlowKit, which
    # it matches event for event. Measured on this workspace (Nov 2026): 8 of 42 populations
    # exact; the 19 populations of >= 1000 events within 0.41 %; the 23 smaller ones within
    # 10 events (worst case 64 vs 74, i.e. 13.5 %, for a rare cytokine gate). The bounds below
    # are those observations plus a small margin; they are regression guards, not a claim of
    # FlowJo equivalence. See docs/omiq_evaluation.md.
    assert len(large) == 19 and len(small) == 23
    worst_rel = max(d / s for d, s, *_ in large)
    worst_abs = max(d for d, *_ in small)
    assert worst_rel < 0.005, f"large populations differ from FlowJo by up to {100 * worst_rel:.3f}%"
    assert worst_abs <= 12, f"small populations differ from FlowJo by up to {worst_abs} events"
    assert sum(d == 0 for d, *_ in large + small) >= 8


# ---- export round trip ----------------------------------------------------------------------------------------
def test_gatingml_export_round_trips_through_flowkit_with_identical_counts(synth, tmp_path):
    out, manifest = synth
    name, truth = next(iter(manifest["files"].items()))
    logicle = scales.default_scale("fluoro", 262144.0)
    asinh = scales.make_scale("asinh", {"t": 262144.0, "cofactor": 150.0})
    thr = float(scales.forward(logicle, np.array([synthetic.GATE_THRESHOLD]))[0])
    thr_a = float(scales.forward(asinh, np.array([synthetic.GATE_THRESHOLD]))[0])
    sc = lambda s: {"kind": s.kind, "params": s.p}  # noqa: E731
    lin = {"kind": "linear", "params": {}}
    gates = [
        {"id": 1, "parent_id": None, "name": "Cells", "kind": "rect", "geom": synthetic.SCATTER_GATE,
         "dims": [{"pnn": "FSC-A", "scale": lin}, {"pnn": "SSC-A", "scale": lin}]},
        {"id": 2, "parent_id": 1, "name": "CD3+", "kind": "range", "geom": {"x0": thr, "x1": None},
         "dims": [{"pnn": "B530-A", "scale": sc(logicle)}]},
        # a name that is not a valid xs:ID, and vertices pushed far outside the window (edge-extended)
        {"id": 3, "parent_id": 2, "name": "CD4+ T cells (helper)", "kind": "polygon",
         "geom": {"vertices": [[thr, -1.06], [2.06, -1.06], [2.06, thr], [thr, thr]]},
         "dims": [{"pnn": "YG585-A", "scale": sc(logicle)}, {"pnn": "R670-A", "scale": sc(logicle)}]},
        {"id": 4, "parent_id": 1, "name": "B cells", "kind": "range", "geom": {"x0": thr_a, "x1": None},
         "dims": [{"pnn": "V450-A", "scale": sc(asinh)}]},
        {"id": 5, "parent_id": 1, "name": "CD3+", "kind": "ellipse",       # same name in another branch
         "geom": {"center": [0.9, 0.9], "cov": [[0.02, 0.0], [0.0, 0.02]], "distance_square": 1.0},
         "dims": [{"pnn": "B530-A", "scale": sc(logicle)}, {"pnn": "YG585-A", "scale": sc(logicle)}]},
    ]
    xml, id_map = gml.export_gatingml(gates, comp_detectors=set(synthetic.FLUORO))
    assert set(id_map.values()) == {g["name"] for g in gates} and len(id_map) == 5      # unique ids
    assert 'compensation-ref="FCS"' in xml and 'compensation-ref="uncompensated"' in xml

    engine = Engine(ArrayCache(), None)
    sdata, _ = load_sample(out / name, tmp_path, engine.cache)
    nodes = snap.nodes_for_sample(gates, 0)
    mine = {g["name"] if g["id"] != 5 else "ellipse": engine.mask(sdata, nodes[g["id"]]) for g in gates}
    assert int(mine["CD4+ T cells (helper)"].sum()) == truth["counts"]["CD4 T cell"]
    assert int(mine["B cells"].sum()) == truth["counts"]["B cell"]

    gml_path = tmp_path / "export.xml"
    gml_path.write_text(xml)
    gs = fk.parse_gating_xml(str(gml_path))                    # would raise on an invalid xs:ID
    results = gs.gate_sample(fk.Sample(str(out / name)))
    by_name = {v: k for k, v in id_map.items() if v != "CD3+"}
    for original, xml_id in by_name.items():
        assert np.array_equal(results.get_gate_membership(xml_id), mine[original]), original
    cd3_range = next(k for k, v in id_map.items() if v == "CD3+" and k.startswith("G2_"))
    cd3_ellipse = next(k for k, v in id_map.items() if v == "CD3+" and k.startswith("G5_"))
    assert np.array_equal(results.get_gate_membership(cd3_range), mine["CD3+"])
    assert np.array_equal(results.get_gate_membership(cd3_ellipse), mine["ellipse"])


def test_gatingml_endpoint_serves_the_template(client, synth):
    from conftest import upload

    out, manifest = synth
    exp = client.post("/api/experiments", json={"name": "e"}).json()["id"]
    upload(client, exp, [out / sorted(manifest["files"])[0]])
    client.post(f"/api/experiments/{exp}/gates", json={"name": "Cells & debris?", "kind": "rect",
                                                       "dims": ["FSC-A", "SSC-A"], "geom": synthetic.SCATTER_GATE})
    res = client.get(f"/api/experiments/{exp}/gatingml")
    assert res.status_code == 200 and "attachment" in res.headers["content-disposition"]
    assert res.text.lstrip().startswith("<?xml") and "RectangleGate" in res.text
    assert "Cells & debris?" in res.headers["x-lymphflow-gate-ids"]
