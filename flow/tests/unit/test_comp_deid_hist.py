import numpy as np
import pytest

from lymphflow import synthetic
from lymphflow.core import comp, deid, hist


# ---- compensation ---------------------------------------------------------------------------
def test_parse_and_apply_recovers_true_values():
    cm = comp.parse_spill(synthetic.spill_text())
    assert cm.detectors == tuple(synthetic.FLUORO)
    true = np.random.default_rng(0).uniform(-200, 30000, (5000, 4))
    np.testing.assert_allclose(comp.apply(true @ synthetic.SPILL, cm), true, rtol=1e-10, atol=1e-8)


def test_classification_states():
    pnn = synthetic.CHANNELS
    assert comp.classify(comp.parse_spill(synthetic.spill_text()), pnn)[0] == "embedded"
    assert comp.classify(comp.parse_spill(synthetic.spill_text(matrix=np.eye(4))), pnn)[0] == "identity"
    state, msg = comp.classify(comp.parse_spill(synthetic.spill_text(detectors=["B530-A", "YG585-A", "R670-A", "NOPE-A"])), pnn)
    assert state == "invalid" and "NOPE-A" in msg
    singular = np.ones((4, 4))
    assert comp.classify(comp.parse_spill(synthetic.spill_text(matrix=singular)), pnn)[0] == "invalid"
    dup = comp.CompMatrix(("B530-A", "B530-A"), np.eye(2))
    assert comp.classify(dup, pnn)[0] == "invalid"


@pytest.mark.parametrize("text", ["", "abc", "3,A,B,C,1,0,0", "2,A,B,1,x,0,1"])
def test_malformed_spill_text_raises(text):
    with pytest.raises(comp.CompError):
        comp.parse_spill(text)


def test_find_spill_prefers_standard_keyword_and_accepts_vendor_form():
    assert comp.find_spill_text({"spill": "a", "spillover": "b"}) == ("spillover", "b")
    assert comp.find_spill_text({"SPILL": "a"}) == ("spill", "a")
    assert comp.find_spill_text({"$SPILLOVER": "c"}) == ("spillover", "c")
    assert comp.find_spill_text({"other": "x"}) == (None, None)


def test_matrix_hash_is_content_addressed():
    a = comp.parse_spill(synthetic.spill_text())
    b = comp.CompMatrix.from_json(a.to_json())
    assert a.hash == b.hash
    assert comp.parse_spill(synthetic.spill_text(matrix=np.eye(4))).hash != a.hash


# ---- de-identification ----------------------------------------------------------------------
IDENTIFYING = {"op": "Jane Operator", "inst": "Example University", "src": "Patient 0042", "smno": "MRN-0042",
               "fil": "Patient_0042.fcs", "cytsn": "SN1", "com": "free text", "exp": "Dr X", "proj": "secret",
               "case number": "0042", "donor age": "57", "cd first viral load date": "2020-01-01",
               "plateid": "P1", "last_modifier": "someone", "sample id": "S1"}


def test_allow_list_drops_everything_not_named():
    text = {**IDENTIFYING, "tot": "100", "par": "2", "p1n": "FSC-A", "p1s": "", "p2n": "B530-A", "p2s": "CD3e",
            "p2r": "262144", "p2e": "0,0", "p2g": "1", "p2b": "32", "p2v": "450", "cyt": "Aurora",
            "timestep": "0.01", "spillover": "1,B530-A,1", "date": "07-JUL-2026", "btim": "10:00:00"}
    kept, dropped = deid.filter_keywords(text)
    assert set(kept) == {"tot", "par", "p1n", "p1s", "p2n", "p2s", "p2r", "p2e", "p2g", "p2b", "p2v", "cyt", "timestep"}
    assert dropped == len(IDENTIFYING) + 2          # + date, btim
    assert not set(kept) & set(IDENTIFYING)


def test_acquisition_times_are_opt_in():
    kept, _ = deid.filter_keywords({"date": "07-JUL-2026", "btim": "1", "etim": "2", "op": "x"}, keep_times=True)
    assert set(kept) == {"date", "btim", "etim"}


def test_dollar_prefixed_and_mixed_case_keys_are_normalised():
    kept, dropped = deid.filter_keywords({"$TOT": "5", "$OP": "x", "$P1N": "FSC-A"})
    assert kept == {"tot": "5", "p1n": "FSC-A"} and dropped == 1


# ---- binning ----------------------------------------------------------------------------------
def test_hist2d_total_equals_population_and_edges_pile_up():
    x = np.array([-5.0, 0.0, 0.5, 0.999, 1.0, 7.0])
    y = np.array([0.5, 0.5, 0.5, 0.5, 0.5, 0.5])
    counts, n_shown, n_nonfinite = hist.hist2d(x, y, None, (0.0, 1.0), (0.0, 1.0), nb=4)
    assert counts.sum() == 6 and n_shown == 6 and n_nonfinite == 0
    assert counts[2].tolist() == [2, 0, 1, 3]      # -5 and 0 in the first bin; 0.999, 1.0, 7.0 in the last
    assert hist.off_scale(x, None, (0.0, 1.0)) == {"below": 1, "above": 1}


def test_hist_excludes_non_finite_and_respects_mask():
    x = np.array([0.1, np.nan, 0.3, 0.4])
    y = np.array([0.1, 0.2, np.inf, 0.4])
    mask = np.array([True, True, True, False])
    counts, n_shown, n_nonfinite = hist.hist2d(x, y, mask, (0, 1), (0, 1), nb=8)
    assert counts.sum() == 1 and n_shown == 1 and n_nonfinite == 2
    c1, shown1, bad1 = hist.hist1d(x, mask, (0, 1), nb=8)
    assert c1.sum() == 2 and shown1 == 2 and bad1 == 1


def test_frame_round_trip_is_4_byte_aligned():
    counts = np.arange(16, dtype=np.uint32).reshape(4, 4)
    for label in ("a", "ab", "abc", "abcd"):
        buf = hist.pack_frame({"k": label}, counts)
        header, out = hist.unpack_frame(buf)
        assert header == {"k": label} and out.tolist() == list(range(16))
        assert (len(buf) - counts.nbytes) % 4 == 0
