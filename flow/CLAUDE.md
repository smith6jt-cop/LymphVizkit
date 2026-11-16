# CLAUDE.md — flow/ (lymphflow)

Flow cytometry workbench prototype: a Python (FastAPI) service, independent of the Shiny app.
Read [`README.md`](README.md) for layout and commands and
[`../docs/omiq_evaluation.md`](../docs/omiq_evaluation.md) for scope, what is verified, and what
is not done. This file holds the rules and the traps.

## Rules that must not be weakened

Each one prevents a silent scientific error and has a test that fails if it is broken.

1. **A gate binds to an immutable scale row, never to "the channel's current scale".** Gate
   coordinates are in scale units. `gates.dims` stores `scale_id`; `channel_scales` is only the
   display default. Changing a display scale must leave every count unchanged
   (`test_changing_the_display_scale_does_not_move_a_gate`). Re-binding is the explicit
   `PUT gates/{id}` with `dims` + `geom`, audited as `gate.rebind`.
2. **Raw events are never rewritten.** Compensation is applied on read (`SampleData.linear`).
   A sample's `comp_state` is `embedded | identity | none | invalid`; `invalid` blocks the sample
   (`CompensationInvalid`) until the user switches compensation off. Never turn that into a skip.
3. **Statistics use compensated linear float64 values**, never scale-transformed ones
   (`core/stats.py`). Geometric mean reports `n_nonpositive`.
4. **A missing channel is a visible per-sample failure** (`PanelMismatch`), surfaced in the
   population tree, the job result, the summary and the CSV `error` column. This is the same
   lesson as the Shiny app's silently-ignored filters: a guard on a missing column must surface.
5. **Validate scale parameters before calling FlowUtils** (`make_scale` + `_probe`). The C routine
   does not validate and returns `-1` on non-convergence.
6. **`dev` auth mode is loopback only; `proxy` mode needs an explicit header name.** Do not add a
   fallback that serves unauthenticated requests on another interface.

## Things that are easy to get wrong

- **Cache keys are content hashes.** `Engine.gate_key` hashes engine version, FCS sha256, the
  compensation matrix hash (only when the gate's channel is a matrix detector), gate kind,
  geometry, each dimension's scale hash, and the parent key. If gate, scale or compensation
  semantics change, bump `ENGINE_VERSION` in `config.py`; do not try to delete cache files.
- **Boundary rules follow GatingML / FlowKit**: rectangle and range are min-inclusive,
  max-exclusive; polygon and ellipse boundaries are inclusive; NaN is never inside a gate. The
  parity tests compare membership event for event, so an "equivalent" rewrite is not equivalent.
- **Off-scale events.** Events outside the display window are clamped into the edge bins for
  drawing. A gate edge within `EDGE_PX` of the plot border is sent as unbounded (rectangle,
  range) or pushed one window span out (polygon vertex) in `web/js/plot.js`. The frame header's
  `off_scale` counts must keep being shown.
- **Arm drawing tools in a layout effect** (`Plot` in `web/js/app.js`). With a normal effect a
  pointer-down can arrive before the tool is armed and is dropped; the browser test hits this.
- **`setFrame` keeps an in-progress draft only when the axes are unchanged** (same channels,
  scale ids and windows); otherwise the draft's pixel coordinates are meaningless.
- **Everything in `web/` uses relative URLs** (`new URL('api/', document.baseURI)`), and the
  Content-Security-Policy allows scripts from the same origin only. No CDN, no inline script.
  New browser libraries go through `scripts/vendor_js.sh` and `web/vendor/VENDOR.json`.
- **Jobs never open SQLite.** The service writes a snapshot into `spec.json`; the job rebuilds
  engine objects through `snapshot.py`; the dispatcher integrates results. Keep both sides going
  through `snapshot.py` so a job evaluates exactly what the service would.
- **`flowsom` is imported only in `jobs/steps/flowsom_step.py`** (GPL, optional). Its estimator
  takes flat keyword arguments: `FlowSOMEstimator(xdim=, ydim=, seed=, n_clusters=)`. Cluster
  frequencies come from mapping all gated events, not from the training subsample.
- **FlowKit pins `pandas<3`** and is only imported in `interop/` and tests. In a GatingML matrix
  gate the dimension id is the *fluorochrome* name; `from_flowkit_strategy` maps it to the detector.
- **Parquet encoding**: `use_dictionary=False, use_byte_stream_split=True`. The pyarrow defaults
  made event files 41% larger than the FCS.
- **`.gitignore` does not support trailing comments** on a pattern line.

## Verification status (Nov 2026)

129 pytest tests and the browser test pass. 22/22 ISAC GatingML gates match the published
per-event truth tables; 42/42 FlowJo-workspace gates match FlowKit event for event. FlowJo's own
stored counts are **not** reproduced exactly (within 0.41% for populations ≥ 1,000 events, within
10 events below that) — the same differences FlowKit has. Do not describe workspace import as
FlowJo-equivalent.

Never run on real lab data, on Cytek/Sony unmixed files, on a PubApps VM, behind the RC proxy, or
under SLURM. `deploy/lymphvizkit-flow.service` is an untested template.

## Adding things

- **An algorithm**: a function `run(ctx) -> dict` in `jobs/steps/`, registered in `jobs/run.py`
  and `api/jobs.py::JOB_TYPES`, with parameter validation in the API. Report skipped samples.
- **A gate kind**: `core/gates.py` (`KINDS`, `validate_geom`, `membership`, `bbox`, `outline`),
  `interop/gatingml.py` both directions, a drawing tool in `web/js/plot.js`, and a parity case.
- **A schema change**: a new `db/migrations/NNN_name.sql`; never edit an applied migration.
