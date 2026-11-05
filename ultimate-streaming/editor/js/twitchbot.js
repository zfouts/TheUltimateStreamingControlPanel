/* Twitch Bot view (nested under Twitch in the sidebar).

   Scheduled chat messages, !commands and channel-point rewards with the actions they run.
   Everything is edited in a local draft (S.draft) and saved as one POST /api/bot/config;
   the master switch, "Send now", tests, sync and reward delete act right away.

   Every string from the daemon, Twitch or viewers (activity users, titles, errors) goes
   through h()/textContent. Reward colors are shown only through safeColor(). */
import { api, on, ApiError } from './api.js';
import { h, icon, toast, openModal, closeModal, confirmDialog, switchInput, asyncButton, chip, empty, safeColor, clone, isObj, fmtDuration } from './ui.js';
import { go } from './main.js';
import { actionStepsEditor, ensureCatalog } from './app.js';

/* Mirrors modules/twitch_bot.py (validate() and the reward allowlist). */
const SAFE = ['obs_scene', 'obs_color', 'obs_mute', 'ha_light', 'notify', 'countdown', 'delay', 'radio', 'media', 'page'];
const COLOR_ACTIONS = ['obs_color', 'ha_light'];
const INPUT = '{input}';
const CHAT_MAX = 500, TITLE_MAX = 45, PROMPT_MAX = 200, DELAY_MAX = 30, DELAY_TOTAL = 25, ACTIONS_MAX = 20;
const TIMER_PH = ['channel', 'uptime', 'game', 'title', 'count'];
const COMMAND_PH = ['user', ...TIMER_PH];
const REPLY_PH = ['user', 'color', ...TIMER_PH];
const PH_HELP = {
  user: 'The viewer’s name', channel: 'Your channel name', uptime: 'How long you’ve been live',
  game: 'The current category', title: 'The stream title', count: 'How many times this ran this stream',
  color: 'The color the viewer typed',
  messages: 'Their chat messages, all time', rank: 'Their all-time rank', stream_messages: 'Their messages this stream',
  stream_rank: 'Their rank this stream', first_seen: 'The day they first chatted', redemptions: 'Rewards they redeemed',
  commands: 'Commands they used', list: 'The list the command builds', period: '“all time” or “this stream”',
};
/* Placeholders per command kind, as modules/twitch_bot.py KIND_PH validates them:
   [response placeholders, extra response field, its placeholders]. */
const KIND_PH = {
  text: [COMMAND_PH, null, null],
  stats: [[...COMMAND_PH, 'messages', 'rank', 'stream_messages', 'stream_rank', 'first_seen', 'redemptions', 'commands'],
    'missing_response', ['user', 'channel']],
  top10: [['channel', 'list', 'period'], null, null],
  uptime: [['channel', 'uptime', 'game', 'title'], 'offline_response', ['channel', 'game', 'title']],
  commands: [['channel', 'list', 'user'], null, null],
};
const BUILTINS = {
  stats: ['Stats', 'Answers with the viewer’s own chat stats. “!stats name” looks up someone else.'],
  top10: ['Top chatters', 'Lists the top chatters. “!top10 stream” shows this stream only.'],
  uptime: ['Uptime', 'Says how long you’ve been live.'],
  commands: ['Command list', 'Lists the commands the viewer is allowed to use.'],
};
/* Built-ins added by plugins (GET /api/bot "builtins"), merged into the tables above at load. */
const PLUGIN_BUILTINS = new Map();   // kind -> default_response
function mergeBuiltins(r) {
  const b = r && r.builtins && typeof r.builtins === 'object' ? r.builtins : {};
  for (const kind of PLUGIN_BUILTINS.keys()) { delete BUILTINS[kind]; delete KIND_PH[kind]; }
  PLUGIN_BUILTINS.clear();
  for (const [kind, d] of Object.entries(b)) {
    if (!/^[a-z0-9_]+$/.test(kind) || !d || typeof d !== 'object' || kind in KIND_PH) continue;
    const ph = Array.isArray(d.placeholders) ? d.placeholders.filter((x) => typeof x === 'string') : [];
    BUILTINS[kind] = [String(d.label || kind), String(d.help || '')];
    KIND_PH[kind] = [[...COMMAND_PH, ...ph], null, null];
    PLUGIN_BUILTINS.set(kind, typeof d.default_response === 'string' ? d.default_response : '');
  }
}
const notLoaded = (c) => !!c.builtin && !BUILTINS[c.builtin];

const EXTRA_LABEL = {
  missing_response: ['When there are no stats', 'Used when the viewer (or the name they asked about) hasn’t chatted yet.'],
  offline_response: ['When you’re offline', 'Used instead of the response while you aren’t streaming.'],
};
const TODO_RE = /add your [^.!?]*? here/i;   // the default commands' "add your … here" text
const PH_RE = /\{([a-z_]+)\}/g;
const TRIGGER_RE = /^![a-z0-9_]{1,25}$/;
const LEVELS = [['everyone', 'Everyone'], ['subscriber', 'Subscribers'], ['vip', 'VIPs'], ['moderator', 'Moderators'], ['broadcaster', 'Only you']];
const ANNOUNCE = [['primary', 'Default'], ['blue', 'Blue'], ['green', 'Green'], ['orange', 'Orange'], ['purple', 'Purple']];
const KINDS = {
  timer: ['clock', 'Scheduled message'], command: ['code', 'Command'], reward: ['sparkle', 'Reward'],
  revert: ['undo', 'Undo'], error: ['warn', 'Problem'],
};
const ES_ERRORS = {
  relink: 'needs re-link', subscribe: 'couldn’t subscribe', account: 'wrong account', revoked: 'access revoked',
};
const FEED_MAX = 50;

const STARTERS = [
  {
    name: 'Change the background color', sub: 'Viewers type a color; your OBS color source changes to it.',
    make: () => ({
      title: 'Change the background color', cost: 500, prompt: 'Type a color, like purple or #00ff88',
      input_required: true, background_color: '#9146FF',
      actions: [{ obs_color: { color: INPUT } }], reply: '{user} painted the background {color}!',
    }),
  },
  {
    name: 'Room lights color', sub: 'Viewers type a color for your Home Assistant light.',
    make: () => ({
      title: 'Room lights color', cost: 300, prompt: 'Type a color, like orange or #ff0066',
      input_required: true, background_color: '#F5B544',
      actions: [{ ha_light: { color: INPUT } }], reply: '{user} set the lights to {color}',
    }),
  },
  {
    name: 'Party mode 30 s', sub: 'Pink background and lights, back to normal after 30 seconds.',
    make: () => ({
      title: 'Party mode', cost: 1000, prompt: '30 seconds of party colors', background_color: '#FF3EA5',
      actions: [{ obs_color: { color: '#FF00AA' } }, { ha_light: { color: '#FF00AA', brightness: 255 } }],
      revert_after_seconds: 30,
      revert: [{ obs_color: { color: '#1F1F24' } }, { ha_light: { color: '#FFFFFF', brightness: 180 } }],
      reply: 'Party mode on, thanks {user}!',
    }),
  },
];

/* ------------------------------------------------------------------ state */

const S = {
  root: null, visible: false,
  mode: 'loading',          // loading | error | missing | ready
  loadErr: '',
  linked: null,             // Twitch linked? null = unknown
  anyway: false,            // "Set it up anyway" while Twitch isn't linked
  server: null,             // last GET /api/bot (config is the saved config)
  hash: null,               // config.yaml hash for conflict-safe saves (absent on older daemons)
  draft: null,              // the config being edited
  dirty: false, saving: false,
  saveErr: '',              // a 400 from save ("Fix these first: …")
  syncErrs: [],
  statusAt: 0,              // when server.status arrived (the countdowns count from it)
  feed: [], feedKeys: new Set(),
  onTwitch: null,           // GET /api/bot/rewards (ids on the channel) or null
  catalogErr: '',
  ticker: 0, poller: 0,
  els: {},
};
const UI = new WeakMap();     // draft item -> {open, name, lastInput, badge, ...}
const ui = (item) => { if (!UI.has(item)) UI.set(item, {}); return UI.get(item); };

let seq = 0;
const nid = () => 'bot-f' + (++seq);

/* ------------------------------------------------------------------ helpers */

/** JSON with sorted keys, so key order never makes the draft look edited. */
function stable(v) {
  if (Array.isArray(v)) return '[' + v.map(stable).join(',') + ']';
  if (isObj(v)) return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + stable(v[k])).join(',') + '}';
  return JSON.stringify(v === undefined ? null : v);
}
const asList = (v) => (v === undefined || v === null ? [] : Array.isArray(v) ? v : [v]);

function normalised(cfg) {
  const out = clone(cfg);
  delete out.database_url;   // config.yaml only: never shown or sent
  for (const r of out.rewards || []) { r.actions = asList(r.actions); r.revert = asList(r.revert); }
  return out;
}

function slug(text, fallback) {
  const s = String(text || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase()
    .replace(/[^a-z0-9_]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32).replace(/-+$/, '');
  return s || fallback;
}

/** A unique id for `item` in list `kind`, made from `text`. */
function uniqueId(kind, item, text, fallback) {
  const taken = new Set(S.draft[kind].filter((x) => x !== item).map((x) => x.id));
  const base = slug(text, fallback);
  if (!taken.has(base)) return base;
  for (let n = 2; ; n++) {
    const id = `${base.slice(0, 32 - String(n).length - 1)}-${n}`;
    if (!taken.has(id)) return id;
  }
}

const savedOf = (kind, item) => (S.server ? S.server.config[kind].find((x) => x.id === item.id) : null);
const isNew = (kind, item) => !savedOf(kind, item);

function itemState(kind, item) {
  const s = savedOf(kind, item);
  if (!s) return 'new';
  const a = kind === 'rewards' ? { ...item, actions: asList(item.actions), revert: asList(item.revert) } : item;
  return stable(s) === stable(a) ? '' : 'edited';
}

/** Errors in a chat template (same rules as the daemon's _Fields.chat). */
function chatError(value, allowed, required = true) {
  const v = String(value || '').trim();
  if (!v) return required ? 'Write a message.' : '';
  if (v.length > CHAT_MAX) return `At most ${CHAT_MAX} characters (now ${v.length}).`;
  if ('!/'.includes(v[0])) return 'Can’t start with ! or / (chat would treat it as a command).';
  if (v.includes(INPUT)) return 'Can’t use {input}: viewers never choose what your account says. Use {color} in reward replies.';
  for (const m of v.matchAll(PH_RE)) {
    if (PH_HELP[m[1]] && !allowed.includes(m[1])) return `{${m[1]}} isn’t available here.`;
  }
  return '';
}

function usesInput(actions) {
  return asList(actions).some((a) => (Array.isArray(a) ? usesInput(a)
    : isObj(a) && COLOR_ACTIONS.some((k) => isObj(a[k]) && a[k].color === INPUT)));
}

function delayTotal(actions) {
  let t = 0;
  for (const a of asList(actions)) {
    if (Array.isArray(a)) t += delayTotal(a);
    else if (isObj(a) && a.delay !== undefined) t += Number(a.delay) || 0;
  }
  return t;
}

/** Black or white text on a reward's color (a class, never a color from data). */
function inkClass(hex) {
  const c = safeColor(hex);
  if (!c) return 'ink-light';
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16) / 255)
    .map((x) => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.4 ? 'ink-dark' : 'ink-light';
}

function timeOf(ts) {
  const d = new Date((Number(ts) || 0) * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - (Number(ts) || 0));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return `${Math.floor(s / 3600)} h ago`;
}
const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + 's'}`;
const secs = (n) => (n >= 3600 && n % 3600 === 0 ? `${n / 3600} h` : n >= 60 && n % 60 === 0 ? `${n / 60} min` : `${n} s`);

/** api() with friendlier 401/403 (Twitch not linked / needs another permission). */
async function call(path, body) {
  try {
    return await api(path, body);
  } catch (e) {
    if (e.status === 401) { refreshAuth(); throw new ApiError('Twitch isn’t linked. Link it in the Twitch view first.', 401); }
    if (e.status === 403) { refresh(); throw new ApiError(`${e.message}. Re-link Twitch in the Twitch view.`, 403); }
    throw e;
  }
}

/* ------------------------------------------------------------------ small controls */

function fld(label, control, help, { error, cls } = {}) {
  const target = /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName) ? control : control.querySelector('input:not([type="color"]), select, textarea');
  if (target && !target.id) target.id = nid();
  const helpEl = h('div', { class: 'help', id: nid(), text: help || '', hidden: !help });
  const errEl = h('div', { class: 'help error', id: nid(), role: 'alert', hidden: true });
  if (target) target.setAttribute('aria-describedby', `${helpEl.id} ${errEl.id}`);
  const box = h('div', { class: 'field' + (cls ? ' ' + cls : '') },
    h('label', { text: label, for: target ? target.id : null }), control, helpEl, errEl);
  box.setError = (msg) => {
    errEl.textContent = msg || '';
    errEl.hidden = !msg;
    box.classList.toggle('error', !!msg);
    if (target) target.setAttribute('aria-invalid', msg ? 'true' : 'false');
  };
  if (error) box.setError(error);
  return box;
}

function toggle(checked, onchange, label, { hideLabel, help } = {}) {
  const sw = switchInput(checked, onchange, hideLabel ? null : label);
  const input = sw.querySelector('input');
  if (hideLabel) input.setAttribute('aria-label', label);
  if (!help) return sw;
  const hid = nid();
  input.setAttribute('aria-describedby', hid);
  return h('div', { class: 'bot-toggle' }, sw, h('div', { class: 'help', id: hid, text: help }));
}

/** A whole-number input bound to obj[prop]; out-of-range values are flagged, not saved. */
function numField(obj, prop, label, { min = 0, max, help, unit, onchange } = {}) {
  const input = h('input', { type: 'number', min, max, step: 1, inputmode: 'numeric', value: obj[prop] ?? '' });
  const box = fld(label + (unit ? ` (${unit})` : ''), input, help);
  input.addEventListener('input', () => {
    const n = Number(input.value);
    const ok = input.value.trim() !== '' && Number.isInteger(n) && n >= min && (max === undefined || n <= max);
    box.setError(ok ? '' : max === undefined ? `A whole number, at least ${min}.` : `A whole number from ${min} to ${max}.`);
    if (ok) { obj[prop] = n; changed(); if (onchange) onchange(n); }
  });
  return box;
}

/** A text input/textarea bound to obj[prop] with an optional validator and counter. */
function textField(obj, prop, label, { multiline, max, placeholder, help, check, onchange, cls } = {}) {
  const input = multiline
    ? h('textarea', { rows: 2, maxlength: max, placeholder: placeholder || '', spellcheck: 'true' })
    : h('input', { type: 'text', maxlength: max, placeholder: placeholder || '', autocomplete: 'off' });
  input.value = obj[prop] ?? '';
  const count = max ? h('span', { class: 'bot-count', 'aria-hidden': 'true' }) : null;
  const wrap = count ? h('div', { class: 'bot-input-count' }, input, count) : input;
  const box = fld(label, wrap, help, { cls });
  const update = (initial) => {
    if (count) {
      count.textContent = `${input.value.length}/${max}`;
      count.classList.toggle('near', input.value.length > max * 0.9);
    }
    if (check) box.setError(initial && !input.value ? '' : check(input.value));
  };
  input.addEventListener('input', () => {
    if (multiline) input.value = input.value.replace(/[\r\n]+/g, ' ');   // chat is one line
    obj[prop] = input.value;
    update(false);
    changed();
    if (onchange) onchange(input.value);
  });
  update(true);
  box.input = input;
  return box;
}

/** Clickable {placeholder} chips that insert into target() at the cursor. */
function phChips(names, target) {
  return h('div', { class: 'bot-ph', role: 'group', 'aria-label': 'Insert a placeholder' },
    h('span', { class: 'bot-ph-label', text: 'Insert' }),
    names.map((n) => h('button', {
      type: 'button', class: 'bot-ph-chip', title: PH_HELP[n], 'aria-label': `Insert {${n}}: ${PH_HELP[n]}`,
      onmousedown: (e) => e.preventDefault(),   // keep the cursor in the text box
      onclick: () => {
        const el = target();
        if (!el) return;
        const start = el.selectionStart ?? el.value.length, end = el.selectionEnd ?? el.value.length;
        el.focus();
        el.setRangeText(`{${n}}`, start, end, 'end');
        el.dispatchEvent(new Event('input', { bubbles: true }));
      },
    }, `{${n}}`)));
}

function seg(options, value, onpick, label) {
  const box = h('div', { class: 'seg', role: 'group', 'aria-label': label });
  const draw = (v) => box.replaceChildren(...options.map(([val, text]) => h('button', {
    type: 'button', 'aria-pressed': String(val === v), dataset: { v: val },
    onclick: () => { draw(val); onpick(val); },
  }, h('span', { class: `bot-ann-dot ${val}`, 'aria-hidden': 'true' }), text)));
  draw(value);
  return box;
}

function colorPicker(obj, prop, label, { fallback = '#9146FF', onchange } = {}) {
  const txt = h('input', { type: 'text', class: 'mono', placeholder: fallback, value: obj[prop] || '', maxlength: 32, autocomplete: 'off', spellcheck: 'false' });
  const pick = h('input', { type: 'color', 'aria-label': `${label}: color picker`, value: (safeColor(obj[prop]) || fallback).toLowerCase() });
  const box = fld(label, h('div', { class: 'row nowrap bot-color' }, pick, txt), 'A hex color like #9146FF.');
  const apply = (v) => { obj[prop] = v; changed(); if (onchange) onchange(v); };
  pick.addEventListener('input', () => { txt.value = pick.value.toUpperCase(); box.setError(''); apply(txt.value); });
  txt.addEventListener('input', () => {
    const v = txt.value.trim();
    const ok = /^#[0-9a-f]{6}$/i.test(v);
    if (ok) pick.value = v.toLowerCase();
    box.setError(ok || !v ? '' : 'Use a hex color like #9146FF.');
    if (ok) apply(v.toUpperCase());
  });
  return box;
}

/** Dropdown menu (keyboard: arrows, Escape) next to a trigger button. */
function menu(btn, label, items) {
  const wrap = h('div', { class: 'bot-menu-wrap' }, btn);
  btn.setAttribute('aria-haspopup', 'menu');
  btn.setAttribute('aria-expanded', 'false');
  let cleanup = null;
  const close = (refocus) => {
    const m = wrap.querySelector('.bot-menu');
    if (m) m.remove();
    btn.setAttribute('aria-expanded', 'false');
    if (cleanup) { cleanup(); cleanup = null; }
    if (refocus) btn.focus();
  };
  btn.addEventListener('click', () => {
    if (wrap.querySelector('.bot-menu')) { close(true); return; }
    const els = items.map((it) => (it === '-' ? h('div', { class: 'bot-menu-sep', role: 'separator' })
      : h('button', { type: 'button', role: 'menuitem', tabindex: '-1', class: 'bot-menu-item', onclick: () => { close(false); it.run(); } },
        icon(it.icon || 'plus'), h('span', { class: 'bot-menu-text' }, h('span', { class: 'bot-menu-name', text: it.name }),
          it.sub && h('span', { class: 'bot-menu-sub', text: it.sub })))));
    const m = h('div', { class: 'bot-menu', role: 'menu', 'aria-label': label }, els);
    const focusable = els.filter((e) => e.getAttribute('role') === 'menuitem');
    m.addEventListener('keydown', (e) => {
      const i = focusable.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); focusable[(i + 1) % focusable.length].focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); focusable[(i - 1 + focusable.length) % focusable.length].focus(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(true); }
      else if (e.key === 'Tab') close(false);
    });
    wrap.append(m);
    btn.setAttribute('aria-expanded', 'true');
    focusable[0].focus();
    const outside = (e) => { if (!wrap.contains(e.target)) close(false); };
    setTimeout(() => document.addEventListener('mousedown', outside));
    cleanup = () => document.removeEventListener('mousedown', outside);
  });
  return wrap;
}

function badge(kind, item) {
  const el = h('span', { class: 'badge warn bot-state', hidden: true });
  ui(item).badge = { el, kind };
  paintBadge(kind, item);
  return el;
}
function paintBadge(kind, item) {
  const b = ui(item).badge;
  if (!b) return;
  const st = itemState(kind, item);
  b.el.hidden = !st;
  b.el.textContent = st === 'new' ? 'New' : 'Edited';
  b.el.title = st === 'new' ? 'Not saved yet' : 'Changed since the last save';
}

/* ------------------------------------------------------------------ draft bookkeeping */

/** Something in the draft changed (typing): refresh the dirty bar and the item badges. */
function changed() {
  S.dirty = stable(normalised(S.draft)) !== stable(normalised(S.server.config));
  for (const kind of ['timers', 'commands', 'rewards']) for (const it of S.draft[kind]) paintBadge(kind, it);
  renderBar();
}

function setDraft(cfg) {
  const old = S.draft;
  S.draft = clone(cfg);
  if (old) {   // keep cards open across a save or reload
    for (const kind of ['timers', 'commands', 'rewards']) {
      for (const it of S.draft[kind]) {
        const prev = old[kind].find((x) => x.id === it.id);
        if (prev && UI.has(prev)) ui(it).open = ui(prev).open;
      }
    }
  }
  S.dirty = false;
}

function applyServer(r, { replace } = {}) {
  mergeBuiltins(r);
  const prev = S.server;
  S.server = r;
  S.hash = r.hash || null;
  S.statusAt = Date.now();
  mergeFeed(((r.status && r.status.recent) || []).slice());
  if (replace || !S.draft || !S.dirty) {
    const same = prev && S.draft && stable(normalised(S.draft)) === stable(normalised(r.config));
    if (!same || replace) { setDraft(r.config); return true; }
    return false;
  }
  // keep the user's edits, but take what the daemon itself wrote (master switch, synced reward ids)
  S.draft.enabled = r.config.enabled;
  for (const dr of S.draft.rewards) {
    const sr = r.config.rewards.find((x) => x.id === dr.id);
    if (sr && !dr.twitch_id && sr.twitch_id) dr.twitch_id = sr.twitch_id;
  }
  S.dirty = stable(normalised(S.draft)) !== stable(normalised(r.config));
  return false;
}

/* ------------------------------------------------------------------ loading */

async function refreshAuth() {
  try {
    const a = await api('/api/twitch/auth');
    const linked = !!(a && a.linked);
    if (linked !== S.linked) { S.linked = linked; if (S.mode === 'ready') render(); }
  } catch (e) { /* the Twitch module isn't loaded: the bot view says so through its own calls */ }
}

async function load() {
  S.mode = 'loading';
  render();
  const [bot] = await Promise.allSettled([api('/api/bot'), refreshAuth(),
    ensureCatalog().then((c) => { catalogCache = c; S.catalogErr = ''; }, (e) => { S.catalogErr = e.message; })]);
  if (bot.status === 'rejected') {
    const e = bot.reason;
    S.mode = e.status === 503 ? 'missing' : 'error';
    S.loadErr = e.message || 'The daemon didn’t answer';
    render();
    return;
  }
  applyServer(bot.value, { replace: true });
  S.mode = 'ready';
  render();
  loadOnTwitch();
}

/** GET /api/bot again; redraws the editor only when the saved config changed and nothing is being edited. */
async function refresh() {
  if (S.mode !== 'ready') return;
  try {
    const r = await api('/api/bot');
    const redraw = applyServer(r);
    if (redraw) renderSections(); else { renderHead(); renderBar(); tick(); paintStatsMeta(); if (!ST.top) paintStatsData(); }
    renderFeed();
  } catch (e) { /* keep what is shown; the next poll tries again */ }
}

async function loadOnTwitch() {
  if (!S.linked) { S.onTwitch = null; return; }
  try {
    const r = await api('/api/bot/rewards');
    S.onTwitch = new Map((r.rewards || []).map((x) => [x.id, x]));
  } catch (e) { S.onTwitch = null; }
  if (S.mode === 'ready') renderRewards();
}

let refreshSoon = 0;
function scheduleRefresh() { clearTimeout(refreshSoon); refreshSoon = setTimeout(refresh, 600); }

/* ------------------------------------------------------------------ save / discard */

async function save() {
  if (S.saving || !S.dirty) return;
  S.saving = true;
  renderBar();
  try {
    const body = { config: normalised(S.draft) };
    if (S.hash) body.hash = S.hash;
    const r = await api('/api/bot/config', body);
    S.saveErr = '';
    applyServer(r, { replace: true });
    renderSections();
    toast('Twitch Bot saved', { ok: true });
    loadOnTwitch();
  } catch (e) {
    if (e.status === 409) conflict();
    else if (e.status === 400) {
      S.saveErr = e.message;
      renderSaveErr();
      if (S.els.saveErr) S.els.saveErr.scrollIntoView({ block: 'center', behavior: 'smooth' });
    } else toast('Not saved: ' + e.message, { bad: true, ms: 8000 });
  } finally {
    S.saving = false;
    renderBar();
  }
}

function conflict() {
  openModal('Settings changed somewhere else', h('div', {},
    h('p', { class: 'hint', text: 'config.yaml was changed outside this page since you opened it (a deck key, another tab, or a text editor). Saving now would overwrite that.' }),
    h('div', { class: 'row end mt-3' },
      h('button', { class: 'ghost', text: 'Keep editing', onclick: closeModal }),
      h('button', { class: 'danger', onclick: async () => { closeModal(); S.saveErr = ''; await reload(); } }, icon('refresh'), 'Reload (your edits will be lost)'))));
}

async function reload() {
  try {
    applyServer(await api('/api/bot'), { replace: true });
    renderSections();
    toast('Reloaded the saved settings', { ms: 2000 });
  } catch (e) { toast('Couldn’t reload: ' + e.message, { bad: true }); }
}

async function discard() {
  const ok = await confirmDialog('Discard your changes?', 'Everything you changed since the last save is undone.', { ok: 'Discard changes', danger: true });
  if (!ok) return;
  S.saveErr = '';
  setDraft(S.server.config);
  renderSections();
}

/* ------------------------------------------------------------------ page */

function render() {
  if (!S.root) return;
  const head = h('header', { class: 'page-head' },
    h('div', { class: 'page-titles' },
      h('h1', { class: 'page-title', text: 'Twitch Bot' }),
      h('p', { class: 'page-sub', text: 'Scheduled chat messages, !commands and channel-point rewards.' })),
    h('div', { class: 'page-actions bot-chips', 'aria-live': 'polite' }));
  S.els = { chips: head.lastChild };

  if (S.mode === 'loading') {
    S.root.replaceChildren(h('div', { class: 'page bot-page' }, head,
      h('div', { class: 'card', 'aria-busy': 'true', 'aria-label': 'Loading' },
        [92, 70, 84].map((w) => h('span', { class: 'skeleton line', style: { width: w + '%' } }))),
      h('div', { class: 'skeleton block' })));
    return;
  }
  if (S.mode === 'missing' || S.mode === 'error') {
    S.root.replaceChildren(h('div', { class: 'page bot-page' }, head,
      h('section', { class: 'card' },
        S.mode === 'missing'
          ? empty('The Twitch Bot isn’t running', `${S.loadErr}. Restart the panel’s service, then try again.`, 'bot')
          : empty('Can’t load the Twitch Bot', S.loadErr, 'warn'),
        h('div', { class: 'row bot-center' }, asyncButton({ class: 'primary' }, load, icon('refresh'), 'Try again')))));
    return;
  }

  if (S.linked === false && !S.anyway) {
    S.root.replaceChildren(h('div', { class: 'page bot-page' }, head,
      h('section', { class: 'card bot-unlinked' },
        empty('Link Twitch to use the bot', 'The bot posts in your chat as your account and runs your channel-point rewards, so it needs your Twitch login first.', 'twitch'),
        h('div', { class: 'row bot-center' },
          h('button', { class: 'twitch', onclick: () => go('twitch') }, icon('twitch'), 'Set up Twitch'),
          h('button', { class: 'ghost', onclick: () => { S.anyway = true; render(); } }, 'Prepare the bot anyway')))));
    renderHead();
    return;
  }

  const banners = h('div', { class: 'bot-banners' });
  const saveErr = h('div', { class: 'bot-banners' });
  const timers = h('div'), commands = h('div'), rewards = h('div'), stats = h('div'), feed = h('div');
  const hero = h('div');
  const bar = h('div', { class: 'bot-savebar', role: 'region', 'aria-label': 'Unsaved changes', hidden: true });
  Object.assign(S.els, { banners, saveErr, timers, commands, rewards, stats, feed, hero, bar });
  S.root.replaceChildren(h('div', { class: 'page bot-page' }, head, hero, banners, saveErr,
    section('clock', 'Scheduled messages', 'Posted in your chat on a timer, one message at a time in turn.', timers, 'bot-sec-timers'),
    section('code', 'Commands', 'Viewers type a !command; the bot answers in chat.', commands, 'bot-sec-commands'),
    section('sparkle', 'Channel-point rewards', 'Rewards on your channel and what happens when someone redeems one.', rewards, 'bot-sec-rewards'),
    section('chart', 'Chat stats', 'Who chats the most, for !stats and !top10.', stats, 'bot-sec-stats'),
    section('layers', 'Activity', 'What the bot did recently, newest first.', feed, 'bot-sec-feed'),
    bar));
  renderSections();
}

function section(iconName, title, sub, body, id) {
  const hid = id + '-title';
  body.classList.add('bot-sec-body');
  return h('section', { class: 'bot-sec', id, 'aria-labelledby': hid },
    h('div', { class: 'bot-sec-head' },
      h('span', { class: 'card-icon twitch', 'aria-hidden': 'true' }, iconName === 'chart' ? chartIcon() : icon(iconName)),
      h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', id: hid, text: title }), h('p', { class: 'card-sub', text: sub })),
      h('div', { class: 'bot-sec-actions', dataset: { for: id } })),
    body);
}

function sectionActions(id, ...kids) {
  const el = S.root.querySelector(`.bot-sec-actions[data-for="${id}"]`);
  if (el) el.replaceChildren(...kids.filter(Boolean));
}

function renderSections() {
  if (!S.els.timers) { render(); return; }
  renderHead();
  renderSaveErr();
  renderTimers();
  renderCommands();
  renderRewards();
  renderStats();
  renderFeed();
  renderBar();
  tick();
  loadStats();
}

/* ------------------------------------------------------------------ header, banners, save bar */

function renderHead() {
  const srv = S.server, st = (srv && srv.status) || {}, es = (srv && srv.eventsub) || {};
  if (S.els.chips) {
    const chips = [];
    if (srv && S.linked !== false) {   // live status and events only mean something once Twitch is linked
      chips.push(st.live === true ? chip('live', 'LIVE', 'You’re streaming') : st.live === false ? chip('', 'Offline', 'You’re not streaming')
        : chip('', 'Live status unknown'));
      if (es.connected) chips.push(chip(true, 'Live events: connected', 'Twitch tells the bot about redemptions and going live right away'));
      else if (es.error) chips.push(chip(false, `Live events: ${ES_ERRORS[es.error] || 'offline'}`, ES_ERRORS[es.error] ? undefined : String(es.error)));
      else chips.push(chip('warn', 'Live events: connecting…'));
    }
    if (srv && st.paused) chips.push(chip('warn', 'Scheduled messages paused', 'Paused from a deck key; resume it there'));
    S.els.chips.replaceChildren(...chips);
  }
  if (S.els.hero) renderHero();
  if (S.els.banners) {
    const out = [];
    const missing = (srv && srv.scopes_missing) || [];
    if (S.linked === false) {
      out.push(banner('warn', 'Twitch isn’t linked, so the bot can’t post or run rewards yet. You can still prepare everything here.',
        h('button', { class: 'small', onclick: () => go('twitch') }, icon('twitch'), 'Set up Twitch')));
    } else if (missing.length || es.error === 'relink') {
      out.push(banner('warn', 'Re-link Twitch to allow channel-point rewards' + (missing.length ? ` (missing: ${missing.join(', ')}).` : '.'),
        h('button', { class: 'small', onclick: () => go('twitch') }, icon('twitch'), 'Open Twitch')));
    } else if (es.error === 'account' || es.error === 'revoked') {
      out.push(banner('bad', es.error === 'account' ? 'Live events are for a different Twitch account than your channel. Re-link the right account in the Twitch view.'
        : 'Twitch revoked the bot’s live events. Re-link in the Twitch view to turn rewards back on.',
      h('button', { class: 'small', onclick: () => go('twitch') }, icon('twitch'), 'Open Twitch')));
    }
    if (S.catalogErr) out.push(banner('bad', `Couldn’t load the list of actions (${S.catalogErr}). Reward actions can’t be edited until it loads.`,
      asyncButton({ class: 'small' }, async () => { await ensureCatalog(); S.catalogErr = ''; renderSections(); }, icon('refresh'), 'Try again')));
    S.els.banners.replaceChildren(...out);
    S.els.banners.hidden = !out.length;
  }
}

function banner(kind, text, action) {
  return h('div', { class: `banner ${kind}`, role: kind === 'bad' ? 'alert' : 'status' },
    h('div', { class: 'row nowrap bot-banner-row' }, icon('warn'), h('span', { class: 'grow', text }), action));
}

function renderHero() {
  const on = !!S.server.config.enabled;
  const saved = S.server.config;   // what is running: the saved settings
  const counts = [plural(saved.timers.filter((t) => t.enabled).length, 'scheduled message'),
    plural(saved.commands.filter((c) => c.enabled).length, 'command'),
    plural(saved.rewards.filter((r) => r.enabled).length, 'reward')].join(' · ');
  const sw = switchInput(on, async (v) => {
    input.disabled = true;
    try {
      const r = await call('/api/bot/enable', { on: v });
      S.server.config.enabled = S.draft.enabled = r.enabled;
      if (S.server.status) S.server.status.enabled = r.enabled;
      toast(r.enabled ? 'The bot is on' : 'The bot is off; panel rewards are paused on Twitch', { ok: r.enabled });
      scheduleRefresh();   // the daemon wrote config.yaml: pick up its new hash
    } catch (e) {
      input.checked = !v;
      toast('Couldn’t switch the bot: ' + e.message, { bad: true });
    } finally { input.disabled = false; renderHead(); tick(); }
  }, on ? 'On' : 'Off');
  const input = sw.querySelector('input');
  const hid = nid();
  input.setAttribute('aria-describedby', hid);
  input.setAttribute('aria-label', 'Twitch Bot on');
  S.els.hero.replaceChildren(h('section', { class: 'card bot-hero' + (on ? ' on' : ''), 'aria-label': 'Bot power' },
    h('span', { class: 'bot-hero-icon', 'aria-hidden': 'true' }, icon('bot')),
    h('div', { class: 'bot-hero-text' },
      h('div', { class: 'bot-hero-title', text: on ? 'The bot is on' : 'The bot is off' }),
      h('div', { class: 'help', id: hid, text: on ? `Running ${counts}.` : 'Off: no messages, and your panel rewards are paused on Twitch.' })),
    h('div', { class: 'bot-hero-switch' }, sw)));
}

function renderSaveErr() {
  const box = S.els.saveErr;
  if (!box) return;
  if (!S.saveErr) { box.replaceChildren(); box.hidden = true; return; }
  const msg = S.saveErr.replace(/^Fix these first:\s*/, '');
  const items = msg === S.saveErr ? [msg] : msg.split(/;\s+/);
  box.hidden = false;
  box.replaceChildren(h('div', { class: 'banner bad', role: 'alert', tabindex: '-1' },
    h('div', { class: 'row nowrap bot-banner-row' }, icon('warn'),
      h('strong', { class: 'grow', text: items.length > 1 ? `Not saved: fix these ${items.length} things first` : 'Not saved: fix this first' }),
      h('button', { class: 'small ghost icon-btn', 'aria-label': 'Dismiss', title: 'Dismiss', onclick: () => { S.saveErr = ''; renderSaveErr(); } }, icon('x'))),
    h('ul', { class: 'bot-errs' }, items.map((t) => h('li', { text: t })))));
}

function renderBar() {
  const bar = S.els.bar;
  if (!bar) return;
  bar.hidden = !S.dirty && !S.saving;
  bar.replaceChildren(
    h('span', { class: 'bot-savebar-dot', 'aria-hidden': 'true' }),
    h('span', { class: 'grow bot-savebar-text', text: 'Unsaved changes' }),
    h('span', { class: 'bot-savebar-kbd hint' }, h('kbd', { text: 'Ctrl' }), '+', h('kbd', { text: 'S' })),
    h('button', { class: 'ghost', disabled: S.saving, onclick: discard, text: 'Discard' }),
    h('button', { class: 'primary' + (S.saving ? ' busy' : ''), disabled: S.saving, onclick: save }, icon('check'), 'Save changes'));
}

/* ------------------------------------------------------------------ scheduled messages */

function newTimer() {
  const t = { id: '', enabled: true, messages: [''], interval_minutes: 15, min_chat_lines: 5, announce: false, color: 'primary' };
  t.id = uniqueId('timers', t, 'message', 'message');
  ui(t).open = true;
  S.draft.timers.push(t);
  changed();
  renderTimers();
  focusLast(S.els.timers, '.bot-timer:last-child input[type="text"]');
}

function focusLast(root, sel) {
  requestAnimationFrame(() => { const el = root.querySelector(sel); if (el) { el.focus(); el.scrollIntoView({ block: 'center', behavior: 'smooth' }); } });
}

function renderTimers() {
  const box = S.els.timers;
  if (!box) return;
  const cfg = S.draft;
  sectionActions('bot-sec-timers',
    toggle(cfg.only_when_live, (v) => { cfg.only_when_live = v; changed(); tick(); }, 'Only while live'),
    h('button', { class: 'small', onclick: newTimer }, icon('plus'), 'Add message'));
  S.els.due = new Map();
  if (!cfg.timers.length) {
    box.replaceChildren(h('div', { class: 'card' }, empty('No scheduled messages yet',
      'Remind chat about your Discord, socials or schedule every few minutes.', 'clock'),
    h('div', { class: 'row bot-center' }, h('button', { class: 'primary', onclick: newTimer }, icon('plus'), 'Add a scheduled message'))));
    return;
  }
  box.replaceChildren(h('div', { class: 'bot-timers' }, cfg.timers.map(timerCard)));
}

function timerCard(t) {
  const fresh = isNew('timers', t);
  const msgs = h('ol', { class: 'bot-msgs' });
  const title = h('h3', { class: 'bot-item-title', text: t.id });
  const sub = h('p', { class: 'card-sub' });
  const paintSub = () => {
    sub.textContent = `Every ${t.interval_minutes} min` + (t.min_chat_lines ? ` · after ${plural(t.min_chat_lines, 'chat line')}` : '') +
      ` · ${plural(t.messages.length, 'message')}` + (t.announce ? ' · announcement' : '');
  };
  paintSub();
  const drawMsgs = (focusIndex) => {
    msgs.replaceChildren(...t.messages.map((m, i) => {
      const input = h('input', { type: 'text', maxlength: CHAT_MAX, value: m, 'aria-label': `Message ${i + 1}`, placeholder: 'Join the Discord: discord.gg/…', autocomplete: 'off' });
      const err = h('div', { class: 'help error', role: 'alert', id: nid(), hidden: true });
      input.setAttribute('aria-describedby', err.id);
      const check = (initial) => {
        const e = initial && !input.value ? '' : chatError(input.value, TIMER_PH);
        err.textContent = e; err.hidden = !e; input.setAttribute('aria-invalid', e ? 'true' : 'false');
      };
      input.addEventListener('input', () => { t.messages[i] = input.value; check(false); changed(); });
      input.addEventListener('focus', () => { ui(t).lastInput = input; });
      check(true);
      const move = (d) => { [t.messages[i], t.messages[i + d]] = [t.messages[i + d], t.messages[i]]; changed(); drawMsgs(i + d); };
      return h('li', { class: 'bot-msg' },
        h('span', { class: 'bot-msg-num', 'aria-hidden': 'true', text: i + 1 }),
        h('div', { class: 'bot-msg-main' }, input, err),
        h('div', { class: 'bot-msg-tools' },
          h('button', { type: 'button', class: 'small ghost icon-btn', 'aria-label': `Move message ${i + 1} up`, title: 'Move up', disabled: i === 0, onclick: () => move(-1) }, icon('chevronDown', 'ic flip')),
          h('button', { type: 'button', class: 'small ghost icon-btn', 'aria-label': `Move message ${i + 1} down`, title: 'Move down', disabled: i === t.messages.length - 1, onclick: () => move(1) }, icon('chevronDown')),
          h('button', { type: 'button', class: 'small ghost icon-btn', 'aria-label': `Remove message ${i + 1}`, title: t.messages.length === 1 ? 'A scheduled message needs at least one message' : 'Remove',
            disabled: t.messages.length === 1, onclick: () => { t.messages.splice(i, 1); changed(); paintSub(); drawMsgs(Math.max(0, i - 1)); } }, icon('x'))));
    }));
    if (focusIndex !== undefined) { const el = msgs.querySelectorAll('input')[focusIndex]; if (el) el.focus(); }
  };
  drawMsgs();

  const due = h('span', { class: 'bot-due' });
  S.els.due.set(t.id, { el: due, t });
  const colorRow = h('div', { class: 'field', hidden: !t.announce },
    h('span', { class: 'label', text: 'Announcement color' }),
    seg(ANNOUNCE, t.color, (v) => { t.color = v; changed(); }, 'Announcement color'));
  const sendNow = asyncButton({ class: 'small', disabled: fresh, title: fresh ? 'Save first' : 'Post the next message now' }, async () => {
    const edited = itemState('timers', t);
    await call('/api/bot/timer/send', { id: t.id });
    toast(edited ? 'Posted the saved version. Save to post your changes.' : 'Posted in chat', { ok: !edited });
    scheduleRefresh();
  }, icon('send'), 'Send now');

  const nameField = fresh ? textField(ui(t), 'name', 'Name', {
    placeholder: 'discord', help: `Saved as “${t.id}”. Deck keys and the activity list use this name.`,
    onchange: (v) => { t.id = uniqueId('timers', t, v, 'message'); title.textContent = t.id; nameField.querySelector('.help').textContent = `Saved as “${t.id}”. Deck keys and the activity list use this name.`; },
  }) : null;

  return h('article', { class: 'card bot-timer' + (t.enabled ? '' : ' off'), 'aria-label': `Scheduled message ${t.id}` },
    h('div', { class: 'card-head bot-item-head' },
      toggle(t.enabled, (v) => { t.enabled = v; changed(); tick(); }, `Post “${t.id}”`, { hideLabel: true }),
      h('div', { class: 'card-titles' }, h('div', { class: 'row nowrap bot-title-row' }, title, badge('timers', t)), sub),
      h('button', { class: 'small ghost icon-btn', 'aria-label': `Delete scheduled message ${t.id}`, title: 'Delete',
        onclick: async () => {
          if (!(await confirmDialog('Delete this scheduled message?', `“${t.id}” stops posting once you save.`, { ok: 'Delete', danger: true }))) return;
          S.draft.timers.splice(S.draft.timers.indexOf(t), 1); changed(); renderTimers();
        } }, icon('trash'))),
    h('div', { class: 'bot-timer-status' }, due, sendNow),
    nameField,
    h('div', { class: 'field' },
      h('span', { class: 'label', text: t.messages.length > 1 ? 'Messages (posted in turn)' : 'Message' }),
      msgs,
      h('div', { class: 'row between bot-msg-foot' },
        h('button', { type: 'button', class: 'small ghost', disabled: t.messages.length >= 20,
          onclick: () => { t.messages.push(''); changed(); paintSub(); drawMsgs(t.messages.length - 1); } }, icon('plus'), 'Add another message'),
        phChips(TIMER_PH, () => ui(t).lastInput && ui(t).lastInput.isConnected ? ui(t).lastInput : msgs.querySelector('input')))),
    h('div', { class: 'grid cols-2 fields bot-tight' },
      numField(t, 'interval_minutes', 'Every', { min: 2, max: 240, unit: 'minutes', onchange: paintSub }),
      numField(t, 'min_chat_lines', 'Only after', { min: 0, max: 1000, unit: 'chat lines', help: 'Skips quiet chats. 0 = always.', onchange: paintSub })),
    h('div', { class: 'bot-announce' },
      toggle(t.announce, (v) => { t.announce = v; colorRow.hidden = !v; changed(); paintSub(); }, 'Post as an announcement',
        { help: 'Highlighted in chat. Needs the announcements permission.' }),
      colorRow));
}

/* ------------------------------------------------------------------ commands */

function allTriggers(except) {
  const out = new Map();
  for (const c of S.draft.commands) {
    if (c === except) continue;
    for (const t of [c.trigger, ...(c.aliases || [])]) if (t) out.set(String(t).toLowerCase(), c);
  }
  return out;
}

function triggerError(v, c) {
  const t = String(v || '').trim().toLowerCase();
  if (!TRIGGER_RE.test(t)) return 'An ! then 1-25 letters, numbers or _ (like !discord).';
  if (allTriggers(c).has(t)) return `${t} is already used by another command.`;
  return '';
}

function newCommand() {
  const c = { id: '', enabled: true, trigger: '', aliases: [], response: '', builtin: null, offline_response: '', missing_response: '',
    count: 10, permission: 'everyone', cooldown_seconds: 30, user_cooldown_seconds: 0, reply: true };
  c.id = uniqueId('commands', c, 'command', 'command');
  ui(c).open = true;
  S.draft.commands.push(c);
  changed();
  renderCommands();
  focusLast(S.els.commands, '.bot-cmd:last-child .bot-cmd-edit input');
}

/** Append every default command whose id (and trigger) isn't in the draft yet. */
async function addDefaults() {
  const r = await call('/api/bot/defaults');
  const ids = new Set(S.draft.commands.map((c) => c.id));
  const added = [], clash = [];
  for (const d of r.commands || []) {
    if (!isObj(d) || ids.has(d.id)) continue;
    const used = allTriggers(null);
    if ([d.trigger, ...(d.aliases || [])].some((t) => used.has(String(t).toLowerCase()))) { clash.push(d.trigger); continue; }
    const c = clone(d);
    S.draft.commands.push(c);
    ids.add(c.id);
    added.push(c);
  }
  changed();
  renderCommands();
  if (!added.length && !clash.length) toast('All the default commands are already here.', { ms: 3000 });
  else {
    const todo = added.filter((c) => TODO_RE.test(c.response)).length;
    toast([added.length ? `Added ${plural(added.length, 'command')}. Save to keep them.` : '',
      todo ? `${todo} are off until you add your own text.` : '',
      clash.length ? `Skipped ${clash.join(', ')}: already used by your commands.` : ''].filter(Boolean).join(' '), { ok: !!added.length, ms: 6000 });
  }
}

const kindOf = (c) => (c.builtin && KIND_PH[c.builtin] ? c.builtin : 'text');

/** Text with the "add your … here" part in a <mark> (text nodes only). */
function marked(text) {
  const m = TODO_RE.exec(text || '');
  if (!m) return [text];
  return [text.slice(0, m.index), h('mark', { class: 'bot-todo', text: m[0] }), text.slice(m.index + m[0].length)];
}

function renderCommands() {
  const box = S.els.commands;
  if (!box) return;
  sectionActions('bot-sec-commands',
    asyncButton({ class: 'small ghost', title: 'Bring back the built-in and starter commands you don’t have' }, addDefaults, icon('refresh'), 'Add missing defaults'),
    h('button', { class: 'small', onclick: newCommand }, icon('plus'), 'Add command'));
  if (!S.draft.commands.length) {
    box.replaceChildren(h('div', { class: 'card' }, empty('No commands yet', 'Answer !discord, !socials or !uptime with one line of text.', 'code'),
      h('div', { class: 'row bot-center' },
        asyncButton({ class: 'primary' }, addDefaults, icon('plus'), 'Add the default commands'),
        h('button', { onclick: newCommand }, icon('plus'), 'Add your own'))));
    return;
  }
  box.replaceChildren(h('div', { class: 'card flush' }, h('ul', { class: 'bot-cmds' }, S.draft.commands.map(commandRow))));
}

function commandRow(c) {
  const u = ui(c);
  const li = h('li', { class: 'bot-cmd' + (c.enabled ? '' : ' off') + (u.open ? ' open' : '') });
  const editId = nid();
  const trig = h('code', { class: 'bot-trigger' });
  const aliases = h('span', { class: 'bot-aliases' });
  const todoBadge = h('span', { class: 'badge warn', text: 'Needs your text', title: 'Replace the “add your … here” text', hidden: true });
  const resp = h('span', { class: 'bot-cmd-resp' });
  const meta = h('span', { class: 'bot-cmd-meta' });
  const out = h('div', { class: 'bot-test-out', 'aria-live': 'polite', hidden: true });
  const paint = () => {
    trig.textContent = c.trigger || '!new';
    aliases.replaceChildren(...(c.aliases || []).filter(Boolean).map((a) => h('span', { class: 'badge', text: a })));
    resp.replaceChildren(...(c.response ? marked(c.response) : ['No response yet']));
    resp.classList.toggle('muted', !c.response);
    todoBadge.hidden = !TODO_RE.test(c.response || '');
    const level = (LEVELS.find(([v]) => v === c.permission) || LEVELS[0])[1];
    meta.replaceChildren(
      h('span', { class: 'badge' + (c.permission === 'everyone' ? '' : ' accent'), text: level, title: 'Who can use it' }),
      c.cooldown_seconds ? h('span', { class: 'bot-cool', title: 'Cooldown' }, icon('clock'), secs(c.cooldown_seconds)) : h('span', { class: 'bot-cool', text: 'No cooldown' }));
  };
  paint();

  const editBtn = h('button', { class: 'small ghost icon-btn bot-chev', 'aria-expanded': String(!!u.open), 'aria-controls': editId,
    'aria-label': `Edit ${c.trigger || 'command'}`, title: 'Edit' }, icon('chevronDown'));
  const edit = h('div', { class: 'bot-cmd-edit', id: editId, hidden: !u.open });
  editBtn.addEventListener('click', () => {
    u.open = !u.open;
    edit.hidden = !u.open;
    li.classList.toggle('open', u.open);
    editBtn.setAttribute('aria-expanded', String(u.open));
    if (u.open && !edit.firstChild) edit.append(...commandForm(c, paint));
  });
  if (u.open) edit.append(...commandForm(c, paint));

  const fresh = isNew('commands', c);
  const test = asyncButton({ class: 'small', disabled: fresh, title: fresh ? 'Save first' : 'Show what the bot would answer (nothing is posted)' }, async () => {
    const edited = itemState('commands', c);
    const r = await call('/api/bot/command/test', { id: c.id });
    out.hidden = false;
    out.replaceChildren(
      h('div', { class: 'bot-test-who' }, icon('eye'), h('span', { text: edited ? 'Saved version, as TestViewer would see it:' : 'TestViewer would see:' })),
      h('div', { class: 'bot-bubble', text: r.text || '(nothing: the response is empty)' }),
      h('button', { class: 'small ghost icon-btn', 'aria-label': 'Close the test', title: 'Close', onclick: () => { out.hidden = true; } }, icon('x')));
  }, icon('play'), 'Test');

  const kind = c.builtin && BUILTINS[c.builtin];
  li.append(
    h('div', { class: 'bot-cmd-row' },
      toggle(c.enabled, (v) => { c.enabled = v; li.classList.toggle('off', !v); changed(); }, `Enable ${c.trigger || 'this command'}`, { hideLabel: true }),
      h('div', { class: 'bot-cmd-trig' }, trig, aliases,
        kind ? h('span', { class: 'badge accent bot-builtin', text: 'Built-in', title: `Built-in: ${kind[0]}` }) : '',
        notLoaded(c) ? h('span', { class: 'badge warn bot-builtin', text: `Built-in: ${c.builtin} (not loaded)`, title: 'Install or enable the plugin that provides it.' }) : '',
        todoBadge, badge('commands', c)),
      resp, meta,
      h('div', { class: 'bot-cmd-tools' }, test, editBtn,
        h('button', { class: 'small ghost icon-btn', 'aria-label': `Delete ${c.trigger || 'command'}`, title: 'Delete',
          onclick: async () => {
            const more = kind ? ' “Add missing defaults” brings it back.' : '';
            if (!(await confirmDialog('Delete this command?', `${c.trigger || 'This command'} stops working once you save.${more}`, { ok: 'Delete', danger: true }))) return;
            S.draft.commands.splice(S.draft.commands.indexOf(c), 1); changed(); renderCommands();
          } }, icon('trash')))),
    out, edit);
  return li;
}

function commandForm(c, paint) {
  const [respPh, extraKey, extraPh] = KIND_PH[kindOf(c)];
  const builtin = c.builtin && BUILTINS[c.builtin];
  const trigger = textField(c, 'trigger', 'Command', {
    placeholder: '!discord', max: 26, check: (v) => triggerError(v, c),
    onchange: (v) => {
      const t = v.trim().toLowerCase();
      c.trigger = t;
      if (isNew('commands', c)) c.id = uniqueId('commands', c, t.replace(/^!/, ''), 'command');
      paint();
    },
  });
  trigger.input.addEventListener('blur', () => {
    const v = trigger.input.value.trim().toLowerCase();
    if (v && !v.startsWith('!')) { trigger.input.value = '!' + v; trigger.input.dispatchEvent(new Event('input')); }
  });
  const aliasInput = h('input', { type: 'text', placeholder: '!dc !disc', value: (c.aliases || []).join(' '), autocomplete: 'off' });
  const aliases = fld('Also answers to', aliasInput, 'Up to 5, separated by spaces.');
  aliasInput.addEventListener('input', () => {
    const list = aliasInput.value.split(/[\s,]+/).filter(Boolean).map((a) => (a.startsWith('!') ? a : '!' + a).toLowerCase());
    c.aliases = list;
    const bad = list.length > 5 ? 'At most 5.' : list.map((a) => triggerError(a, c) || (a === c.trigger ? `${a} is the command itself.` : '')).find(Boolean) || '';
    aliases.setError(bad);
    changed(); paint();
  });
  const todo = h('div', { class: 'bot-todo-note', role: 'status' });
  const paintTodo = (v) => {
    const m = TODO_RE.exec(v || '');
    todo.hidden = !m;
    todo.replaceChildren(...(m ? [icon('edit'), h('span', { text: `Replace “${m[0]}” with your own text, then switch the command on.` })] : []));
  };
  const response = textField(c, 'response', 'Response', {
    multiline: true, max: CHAT_MAX, placeholder: builtin ? '' : 'Join us on Discord: discord.gg/…, {user}!',
    check: (v) => chatError(v, respPh), onchange: (v) => { paintTodo(v); paint(); },
  });
  response.classList.toggle('bot-has-todo', TODO_RE.test(c.response || ''));
  response.input.addEventListener('input', () => response.classList.toggle('bot-has-todo', TODO_RE.test(response.input.value)));
  paintTodo(c.response);
  response.append(phChips(respPh, () => response.input), todo);

  let extra = null;
  if (extraKey) {
    const [label, help] = EXTRA_LABEL[extraKey];
    extra = textField(c, extraKey, label, { multiline: true, max: CHAT_MAX, help, check: (v) => chatError(v, extraPh) });
    extra.append(phChips(extraPh, () => extra.input));
  }
  let kindPick = null;
  if (PLUGIN_BUILTINS.size && (PLUGIN_BUILTINS.has(c.builtin) || (!c.builtin && isNew('commands', c)))) {
    const sel = h('select', {}, h('option', { value: '', text: 'Plain text', selected: !c.builtin }),
      [...PLUGIN_BUILTINS.keys()].map((k) => h('option', { value: k, text: BUILTINS[k][0], selected: k === c.builtin })));
    sel.addEventListener('change', () => {
      const prevDef = PLUGIN_BUILTINS.get(c.builtin);
      c.builtin = sel.value || null;
      const def = PLUGIN_BUILTINS.get(c.builtin);
      if (def && (!c.response || c.response === prevDef)) c.response = def;
      changed(); renderCommands();
    });
    kindPick = fld('Answers with', sel, 'Plain text, or a built-in answer from a plugin.');
  }
  const perm = h('select', {}, LEVELS.map(([v, t]) => h('option', { value: v, text: t, selected: v === c.permission })));
  perm.addEventListener('change', () => { c.permission = perm.value; changed(); paint(); });
  return [
    notLoaded(c) && h('div', { class: 'bot-builtin-note' }, h('span', { class: 'badge warn', text: `Built-in: ${c.builtin} (not loaded)` }),
      h('span', { class: 'hint', text: 'Install or enable the plugin that provides it.' })),
    kindPick,
    builtin && h('div', { class: 'bot-builtin-note' }, h('span', { class: 'badge accent', text: `Built-in: ${builtin[0]}` }),
      h('span', { class: 'hint', text: builtin[1] })),
    h('div', { class: 'grid cols-2 fields bot-tight' }, trigger, aliases),
    response,
    extra,
    c.builtin === 'top10' && h('div', { class: 'bot-narrow' }, numField(c, 'count', 'How many to list', { min: 1, max: 10, help: '1 to 10. Long lists are shortened to fit one chat message.' })),
    h('div', { class: 'grid cols-3 fields bot-tight' },
      fld('Who can use it', perm),
      numField(c, 'cooldown_seconds', 'Cooldown', { min: 0, max: 3600, unit: 'seconds', help: 'For everyone, after each use.', onchange: paint }),
      numField(c, 'user_cooldown_seconds', 'Per-viewer cooldown', { min: 0, max: 86400, unit: 'seconds', help: '0 = none.' })),
    toggle(c.reply, (v) => { c.reply = v; changed(); }, 'Reply to the viewer’s message', { help: 'Threads the answer under their message instead of a new line.' }),
  ].filter(Boolean);
}

/* ------------------------------------------------------------------ rewards */

function newReward(over = {}) {
  const r = {
    id: '', twitch_id: '', managed: true, enabled: true, title: '', cost: 100, prompt: '', input_required: false,
    background_color: '#9146FF', global_cooldown_seconds: 0, max_per_stream: 0, max_per_user_per_stream: 0, skip_queue: false,
    actions: [], revert_after_seconds: 0, revert: [], reply: '', allow_unsafe_actions: false, ...over,
  };
  if (r.title) {   // Twitch needs unique titles
    const titles = new Set(S.draft.rewards.map((x) => x.title.toLowerCase()));
    let t = r.title, n = 2;
    while (titles.has(t.toLowerCase())) t = `${r.title.slice(0, TITLE_MAX - 3)} ${n++}`;
    r.title = t;
  }
  r.id = uniqueId('rewards', r, r.title || 'reward', 'reward');
  ui(r).open = true;
  S.draft.rewards.push(r);
  changed();
  renderRewards();
  focusLast(S.els.rewards, '.bot-reward:last-child .bot-reward-body input');
  return r;
}

async function linkExisting() {
  const list = h('div', {}, h('span', { class: 'skeleton line' }), h('span', { class: 'skeleton line' }));
  openModal('Use a reward from twitch.tv', h('div', {},
    h('p', { class: 'hint', text: 'Pick a reward you made on twitch.tv. The bot runs your actions when it’s redeemed; its cost and color stay on twitch.tv.' }),
    h('div', { class: 'mt-3' }, list)));
  try {
    const r = await call('/api/bot/rewards');
    const used = new Set(S.draft.rewards.map((x) => x.twitch_id).filter(Boolean));
    const free = (r.rewards || []).filter((x) => !used.has(x.id));
    if (!free.length) { list.replaceChildren(empty('Nothing to add', 'Every reward on your channel is already here.', 'sparkle')); return; }
    list.replaceChildren(h('ul', { class: 'bot-pick' }, free.map((x) => h('li', {},
      h('button', { type: 'button', class: 'bot-pick-item', onclick: () => {
        closeModal();
        newReward({ title: String(x.title || '').slice(0, TITLE_MAX), cost: Number(x.cost) || 1, managed: false, twitch_id: String(x.id) });
      } }, h('span', { class: 'grow', text: x.title }), h('span', { class: 'badge', text: `${x.cost} pts` }),
      !x.enabled && h('span', { class: 'badge', text: 'Paused' }))))));
  } catch (e) {
    list.replaceChildren(h('p', { class: 'err', text: e.message }));
  }
}

async function syncAll() {
  if (S.dirty) {
    await save();
    if (S.dirty) return;   // not saved: the banner explains
  }
  const r = await call('/api/bot/rewards/sync', {});
  S.syncErrs = r.errors || [];
  const parts = [];
  if (r.created) parts.push(`${r.created} created`);
  if (r.updated) parts.push(`${r.updated} updated`);
  toast(S.syncErrs.length ? `Synced with problems (${S.syncErrs.length})` : `Synced to Twitch${parts.length ? ': ' + parts.join(', ') : ''}`,
    { ok: !S.syncErrs.length, bad: !!S.syncErrs.length });
  await refresh();
  await loadOnTwitch();
}

function renderRewards() {
  const box = S.els.rewards;
  if (!box) return;
  const add = menu(h('button', { class: 'small' }, icon('plus'), 'Add reward', icon('chevronDown')), 'Add a reward', [
    { name: 'Blank reward', sub: 'Start from scratch', icon: 'plus', run: () => newReward() },
    ...STARTERS.map((s) => ({ name: s.name, sub: s.sub, icon: 'sparkle', run: () => { newReward(s.make()); toast('Added. Pick the OBS source or light, then save.', { ms: 4000 }); } })),
    '-',
    { name: 'Use one from twitch.tv', sub: 'A reward you already made on Twitch', icon: 'link', run: linkExisting },
  ]);
  const starters = menu(h('button', { class: 'small ghost' }, icon('sparkle'), 'Starter rewards', icon('chevronDown')), 'Starter rewards',
    STARTERS.map((s) => ({ name: s.name, sub: s.sub, icon: 'sparkle', run: () => { newReward(s.make()); toast('Added. Pick the OBS source or light, then save.', { ms: 4000 }); } })));
  const sync = asyncButton({ class: 'small twitch', disabled: S.linked === false || !S.draft.rewards.length,
    title: S.linked === false ? 'Link Twitch first' : 'Create and update your panel rewards on Twitch' }, syncAll,
  icon('refresh'), S.dirty ? 'Save & sync to Twitch' : 'Sync to Twitch');
  sectionActions('bot-sec-rewards', starters, add, sync);

  const out = [];
  if (S.syncErrs.length) {
    out.push(h('div', { class: 'banner bad', role: 'alert' },
      h('div', { class: 'row nowrap bot-banner-row' }, icon('warn'), h('strong', { class: 'grow', text: 'Some rewards didn’t sync' }),
        h('button', { class: 'small ghost icon-btn', 'aria-label': 'Dismiss', onclick: () => { S.syncErrs = []; renderRewards(); } }, icon('x'))),
      h('ul', { class: 'bot-errs' }, S.syncErrs.map((t) => h('li', { text: t })))));
  }
  if (!S.draft.rewards.length) {
    out.push(h('div', { class: 'card' }, empty('No channel-point rewards yet',
      'Let viewers change your background or room lights, or switch a scene, with channel points.', 'sparkle'),
    h('div', { class: 'row bot-center' }, STARTERS.map((s, i) => h('button', { class: i ? '' : 'primary', onclick: () => newReward(s.make()) }, icon('sparkle'), s.name)))));
  } else out.push(h('div', { class: 'bot-rewards' }, S.draft.rewards.map(rewardCard)));
  box.replaceChildren(...out);
}

function actionsSummary(actions) {
  const cat = (S_catalog() || {}).actions || {};
  return asList(actions).flatMap((a) => (Array.isArray(a) ? a : [a])).filter(isObj).map((a) => {
    const k = Object.keys(a)[0];
    const label = (cat[k] && cat[k].label) || k;
    return COLOR_ACTIONS.includes(k) && isObj(a[k]) && a[k].color === INPUT ? `${label} (viewer’s color)` : label;
  });
}
let catalogCache = null;
function S_catalog() { return catalogCache; }

function rewardPreview(r) {
  const el = h('span', { class: 'bot-reward-chip' });
  el.paint = () => {
    el.className = 'bot-reward-chip ' + inkClass(r.background_color);
    el.style.background = safeColor(r.background_color) || '';
    el.replaceChildren(h('span', { class: 'bot-reward-pts' }, icon('sparkle'), String(r.cost || 0)),
      h('span', { class: 'bot-reward-name', text: r.title || 'Untitled reward' }));
  };
  el.paint();
  return el;
}

function syncBadge(r) {
  if (!r.managed) return h('span', { class: 'badge', text: 'From twitch.tv', title: 'Created on twitch.tv' });
  if (!r.twitch_id) return h('span', { class: 'badge warn', text: 'Not on Twitch yet', title: 'Save, then Sync to Twitch' });
  if (S.onTwitch && !S.onTwitch.has(r.twitch_id)) return h('span', { class: 'badge bad', text: 'Missing on Twitch', title: 'Sync to Twitch creates it again' });
  return h('span', { class: 'badge ok', text: 'On Twitch' });
}

function rewardCard(r) {
  const u = ui(r);
  const card = h('article', { class: 'card bot-reward' + (r.enabled ? '' : ' off') + (u.open ? ' open' : ''), 'aria-label': `Reward ${r.title || 'untitled'}` });
  const preview = rewardPreview(r);
  const summary = h('div', { class: 'bot-reward-summary' });
  const tags = h('div', { class: 'bot-reward-tags' });
  const paint = () => {
    preview.paint();
    const acts = actionsSummary(r.actions);
    summary.textContent = acts.length ? acts.join(' → ') : 'No actions yet: redeeming it only posts the reply.';
    summary.classList.toggle('muted', !acts.length);
    tags.replaceChildren(...[syncBadge(r),
      r.input_required && h('span', { class: 'badge accent', text: usesInput(r.actions) ? 'Viewer types a color' : 'Viewer types text' }),
      r.revert_after_seconds > 0 && h('span', { class: 'badge', text: `Undo after ${secs(r.revert_after_seconds)}` }),
      r.allow_unsafe_actions && h('span', { class: 'badge bad', text: 'Unsafe actions' }),
      badge('rewards', r)].filter(Boolean));
  };
  u.paint = paint;
  paint();

  const bodyId = nid();
  const body = h('div', { class: 'bot-reward-body', id: bodyId, hidden: !u.open });
  const editBtn = h('button', { class: 'small ghost bot-chev-btn', 'aria-expanded': String(!!u.open), 'aria-controls': bodyId },
    h('span', { text: u.open ? 'Close' : 'Edit' }), icon('chevronDown'));
  const fill = () => body.replaceChildren(...rewardForm(r, paint, fill));
  editBtn.addEventListener('click', () => {
    u.open = !u.open;
    body.hidden = !u.open;
    card.classList.toggle('open', u.open);
    editBtn.setAttribute('aria-expanded', String(u.open));
    editBtn.firstChild.textContent = u.open ? 'Close' : 'Edit';
    if (u.open && !body.firstChild) fill();
  });
  if (u.open) fill();

  const fresh = isNew('rewards', r);
  const test = asyncButton({ class: 'small', disabled: fresh, title: fresh ? 'Save first' : 'Run the actions now, as a pretend redemption (no points, no chat reply)' },
    () => testReward(r), icon('play'), 'Test');
  const del = h('button', { class: 'small ghost icon-btn', 'aria-label': `Delete reward ${r.title || ''}`.trim(), title: 'Delete', onclick: () => deleteReward(r) }, icon('trash'));

  card.append(
    h('div', { class: 'bot-reward-head' },
      toggle(r.enabled, (v) => { r.enabled = v; card.classList.toggle('off', !v); changed(); }, `Enable reward ${r.title || ''}`.trim(), { hideLabel: true }),
      preview,
      h('div', { class: 'bot-reward-tools' }, test, editBtn, del)),
    h('div', { class: 'bot-reward-info' }, summary, tags),
    r.managed ? '' : h('p', { class: 'note warn bot-note' }, icon('info'), h('span', { text: 'Created on twitch.tv: points can’t be refunded automatically.' })),
    body);
  return card;
}

/** The color argument of obs_color / ha_light: the viewer's color, or a fixed one. */
function colorArg(r, value, set, label) {
  const viewer = value === INPUT;
  const lid = nid();
  const txt = h('input', { type: 'text', class: 'mono', placeholder: '#9146FF or purple', value: viewer ? '' : value ?? '', 'aria-label': `${label}: hex or name`, autocomplete: 'off', spellcheck: 'false', maxlength: 32 });
  const pick = h('input', { type: 'color', 'aria-label': `${label}: color picker`, value: (safeColor(value) || '#9146ff').toLowerCase() });
  const manual = h('div', { class: 'row nowrap bot-color', hidden: viewer }, pick, txt);
  const note = h('div', { class: 'help', hidden: !viewer, text: 'Viewers type a color (like purple or #00ff88) when they redeem.' });
  pick.addEventListener('input', () => { txt.value = pick.value.toUpperCase(); set(txt.value); });
  txt.addEventListener('input', () => { const v = txt.value.trim(); if (/^#[0-9a-f]{6}$/i.test(v)) pick.value = v.toLowerCase(); set(v || undefined); });
  const sw = toggle(viewer, (on) => {
    manual.hidden = on; note.hidden = !on;
    if (on) {
      set(INPUT);
      if (!r.input_required) {
        r.input_required = true;
        if (ui(r).inputSwitch) ui(r).inputSwitch.checked = true;
        toast('“Viewer types text” is now on, so viewers can type their color.', { ms: 3500 });
      }
    } else set(txt.value.trim() || undefined);
    ui(r).checkInput && ui(r).checkInput();
    ui(r).paint && ui(r).paint();
  }, 'Use the viewer’s color');
  return h('div', { class: 'field bot-color-arg', role: 'group', 'aria-labelledby': lid },
    h('span', { class: 'label', id: lid, text: label }), sw, manual, note);
}

function delayArg(r, value, set) {
  const input = h('input', { type: 'number', min: 0, max: DELAY_MAX, step: 0.5, value: value ?? '' });
  const box = fld('Seconds', input, `A reward’s waits must add up to less than ${DELAY_TOTAL} seconds.`);
  input.addEventListener('input', () => {
    const n = Number(input.value);
    const ok = input.value !== '' && n >= 0 && n <= DELAY_MAX;
    box.setError(ok ? '' : `0 to ${DELAY_MAX} seconds.`);
    if (ok) set(n);
    ui(r).checkDelays && ui(r).checkDelays();
  });
  return box;
}

function stepsFor(r, prop, paint) {
  if (!S_catalog()) return h('p', { class: 'note warn', text: 'The action list didn’t load. Use “Try again” at the top of the page.' });
  return actionStepsEditor(r, prop, {
    allowed: r.allow_unsafe_actions ? undefined : SAFE,
    onchange: () => {
      r[prop] = asList(r[prop]);
      changed();
      paint();
      ui(r).checkInput && ui(r).checkInput();
      ui(r).checkDelays && ui(r).checkDelays();
    },
    field: (kind, name, f, value, set) => {
      if (COLOR_ACTIONS.includes(kind) && name === 'color') return colorArg(r, value, set, f.label || 'Color');
      if (kind === 'delay' && name === null) return delayArg(r, value, set);
      return null;
    },
  });
}

function rewardForm(r, paint, refill) {
  const out = [];
  const u = ui(r);
  const titleField = textField(r, 'title', 'Title', {
    max: TITLE_MAX, placeholder: 'Change the background color',
    help: r.managed ? 'What viewers see on Twitch.' : 'Must match the reward’s title on twitch.tv.',
    check: (v) => {
      const t = v.trim();
      if (!t) return 'Give the reward a title.';
      if (S.draft.rewards.some((x) => x !== r && x.title.trim().toLowerCase() === t.toLowerCase())) return 'Another reward has this title (Twitch needs unique titles).';
      return '';
    },
    onchange: (v) => { if (isNew('rewards', r)) r.id = uniqueId('rewards', r, v, 'reward'); paint(); },
  });

  if (r.managed) {
    const inputSw = toggle(r.input_required, (v) => { r.input_required = v; changed(); paint(); u.checkInput(); }, 'Viewer types text',
      { help: 'Twitch asks for a message when they redeem (needed for “the viewer’s color”).' });
    u.inputSwitch = inputSw.querySelector('input');
    const inputErr = h('div', { class: 'help error', role: 'alert', hidden: true });
    u.checkInput = () => {
      const bad = (usesInput(r.actions) || usesInput(r.revert)) && !r.input_required;
      inputErr.hidden = !bad;
      inputErr.textContent = bad ? 'Your actions use the viewer’s color, so this must be on.' : '';
    };
    u.checkInput();
    out.push(group('On Twitch', null,
      h('div', { class: 'grid cols-2 fields bot-tight' },
        titleField,
        numField(r, 'cost', 'Cost', { min: 1, unit: 'channel points', onchange: paint }),
        textField(r, 'prompt', 'Description', { max: PROMPT_MAX, placeholder: 'Type a color, like purple', help: 'Shown when viewers open the reward.' }),
        colorPicker(r, 'background_color', 'Background', { onchange: paint })),
      h('div', { class: 'grid cols-2 fields bot-tight' },
        h('div', {}, inputSw, inputErr),
        toggle(r.skip_queue, (v) => { r.skip_queue = v; changed(); }, 'Skip the reward queue',
          { help: 'Redemptions are marked done right away, so points can’t be refunded if an action fails.' })),
      h('div', { class: 'grid cols-3 fields bot-tight' },
        numField(r, 'global_cooldown_seconds', 'Cooldown', { min: 0, max: 604800, unit: 'seconds', help: '0 = none.' }),
        numField(r, 'max_per_stream', 'Max per stream', { min: 0, max: 1000000, help: '0 = no limit.' }),
        numField(r, 'max_per_user_per_stream', 'Max per viewer per stream', { min: 0, max: 1000000, help: '0 = no limit.' }))));
  } else {
    u.checkInput = () => {};
    out.push(group('On Twitch', 'Cost, color and limits are set on twitch.tv.', titleField));
  }

  const delayErr = h('p', { class: 'help error', role: 'alert', hidden: true });
  u.checkDelays = () => {
    const total = delayTotal(r.actions);
    delayErr.hidden = total < DELAY_TOTAL;
    delayErr.textContent = `The waits add up to ${total} s; keep them under ${DELAY_TOTAL} s.`;
  };
  out.push(group('When someone redeems it', r.allow_unsafe_actions ? 'Any action is allowed for this reward.' : 'Only actions that are safe for viewers to trigger are listed.',
    stepsFor(r, 'actions', paint), delayErr));
  u.checkDelays();

  const revertBox = h('div', { class: 'bot-revert', hidden: !(r.revert_after_seconds > 0) }, stepsFor(r, 'revert', paint));
  out.push(group('Undo afterwards', 'Put things back a while later, like the original background color.',
    h('div', { class: 'bot-narrow' }, numField(r, 'revert_after_seconds', 'Undo after', {
      min: 0, max: 3600, unit: 'seconds', help: '0 = never. A newer redemption restarts the clock.',
      onchange: (n) => { revertBox.hidden = !(n > 0); paint(); },
    })), revertBox));

  const reply = textField(r, 'reply', 'Chat reply', {
    multiline: true, max: CHAT_MAX, placeholder: '{user} painted the background {color}!',
    help: 'Optional. Posted after the actions ran.', check: (v) => chatError(v, REPLY_PH, false),
  });
  reply.append(phChips(REPLY_PH, () => reply.input));
  out.push(group('Reply', null, reply));

  const unsafe = h('div', { class: 'bot-unsafe' },
    toggle(r.allow_unsafe_actions, (v) => {
      r.allow_unsafe_actions = v; changed(); paint(); refill();
    }, 'Allow unsafe actions', { help: 'Lets this reward run any deck action, like shell commands, key presses or apps.' }),
    r.allow_unsafe_actions && h('div', { class: 'banner bad', role: 'alert' },
      h('div', { class: 'row nowrap bot-banner-row' }, icon('warn'),
        h('span', { class: 'grow', text: 'Anyone with enough channel points can now run these actions on your computer. Only turn this on for actions you’d let any viewer press.' }))));
  out.push(group('Safety', null, unsafe));
  return out;
}

function group(title, sub, ...kids) {
  return h('div', { class: 'bot-group' },
    h('div', { class: 'bot-group-head' }, h('h4', { class: 'bot-group-title', text: title }), sub && h('p', { class: 'hint', text: sub })),
    ...kids);
}

async function testReward(r) {
  const saved = savedOf('rewards', r);
  const edited = itemState('rewards', r);
  const run = async (input) => {
    const res = await call('/api/bot/reward/test', input === undefined ? { id: r.id } : { id: r.id, input });
    if (res.ok) toast(edited ? `Ran the saved “${saved.title}”. Save to test your changes.` : `Ran “${saved.title}”`, { ok: true });
    else toast(`“${saved.title}” failed: ${res.error || 'unknown error'}`, { bad: true, ms: 7000 });
  };
  if (!(usesInput(saved.actions) || usesInput(saved.revert))) { await run(); return; }
  const txt = h('input', { type: 'text', class: 'mono', value: '#00FF88', autocomplete: 'off', spellcheck: 'false', maxlength: 32 });
  const pick = h('input', { type: 'color', value: '#00ff88', 'aria-label': 'Sample color picker' });
  pick.addEventListener('input', () => { txt.value = pick.value.toUpperCase(); });
  txt.addEventListener('input', () => { if (/^#[0-9a-f]{6}$/i.test(txt.value.trim())) pick.value = txt.value.trim().toLowerCase(); });
  const go_ = asyncButton({ class: 'primary', type: 'submit' }, async () => { closeModal(); await run(txt.value.trim()); }, icon('play'), 'Run test');
  openModal(`Test “${saved.title}”`, h('form', { novalidate: true, onsubmit: (e) => { e.preventDefault(); go_.click(); } },
    h('p', { class: 'hint', text: 'This reward uses the color the viewer types. Pick a sample color to try it with.' }),
    h('div', { class: 'mt-3' }, fld('Sample color', h('div', { class: 'row nowrap bot-color' }, pick, txt), 'A hex color or a name like purple.')),
    h('div', { class: 'row end mt-3' }, h('button', { type: 'button', class: 'ghost', text: 'Cancel', onclick: closeModal }), go_)));
}

async function deleteReward(r) {
  const saved = savedOf('rewards', r);
  const text = !saved ? 'It was never saved, so nothing changes on Twitch.'
    : !saved.managed ? 'It stays on twitch.tv; the bot just stops running its actions. This happens right away.'
      : saved.twitch_id ? 'It is deleted on Twitch too, right away.' : 'It isn’t on Twitch yet. This happens right away.';
  if (!(await confirmDialog(`Delete “${r.title || 'this reward'}”?`, text, { ok: 'Delete reward', danger: true }))) return;
  if (saved) {
    try {
      const res = await call('/api/bot/reward/delete', { id: r.id });
      S.server.config.rewards = S.server.config.rewards.filter((x) => x.id !== r.id);
      toast(res.deleted_on_twitch ? 'Deleted, on Twitch too' : 'Deleted', { ok: true });
    } catch (e) { toast('Couldn’t delete: ' + e.message, { bad: true }); return; }
  }
  S.draft.rewards.splice(S.draft.rewards.indexOf(r), 1);
  changed();
  renderRewards();
  if (saved) { scheduleRefresh(); loadOnTwitch(); }   // the daemon wrote config.yaml: new hash
}

/* ------------------------------------------------------------------ chat stats */

const ST = { period: 'all', top: null, totals: null, err: '', loading: false, lookup: null, timer: 0 };
const fmtN = (n) => (Number(n) || 0).toLocaleString();
const fmtDay = (ts) => (ts ? new Date(Number(ts) * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) : '—');
const STAT_PATH = 'M4 20V10m6 10V4m6 16v-7m4 7H2';
function chartIcon() {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'ic');
  svg.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', STAT_PATH);
  svg.append(p);
  return svg;
}

/** GET /api/bot/stats for the table and totals (when the card is shown and every 30 s). */
async function loadStats() {
  if (S.mode !== 'ready' || !S.els.statsTop || ST.loading) return;
  ST.loading = true;
  try {
    const r = await api(`/api/bot/stats?period=${ST.period}&n=25`);
    ST.top = Array.isArray(r.top) ? r.top : [];
    ST.totals = r.totals || null;
    ST.err = '';
  } catch (e) {
    ST.err = e.message || 'Couldn’t load the stats';
  } finally {
    ST.loading = false;
  }
  paintStatsData();
}

function renderStats() {
  const box = S.els.stats;
  if (!box) return;
  const cfg = S.draft.stats || (S.draft.stats = { enabled: true, include_broadcaster: false, exclude: [] });
  const resetMenu = menu(h('button', { class: 'small danger' }, icon('trash'), 'Reset…', icon('chevronDown')), 'Reset chat stats', [
    { name: 'This stream’s counts', sub: 'Everyone’s “this stream” numbers go back to 0.', icon: 'undo', run: () => resetStats('stream') },
    { name: 'Everything', sub: 'Deletes every viewer’s stats. SQLite keeps a backup copy.', icon: 'trash', run: () => resetStats('all') },
  ]);
  sectionActions('bot-sec-stats',
    toggle(cfg.enabled, (v) => { cfg.enabled = v; changed(); }, 'Count chat'),
    h('button', { class: 'small ghost icon-btn', 'aria-label': 'Refresh the stats', title: 'Refresh', onclick: loadStats }, icon('refresh')),
    resetMenu);

  const meta = h('div', { class: 'bot-stats-meta' });
  const totals = h('div', { class: 'bot-stats-totals' });
  const top = h('div', { class: 'bot-stats-table' });
  Object.assign(S.els, { statsMeta: meta, statsTotals: totals, statsTop: top });

  const periodSeg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Period' });
  const drawSeg = () => periodSeg.replaceChildren(...[['all', 'All time'], ['stream', 'This stream']].map(([v, t]) =>
    h('button', { type: 'button', 'aria-pressed': String(ST.period === v), text: t,
      onclick: () => { if (ST.period === v) return; ST.period = v; drawSeg(); ST.top = null; paintStatsData(); loadStats(); } })));
  drawSeg();

  box.replaceChildren(h('div', { class: 'card bot-stats' },
    meta,
    totals,
    h('div', { class: 'bot-stats-grid' },
      h('div', { class: 'bot-stats-col' },
        h('div', { class: 'row between bot-stats-sub' }, h('h3', { class: 'bot-group-title', text: 'Top chatters' }), periodSeg),
        top),
      h('div', { class: 'bot-stats-col' },
        lookupBox(),
        h('div', { class: 'bot-group' },
          h('div', { class: 'bot-group-head' }, h('h4', { class: 'bot-group-title', text: 'Who is counted' })),
          toggle(cfg.include_broadcaster, (v) => { cfg.include_broadcaster = v; changed(); }, 'Include me (the broadcaster)',
            { help: 'Off: your own messages aren’t counted.' }),
          excludeEditor(cfg))))));
  paintStatsMeta();
  paintStatsData();
}

function paintStatsMeta() {
  const meta = S.els.statsMeta;
  if (!meta) return;
  const st = (S.server && S.server.stats) || {};
  meta.replaceChildren(
    h('div', { class: 'bot-stats-db' },
      h('span', { class: 'badge', text: st.backend ? String(st.backend) : 'database' }),
      h('code', { class: 'bot-stats-url', title: st.url ? String(st.url) : '', text: st.url ? String(st.url) : 'Not connected' })),
    h('span', { class: 'hint', text: 'Set database_url in config.yaml to use another database.' }),
    st.error ? h('div', { class: 'banner bad', role: 'alert' },
      h('div', { class: 'row nowrap bot-banner-row' }, icon('warn'), h('span', { class: 'grow', text: String(st.error) }))) : '',
    S.server && S.server.config.stats && !S.server.config.stats.enabled
      ? h('div', { class: 'banner warn', role: 'status' }, h('div', { class: 'row nowrap bot-banner-row' }, icon('info'),
        h('span', { class: 'grow', text: 'Chat isn’t being counted. Switch on “Count chat” and save to start.' }))) : '');
}

function paintStatsData() {
  const tot = ST.totals || (S.server && S.server.stats && S.server.stats.totals) || null;
  if (S.els.statsTotals) {
    const tile = (label, v) => h('div', { class: 'stat' }, h('div', { class: 'stat-label', text: label }),
      h('div', { class: 'stat-value', text: tot ? fmtN(v) : '—' }));
    S.els.statsTotals.replaceChildren(
      tile('Chatters', tot && tot.chatters), tile('Messages', tot && tot.messages), tile('Messages this stream', tot && tot.stream_messages));
  }
  const box = S.els.statsTop;
  if (!box) return;
  if (ST.err && !ST.top) { box.replaceChildren(h('p', { class: 'note warn', text: ST.err })); return; }
  if (!ST.top) {
    box.replaceChildren(h('div', { 'aria-busy': 'true', 'aria-label': 'Loading' }, [90, 75, 82, 68].map((w) => h('span', { class: 'skeleton line', style: { width: w + '%' } }))));
    return;
  }
  if (!ST.top.length) {
    box.replaceChildren(empty(ST.period === 'stream' ? 'Nobody has chatted this stream yet' : 'Nobody has chatted yet',
      'The table fills in as people chat while the bot is on.', 'layers'));
    return;
  }
  box.replaceChildren(h('div', { class: 'bot-table-wrap scroll' }, h('table', { class: 'table bot-top' },
    h('thead', {}, h('tr', {}, h('th', { scope: 'col', class: 'num', text: '#' }), h('th', { scope: 'col', text: 'Name' }), h('th', { scope: 'col', class: 'num', text: 'Messages' }))),
    h('tbody', {}, ST.top.map((r) => h('tr', { class: r.rank <= 3 ? `bot-podium p${r.rank}` : '' },
      h('td', { class: 'num bot-rank', text: String(r.rank) }),
      h('td', {}, h('button', { type: 'button', class: 'bot-name-link', title: 'Show this viewer’s stats', text: String(r.display || r.login || ''),
        onclick: () => lookup(String(r.login || '')) }),
      r.display && r.login && String(r.display).toLowerCase() !== String(r.login) ? h('span', { class: 'muted bot-login', text: String(r.login) }) : ''),
      h('td', { class: 'num', text: fmtN(r.messages) })))))),
  ST.err ? h('p', { class: 'note warn', text: `Couldn’t refresh: ${ST.err}` }) : '');
}

const LOOKUP_RE = /^@?[A-Za-z0-9_]{3,25}$/;

function lookupBox() {
  const input = h('input', { type: 'search', placeholder: 'Twitch name', autocomplete: 'off', spellcheck: 'false', maxlength: 26 });
  const box = fld('Look up a viewer', h('div', { class: 'row nowrap' }, input,
    h('button', { type: 'submit', class: 'small' }, icon('search'), 'Look up')));
  const result = h('div', { class: 'bot-lookup-out', 'aria-live': 'polite' });
  S.els.lookupInput = input;
  S.els.lookupOut = result;
  S.els.lookupField = box;
  const form = h('form', { class: 'bot-lookup', novalidate: true, role: 'search', onsubmit: (e) => { e.preventDefault(); lookup(input.value); } }, box, result);
  paintLookup();
  return form;
}

async function lookup(login) {
  const v = String(login || '').trim();
  if (S.els.lookupInput) S.els.lookupInput.value = v;
  if (!LOOKUP_RE.test(v)) { if (S.els.lookupField) S.els.lookupField.setError('A Twitch name: 3-25 letters, numbers or _.'); return; }
  if (S.els.lookupField) S.els.lookupField.setError('');
  ST.lookup = { state: 'loading', login: v };
  paintLookup();
  try {
    ST.lookup = { state: 'ok', row: await api(`/api/bot/stats/user?login=${encodeURIComponent(v)}`) };
  } catch (e) {
    ST.lookup = e.status === 404 ? { state: 'missing', login: v } : { state: 'error', msg: e.message };
  }
  paintLookup();
}

function paintLookup() {
  const out = S.els.lookupOut;
  if (!out) return;
  const L = ST.lookup;
  if (!L) { out.replaceChildren(); return; }
  if (L.state === 'loading') { out.replaceChildren(h('span', { class: 'skeleton block' })); return; }
  if (L.state === 'missing') { out.replaceChildren(h('p', { class: 'note', text: 'No stats for that viewer yet.' })); return; }
  if (L.state === 'error') { out.replaceChildren(h('p', { class: 'err', text: L.msg })); return; }
  const r = L.row || {};
  const item = (label, value) => h('div', { class: 'bot-kv' }, h('dt', { text: label }), h('dd', { text: value }));
  out.replaceChildren(h('article', { class: 'bot-viewer', 'aria-label': `Stats for ${String(r.display || r.login || '')}` },
    h('div', { class: 'bot-viewer-head' },
      h('span', { class: 'bot-viewer-avatar', 'aria-hidden': 'true', text: String(r.display || r.login || '?').slice(0, 1).toUpperCase() }),
      h('div', { class: 'bot-viewer-names' }, h('strong', { text: String(r.display || r.login || '') }), h('span', { class: 'muted', text: String(r.login || '') })),
      h('button', { type: 'button', class: 'small ghost icon-btn', 'aria-label': 'Close', title: 'Close', onclick: () => { ST.lookup = null; paintLookup(); } }, icon('x'))),
    h('dl', { class: 'bot-kvs' },
      item('Messages', fmtN(r.messages)),
      item('All-time rank', r.rank ? `#${fmtN(r.rank)}` : '—'),
      item('This stream', fmtN(r.stream_messages)),
      item('Stream rank', r.stream_rank ? `#${fmtN(r.stream_rank)}` : '—'),
      item('First seen', fmtDay(r.first_seen)),
      item('Last seen', r.last_seen ? ago(r.last_seen) : '—'),
      item('Redemptions', fmtN(r.redemptions)),
      item('Commands', fmtN(r.commands)))));
}

const EXCLUDE_RE = /^[a-z0-9_]{1,25}$/;

function excludeEditor(cfg) {
  if (!Array.isArray(cfg.exclude)) cfg.exclude = [];
  const list = h('ul', { class: 'bot-excl', 'aria-label': 'Excluded accounts' });
  const draw = () => list.replaceChildren(...(cfg.exclude.length ? cfg.exclude.map((login, i) => h('li', { class: 'bot-excl-chip' },
    h('span', { text: login }),
    h('button', { type: 'button', class: 'bot-excl-x', 'aria-label': `Count ${login} again`, title: 'Remove',
      onclick: () => { cfg.exclude.splice(i, 1); changed(); draw(); } }, icon('x'))))
    : [h('li', { class: 'muted bot-excl-none', text: 'No accounts excluded.' })]));
  draw();
  const input = h('input', { type: 'text', placeholder: 'nightbot', autocomplete: 'off', spellcheck: 'false', maxlength: 26 });
  const box = fld('Bots and accounts not counted', h('div', { class: 'row nowrap' }, input, h('button', { type: 'submit', class: 'small' }, icon('plus'), 'Add')),
    'They are never counted or shown.');
  const add = () => {
    const v = input.value.trim().replace(/^@/, '').toLowerCase();
    if (!v) return;
    if (!EXCLUDE_RE.test(v)) { box.setError('A Twitch login: a-z, 0-9 or _.'); return; }
    if (cfg.exclude.length >= 200) { box.setError('At most 200.'); return; }
    box.setError('');
    if (!cfg.exclude.includes(v)) { cfg.exclude.push(v); changed(); draw(); }
    input.value = '';
    input.focus();
  };
  return h('form', { class: 'bot-excl-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); add(); } }, box, list);
}

async function resetStats(scope) {
  const all = scope === 'all';
  const ok = await confirmDialog(all ? 'Delete all chat stats?' : 'Reset this stream’s counts?',
    all ? 'Every viewer’s message counts, ranks, redemptions and commands are deleted. A SQLite database is backed up first.'
      : 'Everyone’s “this stream” numbers go back to 0. All-time numbers stay.',
    { ok: all ? 'Delete everything' : 'Reset this stream', danger: true });
  if (!ok) return;
  try {
    const r = await api('/api/bot/stats/reset', { scope, confirm: true });
    toast(r.backup ? `Reset. Backup saved as ${r.backup}` : 'Reset', { ok: true, ms: r.backup ? 7000 : 3000 });
  } catch (e) {
    toast('Couldn’t reset: ' + e.message, { bad: true });
    return;
  }
  ST.lookup = null;
  paintLookup();
  await loadStats();
  refresh();
}

function startStatsTimer() {
  clearInterval(ST.timer);
  ST.timer = setInterval(loadStats, 30000);
}

/* ------------------------------------------------------------------ activity */

function feedKey(e) { return `${e.ts}|${e.kind}|${e.title}|${e.user}`; }

function mergeFeed(events) {
  let added = false;
  for (const e of events) {
    if (!isObj(e)) continue;
    const k = feedKey(e);
    if (S.feedKeys.has(k)) continue;
    S.feedKeys.add(k);
    S.feed.push(e);
    added = true;
  }
  if (!added) return false;
  S.feed.sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0));
  for (const e of S.feed.splice(FEED_MAX)) S.feedKeys.delete(feedKey(e));
  return true;
}

function eventTitle(e) {
  if (e.kind === 'command') {
    const c = S.draft && S.draft.commands.find((x) => x.id === e.title);
    return c && c.trigger ? c.trigger : `!${e.title}`;
  }
  return String(e.title || '');
}

function renderFeed() {
  const box = S.els.feed;
  if (!box) return;
  if (!S.feed.length) {
    box.replaceChildren(h('div', { class: 'card' }, empty('Nothing yet', 'Messages the bot posts, commands viewers use and rewards they redeem show up here as they happen.', 'layers')));
    return;
  }
  box.replaceChildren(h('div', { class: 'card flush' }, h('ol', { class: 'bot-feed', 'aria-label': 'Recent bot activity' }, S.feed.map((e) => {
    const [ic, label] = KINDS[e.kind] || KINDS.error;
    const bad = e.ok === false || e.kind === 'error';
    return h('li', { class: 'bot-ev' + (bad ? ' bad' : '') },
      h('span', { class: `bot-ev-icon ${e.kind}`, 'aria-hidden': 'true' }, icon(bad ? 'warn' : ic)),
      h('div', { class: 'bot-ev-main' },
        h('div', { class: 'bot-ev-line' },
          h('span', { class: 'bot-ev-kind', text: label }),
          h('span', { class: 'bot-ev-title', text: eventTitle(e) }),
          e.user ? h('span', { class: 'bot-ev-user', text: e.user === 'test' ? 'test run' : `by ${e.user}` }) : null),
        e.error ? h('div', { class: 'bot-ev-err', text: String(e.error) }) : null),
      h('time', { class: 'bot-ev-time', datetime: new Date((Number(e.ts) || 0) * 1000).toISOString(), title: ago(e.ts), text: timeOf(e.ts) }));
  }))));
}

/* ------------------------------------------------------------------ timer countdowns */

function dueText(t) {
  const srv = S.server, st = (srv && srv.status) || {};
  const saved = srv && srv.config.timers.find((x) => x.id === t.id);
  const live = saved && (st.timers || []).find((x) => x.id === t.id);
  if (!saved || !live) return ['', 'Starts once saved'];
  if (!srv.config.enabled) return ['', 'Bot is off'];
  if (!saved.enabled) return ['', 'Not posting'];
  if (st.paused) return ['warn', 'Paused'];
  if (srv.config.only_when_live && st.live === false) return ['', 'Waits until you’re live'];
  const left = Math.max(0, live.next_in - (Date.now() - S.statusAt) / 1000);
  if (left > 0) return ['accent', `Next in ${fmtDuration(left)}`];
  if (live.lines < saved.min_chat_lines) return ['', `Waiting for chat · ${live.lines}/${saved.min_chat_lines} lines`];
  return ['accent', 'Posting soon'];
}

function tick() {
  if (!S.els.due) return;
  const st = (S.server && S.server.status) || {};
  for (const { el, t } of S.els.due.values()) {
    if (!el.isConnected) continue;
    const [cls, text] = dueText(t);
    const live = (st.timers || []).find((x) => x.id === t.id);
    el.className = 'bot-due ' + cls;
    el.replaceChildren(icon('clock'), h('span', { text }),
      live && live.last_sent ? h('span', { class: 'bot-due-last', text: `· last ${ago(live.last_sent)}` }) : '');
  }
}

/* ------------------------------------------------------------------ lifecycle */

function onKey(e) {
  if (!S.visible) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault();
    if (S.dirty) save();
  }
}

export async function mount(root) {
  S.root = root;
  root.classList.add('bot-view');
  on('bot', (ev) => { if (mergeFeed([ev])) renderFeed(); scheduleRefresh(); });
  on('eventsub', (snap) => { if (S.server && isObj(snap)) { S.server.eventsub = snap; renderHead(); } });
  on('config', () => scheduleRefresh());
  on('twitch_auth', (a) => {
    if (!a || typeof a !== 'object' || !('linked' in a)) return;
    const linked = !!a.linked;
    if (linked !== S.linked) { S.linked = linked; if (S.mode === 'ready') { render(); refresh(); loadOnTwitch(); } }
  });
  document.addEventListener('keydown', onKey);
  window.addEventListener('beforeunload', (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ''; } });
  await load();
}

export function show() {
  S.visible = true;
  clearInterval(S.ticker);
  S.ticker = setInterval(tick, 1000);
  clearInterval(S.poller);
  S.poller = setInterval(refresh, 20000);
  startStatsTimer();
  if (S.mode === 'ready') { refresh(); refreshAuth(); loadStats(); }
}

export function hide() {
  S.visible = false;
  clearInterval(S.ticker);
  clearInterval(S.poller);
  clearInterval(ST.timer);
  S.ticker = S.poller = ST.timer = 0;
}
