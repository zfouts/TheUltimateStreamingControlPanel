/* OBS view: outputs, scenes (with studio mode), audio mixer, performance, raw requests.
   Steady-state data arrives on the "status" event (obs key, every 2 s); /api/obs/state is
   fetched on show and after actions. Both shapes are normalised into one model. */
import { api, on } from './api.js';
import { h, icon, chip, empty, toast, confirmDialog, switchInput, asyncButton, fmtDuration } from './ui.js';
import { go } from './main.js';

const MIN_DB = -60;
const SEND_EVERY_MS = 100;       // <= 10 sends a second while dragging
const SECRET = /key|password|token|secret/i;
const OUTPUTS = [
  { id: 'stream', title: 'Stream', icon: 'live', timed: true },
  { id: 'record', title: 'Record', icon: 'clip', timed: true },
  { id: 'replay', title: 'Replay buffer', icon: 'undo', timed: false },
  { id: 'virtualcam', title: 'Virtual camera', icon: 'camera', timed: false },
];

let host = null;
let model = null;          // normalised state, or {ok:false}
let loadedAt = 0;          // performance.now() when model arrived (for the ticking clocks)
let loading = true;
let ticker = null;
let dragging = 0;          // faders currently held
const sigs = {};           // card -> signature of what it last rendered
const els = {};            // card -> element

/* ------------------------------------------------------------------ model */

function fromState(s) {   // /api/obs/state
  return { ok: true, scene: s.scene, preview: s.preview, scenes: (s.scenes || []).map((x) => x.name), studio: !!s.studio,
    stream: s.stream || {}, record: s.record || {}, replay: !!s.replay, virtualcam: !!s.virtualcam,
    inputs: s.inputs || [], stats: s.stats || {} };
}
function fromSnapshot(o) {   // "status" event, collect() shape
  if (!o || !o.ok) return { ok: false };
  const out = o.outputs || {};
  return { ok: true, scene: o.scene, preview: o.preview, scenes: o.scenes || [], studio: !!o.studio,
    stream: out.stream || {}, record: out.record || {}, replay: !!out.replay, virtualcam: !!out.virtualcam,
    inputs: o.inputs || [], stats: o.stats || {} };
}
function setModel(m) {
  model = m;
  loadedAt = performance.now();
  loading = false;
  render();
}

async function refresh() {
  try {
    setModel(fromState(await api('/api/obs/state')));
  } catch (e) {
    if (e.status === 503 || e.status === 400) setModel({ ok: false });
    else if (!model) setModel({ ok: false, error: e.message });
  }
}

/** Run an API call, then re-fetch; failures toast. */
async function act(path, body) {
  const r = await api(path, body);
  refresh();
  return r;
}

/* ------------------------------------------------------------------ helpers */

const pct = (a, b) => (b > 0 ? (a / b) * 100 : 0);
const fmtPct = (v) => (v < 10 ? v.toFixed(1) : Math.round(v)) + ' %';
const level = (v, warn, bad) => (v >= bad ? 'bad' : v >= warn ? 'warn' : '');
function fmtMb(mb) { return mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : Math.round(mb) + ' MB'; }
function fmtDb(db) { return db <= MIN_DB ? '−∞ dB' : (db === 0 ? '0.0' : db.toFixed(1).replace('-', '−')) + ' dB'; }
const kindLabel = (k) => String(k || 'input').replace(/_/g, ' ');

function elapsed(id) {
  const o = model[id];
  if (!o || !o.active) return 0;
  const running = id === 'record' && o.paused ? 0 : (performance.now() - loadedAt) / 1000;
  return (o.seconds || 0) + running;
}

export function maskSecrets(v) {
  if (Array.isArray(v)) return v.map(maskSecrets);
  if (v && typeof v === 'object') {
    const out = {};
    for (const [k, val] of Object.entries(v)) out[k] = SECRET.test(k) ? '••••' : maskSecrets(val);
    return out;
  }
  return v;
}

function card(title, sub, iconName, ...head) {
  return { title, sub, iconName, head };
}
function cardEl(c, body, cls = '') {
  return h('section', { class: 'card ' + cls, 'aria-label': c.title },
    h('div', { class: 'card-head' },
      h('div', { class: 'card-icon' }, icon(c.iconName)),
      h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: c.title }), c.sub && h('p', { class: 'card-sub', text: c.sub })),
      ...c.head),
    body);
}

/** Replace a card's element only when its signature changed (keeps focus on idle refreshes). */
function place(name, sig, build) {
  if (sigs[name] === sig && els[name]) return;
  sigs[name] = sig;
  const next = build();
  const old = els[name];
  const focusId = old && old.contains(document.activeElement) ? document.activeElement.id : '';
  if (old && old.isConnected) old.replaceWith(next);
  if (focusId) { const f = next.querySelector('#' + CSS.escape(focusId)); if (f) f.focus(); }
  els[name] = next;
}

/* ------------------------------------------------------------------ cards */

function offlineCard() {
  return h('section', { class: 'card obs-offline', 'aria-label': 'OBS is not connected' },
    h('div', { class: 'obs-offline-icon' }, icon('wifiOff')),
    h('div', { class: 'stack' },
      h('h2', { class: 'card-title', text: 'OBS isn’t connected' }),
      h('p', { class: 'hint', text: 'Turn on OBS’s remote control and this page will connect by itself.' }),
      h('ol', { class: 'steps' },
        h('li', { text: 'Start OBS.' }),
        h('li', { text: 'Open Tools, then WebSocket Server Settings.' }),
        h('li', { text: 'Check “Enable WebSocket server”. That’s OBS’s built-in remote control, which this panel uses to start your stream and switch scenes.' }),
        h('li', {}, 'Put the server password in ', h('a', { href: '#view=settings', onclick: (e) => { e.preventDefault(); go('settings'); }, text: 'Settings' }), '.')),
      h('div', { class: 'row' },
        asyncButton({ class: 'primary' }, refresh, icon('refresh'), 'Retry'),
        h('span', { class: 'muted', text: 'Checking again every few seconds.' }))));
}

function outputTile(o) {
  const st = model[o.id];
  const active = o.id === 'stream' || o.id === 'record' ? !!st.active : !!st;
  const paused = o.id === 'record' && active && st.paused;
  const state = paused ? 'Paused' : active ? (o.id === 'stream' ? 'Live' : o.id === 'record' ? 'Recording' : 'On') : 'Off';
  const kind = paused ? 'warn' : active ? (o.id === 'stream' ? 'live' : 'ok') : '';
  const start = () => act('/api/obs/output', { what: o.id, action: 'start' });
  const stop = async () => {
    if (o.id === 'stream' && !(await confirmDialog('Stop the stream?', 'Your viewers will see the stream end.', { ok: 'Stop stream', danger: true }))) return;
    await act('/api/obs/output', { what: o.id, action: 'stop' });
  };
  const label = (a) => (a ? 'Stop' : 'Start') + (o.id === 'replay' ? ' buffer' : o.id === 'virtualcam' ? ' camera' : o.id === 'stream' ? ' stream' : ' recording');
  return h('div', { class: 'obs-tile' + (active ? ' on' : '') + (kind ? ' ' + kind : '') },
    h('div', { class: 'obs-tile-top' },
      h('span', { class: 'obs-tile-name' }, icon(o.icon), o.title),
      h('span', { class: 'dot ' + kind, 'aria-hidden': 'true' })),
    h('div', { class: 'obs-tile-state', text: state }),
    h('div', { class: 'obs-tile-sub' },
      o.timed && active ? h('span', { class: 'mono', 'data-clock': o.id, text: fmtDuration(elapsed(o.id)) }) : h('span', { class: 'muted', text: o.timed ? 'Not running' : active ? 'Ready' : 'Not running' }),
      o.id === 'stream' && active && st.bitrate_kbps ? h('span', { class: 'muted', text: ` · ${st.bitrate_kbps} kbps` }) : null),
    h('div', { class: 'row obs-tile-actions' },
      asyncButton({ id: `out-${o.id}-main`, class: active ? (o.id === 'stream' ? 'danger small' : 'small') : 'primary small' }, active ? stop : start, icon(active ? 'stop' : 'play'), label(active)),
      o.id === 'record' && active ? asyncButton({ id: 'out-record-pause', class: 'ghost small' }, () => act('/api/obs/output', { what: 'record', action: paused ? 'resume' : 'pause' }), icon(paused ? 'play' : 'pause'), paused ? 'Resume' : 'Pause') : null));
}

function outputsCard() {
  return cardEl(card('Outputs', 'Start and stop what OBS sends out.', 'live'),
    h('div', { class: 'obs-tiles' }, OUTPUTS.map(outputTile)));
}

function sceneBtn(name, role, cls) {
  const isProgram = role === 'program' && model.scene === name, isPreview = role === 'preview' && model.studio && model.preview === name;
  return h('button', {
    class: 'obs-scene ' + cls + (isProgram ? ' program' : '') + (isPreview ? ' preview' : ''),
    'aria-pressed': String(isProgram || isPreview),
    title: name,
    onclick: async () => {
      try { await act('/api/obs/scene', role === 'preview' ? { name, preview: true } : { name }); } catch (e) { toast(e.message, { bad: true }); }
    },
  }, h('span', { class: 'obs-scene-name', text: name }),
    isProgram ? h('span', { class: 'badge accent', text: 'Program' }) : isPreview ? h('span', { class: 'badge', text: 'Preview' }) : null);
}

function scenesCard() {
  const sw = switchInput(model.studio, async (on_) => {
    try { await act('/api/obs/studio', { enabled: on_ }); } catch (e) { toast(e.message, { bad: true }); refresh(); }
  }, 'Studio mode');
  let body;
  if (!model.scenes.length) body = empty('No scenes in OBS', 'Add a scene in OBS and it will appear here.', 'layers');
  else if (model.studio) {
    body = h('div', { class: 'obs-studio' },
      h('div', { class: 'obs-col' }, h('h3', { class: 'subhead', text: 'Program (live)' }),
        h('div', { class: 'obs-scenes' }, model.scenes.map((n) => sceneBtn(n, 'program', 'prog')))),
      h('div', { class: 'obs-mid' }, asyncButton({ class: 'primary' }, () => act('/api/obs/studio', { transition: true }), icon('arrowRight'), 'Transition')),
      h('div', { class: 'obs-col' }, h('h3', { class: 'subhead', text: 'Preview (next)' }),
        h('div', { class: 'obs-scenes' }, model.scenes.map((n) => sceneBtn(n, 'preview', 'prev')))));
  } else body = h('div', { class: 'obs-scenes wide' }, model.scenes.map((n) => sceneBtn(n, 'program', 'prog')));
  return cardEl(card('Scenes', model.studio ? 'Pick what to prepare, then transition it live.' : 'Click a scene to take it live.', 'layers', sw), body);
}

/* audio mixer */

function makeSender(name) {
  let last = 0, timer = null, pending = null;
  const flush = async () => {
    timer = null; last = performance.now();
    const db = pending; pending = null;
    if (db === null) return;
    try { await api('/api/obs/input', { name, volume_db: db <= MIN_DB ? -100 : db }); } catch (e) { toast(e.message, { bad: true }); }
  };
  return {
    throttled(db) {
      pending = db;
      if (timer) return;
      const wait = Math.max(0, SEND_EVERY_MS - (performance.now() - last));
      timer = setTimeout(flush, wait);
    },
    final(db) { clearTimeout(timer); timer = null; pending = db; return flush(); },
  };
}

function mixerRow(inp) {
  const sender = makeSender(inp.name);
  const has = typeof inp.volume_db === 'number';
  let db = has ? Math.max(MIN_DB, Math.min(0, inp.volume_db)) : MIN_DB;
  const readout = h('span', { class: 'obs-db mono', text: has ? fmtDb(db) : 'n/a' });
  const idAttr = 'fader-' + Math.abs([...inp.name].reduce((a, c) => (a * 31 + c.charCodeAt(0)) | 0, 7));
  const slider = h('input', { type: 'range', id: idAttr, class: 'obs-fader', min: MIN_DB, max: 0, step: 0.5, value: db, disabled: !has || inp.muted,
    'aria-label': `${inp.name} volume`, 'aria-valuetext': fmtDb(db) });
  const paint = () => {
    slider.style.setProperty('--fill', ((db - MIN_DB) / -MIN_DB) * 100 + '%');
    slider.setAttribute('aria-valuetext', fmtDb(db));
    readout.textContent = fmtDb(db);
  };
  paint();
  let held = false;
  slider.addEventListener('pointerdown', () => { if (!held) { held = true; dragging++; } });
  slider.addEventListener('input', () => { db = parseFloat(slider.value); inp.volume_db = db; paint(); sender.throttled(db); });
  slider.addEventListener('change', async () => {
    release();
    db = parseFloat(slider.value);
    try { await sender.final(db); } catch (e) { /* toasted */ }
    refresh();
  });
  const release = () => { if (held) { held = false; dragging = Math.max(0, dragging - 1); } };
  slider.addEventListener('pointerup', release);
  slider.addEventListener('pointercancel', release);

  const mute = h('button', {
    id: 'mute-' + idAttr, class: 'icon-btn obs-mute' + (inp.muted ? ' muted-on' : ''), 'aria-pressed': String(!!inp.muted),
    'aria-label': `${inp.muted ? 'Unmute' : 'Mute'} ${inp.name}`, title: inp.muted ? 'Unmute' : 'Mute',
    onclick: async () => {
      const next = !inp.muted;
      inp.muted = next; sigs.mixer = null; renderMixerOptimistic();      // optimistic
      try { await api('/api/obs/input', { name: inp.name, muted: next }); } catch (e) {
        inp.muted = !next; renderMixerOptimistic(); toast(e.message, { bad: true });
      }
      refresh();
    },
  }, icon(inp.muted ? 'micOff' : 'mic'));
  return h('li', { class: 'obs-row' + (inp.muted ? ' is-muted' : '') },
    h('div', { class: 'obs-row-name' }, h('label', { for: idAttr, class: 'obs-input-name', text: inp.name, title: inp.name }), h('span', { class: 'muted obs-kind', text: kindLabel(inp.kind) })),
    mute, slider, readout);
}

function renderMixerOptimistic() {
  sigs.mixer = null;
  if (model && model.ok) mixerRender();
}
function mixerRender() {
  if (dragging) return;
  place('mixer', JSON.stringify(model.inputs) + (model.ok ? '' : 'x'), () => cardEl(card('Audio mixer', 'Mute and set levels for each input.', 'volume'),
    model.inputs.length
      ? h('ul', { class: 'obs-mixer' }, model.inputs.map(mixerRow))
      : empty('No audio inputs', 'Add a microphone or desktop audio source in OBS.', 'mic')));
}

function perfCard() {
  const s = model.stats || {};
  const lag = pct(s.render_skipped, s.render_total), skip = pct(s.output_skipped, s.output_total);
  const tile = (label, value, lvl, sub) => h('div', { class: 'stat' },
    h('div', { class: 'stat-label', text: label }), h('div', { class: 'stat-value ' + (lvl || ''), text: value }), sub && h('div', { class: 'stat-sub', text: sub }));
  return cardEl(card('Performance', 'How hard OBS is working.', 'bolt'),
    h('div', { class: 'obs-stats' },
      tile('CPU', (s.cpu ?? 0).toFixed(1) + ' %', level(s.cpu || 0, 80, 101), 'OBS process'),
      tile('Frame rate', (s.fps ?? 0).toFixed(1) + ' fps', '', 'Active'),
      tile('Render lag', fmtPct(lag), level(lag, 1, 5), `${s.render_skipped || 0} of ${s.render_total || 0} frames`),
      tile('Encoder skips', fmtPct(skip), level(skip, 1, 5), `${s.output_skipped || 0} of ${s.output_total || 0} frames`),
      tile('Memory', fmtMb(s.memory_mb || 0), '', 'In use'),
      tile('Free disk', fmtMb(s.disk_mb || 0), '', 'For recordings')));
}

/* advanced */

function advancedCard() {
  const name = h('input', { type: 'text', id: 'obs-req-name', placeholder: 'GetVersion', autocomplete: 'off', spellcheck: 'false', maxlength: 200 });
  const data = h('textarea', { id: 'obs-req-data', rows: 4, class: 'mono', placeholder: '{"sceneName": "Game"}', spellcheck: 'false' });
  const nameErr = h('span', { class: 'help error', hidden: true });
  const dataErr = h('span', { class: 'help error', hidden: true });
  const out = h('pre', { class: 'output', hidden: true, 'aria-live': 'polite' });
  const show = (text) => { out.hidden = false; out.textContent = text; };
  const bad = (el, input, msg) => { el.hidden = !msg; el.textContent = msg || ''; input.setAttribute('aria-invalid', msg ? 'true' : 'false'); input.closest('.field').classList.toggle('error', !!msg); };
  const send = async () => {
    const req = name.value.trim();
    let payload;
    bad(nameErr, name, /^[A-Za-z]+$/.test(req) ? '' : 'Use the request name, letters only, like GetVersion.');
    let dataMsg = '';
    if (data.value.trim()) {
      try { payload = JSON.parse(data.value); if (!payload || typeof payload !== 'object' || Array.isArray(payload)) dataMsg = 'Request data must be a JSON object, like {"a": 1}.'; }
      catch (e) { dataMsg = 'That isn’t valid JSON: ' + e.message; }
    }
    bad(dataErr, data, dataMsg);
    if (!/^[A-Za-z]+$/.test(req) || dataMsg) return;
    try {
      const r = await api('/api/obs/request', { request: req, data: payload });
      show(JSON.stringify(maskSecrets(r.response), null, 2) || '{}');
    } catch (e) { show('Error: ' + e.message); }
  };
  return h('details', { class: 'card obs-advanced' },
    h('summary', {}, h('span', { class: 'card-icon' }, icon('code')),
      h('span', { class: 'card-titles' }, h('span', { class: 'card-title', text: 'Advanced' }), h('span', { class: 'card-sub', text: 'Send a raw obs-websocket request' })),
      icon('chevronDown', 'ic obs-chev')),
    h('div', { class: 'stack obs-adv-body' },
      h('p', { class: 'note warn', text: 'Sends any obs-websocket request. Responses may contain your stream key; don’t show this on stream.' }),
      h('label', { class: 'field' }, h('span', { class: 'label', text: 'Request' }), name, nameErr),
      h('label', { class: 'field' }, h('span', { class: 'label', text: 'Data (JSON, optional)' }), data, dataErr),
      h('div', { class: 'row' }, asyncButton({ class: 'primary' }, send, icon('send'), 'Send request')),
      out));
}

/* ------------------------------------------------------------------ page */

function headEl() {
  const live = model && model.ok && model.stream.active;
  return h('header', { class: 'page-head' },
    h('div', { class: 'page-titles' }, h('h1', { class: 'page-title', text: 'OBS' }),
      h('p', { class: 'page-sub', text: 'Scenes, audio and outputs for your stream.' })),
    h('div', { class: 'page-actions row' },
      loading ? chip('', 'Connecting…') : model.ok ? chip(true, 'Connected') : chip(false, 'Offline'),
      live ? chip('live', 'LIVE') : null,
      asyncButton({ class: 'ghost', disabled: !(model && model.ok) }, async () => {
        const r = await api('/api/obs/screenshot', {});
        toast(`Screenshot saved: ${r.path}`, { ok: true, ms: 6000 });
      }, icon('camera'), 'Screenshot'),
      asyncButton({ class: 'ghost', disabled: !(model && model.ok && model.replay), title: model && model.ok && !model.replay ? 'Start the replay buffer first' : null }, async () => {
        await api('/api/run', { action: { obs: 'SaveReplayBuffer' } });
        toast('Replay saved', { ok: true });
      }, icon('clip'), 'Save replay')));
}

let bodyMode = '';
let advanced = null;

function render() {
  if (!host) return;
  const mode = loading ? 'loading' : model.ok ? 'ok' : 'off';
  place('head', `${mode}|${mode === 'ok' && model.stream.active}|${mode === 'ok' && model.replay}`, headEl);
  if (mode !== bodyMode) {
    bodyMode = mode;
    let next;
    if (mode === 'loading') next = h('div', { class: 'obs-grid' }, h('div', { class: 'skeleton block', style: { height: '140px' } }), h('div', { class: 'skeleton block', style: { height: '180px' } }));
    else if (mode === 'off') next = h('div', { class: 'obs-grid' }, offlineCard());
    else {
      advanced = advanced || advancedCard();
      next = h('div', { class: 'obs-grid' });
      for (const k of ['outputs', 'scenes', 'mixer', 'perf']) { els[k] = h('div'); sigs[k] = null; next.append(els[k]); }
      next.append(advanced);
    }
    els.body.replaceWith(next);
    els.body = next;
  }
  if (mode === 'ok') {
    const o = (id) => (typeof model[id] === 'object' ? [model[id].active, model[id].paused, model[id].bitrate_kbps] : model[id]);
    place('outputs', JSON.stringify(['stream', 'record', 'replay', 'virtualcam'].map(o)), outputsCard);
    place('scenes', JSON.stringify([model.scene, model.preview, model.studio, model.scenes]), scenesCard);
    mixerRender();
    place('perf', JSON.stringify(model.stats), perfCard);
  }
  tick();
}

function tick() {
  if (!host || !model || !model.ok) return;
  for (const el of host.querySelectorAll('[data-clock]')) el.textContent = fmtDuration(elapsed(el.dataset.clock));
}

/* ------------------------------------------------------------------ lifecycle */

export async function mount(root) {
  host = root;
  const head = h('div'), body = h('div');
  els.head = head; els.body = body;
  root.replaceChildren(h('div', { class: 'page obs' }, head, body));
  on('status', (data) => {
    if (!data || !data.obs) return;
    if (dragging) return;
    const m = fromSnapshot(data.obs);
    if (m.ok || !model || model.ok || loading) setModel(m);
  });
  render();
}

export function show() {
  clearInterval(ticker);
  ticker = setInterval(tick, 1000);
  refresh();
}

export function hide() {
  clearInterval(ticker);
  ticker = null;
}
