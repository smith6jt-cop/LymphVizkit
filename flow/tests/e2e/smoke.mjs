// End-to-end smoke test (plain Node + the pre-installed Playwright library; no test runner).
//
//   node tests/e2e/smoke.mjs            # from flow/, with the virtualenv's python on PATH or PYTHON set
//
// Serves the app under the /flow prefix (as behind a proxy that forwards the prefix), uploads
// synthetic FCS files through the browser, draws gates with real pointer events, and asserts
// EXACT gated counts against the synthetic ground truth. Fails on any request that leaves the
// prefix or the host, and on any browser console error.
import { spawn, execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FLOW = path.resolve(HERE, '..', '..');
const PYTHON = process.env.PYTHON || path.join(FLOW, '.venv', 'bin', 'python');
const PORT = Number(process.env.E2E_PORT || 8791);
const PREFIX = '/flow';
const BASE = `http://127.0.0.1:${PORT}${PREFIX}/`;
const SHOT_DIR = process.env.E2E_SHOTS || path.join(HERE, 'screenshots');
const work = mkdtempSync(path.join(tmpdir(), 'lymphflow-e2e-'));
const fcsDir = path.join(work, 'fcs');

let failures = 0;
let page = null;
const consoleErrors = [];
const offPrefix = [];
const check = (cond, msg) => { if (cond) console.log(`  ok   ${msg}`); else { failures++; console.error(`  FAIL ${msg}`); } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

execFileSync(PYTHON, ['-m', 'lymphflow', 'synthetic', fcsDir, '--files', '4', '--events', '20000', '--seed', '7']);
const truth = JSON.parse(readFileSync(path.join(fcsDir, 'truth.json'), 'utf8'));
const fileNames = Object.keys(truth.files).sort();

const server = spawn(PYTHON, ['-m', 'lymphflow', 'serve'], {
  env: { ...process.env, LYMPH_FLOW_DATA: path.join(work, 'data'), LYMPH_FLOW_PORT: String(PORT), LYMPH_FLOW_URL_PREFIX: PREFIX },
  stdio: ['ignore', 'pipe', 'pipe'],
});
let serverLog = '';
server.stdout.on('data', (d) => { serverLog += d; });
server.stderr.on('data', (d) => { serverLog += d; });

async function waitForServer() {
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(BASE + 'api/health')).ok) return; } catch (_) { /* not up yet */ }
    await sleep(200);
  }
  throw new Error('server did not start:\n' + serverLog);
}

async function api(method, p, body) {
  const res = await fetch(BASE + 'api/' + p, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) throw new Error(`${method} ${p} -> ${res.status} ${await res.text()}`);
  return res.json();
}

async function main() {
  await waitForServer();
  const redirect = await fetch(`http://127.0.0.1:${PORT}${PREFIX}`, { redirect: 'manual' });
  check(redirect.status === 308 && redirect.headers.get('location') === PREFIX + '/', `${PREFIX} redirects to ${PREFIX}/`);

  const browser = await chromium.launch();
  page = await browser.newPage({ viewport: { width: 1500, height: 1100 }, deviceScaleFactor: 2, acceptDownloads: true });
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push(String(e)));
  page.on('request', (r) => { const u = r.url(); if (!u.startsWith(BASE) && !u.startsWith('data:') && !u.startsWith('blob:')) offPrefix.push(u); });
  page.on('dialog', (d) => d.accept());

  await page.goto(BASE);
  await page.getByTestId('new-experiment-name').fill('E2E synthetic');
  await page.getByTestId('create-experiment').click();
  await page.getByTestId('upload-input').waitFor();
  await page.getByTestId('upload-input').setInputFiles(fileNames.map((n) => path.join(fcsDir, n)));
  await page.getByTestId('notice').filter({ hasText: 'ingested' }).waitFor({ timeout: 60000 });
  const notice = await page.getByTestId('notice').innerText();
  check(/4 file\(s\) ingested/.test(notice), `upload notice: "${notice.replace(/dismiss$/, '').trim()}"`);
  check((await page.locator('[data-testid="sample-list"] li').count()) === 4, '4 samples listed');

  // Group labels (drives the grouped summary plot).
  const exps = await api('GET', 'experiments');
  const exp = await api('GET', `experiments/${exps[0].id}`);
  for (const s of exp.samples) await api('PATCH', `samples/${s.id}`, { group_label: truth.files[s.name + '.fcs'].group });
  const first = exp.samples[0];
  const t = truth.files[first.name + '.fcs'];
  await page.reload();
  await page.getByTestId('plot-surface').waitFor();
  await page.waitForFunction(() => document.querySelector('[data-testid="plot-caption"]')?.innerText.includes('events plotted')
    && document.querySelector('[data-testid="x-axis-title"]')?.textContent.includes('FSC-A')
    && document.querySelector('[data-testid="y-axis-title"]')?.textContent.includes('SSC-A'));

  const surface = page.getByTestId('plot-surface');
  const box = async () => surface.boundingBox();
  const header = async (params) => {
    const res = await fetch(BASE + 'api/' + params);
    const buf = await res.arrayBuffer();
    const n = new DataView(buf).getUint32(0, true);
    return JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, n)));
  };
  const toPx = (b, win, v, invert) => { const f = (v - win[0]) / (win[1] - win[0]); return invert ? b.y + b.height * (1 - f) : b.x + b.width * f; };

  // ---- 1. rectangle gate "Cells" on FSC-A / SSC-A by dragging -------------------------------
  let h = await header(`samples/${first.id}/plots/density?x=FSC-A&y=SSC-A`);
  const sg = truth.scatter_gate;
  await page.getByTestId('gate-name').fill('Cells');
  await page.getByTestId('tool-rect').click();
  let b = await box();
  await page.mouse.move(toPx(b, h.x.window, sg.x0), toPx(b, h.y.window, sg.y1, true));
  await page.mouse.down();
  await page.mouse.move(toPx(b, h.x.window, (sg.x0 + sg.x1) / 2), toPx(b, h.y.window, (sg.y0 + sg.y1) / 2, true), { steps: 4 });
  await page.mouse.move(toPx(b, h.x.window, sg.x1), toPx(b, h.y.window, sg.y0, true), { steps: 4 });
  await page.mouse.up();
  await page.getByTestId('count-Cells').waitFor();
  const cells = (await page.getByTestId('count-Cells').innerText()).replace(/\D/g, '');
  check(Number(cells) === t.n_cells, `rectangle gate "Cells": ${cells} events (expected exactly ${t.n_cells})`);

  // ---- 2. polygon gate "CD4 T" on CD4 / CD8a inside Cells, by clicking vertices --------------
  await page.locator('[data-pop="Cells"]').click();
  await page.getByTestId('x-select').selectOption('YG585-A');
  await page.getByTestId('y-select').selectOption('R670-A');
  await page.waitForFunction(() => document.querySelector('[data-testid="x-axis-title"]')?.textContent.includes('CD4')
    && document.querySelector('[data-testid="y-axis-title"]')?.textContent.includes('CD8a')
    && document.querySelector('[data-testid="plot-caption"]')?.innerText.includes('Cells'));
  const cells_id = (await api('GET', `experiments/${exp.id}/gates`)).gates.find((g) => g.name === 'Cells').id;
  h = await header(`samples/${first.id}/plots/density?x=YG585-A&y=R670-A&parent=${cells_id}`);
  const thr = truth.gate_threshold;
  const fwd = async (scaleId, values) => (await api('POST', 'scales/apply', { scale_id: scaleId, values })).values;
  const [tx] = await fwd(h.x.scale_id, [thr]);
  const [ty] = await fwd(h.y.scale_id, [thr]);
  // CD4+ CD8- : x above threshold, y below threshold. The threshold corners sit in the gaps
  // between clusters; the other corners are on the plot border, which extends the gate past it
  // so that off-scale events piled on the border are included.
  check(h.off_scale.y.below > 0, `${h.off_scale.y.below} CD8a-negative events are below the display window (piled on the border)`);
  await page.getByTestId('off-scale').waitFor();
  await page.getByTestId('gate-name').fill('CD4 T');
  await page.getByTestId('tool-polygon').click();
  b = await box();
  const px = (v) => toPx(b, h.x.window, v), py = (v) => toPx(b, h.y.window, v, true);
  const right = b.x + b.width - 2, bottom = b.y + b.height - 2;
  for (const [cx, cy] of [[px(tx), bottom], [right, bottom], [right, py(ty)], [px(tx), py(ty)]]) await page.mouse.click(cx, cy);
  await page.keyboard.press('Enter');
  await page.getByTestId('count-CD4 T').waitFor();
  const cd4 = (await page.getByTestId('count-CD4 T').innerText()).replace(/\D/g, '');
  check(Number(cd4) === t.counts['CD4 T cell'], `polygon gate "CD4 T": ${cd4} events (expected exactly ${t.counts['CD4 T cell']})`);
  mkdirSync(SHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOT_DIR, 'gating.png') });

  // ---- 3. range gate on a histogram -----------------------------------------------------------
  await page.getByTestId('x-select').selectOption('V450-A');
  await page.getByTestId('y-select').selectOption('');
  await page.waitForFunction(() => document.querySelector('[data-testid="x-axis-title"]')?.textContent.includes('CD20')
    && !document.querySelector('[data-testid="y-axis-title"]'));
  h = await header(`samples/${first.id}/plots/histogram?x=V450-A&parent=${cells_id}`);
  const [tb] = await fwd(h.x.scale_id, [thr]);
  await page.getByTestId('gate-name').fill('B cells');
  await page.getByTestId('tool-range').click();
  b = await box();
  const yMid = b.y + b.height / 2;
  await page.mouse.move(toPx(b, h.x.window, tb), yMid);
  await page.mouse.down();
  await page.mouse.move(toPx(b, h.x.window, h.x.window[1]) - 2, yMid, { steps: 5 });
  await page.mouse.up();
  await page.getByTestId('count-B cells').waitFor();
  const bc = (await page.getByTestId('count-B cells').innerText()).replace(/\D/g, '');
  check(Number(bc) === t.counts['B cell'], `range gate "B cells": ${bc} events (expected exactly ${t.counts['B cell']})`);

  // ---- 4. statistics basis, template job, CSV download ---------------------------------------
  await page.locator('[data-pop="CD4 T"]').click();
  await page.getByTestId('stats-table').waitFor();
  check((await page.getByTestId('stats-basis').innerText()).includes('compensated linear'), 'statistics state they use compensated linear values');
  await page.getByTestId('apply-all').click();
  await page.getByTestId('apply-state').filter({ hasText: 'no failures' }).waitFor({ timeout: 60000 });
  check(true, 'apply-to-all-samples job finished with no failures: ' + (await page.getByTestId('apply-state').innerText()).trim());
  const [download] = await Promise.all([page.waitForEvent('download'), page.getByTestId('export-csv').click()]);
  const csvPath = path.join(work, 'stats.csv');
  await download.saveAs(csvPath);
  const csv = readFileSync(csvPath, 'utf8');
  const rows = csv.split('\n').filter((l) => l && !l.startsWith('#'));
  check(csv.startsWith('# lymphflow statistics export'), 'CSV starts with a provenance header');
  check(rows.length === 1 + 4 * 4 * 6, `CSV has ${rows.length - 1} data rows (4 samples x 4 populations x 6 channels = 96)`);
  for (const name of fileNames) {
    const sample = name.replace(/\.fcs$/, '');
    const line = rows.find((l) => l.startsWith(`${sample},`) && l.includes(',Cells / CD4 T,'));
    const count = line ? Number(line.split(',')[3]) : NaN;
    check(count === truth.files[name].counts['CD4 T cell'], `CSV ${sample}: CD4 T count ${count} (expected ${truth.files[name].counts['CD4 T cell']})`);
  }

  // ---- 5. summary plot and FlowSOM --------------------------------------------------------------
  await page.getByTestId('tab-summary').click();
  await page.locator('[data-testid="summary-plot"] .plotly').waitFor({ timeout: 30000 });
  check((await page.locator('[data-testid="summary-table"] tbody tr').count()) === 4, 'summary table has one row per sample');
  await page.locator('[data-pop="Cells"]').click();
  await page.getByTestId('tab-cluster').click();
  await page.getByTestId('run-flowsom').click();
  await page.getByTestId('flowsom-state').filter({ hasText: /^done/ }).waitFor({ timeout: 180000 });
  await page.locator('[data-testid="flowsom-heatmap"] .plotly').waitFor({ timeout: 30000 });
  check((await page.locator('[data-testid="flowsom-freq"] tbody tr').count()) === 4, 'FlowSOM frequency table has one row per sample');
  await page.screenshot({ path: path.join(SHOT_DIR, 'workbench.png'), fullPage: true });

  // ---- 6. de-identification visible in the UI -----------------------------------------------------
  await page.getByTestId('tab-keywords').click();
  await page.getByTestId('keywords').waitFor();
  const kw = (await page.getByTestId('keywords').innerText()).toLowerCase();
  check(!/jane operator|patient 0042|mrn-0042|example university|sn123456/.test(kw), 'no identifying keyword value is shown');

  check(offPrefix.length === 0, `no request left ${BASE} (${offPrefix.length} did${offPrefix.length ? ': ' + offPrefix.slice(0, 3).join(', ') : ''})`);
  check(consoleErrors.length === 0, `no browser console errors (${consoleErrors.length}${consoleErrors.length ? ': ' + consoleErrors.slice(0, 3).join(' | ') : ''})`);
  await browser.close();
}

main().catch(async (e) => {
  failures++;
  console.error('ERROR', e.message);
  if (page) {
    try {
      mkdirSync(SHOT_DIR, { recursive: true });
      await page.screenshot({ path: path.join(SHOT_DIR, 'failure.png'), fullPage: true });
      const banner = await page.locator('[data-testid="error-banner"]').allInnerTexts();
      console.error('error banner:', banner, '| console errors:', consoleErrors.slice(0, 5));
    } catch (_) { /* diagnostics are best effort */ }
  }
  console.error(serverLog.split('\n').filter((l) => !/ 200 OK| 304 /.test(l)).slice(-25).join('\n'));
})
  .finally(() => {
    server.kill('SIGTERM');
    rmSync(work, { recursive: true, force: true });
    console.log(failures ? `\n${failures} check(s) FAILED` : '\nall end-to-end checks passed');
    process.exit(failures ? 1 : 0);
  });
