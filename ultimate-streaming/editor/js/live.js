/* Live view: the on-air cockpit. A pressable mirror of the deck, go-live/record controls
   with stream health, scenes and audio, the chat feed, BRB games/hype/timers and the
   activity log. Everything updates from the event stream; cards are built once and then
   updated in place (keyed lists), so focus survives the 2-second status pushes. */
import { api, on } from './api.js';
import { h, icon, toast, confirmDialog, empty, safeColor, safeImage, fmtDuration, debounce } from './ui.js';
import { go, snapshot as shellSnapshot, status as shellStatus } from './main.js';

const CHAT_MAX = 300;
const LOG_MAX = 200;
const WARN_LEVELS = new Set(['warning', 'error', 'critical']);
const TIMERS = {
  countdown: { label: 'Starting soon', seconds: 300 },
  brb_timer: { label: 'Back in', seconds: 900 },
};
const BADGES = {   // allow-listed chat badges worth showing, in display order
  broadcaster: ['Host', 'live'], moderator: ['Mod', 'ok'], vip: ['VIP', 'accent'],
  founder: ['Founder', 'accent'], subscriber: ['Sub', 'accent'],
};

const S = {
  visible: false,
  snap: null, snapAt: 0, snapErr: null,
  decks: [],   // attached decks ([info...], primary first); each has a mirror (see MIRRORS)
  chat: [], chatIds: new Set(), chatLoaded: false, chatErr: null, chatDirty: true, chatStuck: true, chatUnseen: 0,
  log: [], logLoaded: false, logErr: null, logDirty: true, logFilter: 'all',
  ticker: null,
};
const E = {};

/* ------------------------------------------------------------------ small helpers */

const now = () => performance.now();
const elapsed = () => (S.snapAt ? (now() - S.snapAt) / 1000 : 0);
const clockTime = (ts, secs) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', ...(secs ? { second: '2-digit' } : {}) }) : '');
const setText = (el, t) => { t = String(t ?? ''); if (el.textContent !== t) el.textContent = t; };
const vis = (el, yes) => { el.hidden = !yes; };
const skel = (...kinds) => kinds.map((k) => h('span', { class: 'skeleton ' + k }));

function setDisabled(btn, off) { if (!btn.classList.contains('busy')) btn.disabled = !!off; }

/** Run fn with btn showing a spinner; errors become toasts. */
async function busy(btn, fn) {
  if (btn.classList.contains('busy')) return;
  btn.classList.add('busy');
  btn.disabled = true;
  try { await fn(); } catch (e) { toast(e.message || String(e), { bad: true }); }
  finally { btn.classList.remove('busy'); btn.disabled = false; renderSnap(); }
}

/** Run a deck action through /api/run (it answers 200 with ok:false on failure). */
async function run(action) {
  const r = await api('/api/run', { action });
  if (!r.ok) throw new Error(r.error || 'That didn’t work');
  return r;
}

/** Reuse child nodes by key so focused buttons stay put across updates. */
function keyed(parent, items, keyOf, make, update) {
  for (const c of Array.from(parent.children)) if (c._k === undefined) c.remove();
  const old = new Map(Array.from(parent.children, (c) => [c._k, c]));
  items.forEach((it, i) => {
    const k = keyOf(it, i);
    let el = old.get(k);
    if (el) old.delete(k); else { el = make(it, i); el._k = k; }
    update(el, it, i);
    if (parent.children[i] !== el) parent.insertBefore(el, parent.children[i] || null);
  });
  for (const el of old.values()) el.remove();
}

function card(area, iconName, iconCls, title, ...actions) {
  const sub = h('p', { class: 'card-sub' });
  const head = h('div', { class: 'card-head' },
    h('div', { class: 'card-icon' + (iconCls ? ' ' + iconCls : '') }, icon(iconName)),
    h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: title }), sub),
    ...actions);
  const body = h('div', { class: 'live-body' });
  const el = h('section', { class: 'card live-card live-' + area, 'aria-label': title }, head, body);
  return { el, head, sub, body };
}

function errorState(text, retry) {
  return h('div', { class: 'live-error', role: 'alert' }, icon('warn'), h('span', { class: 'grow', text }),
    h('button', { class: 'small', onclick: retry }, icon('refresh'), 'Try again'));
}

function settingsLink(text = 'Open Settings') {
  return h('button', { class: 'small', onclick: () => go('settings') }, text, icon('arrowRight'));
}

/* ------------------------------------------------------------------ snapshot accessors */

const obsState = () => (S.snap && S.snap.obs) || null;
const obsOk = () => !!(obsState() && obsState().ok);
const outputs = () => (obsOk() && obsState().outputs) || {};
const streaming = () => !!(outputs().stream && outputs().stream.active);
const twitch = () => (S.snap && S.snap.twitch) || null;
const twitchLinked = () => !!(twitch() && twitch().linked);
const viewers = () => { const l = twitch() && twitch().live; return l && l.live && typeof l.viewers === 'number' ? l.viewers : null; };
function uptime() {
  const s = outputs().stream;
  return s && s.active ? (s.seconds || 0) + elapsed() : null;
}
function droppedPct() {
  const s = outputs().stream;
  return s && s.total ? (100 * (s.dropped || 0)) / s.total : 0;
}

/* ------------------------------------------------------------------ page head */

function buildHead() {
  E.liveChip = h('span', { class: 'chip live', text: 'LIVE', hidden: true });
  E.title = h('h1', { class: 'page-title live-title' }, 'Live', E.liveChip);
  E.sub = h('p', { class: 'page-sub' });
  E.marker = h('button', { class: 'small', onclick: () => busy(E.marker, async () => {
    await api('/api/twitch/marker', {});
    toast('Stream marker added', { ok: true });
  }) }, icon('flag'), 'Marker');
  E.clip = h('button', { class: 'small', onclick: () => busy(E.clip, async () => {
    const r = await api('/api/twitch/clip', {});
    const url = typeof r.url === 'string' && r.url.startsWith('https://') ? r.url : '';
    toast(url ? h('span', {}, 'Clip created. ', h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', text: 'Open it' })) : 'Clip created', { ok: true, ms: 6000 });
  }) }, icon('clip'), 'Clip');
  E.shot = h('button', { class: 'small', onclick: () => busy(E.shot, async () => {
    const r = await api('/api/obs/screenshot', {});
    toast(r.path ? `Screenshot saved to ${r.path}` : 'Screenshot saved', { ok: true });
  }) }, icon('camera'), 'Screenshot');
  return h('div', { class: 'page-head live-head' },
    h('div', { class: 'page-titles' }, E.title, E.sub),
    h('div', { class: 'page-actions' }, E.marker, E.clip, E.shot));
}

function renderHead() {
  vis(E.liveChip, streaming());
  let sub = 'Loading…';
  if (S.snap) {
    if (streaming()) {
      sub = 'Live for ' + fmtDuration(uptime());
      const v = viewers();
      if (v !== null) sub += ` · ${v.toLocaleString()} viewer${v === 1 ? '' : 's'}`;
    } else sub = obsOk() ? 'Off air' : 'Off air · OBS isn’t running';
  } else if (S.snapErr) sub = 'Can’t read the deck’s status';
  setText(E.sub, sub);
  const linked = twitchLinked();
  setDisabled(E.marker, !linked);
  setDisabled(E.clip, !linked);
  E.marker.title = linked ? 'Mark this moment in the VOD' : 'Link Twitch first (Twitch page)';
  E.clip.title = linked ? 'Clip the last 30 seconds' : 'Link Twitch first (Twitch page)';
  setDisabled(E.shot, !obsOk());
  E.shot.title = obsOk() ? 'Save a screenshot of the program output' : 'Start OBS first';
}

/* ------------------------------------------------------------------ broadcast */

function outputRow(name, iconName) {
  const r = {
    dot: h('span', { class: 'dot' }),
    state: h('span', { class: 'out-state' }),
    actions: h('div', { class: 'out-actions' }),
  };
  r.el = h('div', { class: 'out-row' },
    h('span', { class: 'out-icon' }, icon(iconName)),
    h('div', { class: 'out-text' }, h('span', { class: 'out-name', text: name }), h('span', { class: 'out-sub' }, r.dot, r.state)),
    r.actions);
  return r;
}

function setOutput(what, action, btn, done) {
  return busy(btn, async () => {
    await api('/api/obs/output', { what, action });
    if (done) toast(done, { ok: true });
  });
}

function buildBroadcast() {
  const c = card('broadcast', 'live', 'live', 'Broadcast');
  E.bc = c;
  E.bcChip = h('span', { class: 'chip', hidden: true });
  c.head.append(E.bcChip);

  E.bcBanner = h('div', { class: 'banner warn live-offline', hidden: true },
    h('div', { class: 'row nowrap' }, icon('wifiOff'),
      h('div', { class: 'grow' }, h('strong', { text: 'Start OBS to stream' }),
        h('div', { class: 'hint', text: 'The controls light up once OBS is running with its WebSocket server on. That’s OBS’s built-in remote control, which this panel uses to start your stream and switch scenes.' })),
      settingsLink('Settings')));

  E.goLive = h('button', { class: 'golive primary', onclick: async () => {
    if (E.goLive.getAttribute('aria-disabled') === 'true') return;
    const live = streaming();
    const ok = live
      ? await confirmDialog('End the stream?', 'Viewers will see the stream end. Recording, if on, keeps going.', { ok: 'End stream', danger: true })
      : await confirmDialog('Go live on Twitch?', 'OBS starts streaming to your channel right away.', { ok: 'Go live' });
    if (ok) setOutput('stream', live ? 'stop' : 'start', E.goLive, live ? 'Stream ended' : 'Going live…');
  } });
  E.heroState = h('div', { class: 'hero-state' });
  E.heroScene = h('div', { class: 'hero-scene', id: 'golive-hint' });

  E.rec = outputRow('Recording', 'clip');
  E.recBtn = h('button', { class: 'small', onclick: () => setOutput('record', outputs().record && outputs().record.active ? 'stop' : 'start', E.recBtn) });
  E.recPause = h('button', { class: 'small icon-btn', onclick: () => setOutput('record', outputs().record && outputs().record.paused ? 'resume' : 'pause', E.recPause) });
  E.rec.actions.append(E.recPause, E.recBtn);

  E.rep = outputRow('Replay buffer', 'refresh');
  E.repSave = h('button', { class: 'small ghost', onclick: () => busy(E.repSave, async () => { await run({ obs: 'SaveReplayBuffer' }); toast('Replay saved', { ok: true }); }) }, 'Save replay');
  E.repBtn = h('button', { class: 'small' });
  E.repBtn.addEventListener('click', () => setOutput('replay', 'toggle', E.repBtn));
  E.rep.actions.append(E.repSave, E.repBtn);

  E.cam = outputRow('Virtual camera', 'camera');
  E.camBtn = h('button', { class: 'small' });
  E.camBtn.addEventListener('click', () => setOutput('virtualcam', 'toggle', E.camBtn));
  E.cam.actions.append(E.camBtn);

  const stat = (label) => {
    const v = h('div', { class: 'stat-value' }), s = h('div', { class: 'stat-sub' });
    return { v, s, el: h('div', { class: 'stat' }, h('div', { class: 'stat-label', text: label }), v, s) };
  };
  E.st = { up: stat('Uptime'), view: stat('Viewers'), rate: stat('Bitrate'), drop: stat('Dropped'), cpu: stat('CPU'), fps: stat('FPS') };

  E.bcMain = h('div', { class: 'bc-main', hidden: true },
    E.bcBanner,
    h('div', { class: 'bc-top' },
      h('div', { class: 'bc-hero' }, E.heroState, E.goLive, E.heroScene),
      h('div', { class: 'bc-outputs' }, E.rec.el, E.rep.el, E.cam.el)),
    h('div', { class: 'live-stats' }, Object.values(E.st).map((s) => s.el)));
  E.bcSkel = h('div', { class: 'bc-skel' },
    h('div', { class: 'bc-top' }, skel('block'), h('div', {}, skel('line', 'line', 'line'))),
    h('div', { class: 'live-stats' }, [1, 2, 3, 4, 5, 6].map(() => skel('block'))));
  E.bcErr = h('div', { hidden: true });
  c.body.append(E.bcSkel, E.bcErr, E.bcMain);
  return c.el;
}

function renderBroadcast() {
  const has = !!S.snap;
  vis(E.bcSkel, !has && !S.snapErr);
  vis(E.bcErr, !has && !!S.snapErr);
  if (!has && S.snapErr && !E.bcErr.firstChild) E.bcErr.replaceChildren(errorState(S.snapErr, loadSnapshot));
  vis(E.bcMain, has);
  if (!has) { setText(E.bc.sub, 'Stream, record and outputs'); E.bcChip.hidden = true; return; }

  const ok = obsOk(), o = outputs(), live = streaming();
  const st = o.stream || {}, rec = o.record || {};
  E.bc.el.classList.toggle('on-air', live);
  vis(E.bcBanner, !ok);
  E.bcChip.hidden = false;
  E.bcChip.className = 'chip ' + (!ok ? 'bad' : live ? 'live' : 'ok');
  setText(E.bcChip, !ok ? 'OBS offline' : live ? 'LIVE' : 'Ready');
  setText(E.bc.sub, ok ? (obsState().studio ? 'Studio mode on' : 'Connected to OBS') : 'OBS isn’t reachable');

  // hero
  setText(E.heroState, !ok ? 'Not connected' : live ? 'You’re live' : 'You’re off air');
  E.heroState.className = 'hero-state' + (live ? ' live' : '');
  const goKey = live ? 'end' : 'go';
  if (E.goLive.dataset.mode !== goKey) {
    E.goLive.dataset.mode = goKey;
    E.goLive.replaceChildren(icon(live ? 'stop' : 'live'), h('span', { text: live ? 'End stream' : 'Go live' }));
    E.goLive.classList.toggle('primary', !live);
    E.goLive.classList.toggle('danger', live);
  }
  // not `disabled`: it stays focusable, and the hint under it says why it does nothing
  E.goLive.setAttribute('aria-disabled', ok ? 'false' : 'true');
  if (ok) E.goLive.removeAttribute('aria-describedby'); else E.goLive.setAttribute('aria-describedby', 'golive-hint');
  setText(E.heroScene, ok && obsState().scene ? 'Scene: ' + obsState().scene : ok ? 'No scene selected' : 'Start OBS first');

  // outputs
  setText(E.rec.state, !ok ? 'Unavailable' : rec.active
    ? (rec.paused ? 'Paused · ' : 'Recording · ') + fmtDuration((rec.seconds || 0) + (rec.paused ? 0 : elapsed())) : 'Off');
  E.rec.dot.className = 'dot' + (rec.active ? (rec.paused ? ' warn' : ' live') : '');
  const recKey = rec.active ? 'stop' : 'start';
  if (E.recBtn.dataset.mode !== recKey && !E.recBtn.classList.contains('busy')) {
    E.recBtn.dataset.mode = recKey;
    E.recBtn.replaceChildren(icon(rec.active ? 'stop' : 'play'), h('span', { text: rec.active ? 'Stop' : 'Start' }));
  }
  E.recBtn.setAttribute('aria-label', (rec.active ? 'Stop' : 'Start') + ' recording');
  setDisabled(E.recBtn, !ok);
  E.recPause.hidden = !rec.active;
  const pKey = rec.paused ? 'resume' : 'pause';
  if (E.recPause.dataset.mode !== pKey) { E.recPause.dataset.mode = pKey; E.recPause.replaceChildren(icon(rec.paused ? 'play' : 'pause')); }
  E.recPause.setAttribute('aria-label', rec.paused ? 'Resume recording' : 'Pause recording');
  E.recPause.title = rec.paused ? 'Resume recording' : 'Pause recording';

  const toggleRow = (r, btn, active, what) => {
    setText(r.state, !ok ? 'Unavailable' : active ? 'On' : 'Off');
    r.dot.className = 'dot' + (active ? ' ok' : '');
    if (!btn.classList.contains('busy')) setText(btn, active ? 'Stop' : 'Start');
    btn.setAttribute('aria-label', (active ? 'Stop ' : 'Start ') + what);
    setDisabled(btn, !ok);
  };
  toggleRow(E.rep, E.repBtn, !!o.replay, 'replay buffer');
  E.repSave.hidden = !o.replay;
  toggleRow(E.cam, E.camBtn, !!o.virtualcam, 'virtual camera');

  // stats
  const stats = (ok && obsState().stats) || {};
  const tile = (t, value, sub, cls = '') => { setText(t.v, value); setText(t.s, sub); t.v.className = 'stat-value' + (cls ? ' ' + cls : ''); };
  tile(E.st.up, live ? fmtDuration(uptime()) : '—', live ? 'On air' : 'Off air', live ? 'live' : '');
  const v = viewers();
  tile(E.st.view, v !== null ? v.toLocaleString() : '—', !twitchLinked() ? 'Not linked' : v !== null ? 'Watching' : 'Offline');
  tile(E.st.rate, live ? Math.round(st.bitrate_kbps || 0).toLocaleString() : '—', live ? 'kbps' : 'Off air');
  const pct = droppedPct();
  tile(E.st.drop, live ? (pct < 10 ? pct.toFixed(1) : Math.round(pct)) + ' %' : '—',
    live ? `${(st.dropped || 0).toLocaleString()} frames` : 'Off air', live ? (pct >= 5 ? 'bad' : pct >= 1 ? 'warn' : 'ok') : '');
  E.st.drop.el.title = live ? `${(st.dropped || 0).toLocaleString()} of ${(st.total || 0).toLocaleString()} frames dropped` : '';
  tile(E.st.cpu, ok ? `${stats.cpu ?? 0} %` : '—', ok ? `${Math.round(stats.memory_mb || 0).toLocaleString()} MB RAM` : 'OBS offline');
  const skipped = (stats.render_skipped || 0) + (stats.output_skipped || 0);
  tile(E.st.fps, ok ? String(Math.round(stats.fps || 0)) : '—',
    ok ? (skipped ? `${skipped.toLocaleString()} skipped` : 'Smooth') : 'OBS offline', ok && skipped ? 'warn' : '');
  E.st.fps.el.title = ok && skipped ? `${skipped.toLocaleString()} frames skipped while rendering or encoding` : '';
}

/* ------------------------------------------------------------------ deck mirror */
/* One mirror per attached deck, all live at once (side by side when they fit). With one deck
   (or an older daemon without /api/live/decks) there is a single mirror that never sends a
   `deck` field, exactly as before multi-deck support. */

const MIRRORS = new Map();   // serial ('' = the only deck) -> mirror
const multi = () => S.decks.length > 1;

/** "Main XL"; the end of the serial is added when two decks share a name. */
function deckLabel(info) {
  const name = info.name || info.model || 'Stream Deck';
  const twins = S.decks.filter((o) => (o.name || o.model || 'Stream Deck') === name).length > 1;
  return twins ? `${name} · …${String(info.serial || '').slice(-4)}` : name;
}

function buildDeck() {
  const c = card('deck', 'deck', '', 'Deck',
    h('button', { class: 'small ghost', onclick: () => go('deck') }, icon('edit'), 'Edit keys'));
  E.dk = c;
  E.dkUnits = h('div', { class: 'dk-units' });
  E.dkHint = h('span', { class: 'hint dk-hint', text: 'Click a key to press it. Confirm keys need a second press within 3 s.' });
  c.body.append(E.dkUnits);
  return c.el;
}

/** The mirror of one deck: its own page pills, keys, dials and brightness. */
function makeMirror(serial) {
  const m = { serial, data: null, err: null, busy: false, again: false, dirty: true, brightTouched: -Infinity, timers: new Map(), E: {} };
  const X = m.E;
  const withDeck = (body) => (serial ? { deck: serial, ...body } : body);
  m.withDeck = withDeck;
  m.query = (sep) => (serial ? `${sep}deck=${encodeURIComponent(serial)}` : '');
  X.name = h('h3', { class: 'dk-unit-name' });
  X.meta = h('span', { class: 'dk-unit-meta' });
  X.head = h('div', { class: 'dk-unit-head', hidden: true }, icon('deck'), X.name, X.meta);
  X.pills = h('div', { class: 'pills', role: 'tablist', 'aria-label': 'Deck pages' });
  X.back = h('button', { class: 'small ghost', onclick: () => switchPage(m, 'back') }, icon('undo'), 'Back');
  X.keys = h('div', { class: 'lk-grid', role: 'group', 'aria-label': 'Deck keys' });
  X.dials = h('div', { class: 'lk-dials', hidden: true });
  X.bright = h('input', { type: 'range', min: 0, max: 100, step: 5, 'aria-label': 'Deck brightness' });
  X.brightVal = h('span', { class: 'bright-val' });
  const sendBright = debounce((v) => api('/api/live/brightness', withDeck({ level: v })).catch((e) => toast(e.message, { bad: true })), 150);
  X.bright.addEventListener('input', () => { m.brightTouched = now(); setText(X.brightVal, X.bright.value + ' %'); sendBright(Number(X.bright.value)); });
  X.foot = h('div', { class: 'dk-foot' },
    h('label', { class: 'bright' }, icon('sun'), h('span', { class: 'bright-label', text: 'Brightness' }), X.bright, X.brightVal));
  X.main = h('div', { class: 'dk-main', hidden: true },
    h('div', { class: 'dk-bar' }, X.pills, X.back),
    h('div', { class: 'lk-stage' }, h('div', { class: 'lk-bezel' }, X.keys, X.dials)),
    X.foot);
  X.skel = h('div', { class: 'dk-skel' }, h('div', { class: 'row' }, skel('line pill-skel', 'line pill-skel', 'line pill-skel')), skel('block lk-skel'));
  X.other = h('div', { hidden: true });
  m.el = h('section', { class: 'dk-unit' }, X.head, X.skel, X.other, X.main);
  m.refreshSoon = debounce(() => refreshDeck(m), 120);
  return m;
}

/** Make the mirrors match the attached decks (keeps the ones that are still there). */
function syncMirrors() {
  const want = multi() ? S.decks.map((d) => d.serial) : [''];
  for (const [k, m] of MIRRORS) {
    if (!want.includes(k)) { for (const t of m.timers.values()) clearTimeout(t); m.el.remove(); MIRRORS.delete(k); }
  }
  want.forEach((k, i) => {
    let m = MIRRORS.get(k);
    if (!m) { m = makeMirror(k); MIRRORS.set(k, m); }
    if (E.dkUnits.children[i] !== m.el) E.dkUnits.insertBefore(m.el, E.dkUnits.children[i] || null);
  });
  E.dkUnits.classList.toggle('multi', multi());
  // one shared hint under several decks; inside the footer with one, as before
  const only = MIRRORS.get('');
  if (only) only.E.foot.append(E.dkHint); else E.dk.body.append(E.dkHint);
  for (const m of MIRRORS.values()) renderDeck(m);
}

function setDecks(list) {
  S.decks = Array.isArray(list) ? list.filter((d) => d && d.serial) : [];
  syncMirrors();
}

async function loadDecks() {
  try {
    const r = await api('/api/live/decks');
    setDecks(r.decks);
  } catch (e) {
    // an older daemon has no /api/live/decks: its single /api/live/deck is the only deck
    if (e.status !== 404) toast('Couldn’t list the decks: ' + e.message, { bad: true });
    setDecks([]);
  }
}

/** The mirror an event is for (events without `deck` come from a single-deck daemon). */
function mirrorFor(d) {
  if (!multi()) return MIRRORS.get('');
  return MIRRORS.get(d && d.deck ? d.deck : S.decks[0].serial);
}

async function switchPage(m, page) {
  try { await api('/api/live/page', m.withDeck({ page })); } catch (e) { toast(e.message, { bad: true }); }
  refreshDeck(m);
}

async function press(m, k, btn) {
  const d = m.data;
  if (!d) return;
  btn.classList.remove('pressed');
  void btn.offsetWidth;  // restart the press animation
  btn.classList.add('pressed');
  try {
    await api('/api/live/press', m.withDeck({ page: d.page, key: k.index }));
    if (k.confirm) setTimeout(() => refreshDeck(m), k.armed ? 300 : 3300);  // the armed state comes and goes
  } catch (e) {
    if (e.status === 409) { toast('The deck switched page, so the keys were refreshed.'); refreshDeck(m); }
    else toast(e.message, { bad: true });
  }
}

const keyLabel = (k) => (k.label || '').replace(/\s+/g, ' ').trim();

function renderSub() {
  const only = MIRRORS.get('');
  if (!only) { setText(E.dk.sub, `${S.decks.length} decks · live mirrors`); return; }
  const d = only.data;
  if (only.err || !d) setText(E.dk.sub, 'Live mirror of your Stream Deck');
  else if (!d.attached) setText(E.dk.sub, 'Waiting for the hardware');
  else setText(E.dk.sub, `${d.model || 'Stream Deck'} · page “${d.page}”`);
}

function renderDeck(m) {
  const d = m.data, X = m.E;
  const info = m.serial && S.decks.find((x) => x.serial === m.serial);
  vis(X.head, !!info);
  if (info) {
    setText(X.name, deckLabel(info));
    setText(X.meta, [info.name && info.name !== info.model ? info.model : '', d && d.page ? `page “${d.page}”` : ''].filter(Boolean).join(' · '));
    m.el.style.setProperty('--cols', info.cols || 8);
    m.el.setAttribute('aria-label', deckLabel(info));
  } else if (d && d.cols) m.el.style.setProperty('--cols', d.cols);
  renderSub();
  vis(X.skel, !d && !m.err);
  vis(X.other, !!m.err || !!(d && !d.attached));
  vis(X.main, !!(d && d.attached) && !m.err);
  if (m.err) { X.other.replaceChildren(errorState(m.err, () => { m.err = null; renderDeck(m); refreshDeck(m); })); return; }
  if (!d) return;
  if (!d.attached) {
    X.other.replaceChildren(empty('No Stream Deck connected', 'Plug it in and it shows up here, live.', 'deck'));
    return;
  }

  keyed(X.pills, d.pages || [], (p) => p,
    (p) => h('button', { role: 'tab', text: p, onclick: () => { if (!m.data || p !== m.data.page) switchPage(m, p); } }),
    (el, p) => el.setAttribute('aria-selected', p === d.page ? 'true' : 'false'));
  X.back.disabled = !d.can_back;

  const [kw, kh] = d.key_size && d.key_size[0] ? d.key_size : [1, 1];
  X.keys.style.setProperty('--cols', d.cols || 8);
  X.keys.style.setProperty('--kw', kw);
  X.keys.style.setProperty('--kh', kh);
  if (info) X.keys.setAttribute('aria-label', `${deckLabel(info)} keys`);
  keyed(X.keys, d.keys || [], (k) => k.index,
    () => {
      const b = h('button', { class: 'lk' }, h('img', { alt: '', draggable: 'false', hidden: true }));
      b.addEventListener('click', () => { const cur = m.data && m.data.keys[b._k]; if (cur) press(m, cur, b); });
      b.addEventListener('animationend', (e) => { if (e.animationName === 'lk-press') b.classList.remove('pressed'); });
      return b;
    },
    (b, k) => {
      const img = b.firstChild, src = safeImage(k.image);
      if (src && img.getAttribute('src') !== src) img.src = src;
      img.hidden = !src;
      const label = keyLabel(k);
      b.disabled = !k.has_action;
      b.classList.toggle('blank', !src && !label);
      b.classList.toggle('armed', !!k.armed);
      b.classList.toggle('active', !!k.active);
      b.setAttribute('aria-label', `Key ${k.index + 1}: ${label || (k.has_action ? 'unlabelled' : 'empty')}`
        + (k.armed ? ' (press again to confirm)' : k.confirm ? ' (asks to confirm)' : ''));
      b.title = label ? label + (k.armed ? ' · press again to confirm' : k.confirm ? ' · press twice' : '') : '';
    });

  const dials = d.dials || [];
  vis(X.dials, dials.length > 0);
  keyed(X.dials, dials, (x) => x.index,
    () => h('div', { class: 'lk-dial' }, h('span', { class: 'lk-knob', 'aria-hidden': 'true' }), h('span', { class: 'lk-dial-label' })),
    (el, x) => setText(el.lastChild, x.label || `Dial ${x.index + 1}`));

  if (now() - m.brightTouched > 1500) {
    X.bright.value = d.brightness ?? 0;
    setText(X.brightVal, (d.brightness ?? 0) + ' %');
  }
  X.bright.setAttribute('aria-label', info ? `${deckLabel(info)} brightness` : 'Deck brightness');
}

async function refreshDeck(m) {
  if (!S.visible) { m.dirty = true; return; }
  if (m.busy) { m.again = true; return; }
  m.busy = true;
  m.dirty = false;
  try {
    m.data = await api('/api/live/deck' + m.query('?'));
    m.err = null;
  } catch (e) {
    if (e.status === 404 && m.serial) { m.busy = false; loadDecks(); return; }   // unplugged meanwhile
    if (!m.data) m.err = e.message || 'Couldn’t load the deck';
    else toast('Couldn’t refresh the deck: ' + e.message, { bad: true });
  }
  m.busy = false;
  if (MIRRORS.get(m.serial) !== m) return;   // removed while loading
  renderDeck(m);
  if (m.again) { m.again = false; refreshDeck(m); }
}

/** A `key` event: refetch just that key (per-key debounce); many at once fall back to one full refetch. */
function refreshKeySoon(m, index) {
  if (!Number.isInteger(index)) { m.refreshSoon(); return; }
  if (m.timers.has(index)) return;
  if (m.timers.size >= 8) {
    for (const t of m.timers.values()) clearTimeout(t);
    m.timers.clear();
    m.refreshSoon();
    return;
  }
  m.timers.set(index, setTimeout(() => refreshKey(m, index), 120));
}
async function refreshKey(m, index) {
  m.timers.delete(index);
  const d = m.data;
  if (!S.visible || !d || !d.attached || m.busy) { if (S.visible) m.refreshSoon(); return; }
  try {
    const r = await api(`/api/live/key?page=${encodeURIComponent(d.page)}&key=${index}${m.query('&')}`);
    if (m.data !== d || !r || !r.key || !d.keys) return;
    const at = d.keys.findIndex((k) => k.index === index);
    if (at < 0) return;
    d.keys[at] = r.key;
    renderDeck(m);
  } catch (e) {
    m.refreshSoon();
  }
}

function onKeyEvent(d) {
  const m = mirrorFor(d);
  if (!m) return;
  if (m.data && d && d.page && d.page !== m.data.page) return;
  if (!S.visible) m.dirty = true;
  else if (m.data && m.data.attached && d) refreshKeySoon(m, d.key);
  else m.refreshSoon();
}
function onPageEvent(d) {
  const m = mirrorFor(d);
  if (!m) return;
  if (S.visible) m.refreshSoon(); else m.dirty = true;
}
function onDecksEvent(d) {
  setDecks(d && d.decks);
  for (const m of MIRRORS.values()) { if (S.visible) m.refreshSoon(); else m.dirty = true; }
}

/* ------------------------------------------------------------------ scenes & audio */

function buildScenes() {
  const c = card('scenes', 'layers', '', 'Scenes');
  E.sc = c;
  E.scGrid = h('div', { class: 'scene-grid' });
  E.audio = h('div', { class: 'audio-grid' });
  E.scMain = h('div', { hidden: true }, E.scGrid, h('h3', { class: 'subhead', text: 'Audio' }), E.audio);
  E.scOther = h('div', { hidden: true });
  E.scSkel = h('div', { class: 'scene-grid' }, [1, 2, 3, 4, 5, 6].map(() => skel('block scene-skel')));
  c.body.append(E.scSkel, E.scOther, E.scMain);
  return c.el;
}

const isMic = (inp) => /input_capture|_input$|wasapi_input|coreaudio_input/.test(inp.kind || '') || /mic/i.test(inp.name || '');

function renderScenes() {
  const has = !!S.snap, ok = obsOk();
  vis(E.scSkel, !has && !S.snapErr);
  vis(E.scOther, (has && !ok) || (!has && !!S.snapErr));
  vis(E.scMain, has && ok);
  if (!has) {
    setText(E.sc.sub, 'Switch what viewers see');
    if (S.snapErr && !E.scOther.firstChild) E.scOther.replaceChildren(errorState(S.snapErr, loadSnapshot));
    return;
  }
  if (!ok) {
    setText(E.sc.sub, 'OBS isn’t reachable');
    if (!E.scOther.querySelector('.empty')) {
      const e = empty('Scenes show up when OBS runs', 'Start OBS and turn on its WebSocket server.', 'layers');
      e.append(h('div', { class: 'mt-2' }, settingsLink()));
      E.scOther.replaceChildren(e);
    }
    return;
  }
  const o = obsState();
  setText(E.sc.sub, o.studio ? `Studio mode · preview “${o.preview || '—'}”` : (o.scene ? `On air: ${o.scene}` : 'Switch what viewers see'));
  const scenes = o.scenes || [];
  if (!scenes.length) {
    E.scGrid.replaceChildren(h('p', { class: 'hint', text: 'This scene collection has no scenes yet.' }));
  } else {
    keyed(E.scGrid, scenes, (n) => n,
      (n) => {
        const b = h('button', { class: 'scene-btn', title: n }, h('span', { class: 'scene-name', text: n }));
        b.addEventListener('click', () => busy(b, () => api('/api/obs/scene', { name: n })));
        return b;
      },
      (b, n) => {
        b.setAttribute('aria-pressed', n === o.scene ? 'true' : 'false');
        b.classList.toggle('preview', !!o.studio && n === o.preview && n !== o.scene);
      });
  }
  const inputs = o.inputs || [];
  if (!inputs.length) E.audio.replaceChildren(h('p', { class: 'hint', text: 'No audio sources in OBS.' }));
  else {
    keyed(E.audio, inputs, (i) => i.name,
      (i) => {
        const b = h('button', { class: 'audio-btn' }, h('span', { class: 'audio-ic' }), h('span', { class: 'audio-text' },
          h('span', { class: 'audio-name', text: i.name }), h('span', { class: 'audio-state' })));
        b.addEventListener('click', () => {
          const cur = ((obsState() || {}).inputs || []).find((x) => x.name === i.name);
          busy(b, () => api('/api/obs/input', { name: i.name, muted: !(cur && cur.muted) }));
        });
        return b;
      },
      (b, i) => {
        const mic = isMic(i);
        b.setAttribute('aria-pressed', i.muted ? 'true' : 'false');
        b.setAttribute('aria-label', `${i.muted ? 'Unmute' : 'Mute'} ${i.name}`);
        b.title = i.muted ? 'Muted: click to unmute' : 'Click to mute';
        b.classList.toggle('muted', !!i.muted);
        const ic = b.querySelector('.audio-ic'), want = mic ? (i.muted ? 'micOff' : 'mic') : (i.muted ? 'x' : 'volume');
        if (ic.dataset.ic !== want) { ic.dataset.ic = want; ic.replaceChildren(icon(want)); }
        const db = typeof i.volume_db === 'number' && isFinite(i.volume_db) ? `${Math.round(i.volume_db)} dB` : '';
        setText(b.querySelector('.audio-state'), i.muted ? 'Muted' : db || 'On');
      });
  }
}

/* ------------------------------------------------------------------ chat */

function buildChat() {
  const c = card('chat', 'twitch', 'twitch', 'Chat');
  E.ch = c;
  E.chChip = h('span', { class: 'chip', hidden: true });
  c.head.append(E.chChip);
  E.feed = h('div', { class: 'chat-feed scroll', role: 'log', 'aria-label': 'Chat messages', tabindex: '0' });
  E.feed.addEventListener('scroll', () => {
    const stuck = E.feed.scrollHeight - E.feed.scrollTop - E.feed.clientHeight < 40;
    if (stuck !== S.chatStuck) { S.chatStuck = stuck; if (stuck) S.chatUnseen = 0; renderChatPill(); }
  }, { passive: true });
  E.more = h('button', { class: 'chat-more small primary', hidden: true, onclick: () => { S.chatStuck = true; S.chatUnseen = 0; scrollChat(); renderChatPill(); } });
  c.body.append(h('div', { class: 'chat-wrap' }, E.feed, E.more));
  c.el.append(h('div', { class: 'chat-foot' },
    h('button', { class: 'small ghost', onclick: () => go('twitch') }, 'Moderate on the Twitch page', icon('arrowRight'))));
  E.feed.append(...skel('line', 'line chat-skel-2', 'line', 'line chat-skel-2', 'line'));
  return c.el;
}

function renderChatHead() {
  const ch = S.snap && S.snap.chat;
  if (!ch) { E.chChip.hidden = true; setText(E.ch.sub, S.snap ? 'Chat reader is off' : 'Live chat'); return; }
  E.chChip.hidden = false;
  E.chChip.className = 'chip ' + (ch.connected ? 'ok' : 'bad');
  setText(E.chChip, ch.connected ? 'Connected' : 'Offline');
  setText(E.ch.sub, ch.channel ? '#' + ch.channel : 'No channel set');
}

function msgEl(m) {
  const names = Array.isArray(m.badges) ? m.badges.map(String) : [];
  if (!names.length) { if (m.mod) names.push('moderator'); if (m.sub) names.push('subscriber'); }
  const badges = Object.keys(BADGES).filter((b) => names.includes(b))
    .map((b) => h('span', { class: 'badge ' + BADGES[b][1], text: BADGES[b][0] }));
  const name = h('span', { class: 'msg-name', text: String(m.user || 'someone') });
  const c = safeColor(m.color);
  if (c) name.style.color = c;
  return h('div', { class: 'msg', title: m.ts ? new Date(m.ts * 1000).toLocaleString() : null },
    h('span', { class: 'msg-time', text: clockTime(m.ts) }),
    badges.length ? h('span', { class: 'msg-badges' }, badges) : null,
    name, h('span', { class: 'msg-sep', text: ': ' }),
    h('span', { class: 'msg-text', text: String(m.text ?? '') }));
}

function chatEmpty() {
  const ch = S.snap && S.snap.chat;
  if (ch && !ch.connected) {
    const e = empty('Chat isn’t connected', ch.channel ? 'Reconnecting to Twitch chat…' : 'Set your channel on the Twitch page to read chat here.', 'twitch');
    if (!ch.channel) e.append(h('div', { class: 'mt-2' }, h('button', { class: 'small', onclick: () => go('twitch') }, 'Twitch page', icon('arrowRight'))));
    return e;
  }
  return empty('No messages yet', 'Chat shows up here as it arrives.', 'twitch');
}

function scrollChat() { E.feed.scrollTop = E.feed.scrollHeight; }

function renderChatPill() {
  E.more.hidden = S.chatStuck || !S.chatUnseen;
  setText(E.more, `${S.chatUnseen} new message${S.chatUnseen === 1 ? '' : 's'} ↓`);
}

function renderChatFull() {
  S.chatDirty = false;
  if (S.chatErr && !S.chatLoaded) { E.feed.replaceChildren(errorState(S.chatErr, loadChat)); return; }
  if (!S.chatLoaded) return;
  if (!S.chat.length) { E.feed.replaceChildren(chatEmpty()); E.feed.dataset.empty = '1'; return; }
  delete E.feed.dataset.empty;
  E.feed.replaceChildren(...S.chat.map(msgEl));
  if (S.chatStuck) scrollChat();
}

function addChat(m) {
  if (m.id && S.chatIds.has(m.id)) return;
  if (m.id) S.chatIds.add(m.id);
  S.chat.push(m);
  while (S.chat.length > CHAT_MAX) { const old = S.chat.shift(); if (old.id) S.chatIds.delete(old.id); }
  if (!S.visible || !S.chatLoaded) { S.chatDirty = true; return; }
  if (E.feed.dataset.empty) { delete E.feed.dataset.empty; E.feed.replaceChildren(); }
  E.feed.append(msgEl(m));
  while (E.feed.children.length > CHAT_MAX) E.feed.firstChild.remove();
  if (S.chatStuck) scrollChat();
  else { S.chatUnseen++; renderChatPill(); }
}

async function loadChat() {
  try {
    const r = await api('/api/live/chat?n=150');
    const pending = S.chat;
    S.chat = [];
    S.chatIds.clear();
    S.chatLoaded = true;
    S.chatErr = null;
    for (const m of [...(r.messages || []), ...pending]) addChat(m);
  } catch (e) { S.chatErr = e.message || 'Couldn’t load chat'; }
  if (S.visible) renderChatFull(); else S.chatDirty = true;
}

/* ------------------------------------------------------------------ BRB */

function buildBrb() {
  const c = card('brb', 'clock', '', 'BRB screen');
  E.brb = c;
  // game
  E.gameTitle = h('div', { class: 'brb-q' });
  E.gameMeta = h('span', { class: 'hint' });
  E.gameOpts = h('div', { class: 'poll-opts' });
  E.gameWinner = h('div', { class: 'banner ok brb-winner', hidden: true });
  E.gameEnd = h('button', { class: 'small danger' }, 'End game');
  E.gameEnd.addEventListener('click', () => busy(E.gameEnd, async () => { await run({ brb_game: 'end' }); toast('Game ended', { ok: true }); }));
  E.secGame = h('div', { class: 'brb-sec' },
    h('div', { class: 'brb-row' }, h('h3', { class: 'brb-h', text: 'Game' }), E.gameMeta, h('span', { class: 'grow' }), E.gameEnd),
    E.gameTitle, E.gameOpts, E.gameWinner);
  // hype
  E.hypeVal = h('span', { class: 'hype-val' });
  E.hypeBar = h('span', {});
  E.hypeTrack = h('div', { class: 'progress hype-track', role: 'progressbar', 'aria-label': 'Hype', 'aria-valuemin': '0' }, E.hypeBar);
  E.hypeReset = h('button', { class: 'small ghost' }, 'Reset hype');
  E.hypeReset.addEventListener('click', () => busy(E.hypeReset, () => run({ brb_hype: 'reset' })));
  E.secHype = h('div', { class: 'brb-sec' },
    h('div', { class: 'brb-row' }, h('h3', { class: 'brb-h', text: 'Hype' }), E.hypeVal, h('span', { class: 'grow' }), E.hypeReset), E.hypeTrack);
  // timers
  E.timers = h('div', { class: 'timer-list' });
  E.secTimers = h('div', { class: 'brb-sec' }, h('h3', { class: 'brb-h', text: 'Timers' }), E.timers);
  // radio
  E.radioText = h('span', { class: 'out-state' });
  E.radioBtn = h('button', { class: 'small' });
  E.radioBtn.addEventListener('click', () => busy(E.radioBtn, () => run({ radio: 'toggle' })));
  E.secRadio = h('div', { class: 'brb-sec out-row brb-radio' }, h('span', { class: 'out-icon' }, icon('volume')),
    h('div', { class: 'out-text' }, h('span', { class: 'out-name', text: 'Radio' }), h('span', { class: 'out-sub' }, E.radioText)), E.radioBtn);
  E.brbMain = h('div', { class: 'brb-main', hidden: true }, E.secGame, E.secHype, E.secTimers, E.secRadio);
  E.brbOther = h('div', { hidden: true });
  E.brbSkel = h('div', {}, skel('line', 'line', 'block'));
  c.body.append(E.brbSkel, E.brbOther, E.brbMain);
  return c.el;
}

const TIMER_ORDER = Object.keys(TIMERS);
const timerDef = (name) => TIMERS[name] || { label: name.replace(/_/g, ' '), seconds: 300 };
function timerAction(name, start) {
  const d = timerDef(name);
  if (name === 'countdown') return { countdown: start ? d.seconds : 'stop' };
  return { countdown: start ? { name, seconds: d.seconds } : { name, stop: true } };
}
const timerLeft = (v) => (typeof v === 'number' ? Math.max(0, Math.ceil(v - elapsed())) : null);
const timerRank = (n) => (TIMER_ORDER.includes(n) ? TIMER_ORDER.indexOf(n) : 99);

function renderTimers() {
  const tm = (S.snap && S.snap.overlay && S.snap.overlay.timers) || {};
  const names = Object.keys(tm).sort((a, b) => timerRank(a) - timerRank(b) || a.localeCompare(b));
  if (!names.length) { E.timers.replaceChildren(h('p', { class: 'hint', text: 'No timers set up.' })); return; }
  keyed(E.timers, names, (n) => n,
    (n) => {
      const b = h('button', { class: 'small' });
      b.addEventListener('click', () => {
        const running = timerLeft(((S.snap.overlay || {}).timers || {})[n]) !== null;
        busy(b, () => run(timerAction(n, !running)));
      });
      return h('div', { class: 'timer' },
        h('div', { class: 'timer-text' }, h('span', { class: 'timer-name', text: timerDef(n).label }), h('span', { class: 'timer-state' })),
        h('span', { class: 'timer-left' }), b);
    },
    (r, n) => {
      const left = timerLeft(tm[n]);
      const running = left !== null;
      r.classList.toggle('running', running && left > 0);
      r.classList.toggle('done', running && left === 0);
      setText(r.querySelector('.timer-left'), fmtDuration(running ? left : timerDef(n).seconds));
      setText(r.querySelector('.timer-state'), !running ? 'Not running' : left > 0 ? 'Counting down' : 'Done');
      const b = r.lastChild, mode = running ? 'stop' : 'start';
      if (b.dataset.mode !== mode && !b.classList.contains('busy')) {
        b.dataset.mode = mode;
        b.replaceChildren(icon(running ? 'stop' : 'play'), h('span', { text: running ? 'Stop' : 'Start ' + fmtDuration(timerDef(n).seconds) }));
      }
      b.setAttribute('aria-label', `${running ? 'Stop' : 'Start'} the “${timerDef(n).label}” timer`);
    });
}

function renderBrb() {
  const has = !!S.snap;
  const brb = has && S.snap.brb, ov = has && S.snap.overlay, radio = has && S.snap.radio;
  const any = !!(brb || ov || radio);
  vis(E.brbSkel, !has && !S.snapErr);
  vis(E.brbOther, (!has && !!S.snapErr) || (has && !any));
  vis(E.brbMain, has && any);
  if (!has) {
    setText(E.brb.sub, 'Games, hype and timers');
    if (S.snapErr && !E.brbOther.firstChild) E.brbOther.replaceChildren(errorState(S.snapErr, loadSnapshot));
    return;
  }
  if (!any) { E.brbOther.replaceChildren(empty('BRB tools are off', 'Turn on the brb and overlay modules in config.yaml.', 'clock')); return; }
  setText(E.brb.sub, brb && brb.video ? 'Playing: ' + brb.video.name + (brb.video.credit ? ' · ' + brb.video.credit : '') : 'Games, hype and timers');

  vis(E.secGame, !!brb);
  vis(E.secHype, !!brb);
  if (brb) {
    const g = brb.game || {};
    E.gameEnd.hidden = !g.kind;
    if (g.kind === 'poll') {
      setText(E.gameTitle, g.question || 'Poll');
      setText(E.gameMeta, `Poll · ${g.total_votes || 0} vote${g.total_votes === 1 ? '' : 's'}`);
      const total = g.total_votes || 0, opts = g.options || [];
      const top = Math.max(0, ...opts.map((o) => o.votes || 0));
      keyed(E.gameOpts, opts, (o, i) => i + ':' + o.label,
        (o, i) => h('div', { class: 'poll-opt' },
          h('div', { class: 'poll-line' }, h('span', { class: 'poll-num', text: String(i + 1) }),
            h('span', { class: 'poll-label', text: String(o.label) }), h('span', { class: 'poll-votes' })),
          h('div', { class: 'progress' }, h('span', {}))),
        (el, o) => {
          const pct = total ? Math.round((100 * (o.votes || 0)) / total) : 0;
          setText(el.querySelector('.poll-votes'), `${o.votes || 0} · ${pct} %`);
          el.querySelector('.progress > span').style.width = pct + '%';
          el.classList.toggle('lead', top > 0 && (o.votes || 0) === top);
        });
    } else if (g.kind === 'guess') {
      setText(E.gameTitle, g.question || 'Guessing game');
      setText(E.gameMeta, `Guessing · ${g.guesses || 0} guess${g.guesses === 1 ? '' : 'es'}`);
      E.gameOpts.replaceChildren();
    } else {
      setText(E.gameTitle, '');
      setText(E.gameMeta, 'None running');
      if (!E.gameOpts.querySelector('.hint')) E.gameOpts.replaceChildren(h('p', { class: 'hint', text: 'Start a poll or guessing game from the deck’s BRB page.' }));
    }
    E.gameTitle.hidden = !g.kind;
    E.gameWinner.hidden = !g.winner;
    if (g.winner) E.gameWinner.replaceChildren(h('div', { class: 'row nowrap' }, icon('check'), h('span', { text: String(g.winner) })));

    const hy = brb.hype || {}, max = hy.max || 100, val = Math.max(0, Math.min(max, hy.value || 0));
    const pct = Math.round((100 * val) / max);
    const full = !!(hy.full_until && hy.full_until * 1000 > Date.now()) || pct >= 100;
    E.hypeBar.style.width = pct + '%';
    E.hypeTrack.setAttribute('aria-valuemax', String(max));
    E.hypeTrack.setAttribute('aria-valuenow', String(Math.round(val)));
    E.secHype.classList.toggle('full', full);
    setText(E.hypeVal, full ? `Full! ${pct} %` : `${pct} %`);
  }

  vis(E.secTimers, !!ov);
  if (ov) renderTimers();

  vis(E.secRadio, !!radio);
  if (radio) {
    setText(E.radioText, radio.on ? (radio.now_playing || (radio.station ? radio.station + ' · on' : 'On')) : 'Off' + (radio.station ? ' · ' + radio.station : ''));
    if (!E.radioBtn.classList.contains('busy')) setText(E.radioBtn, radio.on ? 'Turn off' : 'Turn on');
    E.radioBtn.setAttribute('aria-label', radio.on ? 'Turn the radio off' : 'Turn the radio on');
  }
}

/* ------------------------------------------------------------------ activity log */

function buildLog() {
  const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Filter activity' });
  for (const [v, t] of [['all', 'All'], ['warn', 'Warnings']]) {
    seg.append(h('button', { 'aria-pressed': v === S.logFilter ? 'true' : 'false', text: t, onclick: (e) => {
      S.logFilter = v;
      for (const b of seg.children) b.setAttribute('aria-pressed', b === e.currentTarget ? 'true' : 'false');
      renderLogFull();
    } }));
  }
  const c = card('activity', 'info', '', 'Activity', seg);
  E.lg = c;
  setText(c.sub, 'What the deck has been up to');
  E.logList = h('ol', { class: 'log-list scroll', 'aria-label': 'Activity log' });
  E.logList.append(...[1, 2, 3, 4].map(() => h('li', {}, skel('line'))));
  c.body.append(E.logList);
  return c.el;
}

const logShown = (l) => S.logFilter === 'all' || WARN_LEVELS.has(l.level);
function logEl(l) {
  const lv = String(l.level || 'info');
  const cls = lv === 'error' || lv === 'critical' ? 'bad' : lv === 'warning' ? 'warn' : '';
  return h('li', { class: 'log-line' + (cls ? ' ' + cls : ''), title: l.logger ? `${l.logger} · ${lv}` : lv },
    h('span', { class: 'dot' + (cls ? ' ' + cls : '') }),
    h('time', { class: 'log-time', text: clockTime(l.ts, true) }),
    h('span', { class: 'log-msg', text: String(l.msg ?? '') }));
}

function renderLogFull() {
  S.logDirty = false;
  if (S.logErr && !S.logLoaded) { E.logList.replaceChildren(h('li', {}, errorState(S.logErr, loadLog))); return; }
  if (!S.logLoaded) return;
  const rows = S.log.filter(logShown).reverse();
  if (!rows.length) {
    E.logList.replaceChildren(h('li', { class: 'log-empty' }, S.logFilter === 'all'
      ? empty('Nothing yet', 'Key presses, connections and errors show up here.', 'info')
      : empty('No warnings', 'All quiet. Problems show up here.', 'check')));
    return;
  }
  E.logList.replaceChildren(...rows.map(logEl));
}

function addLog(l) {
  S.log.push(l);
  if (S.log.length > LOG_MAX) S.log.shift();
  if (!S.visible || !S.logLoaded) { S.logDirty = true; return; }
  if (!logShown(l)) return;
  if (E.logList.querySelector('.log-empty, .live-error')) E.logList.replaceChildren();
  E.logList.prepend(logEl(l));
  while (E.logList.children.length > LOG_MAX) E.logList.lastChild.remove();
}

async function loadLog() {
  try {
    const r = await api('/api/live/log?n=100');
    const seen = new Set(S.log.map((l) => l.ts + '|' + l.msg));
    S.log = [...(r.lines || []).filter((l) => !seen.has(l.ts + '|' + l.msg)), ...S.log].slice(-LOG_MAX);
    S.logLoaded = true;
    S.logErr = null;
  } catch (e) { S.logErr = e.message || 'Couldn’t load the activity log'; }
  if (S.visible) renderLogFull(); else S.logDirty = true;
}

/* ------------------------------------------------------------------ plugin cards */

const OWN_KEYS = new Set(['obs', 'twitch', 'overlay', 'brb', 'radio', 'chat', 'deck', 'twitch_bot', 'bot']);   // rendered by their own cards
const CARD_TONES = { ok: 'ok', warn: 'warn', error: 'bad', idle: '' };
const CARD_TONE_LABEL = { ok: 'OK', warn: 'Warning', error: 'Error', idle: 'Idle' };
const CARD_ICONS = new Set(['live', 'deck', 'twitch', 'obs', 'settings', 'camera', 'link', 'clip', 'flag', 'clock', 'grid', 'layers', 'monitor', 'bolt', 'eye', 'code', 'dial', 'bot', 'sparkle', 'mic', 'volume', 'key', 'play', 'send']);

/** The validated `card` of a snapshot entry, or null (invalid shapes are ignored). */
function pluginCard(key, mod) {
  const c = mod && typeof mod === 'object' ? mod.card : null;
  if (c === undefined || c === null) return null;
  const str = (v, n) => typeof v === 'string' && v.length <= n;
  const rows = c && c.rows;
  const ok = c && typeof c === 'object' && str(c.title, 40) && c.title && c.tone in CARD_TONES
    && Array.isArray(rows) && rows.length <= 8
    && rows.every((r) => Array.isArray(r) && r.length === 2 && typeof r[0] === 'string' && typeof r[1] === 'string')
    && (c.note === undefined || c.note === null || typeof c.note === 'string')
    && (c.icon === undefined || c.icon === null || typeof c.icon === 'string')
    && (c.progress === undefined || c.progress === null || (typeof c.progress === 'number' && Number.isFinite(c.progress)));
  if (!ok) { console.debug('live: ignoring invalid card for', key); return null; }
  return c;
}

function buildPlugins() {
  E.plugins = h('div', { class: 'live-plugins', hidden: true },
    h('h2', { class: 'live-plugins-h', text: 'Plugins' }));
  E.pluginList = h('div', { class: 'live-plugins-list' });
  E.plugins.append(E.pluginList);
  return E.plugins;
}

function makePluginCard() {
  const c = card('plugin', 'bolt', '', '');
  c.title = c.head.querySelector('.card-title');
  c.icoBox = c.head.querySelector('.card-icon');
  c.badge = h('span', { class: 'badge' });
  c.head.append(c.badge);
  c.bar = h('span');
  c.prog = h('div', { class: 'progress plugin-progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': 'Progress', hidden: true }, c.bar);
  c.rows = h('dl', { class: 'plugin-rows' });
  c.note = h('p', { class: 'hint plugin-note' });
  c.body.append(c.prog, c.rows, c.note);
  return c;
}

function renderPlugins() {
  if (!E.plugins) return;
  const cards = [];
  for (const key of Object.keys(S.snap || {}).sort()) {
    if (OWN_KEYS.has(key)) continue;
    const c = pluginCard(key, S.snap[key]);
    if (c) cards.push([key, c]);
  }
  vis(E.plugins, cards.length > 0);
  const have = new Map(Array.from(E.pluginList.children, (el) => [el._k, el]));
  cards.forEach(([key, c], i) => {
    // `have` holds the cards' DOM elements; each element keeps its card object in _c
    let x = have.has(key) ? have.get(key)._c : null;
    if (x) have.delete(key);
    else { x = makePluginCard(); x.el._k = key; x.el._c = x; }
    const name = CARD_ICONS.has(c.icon) ? c.icon : 'bolt';
    if (x.iconName !== name) { x.iconName = name; x.icoBox.replaceChildren(icon(name)); }
    const pl = (shellStatus.plugins || []).find((p) => p && p.key === key && p.name);   // its own page, if it has one
    if (pl) {
      if (!x.link) { x.link = h('a', { class: 'plugin-title-link' }); x.title.replaceChildren(x.link); }
      setText(x.link, c.title);
      const href = '#' + new URLSearchParams({ view: 'plugin', name: pl.name }).toString();
      x.link.setAttribute('href', href);
      x.link.onclick = (e) => { e.preventDefault(); go('plugin', { name: pl.name }); };
    } else {
      if (x.link) { x.link = null; x.title.replaceChildren(); }
      setText(x.title, c.title);
    }
    const prog = typeof c.progress === 'number' ? Math.max(0, Math.min(100, c.progress)) : null;
    vis(x.prog, prog !== null);
    if (prog !== null) { x.bar.style.width = prog + '%'; x.prog.setAttribute('aria-valuenow', String(Math.round(prog))); }
    x.el.setAttribute('aria-label', c.title);
    x.badge.className = 'badge ' + CARD_TONES[c.tone];
    setText(x.badge, CARD_TONE_LABEL[c.tone]);
    x.rows.replaceChildren(...c.rows.flatMap(([k, v]) => [h('dt', { text: k }), h('dd', { text: v })]));
    setText(x.note, c.note || '');
    vis(x.note, !!c.note);
    if (E.pluginList.children[i] !== x.el) E.pluginList.insertBefore(x.el, E.pluginList.children[i] || null);
  });
  for (const el of have.values()) el.remove();
}

/* ------------------------------------------------------------------ status */

function setSnap(data) {
  S.snap = data || {};
  S.snapAt = now();
  S.snapErr = null;
  if (S.visible) renderSnap();
}

function renderSnap() {
  if (!E.sub) return;
  renderHead();
  renderBroadcast();
  renderScenes();
  renderBrb();
  renderPlugins();
  renderChatHead();
  if (S.chatLoaded && E.feed.dataset.empty) E.feed.replaceChildren(chatEmpty());
}

async function loadSnapshot() {
  try { setSnap(await api('/api/live/snapshot')); } catch (e) {
    if (!S.snap) {
      S.snapErr = e.message || 'Couldn’t read the status';
      for (const el of [E.bcErr, E.scOther, E.brbOther]) el.replaceChildren();
      if (S.visible) renderSnap();
    }
  }
}

/** Once a second while visible: uptime, recording time and timers count down locally. */
function tick() {
  if (!S.snap) return;
  renderHead();
  renderBroadcast();
  if (S.snap.overlay) renderTimers();
}

/* ------------------------------------------------------------------ lifecycle */

export async function mount(root) {
  root.replaceChildren(h('div', { class: 'live-layout live-page' },
    h('div', { class: 'live-main' }, buildHead(), buildBroadcast(), buildDeck(), buildScenes(), buildBrb(), buildLog(), buildPlugins()),
    h('div', { class: 'live-side' }, buildChat())));

  on('status', (data) => setSnap(data));
  on('chat', (m, meta) => addChat({ ...m, ts: m.ts || (meta && meta.ts) }));
  on('log', (l, meta) => addLog({ ...l, ts: l.ts || (meta && meta.ts) }));
  on('key', onKeyEvent);
  on('page', onPageEvent);
  on('decks', onDecksEvent);

  if (shellSnapshot && Object.keys(shellSnapshot).length) setSnap(shellSnapshot);
  syncMirrors();   // one placeholder mirror until the list arrives
  await loadDecks();
  loadSnapshot();
  loadChat();
  loadLog();
}

export function show() {
  S.visible = true;
  renderSnap();
  for (const m of MIRRORS.values()) { renderDeck(m); if (m.dirty) refreshDeck(m); }
  if (S.chatDirty) renderChatFull();
  if (S.logDirty) renderLogFull();
  clearInterval(S.ticker);
  S.ticker = setInterval(tick, 1000);
}

export function hide() {
  S.visible = false;
  clearInterval(S.ticker);
  S.ticker = null;
}
