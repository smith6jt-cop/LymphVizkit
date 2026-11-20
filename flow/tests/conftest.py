"""Shared pytest fixtures. All data here is synthetic (see lymphflow.synthetic)."""
from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lymphflow import synthetic  # noqa: E402
from lymphflow.core import fcs_ingest  # noqa: E402
from lymphflow.core.cache import ArrayCache  # noqa: E402
from lymphflow.core.engine import ChannelInfo, Engine, SampleData  # noqa: E402


@pytest.fixture(scope="session")
def synth(tmp_path_factory):
    """Four synthetic FCS files in two groups + the truth manifest."""
    out = tmp_path_factory.mktemp("synthetic")
    manifest = synthetic.make_dataset(out, n_files=4, events_per_file=20000, seed=11)
    return out, manifest


def load_sample(fcs_path: Path, workdir: Path, cache: ArrayCache | None = None, **overrides) -> tuple[SampleData, fcs_ingest.IngestResult]:
    """Ingest one FCS file and wrap it as engine SampleData (no database involved)."""
    parquet = Path(workdir) / (Path(fcs_path).stem + ".parquet")
    r = fcs_ingest.ingest_fcs(fcs_path, parquet)
    sdata = SampleData(
        key=r.sha256, name=Path(fcs_path).stem, n_events=r.n_events,
        channels=tuple(ChannelInfo(c.idx, c.col, c.pnn, c.pns, c.kind, c.pnr, c.vmin, c.vmax) for c in r.channels),
        loader=fcs_ingest.parquet_loader(parquet), cache=cache or ArrayCache(),
        comp=r.comp, comp_state=r.comp_state, **overrides)
    return sdata, r


@pytest.fixture()
def engine(tmp_path):
    cache = ArrayCache()
    return Engine(cache, tmp_path / "masks")


def make_client(tmp_path, **env):
    """A TestClient on a fresh data directory. Returned as a context manager."""
    from fastapi.testclient import TestClient

    from lymphflow.app import create_app
    from lymphflow.config import load_settings

    base = {"LYMPH_FLOW_DATA": str(tmp_path / "data")}
    base.update({k: str(v) for k, v in env.items()})
    return TestClient(create_app(load_settings(base)))


@pytest.fixture()
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def upload(client, exp_id: int, paths) -> list[dict]:
    files = [("files", (Path(p).name, open(p, "rb"), "application/octet-stream")) for p in paths]
    try:
        res = client.post(f"/api/experiments/{exp_id}/samples", files=files)
    finally:
        for _, (_, fh, _) in files:
            fh.close()
    assert res.status_code == 200, res.text
    return res.json()["results"]


def wait_job(client, job: dict, timeout: float = 180.0) -> dict:
    import time

    deadline = time.time() + timeout
    while job["state"] in ("queued", "running"):
        assert time.time() < deadline, f"job {job['id']} did not finish: {job}"
        time.sleep(0.2)
        job = client.get(f"/api/jobs/{job['id']}").json()
    return job


REQUIRE_FIXTURES = os.environ.get("LYMPH_FLOW_REQUIRE_FIXTURES", "").lower() in {"1", "true", "yes"}


@pytest.fixture(scope="session")
def flowkit_fixtures():
    """Third-party reference files, fetched with pinned digests. Skips offline unless required."""
    import fetch_fixtures

    try:
        return fetch_fixtures.fetch(quiet=True)
    except SystemExit:
        raise
    except Exception as exc:  # network unavailable
        if REQUIRE_FIXTURES:
            raise
        pytest.skip(f"reference fixtures unavailable ({exc}); set LYMPH_FLOW_REQUIRE_FIXTURES=1 to fail instead")
