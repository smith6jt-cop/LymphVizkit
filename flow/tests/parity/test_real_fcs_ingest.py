"""Ingest of real FCS files in the variants a core facility actually produces.

FCS 2.0 integer data with log amplifiers, FCS 2.0/3.0 float with a vendor (non-`$`) SPILL
keyword, a file whose offsets need tolerant parsing, and real identifying keywords. Values
must equal FlowKit's preprocessed events exactly.
"""
import warnings

import numpy as np
import pytest

fk = pytest.importorskip("flowkit", reason="optional 'interop' extra not installed")

from conftest import load_sample  # noqa: E402

pytestmark = pytest.mark.network
warnings.filterwarnings("ignore")


def stored_events(sdata):
    return np.column_stack([sdata.raw(c.pnn).astype(np.float64) for c in sdata.channels])


def test_fcs2_integer_log_amplified_data_equals_flowkit(flowkit_fixtures, tmp_path):
    path = flowkit_fixtures / "gate_ref" / "data1.fcs"
    sdata, r = load_sample(path, tmp_path)
    ref = fk.Sample(str(path))
    assert r.fcs_version.startswith("2") and r.n_events == ref.event_count == 13367
    assert [c.pnn for c in r.channels] == ref.pnn_labels
    assert np.array_equal(stored_events(sdata), ref.get_events(source="raw"))
    assert r.comp_state == "none"
    kinds = {c.pnn: c.kind for c in r.channels}
    assert kinds["FSC-H"] == "scatter" and kinds["Time"] == "time" and kinds["FL1-H"] == "fluoro"
    # this file carries SAMPLE ID / CASE NUMBER style keywords; none may be stored
    assert not {"sample id", "case number", "op", "src"} & set(r.keywords) and r.n_keywords_dropped > 0


def test_vendor_spill_keyword_and_compensation_equal_flowkit(flowkit_fixtures, tmp_path):
    path = flowkit_fixtures / "100715.fcs"
    sdata, r = load_sample(path, tmp_path)
    assert r.comp_state == "embedded" and r.comp_message.startswith("$SPILL:") and len(r.comp.detectors) == 13
    ref = fk.Sample(str(path))
    ref.apply_compensation(ref.metadata["spill"])
    comp_ref = ref.get_events(source="comp")
    for det in r.comp.detectors:
        assert np.array_equal(sdata.linear(det), comp_ref[:, ref.pnn_labels.index(det)]), det
    for banned in ("op", "inst", "src", "fil", "cd age", "cd first viral load date"):
        assert banned not in r.keywords
    assert r.n_keywords_dropped >= 20


def test_file_needing_offset_tolerance_is_ingested_with_a_warning(flowkit_fixtures, tmp_path):
    path = flowkit_fixtures / "test_comp_example.fcs"
    with pytest.raises(Exception):
        fk.Sample(str(path))                                    # strict parsing fails on this file
    sdata, r = load_sample(path, tmp_path)
    assert r.n_events > 0 and any("offset tolerance" in w for w in r.warnings)
    ref = fk.Sample(str(path), ignore_offset_error=True)
    assert np.array_equal(stored_events(sdata), ref.get_events(source="raw"))
    # Its 14 x 14 SPILL matrix is the identity: reported as such, and not "applied".
    assert r.comp_state == "identity" and len(r.comp.detectors) == 14 and not sdata.comp_active


def test_eight_colour_file_round_trips_exactly(flowkit_fixtures, tmp_path):
    path = sorted((flowkit_fixtures / "8_color_data_set" / "fcs_files").glob("*.fcs"))[0]
    sdata, r = load_sample(path, tmp_path)
    ref = fk.Sample(str(path))
    assert r.n_events == 290172 and r.comp_state == "embedded" and len(r.comp.detectors) == 8
    assert np.array_equal(stored_events(sdata), ref.get_events(source="raw"))
    ref.apply_compensation(ref.metadata["spillover"])
    comp_ref = ref.get_events(source="comp")
    for det in r.comp.detectors:
        assert np.array_equal(sdata.linear(det), comp_ref[:, ref.pnn_labels.index(det)]), det
