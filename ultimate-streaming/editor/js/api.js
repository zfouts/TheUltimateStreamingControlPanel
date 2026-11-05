/* Talking to the daemon: JSON calls and the live event stream.

   A link to the panel carries a one-time launch code in the URL fragment (#code=...), never
   the access token: on load it is traded for the token (POST /api/session), stripped from the
   URL, and the token is kept in sessionStorage. The token travels only in the X-Deck-Token
   header (also for the event stream, which is read with fetch() for that reason). An old
   #token=... link is only cleared from the address bar, never used. */

/** Why there is no token, if a launch code was refused ('' otherwise). */
export let LAUNCH_ERROR = '';

function stored() {
  try { return sessionStorage.getItem('deck_token') || ''; } catch (e) { return ''; }
}

/** Forget the stored token (it was rejected), so a reload doesn't reuse it. */
export function forgetToken() {
  try { sessionStorage.removeItem('deck_token'); } catch (e) { /* storage off */ }
}

async function takeToken() {
  const params = new URLSearchParams(location.hash.slice(1));
  const code = params.get('code');
  if (params.has('code') || params.has('token')) {
    params.delete('code');
    params.delete('token');
    const rest = params.toString();
    history.replaceState(null, '', location.pathname + (rest ? '#' + rest : ''));
  }
  if (!code) return stored();
  try {
    const r = await fetch('/api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) });
    let data = {};
    try { data = await r.json(); } catch (e) { /* no body */ }
    if (r.ok && typeof data.token === 'string' && data.token) {
      try { sessionStorage.setItem('deck_token', data.token); } catch (e) { /* storage off: this tab only */ }
      return data.token;
    }
    LAUNCH_ERROR = data.error || `the daemon answered ${r.status}`;
  } catch (e) {
    LAUNCH_ERROR = 'The daemon isn’t reachable';
  }
  return stored();
}

export const TOKEN = await takeToken();

export class ApiError extends Error {
  constructor(message, status, extra) { super(message); this.status = status; if (extra) Object.assign(this, extra); }
}

/** GET when body is undefined, POST (JSON) otherwise. Throws ApiError with .status. */
export async function api(path, body) {
  const opts = { headers: { 'X-Deck-Token': TOKEN } };
  if (body !== undefined) {
    opts.method = 'POST';
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let r;
  try { r = await fetch(path, opts); } catch (e) { throw new ApiError('The daemon isn’t reachable', 0); }
  let data = {};
  try { data = await r.json(); } catch (e) { /* no body */ }
  if (!r.ok) throw new ApiError(data.error || r.statusText, r.status, data.mismatch ? { mismatch: data.mismatch } : undefined);
  return data;
}

/** POST a file as a raw application/octet-stream body (uploads). Same errors as api(). */
export async function apiUpload(path, blob) {
  let r;
  try {
    r = await fetch(path, { method: 'POST', body: blob, headers: { 'X-Deck-Token': TOKEN, 'Content-Type': 'application/octet-stream' } });
  } catch (e) { throw new ApiError('The daemon isn’t reachable', 0); }
  let data = {};
  try { data = await r.json(); } catch (e) { /* no body */ }
  if (!r.ok) throw new ApiError(data.error || r.statusText, r.status);
  return data;
}

/* ------------------------------------------------------------------ event stream */

const handlers = new Map();   // type -> Set(fn)
let streamState = { connected: false, attempts: 0, abort: null };
const stateListeners = new Set();

/** on('chat', fn) — fn(data, {type, ts}). Returns an unsubscribe function. '*' gets everything. */
export function on(type, fn) {
  if (!handlers.has(type)) handlers.set(type, new Set());
  handlers.get(type).add(fn);
  return () => handlers.get(type).delete(fn);
}

export function onStreamState(fn) { stateListeners.add(fn); fn(streamState); return () => stateListeners.delete(fn); }

function setStream(patch) {
  streamState = { ...streamState, ...patch };
  for (const fn of stateListeners) fn(streamState);
}

function emit(type, ts, data) {
  for (const t of [type, '*']) {
    for (const fn of handlers.get(t) || []) {
      try { fn(data, { type, ts }); } catch (e) { console.error('event handler', type, e); }
    }
  }
}

function parseBlock(block) {
  let type = 'message', ts = 0, data = '';
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) type = line.slice(6).trim();
    else if (line.startsWith('id:')) ts = parseFloat(line.slice(3));
    else if (line.startsWith('data:')) data += line.slice(5).trim();
  }
  let parsed = {};
  try { parsed = data ? JSON.parse(data) : {}; } catch (e) { return; }
  emit(type, ts, parsed);
}

/** Open the Server-Sent Events stream and keep it open (reconnects with backoff). */
export function connect() {
  if (streamState.abort) return;
  const run = async () => {
    const ctl = new AbortController();
    setStream({ abort: ctl });
    try {
      const r = await fetch('/api/events', { headers: { 'X-Deck-Token': TOKEN }, signal: ctl.signal });
      if (!r.ok || !r.body) throw new ApiError(r.status === 401 ? 'token rejected' : `stream ${r.status}`, r.status);
      setStream({ connected: true, attempts: 0 });
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          parseBlock(buf.slice(0, i));
          buf = buf.slice(i + 2);
        }
      }
    } catch (e) {
      if (e.status === 401) { setStream({ connected: false, abort: null, fatal: true }); return; }
    }
    const attempts = streamState.attempts + 1;
    setStream({ connected: false, attempts, abort: null });
    setTimeout(run, Math.min(1000 * 2 ** attempts, 15000));
  };
  run();
}

export function disconnect() {
  if (streamState.abort) streamState.abort.abort();
  setStream({ connected: false, abort: null });
}
