import { html, render, useState, useEffect, useLayoutEffect, useRef } from '../vendor/htm-preact-standalone.module.js';
import * as api from './api.js';
import { PlotView } from './plot.js';

const GROUP_COLORS = ['#4477AA', '#CC6633', '#228833', '#AA3377', '#66CCEE', '#BBBBBB'];
const fmt = (n) => (n == null ? '–' : Number(n).toLocaleString());
const sig = (v, d = 4) => (v == null ? '–' : Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : Number(v).toPrecision(d).replace(/\.?0+$/, ''));
const pct = (v) => (v == null ? '–' : v.toFixed(v < 10 ? 2 : 1) + '%');

// Events outside the display window are drawn on the plot border. Say so, and say what a gate does.
function offScale(h) {
  const parts = [];
  for (const ax of ['x', 'y']) {
    const o = h.off_scale && h.off_scale[ax];
    if (!o) continue;
    if (o.below) parts.push(`${fmt(o.below)} below the ${ax.toUpperCase()} range`);
    if (o.above) parts.push(`${fmt(o.above)} above the ${ax.toUpperCase()} range`);
  }
  return parts.length ? `Off-scale events drawn on the plot edge: ${parts.join(', ')}. `
    + 'A gate dragged to the edge includes them; a gate drawn inside the plot does not.' : '';
}

let plotlyPromise = null;
function ensurePlotly() {
  if (!plotlyPromise) {
    plotlyPromise = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = new URL('vendor/plotly-cartesian.min.js', document.baseURI);
      s.onload = () => resolve(window.Plotly);
      s.onerror = () => reject(new Error('could not load the plotting library'));
      document.head.appendChild(s);
    });
  }
  return plotlyPromise;
}

async function pollJob(job, onUpdate) {
  let j = job;
  while (j.state === 'queued' || j.state === 'running') {
    onUpdate(j);
    await new Promise((r) => setTimeout(r, 1000));
    j = await api.get(`jobs/${j.id}`);
  }
  onUpdate(j);
  return j;
}

// ---------------------------------------------------------------------------------------
function Plot({ frame, tool, onGate, onSelectGate, onAxisClick }) {
  const host = useRef(null);
  const view = useRef(null);
  const cb = useRef({});
  cb.current = { onGate, onSelectGate, onAxisClick };
  useLayoutEffect(() => {   // declared first: runs before the two layout effects below
    view.current = new PlotView(host.current, {
      onGate: (g) => cb.current.onGate(g),
      onSelectGate: (id) => cb.current.onSelectGate(id),
      onAxisClick: (a) => cb.current.onAxisClick(a),
    });
    return () => view.current.destroy();
  }, []);
  // Layout effects: the tool must be armed in the same tick as the click that selected it,
  // otherwise a pointer-down arriving before the next paint would be ignored.
  useLayoutEffect(() => { if (frame) view.current.setFrame(frame.header, frame.counts); }, [frame]);
  useLayoutEffect(() => { view.current.setTool(tool); }, [tool]);
  return html`<div ref=${host} class="plot-host"></div>`;
}

function ScaleEditor({ channel, onApply, onClose }) {
  const sc = channel.scale;
  const cof = sc.kind === 'asinh' ? sc.params.t / Math.sinh(sc.params.m * Math.LN10) : 150;
  const [kind, setKind] = useState(sc.kind === 'asinh' ? 'asinh' : sc.kind === 'logicle' ? 'logicle' : 'linear');
  const [t, setT] = useState(sc.params.t ?? 262144);
  const [w, setW] = useState(sc.params.w ?? 0.5);
  const [m, setM] = useState(sc.kind === 'logicle' ? sc.params.m : 4.5);
  const [a, setA] = useState(sc.kind === 'logicle' ? sc.params.a : 0);
  const [c, setC] = useState(Number(cof.toPrecision(6)));
  const num = (set) => (e) => set(e.target.value);
  const apply = () => {
    const params = kind === 'linear' ? {} : kind === 'asinh' ? { t: +t, cofactor: +c } : { t: +t, w: +w, m: +m, a: +a };
    onApply(channel.pnn, kind, params);
  };
  return html`<div class="scale-editor" data-testid="scale-editor">
    <b>Display scale: ${channel.pns ? channel.pns + ' ' : ''}(${channel.pnn})</b>
    <label>kind <select value=${kind} onChange=${(e) => setKind(e.target.value)}>
      <option value="linear">linear</option><option value="logicle">logicle</option><option value="asinh">arcsinh</option>
    </select></label>
    ${kind !== 'linear' && html`<label>T <input type="number" value=${t} onInput=${num(setT)} /></label>`}
    ${kind === 'logicle' && html`
      <label>W <input type="number" step="0.1" value=${w} onInput=${num(setW)} /></label>
      <label>M <input type="number" step="0.1" value=${m} onInput=${num(setM)} /></label>
      <label>A <input type="number" step="0.1" value=${a} onInput=${num(setA)} /></label>`}
    ${kind === 'asinh' && html`<label>cofactor <input type="number" value=${c} onInput=${num(setC)} /></label>`}
    <button onClick=${apply}>Apply</button><button class="quiet" onClick=${onClose}>Close</button>
    <div class="hint">Changes how this channel is displayed. Existing gates keep the scale they were drawn on
      and are shown dashed (⚠) on a different display scale.</div>
  </div>`;
}

function CompBadge({ s, onToggle }) {
  const applied = s.comp_state === 'embedded' && s.comp_enabled;
  const cls = s.comp_state === 'invalid' && s.comp_enabled ? 'bad' : applied ? 'ok' : 'neutral';
  const text = s.comp_state === 'embedded' ? (applied ? 'comp: applied' : 'comp: off')
    : s.comp_state === 'invalid' ? (s.comp_enabled ? 'comp: INVALID' : 'comp: off (invalid matrix)')
    : s.comp_state === 'identity' ? 'comp: identity' : 'comp: none';
  const canToggle = s.comp_state === 'embedded' || s.comp_state === 'invalid';
  return html`<span class=${'badge ' + cls} title=${s.comp_message || ''}
    onClick=${canToggle ? (e) => { e.stopPropagation(); onToggle(s); } : null}
    style=${canToggle ? 'cursor:pointer' : ''}>${text}</span>`;
}

// ---------------------------------------------------------------------------------------
function SummaryTab({ exp, gateId, popName, rev }) {
  const [metric, setMetric] = useState('pct_parent');
  const [channel, setChannel] = useState('');
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const host = useRef(null);
  const fluoro = exp.channels.filter((c) => c.kind === 'fluoro');
  useEffect(() => {
    if (metric === 'median' && !channel) { if (fluoro[0]) setChannel(fluoro[0].pnn); return; }
    let live = true;
    api.get(`experiments/${exp.id}/summary`, { gate: gateId, metric, channel: metric === 'median' ? channel : '' })
      .then((d) => { if (live) { setData(d); setErr(''); } }).catch((e) => live && setErr(e.message));
    return () => { live = false; };
  }, [exp.id, gateId, metric, channel, rev]);
  useEffect(() => {
    if (!data || !host.current) return;
    const groups = [...new Set(data.rows.map((r) => r.group || '(no group)'))];
    const label = { pct_parent: '% of parent', pct_total: '% of all events', count: 'events', median: `median ${channel} (linear)` }[metric];
    ensurePlotly().then((Plotly) => {
      const traces = groups.map((g, i) => {
        const rows = data.rows.filter((r) => (r.group || '(no group)') === g && r.value != null);
        return { type: 'box', name: g, y: rows.map((r) => r.value), text: rows.map((r) => r.sample),
                 boxpoints: 'all', jitter: 0.4, pointpos: 0, marker: { color: GROUP_COLORS[i % GROUP_COLORS.length], size: 9 },
                 line: { width: 1 }, fillcolor: 'rgba(0,0,0,0)', hovertemplate: '%{text}<br>%{y:.4g}<extra></extra>' };
      });
      Plotly.react(host.current, traces, {
        margin: { l: 70, r: 20, t: 36, b: 50 }, height: 330, showlegend: false,
        title: { text: `${popName}: ${label}`, font: { size: 15 } }, yaxis: { title: { text: label }, rangemode: 'tozero' },
        font: { size: 13 },
      }, { displaylogo: false, responsive: true, toImageButtonOptions: { format: 'png', scale: 2 } });
    }).catch((e) => setErr(e.message));
  }, [data]);
  return html`<div>
    <div class="row">
      <label>Metric <select value=${metric} onChange=${(e) => setMetric(e.target.value)} data-testid="summary-metric">
        <option value="pct_parent">% of parent</option><option value="pct_total">% of all events</option>
        <option value="count">event count</option><option value="median">median (linear)</option></select></label>
      ${metric === 'median' && html`<label>Channel <select value=${channel} onChange=${(e) => setChannel(e.target.value)}>
        ${fluoro.map((c) => html`<option value=${c.pnn}>${c.pns || c.pnn}</option>`)}</select></label>`}
      <span class="hint">Population: <b>${popName}</b>. One point per sample, grouped by the sample's group label.</span>
    </div>
    ${err && html`<div class="error">${err}</div>`}
    <div class="split">
      <div ref=${host} class="plotly-host" data-testid="summary-plot"></div>
      <table class="grid" data-testid="summary-table"><thead><tr><th>Sample</th><th>Group</th><th>Value</th></tr></thead><tbody>
        ${(data ? data.rows : []).map((r) => html`<tr class=${r.error ? 'err' : ''} title=${r.error || ''}>
          <td>${r.sample}${r.overridden ? ' *' : ''}</td><td>${r.group}</td><td>${r.error ? 'error' : sig(r.value)}</td></tr>`)}
      </tbody></table>
    </div>
  </div>`;
}

function ClusterTab({ exp, gateId, popName, onError }) {
  const fluoro = exp.channels.filter((c) => c.kind === 'fluoro');
  const [chosen, setChosen] = useState(() => new Set(fluoro.map((c) => c.pnn)));
  const [nPer, setNPer] = useState(20000);
  const [grid, setGrid] = useState(10);
  const [k, setK] = useState(10);
  const [seed, setSeed] = useState(42);
  const [job, setJob] = useState(null);
  const host = useRef(null);
  const toggle = (pnn) => { const s = new Set(chosen); s.has(pnn) ? s.delete(pnn) : s.add(pnn); setChosen(s); };
  const run = async () => {
    try {
      const j = await api.send('POST', `experiments/${exp.id}/jobs`, {
        type: 'flowsom', gate_id: gateId, channels: fluoro.filter((c) => chosen.has(c.pnn)).map((c) => c.pnn),
        n_per_sample: +nPer, xdim: +grid, ydim: +grid, n_clusters: +k, seed: +seed });
      await pollJob(j, setJob);
    } catch (e) { onError(e.message); }
  };
  const res = job && job.state === 'done' ? job.result : null;
  useEffect(() => {
    if (!res || !host.current) return;
    ensurePlotly().then((Plotly) => {
      const cols = res.channels.map((c) => c.pns || c.pnn);
      Plotly.react(host.current, [{
        type: 'heatmap', z: res.medians, x: cols, y: res.clusters, colorscale: 'Viridis',
        colorbar: { title: { text: 'median<br>(scale units)' } }, hovertemplate: '%{y} · %{x}<br>%{z:.3f}<extra></extra>',
      }], { margin: { l: 70, r: 20, t: 36, b: 60 }, height: 60 + 26 * res.clusters.length + 80,
            title: { text: `FlowSOM metaclusters of ${popName}`, font: { size: 15 } }, font: { size: 13 },
            yaxis: { autorange: 'reversed' } },
      { displaylogo: false, responsive: true, toImageButtonOptions: { format: 'png', scale: 2 } });
    }).catch((e) => onError(e.message));
  }, [res]);
  const running = job && (job.state === 'queued' || job.state === 'running');
  return html`<div>
    <div class="row wrap">
      <span>Cluster <b>${popName}</b> on:</span>
      ${fluoro.map((c) => html`<label class="check"><input type="checkbox" checked=${chosen.has(c.pnn)}
        onChange=${() => toggle(c.pnn)} />${c.pns || c.pnn}</label>`)}
    </div>
    <div class="row wrap">
      <label>events per sample <input type="number" value=${nPer} onInput=${(e) => setNPer(e.target.value)} /></label>
      <label>SOM grid <input type="number" class="narrow" value=${grid} onInput=${(e) => setGrid(e.target.value)} /></label>
      <label>metaclusters <input type="number" class="narrow" value=${k} onInput=${(e) => setK(e.target.value)} /></label>
      <label>seed <input type="number" class="narrow" value=${seed} onInput=${(e) => setSeed(e.target.value)} /></label>
      <button onClick=${run} disabled=${running || chosen.size < 2} data-testid="run-flowsom">Run FlowSOM</button>
      ${job && html`<span class="hint" data-testid="flowsom-state">${job.state}${running ? ` · ${Math.round(100 * job.progress)}% ${job.message}` : ''}</span>`}
    </div>
    ${job && job.state === 'failed' && html`<pre class="error">${job.message}\n${job.log_tail || ''}</pre>`}
    ${res && html`<div>
      <div class="hint">Trained on ${fmt(res.n_train)} events (an equal, seeded subsample per sample; seed ${res.params.seed}).
        Frequencies below are over <b>all</b> events of the population mapped to the SOM, not the subsample.
        Heatmap values are medians in each channel's display-scale units, not linear MFIs.
        ${res.n_empty_metaclusters ? ` ${res.n_empty_metaclusters} of the ${res.params.n_clusters} requested metaclusters received no events and are not shown.` : ''}</div>
      ${res.skipped.length > 0 && html`<div class="error">Samples not included: ${res.skipped.map((s) => `${s.sample} (${s.error})`).join('; ')}</div>`}
      <div class="split">
        <div ref=${host} class="plotly-host" data-testid="flowsom-heatmap"></div>
        <div class="scroll"><table class="grid" data-testid="flowsom-freq"><thead><tr><th>Sample</th><th>Group</th>
          ${res.clusters.map((c) => html`<th>${c}</th>`)}</tr></thead><tbody>
          ${res.samples.map((s) => html`<tr><td>${s.name}</td><td>${s.group}</td>
            ${(res.frequencies[s.id].pct || []).map((v) => html`<td>${v.toFixed(1)}</td>`)}</tr>`)}
        </tbody></table></div>
      </div></div>`}
  </div>`;
}

function CompTab({ sampleId, rev }) {
  const [c, setC] = useState(null);
  useEffect(() => { let live = true; api.get(`samples/${sampleId}/compensation`).then((d) => live && setC(d)); return () => { live = false; }; }, [sampleId, rev]);
  if (!c) return null;
  return html`<div>
    <div class="row"><b>State:</b> ${c.state} · <b>${c.applied ? 'applied to plots, gates and statistics' : 'not applied'}</b>
      <span class="hint">${c.message}</span></div>
    ${c.matrix.length > 0 && html`<div class="scroll"><table class="grid" data-testid="comp-matrix">
      <thead><tr><th>fluorochrome \\ detector</th>${c.detectors.map((d) => html`<th>${d}</th>`)}</tr></thead>
      <tbody>${c.matrix.map((row, i) => html`<tr><th>${c.detectors[i]}</th>
        ${row.map((v) => html`<td class=${v !== 0 && Math.abs(v) < 1 ? 'spill' : ''}>${Number(v).toFixed(4)}</td>`)}</tr>`)}</tbody>
    </table></div>`}
    <div class="hint">Raw events are never modified. Compensation is applied on read; switch it per sample by clicking the badge in the sample list.</div>
  </div>`;
}

function KeywordsTab({ sampleId }) {
  const [s, setS] = useState(null);
  useEffect(() => { let live = true; api.get(`samples/${sampleId}`).then((d) => live && setS(d)); return () => { live = false; }; }, [sampleId]);
  if (!s) return null;
  return html`<div>
    <div class="hint">Only allow-listed FCS keywords are stored. Operator, institution, sample/patient identifiers,
      serial numbers, dates and all vendor free text are discarded at upload; the FCS file itself is not kept.</div>
    <div class="scroll"><table class="grid" data-testid="keywords"><thead><tr><th>Keyword</th><th>Value</th></tr></thead>
      <tbody>${Object.entries(s.keywords).map(([k, v]) => html`<tr><td>${k}</td><td>${v}</td></tr>`)}</tbody></table></div>
  </div>`;
}

// ---------------------------------------------------------------------------------------
function App() {
  const [me, setMe] = useState(null);
  const [experiments, setExperiments] = useState([]);
  const [exp, setExp] = useState(null);
  const [sampleId, setSampleId] = useState(0);
  const [pops, setPops] = useState(null);
  const [parentId, setParentId] = useState(0);
  const [x, setX] = useState('');
  const [y, setY] = useState('');
  const [tool, setTool] = useState('none');
  const [gateName, setGateName] = useState('');
  const [frame, setFrame] = useState(null);
  const [plotErr, setPlotErr] = useState('');
  const [stats, setStats] = useState(null);
  const [rev, setRev] = useState(0);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [tab, setTab] = useState('summary');
  const [editAxis, setEditAxis] = useState('');
  const [applyJob, setApplyJob] = useState(null);
  const [newName, setNewName] = useState('');
  const bump = () => setRev((r) => r + 1);
  const fail = (e) => setError(e.message || String(e));

  const loadExperiment = async (id, keep) => {
    const d = await api.get(`experiments/${id}`);
    setExp(d);
    if (!keep) {
      setParentId(0); setPops(null); setFrame(null); setStats(null);
      setSampleId(d.samples.length ? d.samples[0].id : 0);
    } else if (!d.samples.some((s) => s.id === sampleId)) {
      setSampleId(d.samples.length ? d.samples[0].id : 0);
    }
    const usable = d.channels.filter((c) => c.kind !== 'time');
    const has = (p) => d.channels.some((c) => c.pnn === p);
    if (!keep || !has(x)) setX(has('FSC-A') ? 'FSC-A' : usable[0] ? usable[0].pnn : '');
    if (!keep || (y && !has(y))) setY(has('SSC-A') ? 'SSC-A' : usable[1] ? usable[1].pnn : '');
    return d;
  };

  useEffect(() => {
    (async () => {
      setMe(await api.get('me'));
      const list = await api.get('experiments');
      setExperiments(list);
      if (list.length) await loadExperiment(list[0].id);
    })().catch(fail);
  }, []);

  // plot frame
  useEffect(() => {
    if (!exp || !sampleId || !x) { setFrame(null); return; }
    let live = true;
    const req = y ? api.frame(`samples/${sampleId}/plots/density`, { x, y, parent: parentId })
                  : api.frame(`samples/${sampleId}/plots/histogram`, { x, parent: parentId });
    req.then((f) => { if (live) { setFrame(f); setPlotErr(''); } })
       .catch((e) => { if (live) { setFrame(null); setPlotErr(e.message); } });
    return () => { live = false; };
  }, [exp && exp.id, sampleId, x, y, parentId, rev]);

  // populations of the active sample
  useEffect(() => {
    if (!exp || !sampleId) { setPops(null); return; }
    let live = true;
    api.get(`samples/${sampleId}/populations`).then((p) => {
      if (!live) return;
      setPops(p);
      if (parentId && !p.populations.some((q) => q.id === parentId)) setParentId(0);
    }).catch(fail);
    return () => { live = false; };
  }, [exp && exp.id, sampleId, rev]);

  // statistics of the selected population
  useEffect(() => {
    if (!exp || !sampleId) { setStats(null); return; }
    let live = true;
    api.get(`samples/${sampleId}/stats`, { gate: parentId }).then((s) => live && setStats(s))
      .catch((e) => { if (live) setStats({ error: e.message }); });
    return () => { live = false; };
  }, [exp && exp.id, sampleId, parentId, rev]);

  useEffect(() => { if ((tool === 'range') !== !y && tool !== 'none') setTool('none'); }, [y]);

  const createExperiment = async () => {
    if (!newName.trim()) return;
    try {
      const e = await api.send('POST', 'experiments', { name: newName.trim() });
      setNewName('');
      setExperiments(await api.get('experiments'));
      await loadExperiment(e.id);
    } catch (e) { fail(e); }
  };

  const onUpload = async (ev) => {
    const files = [...ev.target.files];
    ev.target.value = '';
    if (!files.length) return;
    setNotice(`Uploading ${files.length} file(s)…`);
    try {
      const r = await api.upload(`experiments/${exp.id}/samples`, files);
      const ok = r.results.filter((q) => q.ok), bad = r.results.filter((q) => !q.ok);
      const dropped = ok.reduce((n, q) => n + q.sample.keywords_dropped, 0);
      setNotice(`${ok.length} file(s) ingested; ${dropped} FCS keyword(s) discarded by the allow-list.`
        + (ok.some((q) => q.sample.warnings.length) ? ' Warnings: ' + ok.flatMap((q) => q.sample.warnings).join('; ') : ''));
      if (bad.length) setError(bad.map((q) => `${q.file}: ${q.error}`).join(' | '));
      await loadExperiment(exp.id, true);
      bump();
    } catch (e) { setNotice(''); fail(e); }
  };

  const patchSample = async (s, body) => {
    try { await api.send('PATCH', `samples/${s.id}`, body); await loadExperiment(exp.id, true); bump(); } catch (e) { fail(e); }
  };
  const toggleComp = async (s) => {
    try { await api.send('PUT', `samples/${s.id}/compensation`, { enabled: !s.comp_enabled }); await loadExperiment(exp.id, true); bump(); }
    catch (e) { fail(e); }
  };
  const deleteSample = async (s) => {
    if (!confirm(`Remove sample "${s.name}" and its events from this experiment?`)) return;
    try { await api.send('DELETE', `samples/${s.id}`); await loadExperiment(exp.id, true); bump(); } catch (e) { fail(e); }
  };

  const nextName = () => {
    const names = new Set((pops ? pops.populations : []).map((p) => p.name));
    let i = 1;
    while (names.has(`P${i}`)) i++;
    return `P${i}`;
  };
  const onGate = async (spec) => {
    try {
      await api.send('POST', `experiments/${exp.id}/gates`, { ...spec, name: gateName.trim() || nextName(), parent_id: parentId });
      setGateName(''); setTool('none'); bump();
    } catch (e) { fail(e); }
  };
  const deleteGate = async (p) => {
    if (!confirm(`Delete population "${p.name}" and everything gated from it?`)) return;
    try { await api.send('DELETE', `gates/${p.id}`); if (parentId === p.id) setParentId(p.parent_id || 0); bump(); } catch (e) { fail(e); }
  };
  const renameGate = async (p) => {
    const name = prompt('Population name', p.name);
    if (!name || name === p.name) return;
    try { await api.send('PUT', `gates/${p.id}`, { name }); bump(); } catch (e) { fail(e); }
  };
  const applyScale = async (pnn, kind, params) => {
    try { await api.send('PUT', `experiments/${exp.id}/scales`, { pnn, kind, params }); await loadExperiment(exp.id, true); setEditAxis(''); bump(); }
    catch (e) { fail(e); }
  };
  const applyAll = async () => {
    try {
      const j = await api.send('POST', `experiments/${exp.id}/jobs`, { type: 'apply_template' });
      await pollJob(j, setApplyJob);
      bump();
    } catch (e) { fail(e); }
  };

  if (!me) return html`<div class="loading">Loading…${error && html`<div class="error">${error}</div>`}</div>`;
  const sample = exp ? exp.samples.find((s) => s.id === sampleId) : null;
  const chan = (pnn) => (exp ? exp.channels.find((c) => c.pnn === pnn) : null);
  const popList = pops ? pops.populations : [];
  const current = popList.find((p) => p.id === parentId);
  const popName = current ? current.path : 'All events';
  const applying = applyJob && (applyJob.state === 'queued' || applyJob.state === 'running');
  const h = frame ? frame.header : null;

  return html`
  <header>
    <h1>LymphVizkit <span>Flow</span></h1>
    <label>Experiment <select data-testid="experiment-select" value=${exp ? exp.id : ''}
      onChange=${(e) => loadExperiment(+e.target.value).catch(fail)}>
      ${!exp && html`<option value="">(none)</option>`}
      ${experiments.map((e) => html`<option value=${e.id}>${e.name} (${e.n_samples})</option>`)}</select></label>
    <input placeholder="New experiment name" value=${newName} data-testid="new-experiment-name"
      onInput=${(e) => setNewName(e.target.value)} onKeyDown=${(e) => e.key === 'Enter' && createExperiment()} />
    <button onClick=${createExperiment} data-testid="create-experiment">Create</button>
    <span class="spacer"></span>
    <span class="hint">user: ${me.user}${me.auth_mode === 'dev' ? ' (single-user dev mode)' : ''}</span>
  </header>
  ${error && html`<div class="banner error" data-testid="error-banner">${error}<button class="quiet" onClick=${() => setError('')}>dismiss</button></div>`}
  ${notice && html`<div class="banner" data-testid="notice">${notice}<button class="quiet" onClick=${() => setNotice('')}>dismiss</button></div>`}
  ${!exp ? html`<main class="empty">Create an experiment to begin.</main>` : html`
  <main>
    <section class="panel samples">
      <h2>Samples</h2>
      <label class="upload">Add FCS files
        <input type="file" multiple accept=".fcs" onChange=${onUpload} data-testid="upload-input" /></label>
      <div class="hint">Limit ${me.max_upload_mb} MB per file. Files are converted and the FCS is not kept.</div>
      <ul class="sample-list" data-testid="sample-list">
        ${exp.samples.map((s) => html`<li class=${s.id === sampleId ? 'active' : ''} onClick=${() => setSampleId(s.id)}>
          <div class="line"><b title=${s.name}>${s.name}</b>
            <button class="quiet x" title="Remove sample" onClick=${(e) => { e.stopPropagation(); deleteSample(s); }}>×</button></div>
          <div class="line"><span>${fmt(s.n_events)} events${s.n_nonfinite ? `, ${fmt(s.n_nonfinite)} non-finite` : ''}</span>
            <${CompBadge} s=${s} onToggle=${toggleComp} /></div>
          <input class="group" placeholder="group label" value=${s.group_label} onClick=${(e) => e.stopPropagation()}
            onChange=${(e) => patchSample(s, { group_label: e.target.value })} />
        </li>`)}
      </ul>
    </section>

    <section class="panel plot">
      ${!sample ? html`<div class="empty">Add FCS files to this experiment.</div>` : html`
      <div class="row wrap">
        <label>X <select value=${x} onChange=${(e) => setX(e.target.value)} data-testid="x-select">
          ${exp.channels.map((c) => html`<option value=${c.pnn} disabled=${c.pnn === y}>${c.pns ? `${c.pns} (${c.pnn})` : c.pnn}</option>`)}</select></label>
        <label>Y <select value=${y} onChange=${(e) => setY(e.target.value)} data-testid="y-select">
          <option value="">(histogram)</option>
          ${exp.channels.map((c) => html`<option value=${c.pnn} disabled=${c.pnn === x}>${c.pns ? `${c.pns} (${c.pnn})` : c.pnn}</option>`)}</select></label>
        <span class="tools">
          ${(y ? ['rect', 'polygon'] : ['range']).map((t) => html`<button class=${tool === t ? 'on' : ''} data-testid=${'tool-' + t}
            onClick=${() => setTool(tool === t ? 'none' : t)}>${{ rect: 'Rectangle', polygon: 'Polygon', range: 'Range' }[t]}</button>`)}
        </span>
        <input class="gate-name" placeholder=${'name (' + nextName() + ')'} value=${gateName} data-testid="gate-name"
          onInput=${(e) => setGateName(e.target.value)} />
      </div>
      <div class="hint caption" data-testid="plot-caption">
        Showing <b>${popName}</b> of <b>${sample.name}</b>${h ? html`: ${fmt(h.n_shown)} of ${fmt(h.n_parent)} events plotted${h.n_nonfinite ? html`, <span class="warn">${fmt(h.n_nonfinite)} non-finite excluded</span>` : ''}; compensation ${h.comp.applied ? 'applied' : 'not applied (' + h.comp.state + ')'}.` : ''}
        ${tool === 'polygon' ? ' Click to add vertices; double-click, Enter or click the first vertex to close; Esc cancels.' : ''}
        ${tool === 'rect' || tool === 'range' ? ' Drag to draw; Esc cancels.' : ''}
      </div>
      ${plotErr && html`<div class="error" data-testid="plot-error">${plotErr}</div>`}
      <${Plot} frame=${frame} tool=${tool} onGate=${onGate} onSelectGate=${setParentId}
        onAxisClick=${(a) => setEditAxis(a === 'x' ? x : y)} />
      ${editAxis && chan(editAxis) && html`<${ScaleEditor} key=${editAxis + rev} channel=${chan(editAxis)} onApply=${applyScale} onClose=${() => setEditAxis('')} />`}
      <div class="hint offscale" data-testid="off-scale-line">${h && offScale(h) ? html`<span class="warn" data-testid="off-scale">${offScale(h)}</span>` : ''}</div>
      <div class="hint">Click an axis scale (blue text) to change it. Ticks are labelled in raw units. Events outside the display window pile up on the plot edge.</div>`}
    </section>

    <section class="panel pops">
      <h2>Populations <span class="hint">${sample ? sample.name : ''}</span></h2>
      ${pops && pops.error && html`<div class="error">${pops.error.detail}</div>`}
      <table class="grid tree" data-testid="population-tree"><thead><tr><th>Population</th><th>Events</th><th>% parent</th><th></th></tr></thead><tbody>
        <tr class=${parentId === 0 ? 'active' : ''} onClick=${() => setParentId(0)}><td>All events</td><td>${fmt(pops ? pops.n_events : null)}</td><td></td><td></td></tr>
        ${popList.map((p) => html`<tr class=${(parentId === p.id ? 'active ' : '') + (p.error ? 'err' : '')} title=${p.error || ''}
            data-pop=${p.name} onClick=${() => setParentId(p.id)}>
          <td style=${`padding-left:${10 + 14 * (p.depth + 1)}px`}>${p.name}${p.overridden ? ' *' : ''}</td>
          <td data-testid=${'count-' + p.name}>${p.error ? 'error' : fmt(p.count)}</td><td>${pct(p.pct_parent)}</td>
          <td class="actions"><button class="quiet" title="Rename" onClick=${(e) => { e.stopPropagation(); renameGate(p); }}>✎</button>
            <button class="quiet x" title="Delete" onClick=${(e) => { e.stopPropagation(); deleteGate(p); }}>×</button></td></tr>`)}
      </tbody></table>
      <div class="row wrap">
        <button onClick=${applyAll} disabled=${applying || !exp.samples.length} data-testid="apply-all">Apply to all samples</button>
        <a class="button" href=${api.href(`experiments/${exp.id}/stats.csv`)} data-testid="export-csv">Statistics CSV</a>
        <a class="button" href=${api.href(`experiments/${exp.id}/gatingml`)} data-testid="export-gatingml">GatingML</a>
      </div>
      ${applyJob && html`<div class=${'hint ' + (applyJob.state === 'failed' ? 'error' : '')} data-testid="apply-state">
        Template job: ${applyJob.state}${applying ? ` · ${Math.round(100 * applyJob.progress)}% ${applyJob.message}` : ''}
        ${applyJob.state === 'done' && applyJob.result ? html` · ${applyJob.result.n_evaluated} population results for ${applyJob.result.n_samples} sample(s)${applyJob.result.n_failed ? html`, <span class="warn">${applyJob.result.n_failed} failed: ${applyJob.result.failures.map((f) => `${f.sample} / ${f.population}: ${f.error}`).join('; ')}</span>` : ', no failures'}` : ''}
        ${applyJob.state === 'failed' ? ' · ' + applyJob.message : ''}</div>`}
      <h3>Statistics: ${popName}</h3>
      ${stats && stats.error && html`<div class="error">${stats.error}</div>`}
      ${stats && !stats.error && html`<div>
        <div class="hint" data-testid="stats-basis">${fmt(stats.count)} events · ${pct(stats.pct_parent)} of parent · ${pct(stats.pct_total)} of all events.
          Values: <b>${stats.values}</b>.</div>
        <div class="scroll"><table class="grid" data-testid="stats-table"><thead><tr><th>Channel</th><th>Median</th><th>Mean</th><th>CV %</th><th>rCV %</th><th>Geo. mean</th></tr></thead><tbody>
          ${Object.entries(stats.channels).map(([pnn, c]) => html`<tr><td>${stats.stains[pnn] ? `${stats.stains[pnn]} (${pnn})` : pnn}</td>
            <td>${sig(c.median)}</td><td>${sig(c.mean)}</td><td>${sig(c.cv, 3)}</td><td>${sig(c.rcv, 3)}</td>
            <td title=${c.n_nonpositive ? `${c.n_nonpositive} non-positive values excluded` : ''}>${sig(c.geomean)}${c.n_nonpositive ? ' †' : ''}</td></tr>`)}
        </tbody></table></div>
        <div class="hint">† geometric mean over positive values only (hover for the number excluded). * uses a per-sample gate.</div>
      </div>`}
    </section>
  </main>
  ${sample && html`<section class="panel bottom">
    <nav>${[['summary', 'Summary across samples'], ['cluster', 'Clustering (FlowSOM)'], ['comp', 'Compensation'], ['keywords', 'Keywords']]
      .map(([id, label]) => html`<button class=${tab === id ? 'on' : ''} onClick=${() => setTab(id)} data-testid=${'tab-' + id}>${label}</button>`)}</nav>
    ${tab === 'summary' && html`<${SummaryTab} exp=${exp} gateId=${parentId} popName=${popName} rev=${rev} />`}
    ${tab === 'cluster' && html`<${ClusterTab} exp=${exp} gateId=${parentId} popName=${popName} onError=${setError} />`}
    ${tab === 'comp' && html`<${CompTab} sampleId=${sampleId} rev=${rev} />`}
    ${tab === 'keywords' && html`<${KeywordsTab} sampleId=${sampleId} />`}
  </section>`}`}`;
}

render(html`<${App} />`, document.getElementById('app'));
