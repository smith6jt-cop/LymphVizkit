// All requests are relative to the document base, so the app works under any URL prefix.
const API = new URL('api/', document.baseURI);

export class ApiError extends Error {
  constructor(status, body) {
    super((body && body.detail) || `request failed (${status})`);
    this.status = status;
    this.body = body || {};
  }
}

async function parse(res) {
  if (res.ok) return res;
  let body = null;
  try { body = await res.json(); } catch (_) { /* not JSON */ }
  if (body && typeof body.detail !== 'string') body.detail = JSON.stringify(body.detail);
  throw new ApiError(res.status, body);
}

function url(path, params) {
  const u = new URL(path, API);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== '') u.searchParams.set(k, v);
  }
  return u;
}

export async function get(path, params) {
  return (await parse(await fetch(url(path, params)))).json();
}

export async function send(method, path, body) {
  const res = await fetch(url(path), {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return (await parse(res)).json();
}

export async function upload(path, files) {
  const form = new FormData();
  for (const f of files) form.append('files', f, f.name);
  return (await parse(await fetch(url(path), { method: 'POST', body: form }))).json();
}

// Binary plot frame: [uint32 header length][JSON header][uint32 counts].
export async function frame(path, params) {
  const buf = await (await parse(await fetch(url(path, params)))).arrayBuffer();
  const n = new DataView(buf).getUint32(0, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, n)));
  const counts = new Uint32Array(buf, 4 + n);
  return { header, counts };
}

export function href(path, params) {
  return url(path, params).toString();
}
