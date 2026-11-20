import math

import numpy as np
import pytest
from flowutils import transforms as fu

from lymphflow.core import scales
from lymphflow.core.scales import ScaleError, make_scale

X = np.array([-500.0, -50.0, 0.0, 1.0, 37.5, 1000.0, 65000.0, 262144.0])
KINDS = {
    "linear": {"t": 262144.0, "a": 1000.0},
    "asinh": {"t": 262144.0, "m": 4.0, "a": 1.0},
    "logicle": {"t": 262144.0, "w": 0.5, "m": 4.5, "a": 0.0},
    "hyperlog": {"t": 262144.0, "w": 0.5, "m": 4.5, "a": 0.0},
}


@pytest.mark.parametrize("kind", KINDS)
def test_round_trip(kind):
    sc = make_scale(kind, KINDS[kind])
    np.testing.assert_allclose(scales.inverse(sc, scales.forward(sc, X)), X, rtol=1e-6, atol=1e-3)


def test_log_round_trip_positive_only():
    sc = make_scale("log", {"t": 10000.0, "m": 5.0})
    x = np.array([0.5, 1.0, 100.0, 10000.0])
    np.testing.assert_allclose(scales.inverse(sc, scales.forward(sc, x)), x, rtol=1e-9)


def test_values_identical_to_flowutils():
    """Same C routine as FlowKit -> bit-identical transformed values."""
    sc = make_scale("logicle", KINDS["logicle"])
    assert np.array_equal(scales.forward(sc, X), fu.logicle(X, None, t=262144.0, m=4.5, w=0.5, a=0.0))
    sc = make_scale("asinh", KINDS["asinh"])
    assert np.array_equal(scales.forward(sc, X), fu.asinh(X, None, t=262144.0, m=4.0, a=1.0))


def test_identity_linear_is_a_no_op():
    sc = make_scale("linear")
    assert sc.is_identity and scales.forward(sc, X) is not None
    np.testing.assert_array_equal(scales.forward(sc, X), X)


def test_cofactor_form_is_arcsinh_x_over_c():
    t, c = 262144.0, 150.0
    sc = make_scale("asinh", {"t": t, "cofactor": c})
    np.testing.assert_allclose(scales.forward(sc, X), np.arcsinh(X / c) / math.asinh(t / c), rtol=1e-12)
    assert "cofactor=150" in scales.describe(sc)


@pytest.mark.parametrize("kind,params,fragment", [
    ("logicle", {"t": 262144, "w": 3.0, "m": 4.5}, "W must satisfy"),
    ("logicle", {"t": -1, "w": 0.5, "m": 4.5}, "T must be > 0"),
    ("logicle", {"t": 262144, "w": 0.5, "m": 4.5, "a": 5}, "A must satisfy"),
    ("logicle", {"t": 262144, "w": 0.5}, "missing scale parameter 'm'"),
    ("logicle", {"t": "abc", "w": 0.5, "m": 4.5}, "not a number"),
    ("logicle", {"t": float("nan"), "w": 0.5, "m": 4.5}, "not finite"),
    ("asinh", {"t": 262144, "cofactor": 0}, "cofactor must be > 0"),
    ("hyperlog", {"t": 262144, "w": 0, "m": 4.5}, "W must be > 0"),
    ("log", {"t": 0, "m": 4}, "must be > 0"),
    ("biex", {}, "unknown scale kind"),
])
def test_invalid_parameters_are_rejected_before_the_c_routine(kind, params, fragment):
    with pytest.raises(ScaleError, match=fragment):
        make_scale(kind, params)


def test_non_finite_inputs_stay_nan_and_do_not_poison_neighbours():
    sc = make_scale("logicle", KINDS["logicle"])
    x = np.array([1.0, np.nan, 100.0, np.inf])
    y = scales.forward(sc, x)
    assert np.isnan(y[1]) and np.isnan(y[3])
    np.testing.assert_array_equal(y[[0, 2]], scales.forward(sc, np.array([1.0, 100.0])))


def test_hash_is_content_addressed():
    a = make_scale("logicle", KINDS["logicle"])
    b = make_scale("logicle", dict(KINDS["logicle"]))
    c = make_scale("logicle", {**KINDS["logicle"], "w": 0.6})
    assert a.hash == b.hash and a.hash != c.hash


def test_ticks_are_labelled_in_raw_units_at_transformed_positions():
    sc = make_scale("logicle", KINDS["logicle"])
    ticks = scales.ticks(sc, -0.02, 1.02)
    by_label = {t["label"]: t for t in ticks if t["major"]}
    assert {"0", "10³", "10⁴", "10⁵", "-10²"} <= set(by_label)
    assert by_label["10³"]["pos"] == pytest.approx(float(scales.forward(sc, np.array([1000.0]))[0]))
    pos = [t["pos"] for t in ticks]
    assert pos == sorted(pos) and all(-0.02 <= p <= 1.02 for p in pos)


def test_linear_ticks_are_round_numbers():
    ticks = scales.ticks(make_scale("linear"), 0.0, 262144.0)
    assert [t["label"] for t in ticks] == ["0", "50K", "100K", "150K", "200K", "250K"]


def test_default_scale_by_channel_kind():
    assert scales.default_scale("scatter", 262144.0).is_identity
    assert scales.default_scale("time", 262144.0).is_identity
    f = scales.default_scale("fluoro", 4194304.0)
    assert f.kind == "logicle" and f.p["t"] == 4194304.0
    assert scales.default_scale("fluoro", None, None).p["t"] == 262144.0
