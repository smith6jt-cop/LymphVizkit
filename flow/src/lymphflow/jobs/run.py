"""Job entry point: `python -m lymphflow.jobs.run <jobdir>`.

Identical for a local subprocess and for SLURM. Set LYMPH_FLOW_EVENTS_DIR / LYMPH_FLOW_MASKS_DIR
to relocate the data directories when a job directory has been copied to another filesystem.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

from .. import snapshot as snap
from ..core.cache import ArrayCache
from ..core.engine import Engine
from . import spec as spec_mod


class Cancelled(Exception):
    pass


class JobContext:
    def __init__(self, jobdir: Path, spec: dict):
        self.jobdir = Path(jobdir)
        self.spec = spec
        self.params = spec.get("params") or {}
        self.snapshot = spec["snapshot"]
        self.out_dir = self.jobdir / "out"
        self.events_dir = Path(os.environ.get("LYMPH_FLOW_EVENTS_DIR") or spec["events_dir"])
        masks = os.environ.get("LYMPH_FLOW_MASKS_DIR") or spec.get("masks_dir")
        self.cache = ArrayCache(int(os.environ.get("LYMPH_FLOW_JOB_CACHE_MB", "3072")) * 1024 * 1024)
        self.engine = Engine(self.cache, Path(masks) if masks else None)
        self.gates = self.snapshot["gates"]

    def samples(self):
        """Yield (sample dict, SampleData, {gate_id: GateNode}) one sample at a time."""
        for d in self.snapshot["samples"]:
            sdata = snap.sample_from_dict(d, self.events_dir, self.cache)
            yield d, sdata, snap.nodes_for_sample(self.gates, d["id"])

    def progress(self, fraction: float, message: str = "") -> None:
        self.check_cancel()
        spec_mod.write_status(self.jobdir, "running", fraction, message)

    def check_cancel(self) -> None:
        if (self.jobdir / "cancel").exists():
            raise Cancelled()


def _steps() -> dict:
    from .steps import apply_template, flowsom_step

    return {"apply_template": apply_template.run, "flowsom": flowsom_step.run}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m lymphflow.jobs.run <jobdir>", file=sys.stderr)
        return 2
    jobdir = Path(argv[0]).resolve()
    spec = spec_mod.read_json(jobdir / "spec.json")
    if spec is None:
        print(f"no readable spec.json in {jobdir}", file=sys.stderr)
        return 2
    try:
        ctx = JobContext(jobdir, spec)
        ctx.progress(0.0, "starting")
        step = _steps().get(spec["type"])
        if step is None:
            raise ValueError(f"unknown job type '{spec['type']}'")
        result = step(ctx)
        spec_mod.write_json(jobdir / "result.json", result)
        spec_mod.write_status(jobdir, "done", 1.0, "done")
        return 0
    except Cancelled:
        spec_mod.write_status(jobdir, "cancelled", 0.0, "cancelled")
        return 0
    except Exception as exc:  # the job's failure is recorded, never swallowed
        traceback.print_exc()
        spec_mod.write_status(jobdir, "failed", 0.0, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
