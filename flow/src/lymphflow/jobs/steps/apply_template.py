"""Apply the gating template to every sample: masks + statistics for every population.

A sample whose panel lacks a gate's channel, or whose compensation is unresolved, is
reported as a failure for that sample and population. It is never skipped silently.
"""
from __future__ import annotations

from ... import snapshot as snap
from ...core import stats as stats_mod
from ...core.engine import CompensationInvalid, PanelMismatch
from .. import spec as spec_mod


def run(ctx) -> dict:
    gates = snap.depth_first(ctx.gates)
    n_samples = len(ctx.snapshot["samples"])
    rows, failures = [], []
    for i, (d, sdata, nodes) in enumerate(ctx.samples()):
        ctx.progress(i / max(n_samples, 1), f"{d['name']} ({i + 1}/{n_samples})")
        try:
            sdata.require_usable_comp()
        except CompensationInvalid as exc:
            failures.append({"sample_id": d["id"], "sample": d["name"], "gate_id": None,
                             "population": "All events", "error": str(exc)})
            continue
        failed: set[int] = set()
        for gid, node in [(0, None)] + [(g["id"], nodes[g["id"]]) for g in gates]:
            ctx.check_cancel()
            pop = " / ".join(node.path) if node else "All events"
            if node is not None and node.parent is not None and node.parent.gate_id in failed:
                failed.add(gid)
                failures.append({"sample_id": d["id"], "sample": d["name"], "gate_id": gid, "population": pop,
                                 "error": "parent population could not be evaluated"})
                continue
            try:
                st = stats_mod.population_stats(ctx.engine, sdata, node)
            except PanelMismatch as exc:
                failed.add(gid)
                failures.append({"sample_id": d["id"], "sample": d["name"], "gate_id": gid, "population": pop,
                                 "error": str(exc)})
                continue
            rows.append({"sample_id": d["id"], "gate_id": gid,
                         "key": stats_mod.stats_key(ctx.engine, sdata, node), "stats": st})
        sdata.release()  # keep the job's memory bounded to one sample
    spec_mod.write_json(ctx.out_dir / "stats.json", rows)
    return {
        "type": "apply_template",
        "template_revision": ctx.snapshot["experiment"]["revision"],
        "n_samples": n_samples,
        "n_populations": len(gates) + 1,
        "n_evaluated": len(rows),
        "n_failed": len(failures),
        "failures": failures,
    }
