"""Gate geometry and the gating engine, checked against synthetic ground truth."""
import json

import numpy as np
import pytest

from conftest import load_sample
from lymphflow import synthetic
from lymphflow.core import gates, scales
from lymphflow.core import stats as stats_mod
from lymphflow.core.cache import ArrayCache
from lymphflow.core.engine import CompensationInvalid, Engine, GateNode, PanelMismatch, make_dim
from lymphflow.core.gates import GateError

LIN = scales.make_scale("linear")
LOGICLE = scales.default_scale("fluoro", 262144.0)


def thr(scale=LOGICLE) -> float:
    return float(scales.forward(scale, np.array([synthetic.GATE_THRESHOLD]))[0])


def tree(scale=LOGICLE):
    t = thr(scale)
    cells = GateNode(1, "Cells", gates.make_spec("rect", [make_dim("FSC-A", LIN), make_dim("SSC-A", LIN)],
                                                 synthetic.SCATTER_GATE))
    cd3 = GateNode(2, "CD3+", gates.make_spec("range", [make_dim("B530-A", scale)], {"x0": t, "x1": None}), cells)
    cd4 = GateNode(3, "CD4+", gates.make_spec("polygon", [make_dim("YG585-A", scale), make_dim("R670-A", scale)],
                                              {"vertices": [[t, -5], [5, -5], [5, t], [t, t]]}), cd3)
    b = GateNode(4, "B", gates.make_spec("range", [make_dim("V450-A", scale)], {"x0": t, "x1": None}), cells)
    return cells, cd3, cd4, b


# ---- geometry -----------------------------------------------------------------------------------
def test_rect_bounds_are_min_inclusive_max_exclusive():
    x = np.array([0.0, 1.0, 1.5, 2.0, np.nan])
    y = np.array([5.0, 5.0, 5.0, 5.0, 5.0])
    m = gates.membership("rect", gates.validate_geom("rect", {"x0": 1, "x1": 2, "y0": 0, "y1": 10}), [x, y])
    assert m.tolist() == [False, True, True, False, False]


def test_missing_bound_is_unbounded_and_nan_is_never_inside():
    x = np.array([-1e9, 0.0, 3.0, np.nan])
    m = gates.membership("range", gates.validate_geom("range", {"x0": None, "x1": 1.0}), [x])
    assert m.tolist() == [True, True, False, False]


def test_polygon_matches_an_independent_even_odd_test():
    rng = np.random.default_rng(3)
    verts = np.array([[0.1, 0.1], [0.9, 0.2], [0.7, 0.8], [0.4, 0.5], [0.2, 0.9]])
    pts = rng.uniform(0, 1, (20000, 2))
    inside = np.zeros(len(pts), dtype=bool)
    j = len(verts) - 1
    for i in range(len(verts)):                       # textbook ray casting, no shared code
        xi, yi, xj, yj = *verts[i], *verts[j]
        cross = ((yi > pts[:, 1]) != (yj > pts[:, 1])) & (pts[:, 0] < (xj - xi) * (pts[:, 1] - yi) / (yj - yi) + xi)
        inside ^= cross
        j = i
    m = gates.membership("polygon", {"vertices": verts.tolist()}, [pts[:, 0].copy(), pts[:, 1].copy()])
    assert np.array_equal(m, inside)


def test_polygon_respects_parent_mask():
    x = np.array([0.5, 0.5]); y = np.array([0.5, 0.5])
    geom = {"vertices": [[0, 0], [1, 0], [1, 1], [0, 1]]}
    assert gates.membership("polygon", geom, [x, y], within=np.array([True, False])).tolist() == [True, False]


def test_ellipse_matches_the_analytic_form():
    rng = np.random.default_rng(4)
    pts = rng.uniform(-3, 3, (20000, 2))
    geom = gates.validate_geom("ellipse", {"center": [0.5, -0.25], "cov": [[2.0, 0.6], [0.6, 1.0]], "distance_square": 1.5})
    d = pts - np.array(geom["center"])
    inv = np.linalg.inv(np.array(geom["cov"]))
    expected = np.einsum("ij,jk,ik->i", d, inv, d) <= 1.5
    assert np.array_equal(gates.membership("ellipse", geom, [pts[:, 0].copy(), pts[:, 1].copy()]), expected)


@pytest.mark.parametrize("kind,geom", [
    ("rect", {"x0": None, "x1": None, "y0": None, "y1": None}),
    ("rect", {"x0": 1, "x1": 1, "y0": 0, "y1": 1}),
    ("range", {"x0": None, "x1": None}),
    ("polygon", {"vertices": [[0, 0], [1, 1]]}),
    ("polygon", {"vertices": [[0, 0], [0, 0], [0, 0]]}),
    ("polygon", {"vertices": [[0, 0], [1, float("nan")], [1, 1]]}),
    ("ellipse", {"center": [0, 0], "cov": [[1, 2], [2, 1]]}),
    ("ellipse", {"center": [0, 0], "cov": [[1, 0], [0, 1]], "distance_square": 0}),
    ("blob", {}),
])
def test_invalid_geometry_is_rejected(kind, geom):
    with pytest.raises(GateError):
        gates.validate_geom(kind, geom)


def test_spec_requires_matching_and_distinct_dimensions():
    with pytest.raises(GateError):
        gates.make_spec("rect", [make_dim("A", LIN)], {"x0": 0, "x1": 1, "y0": 0, "y1": 1})
    with pytest.raises(GateError):
        gates.make_spec("rect", [make_dim("A", LIN), make_dim("A", LIN)], {"x0": 0, "x1": 1, "y0": 0, "y1": 1})


# ---- engine on synthetic ground truth ------------------------------------------------------------
def test_exact_counts_for_every_population_in_every_file(synth, tmp_path, engine):
    out, manifest = synth
    cells, cd3, cd4, b = tree()
    for name, truth in manifest["files"].items():
        s, r = load_sample(out / name, tmp_path, engine.cache)
        assert r.comp_state == "embedded"
        c = truth["counts"]
        assert engine.count(s, None) == truth["n_events"]
        assert engine.count(s, cells) == truth["n_cells"]
        assert engine.count(s, cd3) == c["CD4 T cell"] + c["CD8 T cell"]
        assert engine.count(s, cd4) == c["CD4 T cell"]
        assert engine.count(s, b) == c["B cell"]


def test_counts_are_wrong_without_compensation_which_is_why_state_is_tracked(synth, tmp_path, engine):
    """CD3 signal spills into the CD4 detector: an uncompensated CD4 gate also catches CD8 T cells."""
    out, manifest = synth
    name, truth = next(iter(manifest["files"].items()))
    cells, cd3, _cd4, _b = tree()
    cd4_1d = GateNode(5, "CD4 (1-D)", gates.make_spec("range", [make_dim("YG585-A", LOGICLE)],
                                                      {"x0": thr(), "x1": None}), cd3)
    on, _ = load_sample(out / name, tmp_path, engine.cache)
    off, _ = load_sample(out / name, tmp_path, engine.cache, comp_enabled=False)
    assert engine.gate_key(on, cd4_1d) != engine.gate_key(off, cd4_1d)   # never served from the same cache entry
    assert engine.gate_key(on, cells) == engine.gate_key(off, cells)     # a scatter gate does not depend on comp
    assert engine.count(on, cd4_1d) == truth["counts"]["CD4 T cell"]
    assert engine.count(off, cd4_1d) > truth["counts"]["CD4 T cell"]


def test_statistics_use_compensated_linear_values(synth, tmp_path, engine):
    out, manifest = synth
    name, truth = next(iter(manifest["files"].items()))
    s, _ = load_sample(out / name, tmp_path, engine.cache)
    *_, cd4, _b = tree()
    st = stats_mod.population_stats(engine, s, cd4)
    assert st["values"] == "compensated linear" and st["comp_applied"] is True
    med = truth["true_fluoro_median"]["CD4 T cell"]
    for pnn, marker in zip(synthetic.FLUORO, synthetic.MARKERS):
        assert st["channels"][pnn]["median"] == pytest.approx(med[marker], rel=1e-5, abs=1e-2)
    # negatives straddle zero: geometric mean must report what it excluded
    cd8 = st["channels"]["R670-A"]
    assert cd8["n_nonpositive"] > 0 and cd8["geomean"] > 0
    assert st["pct_parent"] == pytest.approx(100 * st["count"] / st["parent_count"])


def test_statistics_do_not_change_with_the_display_scale(synth, tmp_path, engine):
    """The same events gated on logicle or arcsinh give identical linear statistics."""
    out, manifest = synth
    name = next(iter(manifest["files"]))
    s, _ = load_sample(out / name, tmp_path, engine.cache)
    asinh = scales.make_scale("asinh", {"t": 262144.0, "cofactor": 150.0})
    a = stats_mod.population_stats(engine, s, tree(LOGICLE)[2])
    b = stats_mod.population_stats(engine, s, tree(asinh)[2])
    assert a["count"] == b["count"] and a["channels"] == b["channels"]
    assert a["gate_key"] != b["gate_key"]


def test_mask_cache_is_content_addressed_and_reused_from_disk(synth, tmp_path, monkeypatch):
    out, manifest = synth
    name, truth = next(iter(manifest["files"].items()))
    cells, cd3, cd4, _b = tree()
    e1 = Engine(ArrayCache(), tmp_path / "masks")
    s1, _ = load_sample(out / name, tmp_path, e1.cache)
    n = e1.count(s1, cd4)
    files = list((tmp_path / "masks").rglob("*.bits"))
    assert len(files) == 3                                   # Cells, CD3+, CD4+

    # A fresh engine must load the masks from disk without evaluating any gate.
    e2 = Engine(ArrayCache(), tmp_path / "masks")
    s2, _ = load_sample(out / name, tmp_path, e2.cache)
    monkeypatch.setattr(gates, "membership", lambda *a, **k: (_ for _ in ()).throw(AssertionError("recomputed")))
    assert e2.count(s2, cd4) == n == truth["counts"]["CD4 T cell"]
    monkeypatch.undo()

    # Any change to geometry, scale or an ancestor gives a different key.
    moved = GateNode(3, "CD4+", gates.make_spec("polygon", list(cd4.spec.dims),
                                                {"vertices": [[0.5, -5], [5, -5], [5, 0.4], [0.5, 0.4]]}), cd3)
    other_parent = GateNode(3, "CD4+", cd4.spec, cells)
    keys = {e2.gate_key(s2, n_) for n_ in (cd4, moved, other_parent, tree(scales.make_scale(
        "logicle", {"t": 262144.0, "w": 0.6, "m": 4.5, "a": 0.0}))[2])}
    assert len(keys) == 4


def test_gate_on_a_missing_channel_is_a_visible_error(synth, tmp_path, engine):
    out, manifest = synth
    s, _ = load_sample(out / next(iter(manifest["files"])), tmp_path, engine.cache)
    bad = GateNode(9, "X", gates.make_spec("range", [make_dim("BV786-A", LOGICLE)], {"x0": 0.5, "x1": None}))
    with pytest.raises(PanelMismatch) as exc:
        engine.count(s, bad)
    assert exc.value.missing == ["BV786-A"]
    child = GateNode(10, "child", tree()[0].spec, bad)
    with pytest.raises(PanelMismatch):
        engine.count(s, child)


# ---- ingest variants -------------------------------------------------------------------------------
def _write(tmp_path, name, **kw):
    counts = {"CD4 T cell": 300, "CD8 T cell": 200, "B cell": 400, "Other": 100}
    events, truth = synthetic.make_events(counts, n_debris=50, seed=5, spill=kw.pop("true_spill", synthetic.SPILL))
    path = tmp_path / name
    synthetic.write_fcs(path, kw.pop("events", events), **kw)
    return path, truth


def test_file_without_a_matrix_is_used_as_stored(tmp_path, engine):
    path, truth = _write(tmp_path, "nospill.fcs", spill=None, true_spill=None)
    s, r = load_sample(path, tmp_path, engine.cache)
    assert r.comp_state == "none" and r.comp is None and not s.comp_active
    assert engine.count(s, tree()[2]) == truth["counts"]["CD4 T cell"]
    assert stats_mod.population_stats(engine, s, None)["values"].startswith("linear (no compensation")


def test_identity_matrix_is_reported_as_identity(tmp_path, engine):
    path, truth = _write(tmp_path, "identity.fcs", spill_matrix=np.eye(4), true_spill=None)
    s, r = load_sample(path, tmp_path, engine.cache)
    assert r.comp_state == "identity" and not s.comp_active
    assert engine.count(s, tree()[2]) == truth["counts"]["CD4 T cell"]


def test_vendor_spill_keyword_is_recognised(tmp_path, engine):
    path, truth = _write(tmp_path, "vendor.fcs", spill="spill")
    s, r = load_sample(path, tmp_path, engine.cache)
    assert r.comp_state == "embedded" and r.comp_message.startswith("$SPILL:")
    assert engine.count(s, tree()[2]) == truth["counts"]["CD4 T cell"]


def test_matrix_naming_an_absent_detector_is_an_error_not_a_skip(tmp_path, engine):
    renamed = [*synthetic.CHANNELS]
    renamed[5] = "V510-A"                                  # the matrix still names V450-A
    path, _ = _write(tmp_path, "badmatrix.fcs", channels=renamed)
    s, r = load_sample(path, tmp_path, engine.cache)
    assert r.comp_state == "invalid" and "V450-A" in r.comp_message
    with pytest.raises(CompensationInvalid):
        engine.count(s, tree()[0])                         # even a scatter gate refuses until resolved
    resolved, _ = load_sample(path, tmp_path, engine.cache, comp_enabled=False)
    assert engine.count(resolved, tree()[0]) == 1000


def test_non_finite_events_are_counted_and_never_gated_in(tmp_path, engine):
    counts = {"CD4 T cell": 300, "CD8 T cell": 200, "B cell": 400, "Other": 100}
    events, truth = synthetic.make_events(counts, n_debris=50, seed=5, spill=None)
    events[:7, 2] = np.nan
    path = tmp_path / "nan.fcs"
    synthetic.write_fcs(path, events, spill=None)
    s, r = load_sample(path, tmp_path, engine.cache)
    assert r.n_nonfinite == 7
    everything = GateNode(1, "all CD3", gates.make_spec("range", [make_dim("B530-A", LOGICLE)], {"x0": None, "x1": 50.0}))
    assert engine.count(s, everything) == r.n_events - 7
    assert stats_mod.population_stats(engine, s, None)["channels"]["B530-A"]["n_nonfinite"] == 7


def test_columns_are_float32_only_when_lossless(synth, tmp_path):
    out, manifest = synth
    _, r = load_sample(out / next(iter(manifest["files"])), tmp_path)
    dtypes = {c.pnn: c.dtype for c in r.channels}
    assert dtypes["B530-A"] == "float32"                   # float32 source, unit gain
    assert dtypes["Time"] == "float64"                     # raw ticks x $TIMESTEP 0.01 is not exact in float32
    assert r.dtype == "mixed"


def test_identifying_keywords_never_reach_the_event_store(synth, tmp_path):
    out, manifest = synth
    name = next(iter(manifest["files"]))
    raw = (out / name).read_bytes()
    assert b"Jane Operator" in raw and b"MRN-0042" in raw   # the fixture really carries identifiers
    _, r = load_sample(out / name, tmp_path)
    assert r.n_keywords_dropped >= 11
    blob = json.dumps(r.keywords).lower()
    for needle in ("jane", "patient", "mrn-0042", "example university", "sn123456", "0042", "07-jul"):
        assert needle not in blob
    parquet = (tmp_path / (name[:-4] + ".parquet")).read_bytes()
    for needle in (b"Jane Operator", b"MRN-0042", b"Patient 0042", b"Example University", b"SN123456"):
        assert needle not in parquet
