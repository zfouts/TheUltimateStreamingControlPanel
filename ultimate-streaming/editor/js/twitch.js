/* Twitch view.

   Not set up yet: a three-step guide (channel, Twitch app, device-code link) that updates
   live from the `twitch_auth` event. Linked: stream info (title, category, tags), chat with
   sending and moderation, chat modes and channel tools.

   Every string from the daemon or Twitch goes through h()/textContent. Chat colors only via
   safeColor(), avatars and box art only from static-cdn.jtvnw.net, links only https. */
import { api, on, ApiError } from './api.js';
import { h, icon, toast, openModal, closeModal, confirmDialog, switchInput, asyncButton, chip, empty, safeColor, fmtDuration, debounce } from './ui.js';
import { snapshot } from './main.js';

const CDN = 'https://static-cdn.jtvnw.net/';
const LOGIN_RE = /^[A-Za-z0-9_]{3,25}$/;
const CLIENT_ID_RE = /^[A-Za-z0-9]{10,64}$/;
const TAG_RE = /^[A-Za-z0-9]{1,25}$/;
const TITLE_MAX = 140, TAGS_MAX = 10, CHAT_MAX = 500, CHAT_CAP = 300, MARKER_MAX = 140;
const AD_LENGTHS = [[30, '30s'], [60, '1m'], [90, '1m 30s'], [120, '2m'], [150, '2m 30s'], [180, '3m']];
const ANNOUNCE_COLORS = [['primary', 'Default'], ['blue', 'Blue'], ['green', 'Green'], ['orange', 'Orange'], ['purple', 'Purple']];
const SCOPE_NAMES = {
  'clips:edit': 'clips',
  'channel:edit:commercial': 'ads',
  'channel:manage:broadcast': 'title, category and markers',
  'moderator:manage:chat_settings': 'chat modes',
  'user:write:chat': 'sending chat',
  'moderator:manage:chat_messages': 'deleting messages',
  'moderator:manage:banned_users': 'bans and timeouts',
  'moderator:manage:announcements': 'announcements',
  'moderator:manage:shoutouts': 'shoutouts',
  'channel:manage:raids': 'raids',
  'moderator:read:followers': 'follow alerts',
  'channel:read:subscriptions': 'sub alerts',
  'bits:read': 'cheer alerts',
};
const BADGES = {
  broadcaster: ['B', 'Broadcaster'], moderator: ['M', 'Moderator'], vip: ['V', 'VIP'], subscriber: ['S', 'Subscriber'],
  founder: ['F', 'Founder'], partner: ['P', 'Partner'], staff: ['St', 'Twitch staff'], turbo: ['T', 'Turbo'],
  premium: ['Pr', 'Prime Gaming'], bits: ['b', 'Bits'],
};
const MODES = [
  ['emote', 'Emote only', 'Messages can only contain emotes.'],
  ['followers', 'Followers only', 'Only people who follow you can chat.'],
  ['slow', 'Slow mode', ''],
  ['subs', 'Subscribers only', 'Only subscribers can chat.'],
  ['unique', 'Unique messages', 'Blocks repeated, identical messages.'],
];

/* Icons ui.js doesn't have (static path literals, same markup as ui.icon). */
const PATHS = {
  chat: 'M4 5h16v11H9l-5 4z',
  shield: 'M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z',
  ban: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM5.6 5.6l12.8 12.8',
  megaphone: 'M3 10v4h3l7 4V6L6 10zm14-1.5a4.5 4.5 0 0 1 0 7',
  users: 'M9 4a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM2 21a7 7 0 0 1 14 0m0-17a4 4 0 0 1 0 8m3 9a7 7 0 0 0-3-5.7',
  eyeOff: 'M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6 0 10 7 10 7a17 17 0 0 1-3.2 3.9M6.6 6.6A17 17 0 0 0 2 12s4 7 10 7a10 10 0 0 0 5.4-1.6M9.9 9.9a3 3 0 0 0 4.2 4.2',
  tag: 'M3 12V3h9l9 9-9 9zM7.5 7.5h.01',
};
function ic(name) {
  if (!PATHS[name]) return icon(name);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'ic');
  svg.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', PATHS[name]);
  svg.append(p);
  return svg;
}

/* ------------------------------------------------------------------ state */

const S = {
  root: null,
  visible: false,
  auth: null,              // last GET /api/twitch/auth (or twitch_auth event)
  authErr: '',             // the auth state couldn't be loaded
  mode: '',                // loading | error | setup | connected
  editing: 0,              // setup step re-opened with "Change"
  celebrating: false,      // the short "linked" moment between setup and connected
  loginErr: '',            // POST /api/twitch/login failed (setup step 3)
  channelName: '',         // the configured channel (from the chat module's snapshot)
  chatReading: false,
  live: null,              // {live, viewers, started_at, ...} | null
  scopeError: '',          // a 403 that missing_scopes doesn't explain
  hideCode: readPref('tw_hide_code') === '1',
  chat: [],                // recent messages (cap CHAT_CAP)
  chatIds: new Set(),
  si: { status: 'idle', err: '', loaded: null, form: null, art: {} },
  modes: { status: 'idle', err: '', data: null },
  relink: null,            // {body, code, started} while the re-link dialog is open
  ticker: 0,
  poller: 0,
  els: {},                 // live elements of the connected view
  inputs: {},              // setup inputs, kept across re-renders so typing survives
};

function readPref(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function writePref(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } }

const hasScope = (s) => !!(S.auth && S.auth.linked && (S.auth.scopes || []).includes(s));
const isConnected = (a) => !!(a && a.linked && a.configured && a.configured.channel);
const login = () => (S.auth && S.auth.user && S.auth.user.login) || S.channelName || '';
const displayName = () => (S.auth && S.auth.user && (S.auth.user.display_name || S.auth.user.login)) || S.channelName || 'Twitch';
const httpsOnly = (u) => (typeof u === 'string' && u.startsWith('https://') ? u : '');
const cdnOnly = (u) => (typeof u === 'string' && u.startsWith(CDN) ? u : '');
const boxArt = (u) => cdnOnly(u).replace('{width}x{height}', '52x72');
const settle = () => new Promise((r) => setTimeout(r, 1200));   // the daemon reloads config.yaml within ~1 s
const MISMATCH = /^Linked account /;

/** api() that turns 401/403 into a refreshed auth state (the banners explain) and a friendly message. */
async function call(path, body) {
  try {
    return await api(path, body);
  } catch (e) {
    if (e.status === 401 || e.status === 403) {
      if (e.status === 403) S.scopeError = e.message;
      refreshAuth();
      throw new ApiError(e.status === 401
        ? 'Twitch isn’t linked any more. Link it again at the top of this page.'
        : 'Twitch needs another permission for this. Use Re-link at the top of this page.', e.status);
    }
    throw e;
  }
}

async function copyText(text, what) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${what} copied`, { ok: true, ms: 1800 });
  } catch (e) {
    const ta = h('textarea', { value: text, readonly: true, 'aria-hidden': 'true', class: 'tw-offscreen' });
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
    ta.remove();
    toast(ok ? `${what} copied` : 'Couldn’t copy. Select the text and press Ctrl+C.', { ok, bad: !ok, ms: 2200 });
  }
}

function extLink(href, text, cls = 'btn') {
  const url = httpsOnly(href);
  if (!url) return null;
  return h('a', { class: cls, href: url, target: '_blank', rel: 'noopener noreferrer' }, text, icon('external'));
}

function skeletonLines(n) {
  return h('div', { class: 'tw-skel', 'aria-busy': 'true', 'aria-label': 'Loading' },
    Array.from({ length: n }, (_, i) => h('span', { class: 'skeleton line', style: { width: `${90 - (i % 3) * 18}%` } })));
}

function loadError(text, retry) {
  return h('div', { class: 'tw-load-err', role: 'alert' }, icon('warn'), h('span', { class: 'grow', text }),
    h('button', { class: 'small', onclick: retry }, icon('refresh'), 'Try again'));
}

function lockedNote(text) {
  return h('div', { class: 'tw-locked' }, icon('lock'), h('span', { class: 'grow', text }),
    h('button', { class: 'small', onclick: openRelink }, icon('refresh'), 'Re-link'));
}

/* ------------------------------------------------------------------ auth */

async function refreshAuth() {
  try {
    applyAuth(await api('/api/twitch/auth'));
  } catch (e) {
    S.authErr = e.message || 'The daemon didn’t answer';
    if (!S.auth) render();
  }
}

function applyAuth(a) {
  if (!a || typeof a !== 'object' || !a.configured) return;
  const prev = S.auth;
  S.auth = a;
  S.authErr = '';
  if (a.pending || a.linked) S.loginErr = '';
  if (a.user && a.user.login && !S.channelName) S.channelName = a.user.login;
  if (S.relink) updateRelink(prev, a);
  const scopesChanged = prev && (prev.scopes || []).join() !== (a.scopes || []).join();
  if (scopesChanged && !(a.missing_scopes || []).length) S.scopeError = '';

  const justLinked = prev && !prev.linked && a.linked;
  if (justLinked && !S.relink) toast(`Twitch linked as ${(a.user && a.user.display_name) || 'your account'}`, { ok: true });

  const mode = isConnected(a) ? 'connected' : 'setup';
  if (S.celebrating) return;
  if (mode === 'connected' && S.mode === 'setup' && justLinked) {
    // Show the finished checklist for a moment, then move on to the connected layout.
    S.celebrating = true;
    renderSetup();
    setTimeout(() => { S.celebrating = false; render(true); }, 900);
    return;
  }
  if (mode !== S.mode) { render(); return; }
  if (mode === 'setup') renderSetup();
  else if (scopesChanged) render();
  else renderHead();
}

/* ------------------------------------------------------------------ the code display (setup step 3 and re-link) */

const expiryHandlers = new WeakMap();

function codeLabel(code) { return S.hideCode ? 'Code (hidden)' : `Code ${code.split('').join(' ')}`; }

function codeDisplay(p, onRetry) {
  const code = String(p.user_code || '');
  const codeEl = h('output', { class: 'tw-code' + (S.hideCode ? ' is-hidden' : ''), 'aria-label': codeLabel(code) }, code);
  const hideBtn = h('button', { class: 'small ghost tw-hide-btn', type: 'button', onclick: () => {
    S.hideCode = !S.hideCode;
    writePref('tw_hide_code', S.hideCode ? '1' : '0');
    for (const el of document.querySelectorAll('.tw-code')) {
      el.classList.toggle('is-hidden', S.hideCode);
      el.setAttribute('aria-label', codeLabel(el.textContent));
    }
    for (const b of document.querySelectorAll('.tw-hide-btn')) setHideBtn(b);
  } });
  setHideBtn(hideBtn);
  const countdown = h('span', { class: 'tw-countdown', dataset: { expires: String(p.expires_at || 0) } });
  const wait = h('div', { class: 'tw-wait', role: 'status' },
    h('span', { class: 'tw-spinner', 'aria-hidden': 'true' }), h('span', { text: 'Waiting for you to approve…' }), countdown);
  const box = h('div', { class: 'tw-code-box' },
    h('p', { class: 'tw-code-label', text: 'Enter this code on Twitch' }),
    codeEl,
    h('div', { class: 'row tw-code-tools' },
      h('button', { class: 'small ghost', type: 'button', onclick: () => copyText(code, 'Code') }, icon('copy'), 'Copy code'),
      hideBtn),
    extLink(p.verification_uri || 'https://www.twitch.tv/activate', 'Open twitch.tv/activate', 'btn twitch tw-activate'),
    wait,
    h('p', { class: 'hint tw-code-safety' }, icon('lock'), h('span', { text: 'Anyone who sees this code could use it. Keep it off stream, or hide it.' })));
  expiryHandlers.set(countdown, () => {
    box.classList.add('expired');
    wait.replaceChildren(icon('clock'), h('span', { text: 'This code expired.' }),
      asyncButton({ class: 'small twitch' }, onRetry, 'Get a new code'));
    refreshAuth();
  });
  updateCountdown(countdown);
  return box;
}

function setHideBtn(b) {
  b.setAttribute('aria-pressed', String(S.hideCode));
  b.replaceChildren(ic(S.hideCode ? 'eye' : 'eyeOff'), S.hideCode ? 'Show code' : 'Hide code');
}

function updateCountdown(el) {
  const left = Number(el.dataset.expires) - Date.now() / 1000;
  if (left > 0) { el.textContent = `Expires in ${fmtDuration(left)}`; return; }
  if (el.dataset.done) return;
  el.dataset.done = '1';
  const fn = expiryHandlers.get(el);
  if (fn) fn();
}

function tick() {
  for (const el of document.querySelectorAll('.tw-countdown')) updateCountdown(el);
  for (const el of document.querySelectorAll('.tw-uptime')) {
    const t = Date.parse(el.dataset.since);
    if (t) el.textContent = fmtDuration((Date.now() - t) / 1000);
  }
}

async function startLogin() {
  S.loginErr = '';
  try {
    applyAuth(await call('/api/twitch/login', {}));
    const link = S.root.querySelector('.tw-activate');
    if (link) link.focus();
  } catch (e) {
    S.loginErr = e.message;
    if (S.mode === 'setup') renderSetup();
  }
}

/* ------------------------------------------------------------------ setup */

function setupInputs() {
  if (S.inputs.channel) return S.inputs;
  const channel = h('input', { type: 'text', id: 'tw-channel', placeholder: 'yourname', autocomplete: 'off', spellcheck: 'false',
    maxlength: 80, 'aria-describedby': 'tw-channel-err' });
  const clientId = h('input', { type: 'text', id: 'tw-client-id', class: 'mono', placeholder: 'Paste the Client ID', autocomplete: 'off',
    spellcheck: 'false', maxlength: 80, 'aria-describedby': 'tw-client-err' });
  S.inputs = { channel, clientId, channelErr: h('p', { class: 'err', id: 'tw-channel-err', role: 'alert' }), clientErr: h('p', { class: 'err', id: 'tw-client-err', role: 'alert' }) };
  return S.inputs;
}

/** "https://twitch.tv/Name/", "@name", "#name" -> "Name". */
function normaliseChannel(raw) {
  let v = String(raw || '').trim();
  const m = v.match(/twitch\.tv\/([^/?#\s]+)/i);
  if (m) v = m[1];
  return v.replace(/^[@#]+/, '').replace(/\/+$/, '');
}

async function saveChannel(input, errEl, after) {
  const v = normaliseChannel(input.value);
  input.value = v;
  if (!LOGIN_RE.test(v)) {
    errEl.textContent = v ? 'That isn’t a Twitch username: 3–25 letters, numbers or _.' : 'Type your Twitch username first.';
    input.setAttribute('aria-invalid', 'true');
    input.focus();
    return false;
  }
  errEl.textContent = '';
  input.removeAttribute('aria-invalid');
  try {
    await call('/api/twitch/config', { channel: v });
  } catch (e) {
    errEl.textContent = e.message;
    return false;
  }
  S.channelName = v.toLowerCase();
  await settle();
  await refreshAuth();
  toast('Channel saved', { ok: true, ms: 2000 });
  if (after) after();
  return true;
}

async function saveClientId() {
  const { clientId, clientErr } = setupInputs();
  const v = clientId.value.trim();
  clientId.value = v;
  if (!CLIENT_ID_RE.test(v)) {
    clientErr.textContent = v ? 'That doesn’t look like a Client ID (10–64 letters and numbers). Don’t paste the secret.' : 'Paste the Client ID first.';
    clientId.setAttribute('aria-invalid', 'true');
    clientId.focus();
    return;
  }
  clientErr.textContent = '';
  clientId.removeAttribute('aria-invalid');
  let r;
  try { r = await call('/api/twitch/config', { client_id: v }); } catch (e) { clientErr.textContent = e.message; return; }
  if (r.relink) toast('Twitch was unlinked because the app changed; link again', { ms: 6000 });
  S.editing = 0;
  await settle();
  await refreshAuth();
  toast('Client ID saved', { ok: true, ms: 2000 });
  focusCurrentStep();
}

function focusCurrentStep() {
  const el = S.root.querySelector('.tw-step.expanded input, .tw-step.expanded .tw-step-cta');
  if (el) el.focus();
}

function copyChip(value, label) {
  return h('span', { class: 'tw-copy' }, h('code', { class: 'mono', text: value }),
    h('button', { class: 'ghost icon-btn small', type: 'button', 'aria-label': `Copy ${label}`, title: 'Copy', onclick: () => copyText(value, label) }, icon('copy')));
}

function stepChannel() {
  const { channel, channelErr } = setupInputs();
  const save = asyncButton({ class: 'primary', type: 'submit' },
    () => saveChannel(channel, channelErr, () => { S.editing = 0; renderSetup(); focusCurrentStep(); }), 'Save channel');
  return h('form', { class: 'tw-step-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); save.click(); } },
    h('p', { class: 'muted', text: 'The Twitch username you stream from. That alone is enough to show your chat here.' }),
    h('label', { class: 'sr-only', for: 'tw-channel', text: 'Twitch username' }),
    h('div', { class: 'tw-inline-form' },
      h('div', { class: 'tw-prefixed' }, h('span', { class: 'tw-prefix', 'aria-hidden': 'true', text: 'twitch.tv/' }), channel),
      save),
    channelErr);
}

function stepApp() {
  const { clientId, clientErr } = setupInputs();
  const save = asyncButton({ class: 'primary', type: 'submit' }, saveClientId, 'Save Client ID');
  return h('div', { class: 'tw-step-form' },
    h('p', { class: 'muted', text: 'Twitch needs a free “app” of your own so the deck can act for you. It takes a couple of minutes.' }),
    h('ol', { class: 'steps tw-app-steps' },
      h('li', {}, 'Open the ', extLink('https://dev.twitch.tv/console/apps', 'Twitch developer console', 'tw-link'),
        ', sign in, and click ', h('strong', { text: 'Register Your Application' }), '.'),
      h('li', {}, 'Fill in the form exactly like this:',
        h('dl', { class: 'tw-fields' },
          h('dt', { text: 'Name' }), h('dd', {}, copyChip('My Stream Deck', 'name'), h('span', { class: 'hint', text: 'or anything you like' })),
          h('dt', { text: 'OAuth Redirect URLs' }), h('dd', {}, copyChip('http://localhost', 'redirect URL')),
          h('dt', { text: 'Category' }), h('dd', {}, h('kbd', { text: 'Broadcaster Suite' })),
          h('dt', { text: 'Client Type' }), h('dd', {}, h('kbd', { text: 'Public' }), h('span', { class: 'hint tw-important', text: 'not Confidential' })))),
      h('li', {}, 'Click ', h('strong', { text: 'Create' }), ', then ', h('strong', { text: 'Manage' }), ' next to your app, and copy the ',
        h('strong', { text: 'Client ID' }), '. You don’t need the client secret.'),
      h('li', {}, h('label', { for: 'tw-client-id', text: 'Paste the Client ID here:' }),
        h('form', { class: 'tw-inline-form mt-2', novalidate: true, onsubmit: (e) => { e.preventDefault(); save.click(); } }, clientId, save),
        clientErr)),
    h('p', { class: 'hint tw-note' }, icon('info'), h('span', { text: 'Twitch asks you to turn on two-factor authentication before you can register an app.' })));
}

function stepLink(a) {
  if (a.pending) return h('div', { class: 'tw-step-form' }, codeDisplay(a.pending, startLogin));
  const err = S.loginErr || (!a.linked && a.error) || '';
  return h('div', { class: 'tw-step-form' },
    h('p', { class: 'muted', text: 'You’ll get a short code to enter on twitch.tv/activate. Approve it there and this page updates by itself.' }),
    err ? h('div', { class: 'banner bad', role: 'alert' }, h('div', { class: 'row nowrap' }, icon('warn'), h('span', { class: 'grow', text: err }))) : null,
    h('div', { class: 'row' },
      asyncButton({ class: 'twitch tw-step-cta' }, startLogin, icon('twitch'), err ? 'Try again' : 'Link with Twitch'),
      h('span', { class: 'hint', text: 'Do this off stream: the code shows on screen.' })));
}

/** While linked, the Twitch app can only be changed after unlinking (the token belongs to it). */
const lockedStep = (n) => n === 2 && S.auth && S.auth.linked;

function renderSetup() {
  const a = S.auth;
  const linked = !!a.linked;
  const done = [!!a.configured.channel, !!(a.configured.client_id || linked), linked];
  const current = done.indexOf(false) + 1;      // 0 = all done
  const open = S.editing || current;
  const name = a.user && (a.user.display_name || a.user.login);
  const steps = [
    ['Your channel', S.channelName ? `twitch.tv/${S.channelName}` : 'Saved', stepChannel],
    ['Connect a Twitch app', 'Client ID saved', stepApp],
    ['Link your account', name ? `Linked as ${name}` : 'Linked', () => stepLink(a)],
  ];
  const doneCount = done.filter(Boolean).length;

  const list = h('ol', { class: 'tw-steps' }, steps.map(([title, summary, body], i) => {
    const n = i + 1, isDone = done[i], expanded = open === n && !(isDone && n === 3);
    const state = expanded ? 'current' : isDone ? 'done' : 'upcoming';
    const canChange = isDone && n < 3 && open !== n && !lockedStep(n) && !S.celebrating;
    return h('li', { class: `tw-step ${state}${expanded ? ' expanded' : ''}`, 'aria-current': expanded ? 'step' : null },
      h('div', { class: 'tw-step-head' },
        h('span', { class: 'tw-step-mark', 'aria-hidden': 'true' }, isDone && !expanded ? icon('check') : String(n)),
        h('div', { class: 'tw-step-titles' },
          h('h3', { class: 'tw-step-title' }, h('span', { class: 'sr-only', text: `Step ${n}${isDone ? ', done' : ''}: ` }), title),
          isDone && !expanded ? h('p', { class: 'tw-step-summary', text: summary }) : null),
        canChange ? h('button', { class: 'small ghost', type: 'button', 'aria-label': `Change ${title.toLowerCase()}`,
          onclick: () => { S.editing = n; renderSetup(); focusCurrentStep(); } }, 'Change') : null,
        S.editing === n ? h('button', { class: 'small ghost', type: 'button', onclick: () => { S.editing = 0; renderSetup(); } }, 'Cancel') : null),
      expanded ? h('div', { class: 'tw-step-body' }, body()) : null);
  }));

  const card = h('section', { class: 'card tw-setup' + (S.celebrating ? ' celebrate' : ''), 'aria-labelledby': 'tw-setup-title' },
    h('div', { class: 'card-head' },
      h('span', { class: 'card-icon ' + (S.celebrating ? 'ok' : 'twitch') }, icon(S.celebrating ? 'check' : 'twitch')),
      h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', id: 'tw-setup-title', text: S.celebrating ? 'You’re all set' : 'Set up Twitch' }),
        h('p', { class: 'card-sub', text: S.celebrating ? 'Opening your Twitch controls…' : `${doneCount} of 3 done · about five minutes` }))),
    h('div', { class: 'progress tw-progress', 'aria-hidden': 'true' }, h('span', { style: { width: `${Math.round(100 * doneCount / 3)}%` } })),
    list);

  const aside = h('aside', { class: 'card inset tw-why', 'aria-labelledby': 'tw-why-title' },
    h('h2', { class: 'card-title', id: 'tw-why-title', text: 'Once linked, you can' }),
    h('ul', { class: 'tw-why-list' },
      [['edit', 'Change your title, category and tags'], ['chat', 'Chat, delete messages, time out and ban'], ['shield', 'Switch chat modes'],
        ['bolt', 'Run ads, add markers, clip, raid and shout out']].map(([i, t]) => h('li', {}, ic(i), h('span', { text: t })))),
    h('p', { class: 'hint tw-why-foot' }, icon('lock'), h('span', { text: 'Your Twitch login stays on this computer. Nothing here asks for your password.' })));

  const focused = document.activeElement;
  S.root.replaceChildren(h('div', { class: 'page tw-page' },
    h('header', { class: 'page-head' }, h('div', { class: 'page-titles' },
      h('h1', { class: 'page-title', text: 'Twitch' }),
      h('p', { class: 'page-sub', text: 'Link your channel to control it from here and from the deck.' }))),
    h('div', { class: 'tw-setup-grid' }, card, aside)));
  if (focused && focused.isConnected && S.root.contains(focused)) focused.focus();
}

/* ------------------------------------------------------------------ connected: head, banners, menu */

function renderHead() {
  const { head, banners } = S.els;
  if (!head || !head.isConnected) return;
  const a = S.auth, user = a.user || {};
  const avatarUrl = cdnOnly(user.avatar);
  const initial = () => h('span', { class: 'tw-avatar tw-avatar-ph', 'aria-hidden': 'true', text: displayName().slice(0, 1).toUpperCase() });
  const avatar = avatarUrl
    ? h('img', { class: 'tw-avatar', src: avatarUrl, alt: '', width: 56, height: 56, referrerpolicy: 'no-referrer',
      onerror: (e) => e.currentTarget.replaceWith(initial()) })
    : initial();
  const lg = login();
  head.replaceChildren(
    h('div', { class: 'tw-ident' }, avatar,
      h('div', { class: 'page-titles' },
        h('h1', { class: 'page-title', text: displayName() }),
        h('div', { class: 'tw-live-line' }, liveLine()))),
    h('div', { class: 'page-actions' },
      lg ? extLink(`https://www.twitch.tv/${lg}`, 'Open channel') : null,
      lg ? extLink(`https://dashboard.twitch.tv/u/${lg}/stream-manager`, 'Stream manager') : null,
      menuButton()));

  const out = [];
  const missing = a.missing_scopes || [];
  if (missing.length || S.scopeError) {
    const names = missing.map((s) => SCOPE_NAMES[s] || s);
    out.push(h('div', { class: 'banner warn', role: 'status' }, h('div', { class: 'row nowrap tw-banner-row' }, icon('warn'),
      h('span', { class: 'grow', text: names.length ? `Re-link to enable: ${names.join(', ')}.` : S.scopeError }),
      h('button', { class: 'small', onclick: openRelink }, icon('refresh'), 'Re-link'))));
  }
  if (a.error) {
    const mismatch = MISMATCH.test(a.error);
    out.push(h('div', { class: 'banner bad', role: 'alert' }, h('div', { class: 'row nowrap tw-banner-row' }, icon('warn'),
      h('span', { class: 'grow', text: mismatch ? `${a.error}. Link the right account, or change the channel.` : a.error }),
      mismatch ? h('button', { class: 'small ghost', onclick: openChannelDialog }, 'Change channel') : null,
      h('button', { class: 'small', onclick: openRelink }, icon('refresh'), 'Re-link'))));
  }
  banners.replaceChildren(...out);
  banners.hidden = !out.length;
}

function liveLine() {
  const l = S.live;
  if (!l) return [h('span', { class: 'skeleton line tw-live-skel' })];
  const lg = login();
  if (!l.live) return [chip('', 'Offline'), lg ? h('span', { class: 'muted', text: `twitch.tv/${lg}` }) : null];
  return [chip('live', 'LIVE'),
    h('span', { class: 'tw-viewers' }, ic('users'), h('strong', { text: Number(l.viewers || 0).toLocaleString() }), ' viewers'),
    Date.parse(l.started_at) ? h('span', { class: 'muted' }, 'Live for ',
      h('span', { class: 'tw-uptime', dataset: { since: String(l.started_at) }, text: fmtDuration((Date.now() - Date.parse(l.started_at)) / 1000) })) : null];
}

function updateLive() {
  const el = S.root && S.root.querySelector('.tw-live-line');
  if (el) el.replaceChildren(...liveLine().filter(Boolean));
  const note = S.els.toolsNote;
  if (note) note.hidden = !!(S.live && S.live.live);
}

async function loadStream() {
  if (S.mode !== 'connected') return;
  try { S.live = await call('/api/twitch/stream'); } catch (e) { if (!S.live) S.live = { live: false }; }
  updateLive();
}

let menuCleanup = null;
function menuButton() {
  const btn = h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'More Twitch options', title: 'More', 'aria-haspopup': 'menu', 'aria-expanded': 'false' }, icon('dots'));
  const wrap = h('div', { class: 'tw-menu-wrap' }, btn);
  const close = (refocus) => {
    const m = wrap.querySelector('.tw-menu');
    if (m) m.remove();
    btn.setAttribute('aria-expanded', 'false');
    if (menuCleanup) { menuCleanup(); menuCleanup = null; }
    if (refocus) btn.focus();
  };
  const item = (iconName, text, fn, danger) => h('button', { role: 'menuitem', type: 'button', class: 'tw-menu-item' + (danger ? ' danger' : ''), tabindex: '-1',
    onclick: () => { close(false); fn(); } }, ic(iconName), text);
  btn.addEventListener('click', () => {
    if (wrap.querySelector('.tw-menu')) { close(true); return; }
    const menu = h('div', { class: 'tw-menu', role: 'menu', 'aria-label': 'Twitch options' },
      item('refresh', 'Re-link account', openRelink),
      item('edit', 'Change channel', openChannelDialog),
      h('div', { class: 'tw-menu-sep', role: 'separator' }),
      item('x', 'Unlink Twitch', unlink, true));
    const items = [...menu.querySelectorAll('[role="menuitem"]')];
    menu.addEventListener('keydown', (e) => {
      const i = items.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length].focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(true); }
      else if (e.key === 'Tab') close(false);
    });
    wrap.append(menu);
    btn.setAttribute('aria-expanded', 'true');
    items[0].focus();
    const outside = (e) => { if (!wrap.contains(e.target)) close(false); };
    setTimeout(() => document.addEventListener('mousedown', outside));
    menuCleanup = () => document.removeEventListener('mousedown', outside);
  });
  return wrap;
}

async function unlink() {
  const ok = await confirmDialog('Unlink Twitch?',
    'The deck and this page stop controlling your channel until you link again. Chat keeps showing.', { ok: 'Unlink', danger: true });
  if (!ok) return;
  try {
    const r = await api('/api/twitch/logout', {});
    toast(r.revoked ? 'Twitch unlinked' : 'Unlinked here, but Twitch didn’t confirm. You can also disconnect the app under Connections on twitch.tv.',
      { ok: !!r.revoked, ms: r.revoked ? 3000 : 7000 });
  } catch (e) {
    toast(e.message, { bad: true });
  }
  refreshAuth();
}

function openChannelDialog() {
  const input = h('input', { type: 'text', id: 'tw-channel-dlg', value: S.channelName || login(), autocomplete: 'off', spellcheck: 'false', maxlength: 80 });
  const err = h('p', { class: 'err', role: 'alert' });
  const save = asyncButton({ class: 'primary', type: 'submit' }, async () => { if (await saveChannel(input, err)) closeModal(); }, 'Save channel');
  openModal('Change channel', h('form', { novalidate: true, onsubmit: (e) => { e.preventDefault(); save.click(); } },
    h('p', { class: 'hint', text: 'The Twitch username you stream from. It should be the account you linked.' }),
    h('label', { class: 'field mt-3' }, h('span', { class: 'label', text: 'Twitch username' }),
      h('div', { class: 'tw-prefixed' }, h('span', { class: 'tw-prefix', 'aria-hidden': 'true', text: 'twitch.tv/' }), input)),
    err,
    h('div', { class: 'row end mt-3' }, h('button', { class: 'ghost', type: 'button', onclick: closeModal }, 'Cancel'), save)));
}

/* ------------------------------------------------------------------ re-link dialog */

async function openRelink() {
  const body = h('div', { class: 'tw-relink' });
  S.relink = { body, code: null, started: false };
  openModal('Re-link Twitch', body);
  body.replaceChildren(h('div', { class: 'tw-wait', role: 'status' }, h('span', { class: 'tw-spinner', 'aria-hidden': 'true' }), 'Getting a code from Twitch…'));
  await relinkStart();
}

async function relinkStart() {
  const rl = S.relink;
  if (!rl) return;
  rl.code = null;
  try {
    const a = await call('/api/twitch/login', {});
    rl.started = true;
    applyAuth(a);
  } catch (e) {
    relinkError(e.message);
  }
}

function relinkError(msg) {
  const rl = S.relink;
  if (!rl || !rl.body.isConnected) { S.relink = null; return; }
  rl.code = null;
  rl.body.replaceChildren(h('div', { class: 'banner bad', role: 'alert' }, h('div', { class: 'row nowrap' }, icon('warn'), h('span', { class: 'grow', text: msg }))),
    h('div', { class: 'row end mt-3' }, h('button', { class: 'ghost', type: 'button', onclick: closeModal }, 'Close'),
      asyncButton({ class: 'twitch' }, relinkStart, 'Try again')));
}

function updateRelink(prev, a) {
  const rl = S.relink;
  if (!rl.body.isConnected) { S.relink = null; return; }   // the dialog was closed
  if (!rl.started) return;
  if (a.pending) {
    if (rl.code === a.pending.user_code) return;
    rl.code = a.pending.user_code;
    rl.body.replaceChildren(
      h('p', { class: 'hint', text: 'Approve on Twitch to grant the new permissions. Your current link keeps working until then.' }),
      codeDisplay(a.pending, relinkStart));
    const link = rl.body.querySelector('.tw-activate');
    if (link) link.focus();
    return;
  }
  if (prev && prev.pending && !a.pending) {
    if (a.error && !MISMATCH.test(a.error)) { relinkError(a.error); return; }
    S.relink = null;
    S.scopeError = '';
    closeModal();
    toast(`Twitch re-linked${a.user && a.user.display_name ? ` as ${a.user.display_name}` : ''}`, { ok: true });
  }
}

/* ------------------------------------------------------------------ connected layout */

function render(animate) {
  const a = S.auth;
  if (!a) {
    S.mode = S.authErr ? 'error' : 'loading';
    S.root.replaceChildren(h('div', { class: 'page tw-page' },
      h('header', { class: 'page-head' }, h('div', { class: 'page-titles' }, h('h1', { class: 'page-title', text: 'Twitch' }))),
      S.authErr
        ? h('section', { class: 'card' }, empty('Can’t load your Twitch settings', S.authErr, 'warn'),
          h('div', { class: 'row tw-center' }, asyncButton({ class: 'primary' }, refreshAuth, icon('refresh'), 'Try again')))
        : h('section', { class: 'card tw-setup' }, skeletonLines(6))));
    return;
  }
  const mode = isConnected(a) ? 'connected' : 'setup';
  S.mode = mode;
  if (mode === 'setup') { S.els = {}; renderSetup(); return; }
  S.editing = 0;
  buildConnected(animate);
}

function buildConnected(animate) {
  const head = h('header', { class: 'page-head tw-head' });
  const banners = h('div', { class: 'tw-banners', hidden: true });
  S.els = { head, banners };
  const page = h('div', { class: 'page tw-page' + (animate ? ' tw-enter' : '') }, head, banners,
    h('div', { class: 'tw-grid' },
      h('div', { class: 'tw-col' }, streamCard(), toolsCard()),
      h('div', { class: 'tw-col' }, chatCard(), modesCard())));
  S.root.replaceChildren(page);
  renderHead();
  renderStreamInfo();
  renderModes();
  renderFeed();
  if (S.si.status === 'idle' || S.si.status === 'error') loadChannel();
  if (S.modes.status === 'idle' || S.modes.status === 'error') loadModes();
  loadStream();
}

/* ------------------------------------------------------------------ stream info */

function streamCard() {
  const body = h('div', { class: 'tw-si-body' });
  const recent = h('select', { class: 'tw-recent', 'aria-label': 'Load a recent setup', hidden: true });
  S.els.si = body;
  S.els.recent = recent;
  recent.addEventListener('change', () => {
    const item = ((S.si.loaded && S.si.loaded.history) || [])[Number(recent.value)];
    recent.value = '';
    if (!item) return;
    S.si.form = { title: item.title, game: { id: item.game_id, name: item.game_name }, tags: [...item.tags] };
    renderStreamInfo();
    lookupArt(item.game_id, item.game_name);
    toast('Loaded. Press Update stream to apply it.', { ms: 2500 });
  });
  return h('section', { class: 'card tw-si', 'aria-labelledby': 'tw-si-title' },
    h('div', { class: 'card-head' },
      h('span', { class: 'card-icon twitch' }, icon('edit')),
      h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', id: 'tw-si-title', text: 'Stream info' }),
        h('p', { class: 'card-sub', text: 'What viewers see in the directory and on your channel.' })),
      recent),
    body);
}

async function loadChannel() {
  if (!hasScope('channel:manage:broadcast')) { renderStreamInfo(); return; }
  S.si.status = 'loading';
  renderStreamInfo();
  try {
    const c = await call('/api/twitch/channel');
    S.si.loaded = c;
    if (!isDirty()) S.si.form = formFrom(c);
    S.si.status = 'ready';
    lookupArt(c.game_id, c.game_name);
    if (S.auth && !S.auth.user) refreshAuth();    // the daemon now knows the account's name and avatar
  } catch (e) {
    S.si.status = 'error';
    S.si.err = e.message;
  }
  renderStreamInfo();
}

const formFrom = (c) => ({ title: c.title || '', game: { id: c.game_id || '', name: c.game_name || '' }, tags: [...(c.tags || [])] });

/** Box art for a category we only know by id/name (the channel endpoint doesn't return it). */
async function lookupArt(id, name) {
  if (!id || !name || S.si.art[id] !== undefined || name.length < 2) return;
  S.si.art[id] = '';
  try {
    const r = await api(`/api/twitch/categories?q=${encodeURIComponent(name)}`);
    for (const c of r.categories || []) if (c.box_art) S.si.art[c.id] = boxArt(c.box_art);
    const art = S.root.querySelector('.tw-combo > .tw-combo-art');
    if (art && S.si.form && S.si.form.game.id === id) art.replaceChildren(artImg(S.si.art[id]));
  } catch (e) { /* box art is decoration */ }
}

function artImg(url) {
  return url ? h('img', { src: url, alt: '', width: 26, height: 36, loading: 'lazy', referrerpolicy: 'no-referrer' }) : ic('tag');
}

function isDirty() {
  const { loaded, form } = S.si;
  if (!loaded || !form) return false;
  return form.title !== (loaded.title || '') || form.game.id !== (loaded.game_id || '') || form.tags.join('\n') !== (loaded.tags || []).join('\n');
}

function renderStreamInfo() {
  const body = S.els.si;
  if (!body || !body.isConnected) return;
  const si = S.si;
  if (!hasScope('channel:manage:broadcast')) { body.replaceChildren(lockedNote('Re-link Twitch to edit your title, category and tags.')); return; }
  if (si.status === 'error' && !si.form) { body.replaceChildren(loadError(`Couldn’t load your stream info. ${si.err}`, loadChannel)); return; }
  if (!si.form) { body.replaceChildren(skeletonLines(5)); return; }

  const hist = (si.loaded && si.loaded.history) || [];
  const recent = S.els.recent;
  recent.hidden = !hist.length;
  recent.replaceChildren(h('option', { value: '', text: 'Recent setups…' }),
    ...hist.map((it, i) => h('option', { value: String(i), text: `${it.title}${it.game_name ? ` · ${it.game_name}` : ''}` })));
  recent.value = '';

  const f = si.form;
  const title = h('input', { type: 'text', id: 'tw-title', value: f.title, maxlength: TITLE_MAX, autocomplete: 'off', 'aria-describedby': 'tw-title-help tw-title-err' });
  const count = h('span', { class: 'tw-count', 'aria-hidden': 'true' });
  const titleErr = h('span', { class: 'help error', id: 'tw-title-err', role: 'alert' });
  const titleField = h('div', { class: 'field' },
    h('div', { class: 'row between nowrap' }, h('label', { for: 'tw-title', text: 'Title' }), count),
    title, h('span', { class: 'sr-only', id: 'tw-title-help', text: `Up to ${TITLE_MAX} characters` }), titleErr);
  const setCount = () => {
    const n = title.value.length;
    count.textContent = `${n}/${TITLE_MAX}`;
    count.classList.toggle('near', n > TITLE_MAX - 15);
  };
  title.addEventListener('input', () => {
    f.title = title.value;
    setCount();
    if (title.value.trim()) { titleErr.textContent = ''; titleField.classList.remove('error'); }
    updateFoot();
  });
  setCount();

  const combo = categoryCombo(f);
  const tags = tagEditor(f);
  const footMsg = h('span', { class: 'tw-foot-msg', role: 'status' });
  const err = h('p', { class: 'err', role: 'alert' });
  const reset = h('button', { class: 'ghost', type: 'button', onclick: () => { S.si.form = formFrom(si.loaded); renderStreamInfo(); } }, 'Discard changes');
  const submit = h('button', { class: 'primary', type: 'submit' }, icon('check'), 'Update stream');
  function updateFoot() {
    const dirty = isDirty();
    submit.disabled = !dirty;
    reset.hidden = !dirty;
    footMsg.replaceChildren(dirty ? h('span', { class: 'dot warn' }) : icon('check'), dirty ? 'Unsaved changes' : 'Matches Twitch');
    footMsg.classList.toggle('dirty', dirty);
  }
  S.els.siFoot = updateFoot;

  const form = h('form', { class: 'tw-si-form', novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    if (submit.disabled) return;
    err.textContent = '';
    if (!tags.flush()) return;
    const t = title.value.trim();
    if (!t) { titleErr.textContent = 'Add a title: Twitch needs one.'; titleField.classList.add('error'); title.focus(); return; }
    submit.disabled = true; submit.classList.add('busy');
    try {
      const r = await call('/api/twitch/channel', { title: t, game_id: f.game.id, game_name: f.game.name, tags: f.tags });
      si.loaded = r.channel;
      si.form = formFrom(r.channel);
      toast('Stream updated', { ok: true });
      renderStreamInfo();
    } catch (e2) {
      err.textContent = e2.message;
      submit.classList.remove('busy');
      updateFoot();
    }
  } },
  titleField, combo, tags.el, err,
  h('div', { class: 'card-foot tw-si-foot' }, footMsg, h('span', { class: 'grow' }), reset, submit));
  body.replaceChildren(form);
  updateFoot();
}

function categoryCombo(f) {
  const si = S.si;
  const listId = 'tw-cat-list';
  const input = h('input', { type: 'text', id: 'tw-cat', role: 'combobox', value: f.game.name, autocomplete: 'off', spellcheck: 'false',
    placeholder: 'Search categories', 'aria-autocomplete': 'list', 'aria-expanded': 'false', 'aria-controls': listId,
    'aria-describedby': 'tw-cat-help' });
  const art = h('span', { class: 'tw-combo-art', 'aria-hidden': 'true' }, artImg(si.art[f.game.id] || ''));
  const clear = h('button', { class: 'ghost icon-btn small tw-combo-clear', type: 'button', 'aria-label': 'Clear category', title: 'Clear category',
    hidden: !f.game.id, onclick: () => { pick({ id: '', name: '' }); input.focus(); } }, icon('x'));
  const list = h('ul', { class: 'tw-combo-list scroll', id: listId, role: 'listbox', 'aria-label': 'Categories', hidden: true });
  const help = h('span', { class: 'help', id: 'tw-cat-help', text: f.game.id ? 'Type to search for a different one.' : 'Type at least 2 letters, then pick from the list.' });
  let results = [], active = -1, seq = 0;

  const setActive = (i) => {
    active = i;
    [...list.querySelectorAll('[role="option"]')].forEach((li, j) => li.setAttribute('aria-selected', String(j === i)));
    if (i >= 0) {
      input.setAttribute('aria-activedescendant', `tw-cat-opt-${i}`);
      const li = list.querySelector(`#tw-cat-opt-${i}`);
      if (li) li.scrollIntoView({ block: 'nearest' });
    } else input.removeAttribute('aria-activedescendant');
  };
  const setOpen = (open) => { list.hidden = !open; input.setAttribute('aria-expanded', String(open)); if (!open) setActive(-1); };
  const message = (text, cls = '') => { results = []; list.replaceChildren(h('li', { class: 'tw-combo-msg ' + cls, role: 'presentation', text })); setOpen(true); };
  function pick(c) {
    f.game = { id: c.id, name: c.name };
    if (c.box_art) si.art[c.id] = boxArt(c.box_art);
    input.value = c.name;
    art.replaceChildren(artImg(si.art[c.id] || ''));
    clear.hidden = !c.id;
    help.textContent = c.id ? 'Type to search for a different one.' : 'No category. Type at least 2 letters to search.';
    setOpen(false);
    if (S.els.siFoot) S.els.siFoot();
  }
  const search = debounce(async (q, mine) => {
    try {
      const r = await call(`/api/twitch/categories?q=${encodeURIComponent(q)}`);
      if (mine !== seq || document.activeElement !== input) return;
      results = r.categories || [];
      if (!results.length) { message(`No categories match “${q}”.`); return; }
      list.replaceChildren(...results.map((c, i) => h('li', { id: `tw-cat-opt-${i}`, role: 'option', 'aria-selected': 'false', class: 'tw-combo-opt',
        onmousedown: (e) => e.preventDefault(), onclick: () => pick(c), onmousemove: () => { if (active !== i) setActive(i); } },
      h('span', { class: 'tw-combo-art' }, artImg(boxArt(c.box_art))), h('span', { class: 'tw-combo-name', text: c.name }))));
      setOpen(true);
      setActive(0);
    } catch (e) {
      if (mine === seq) message(e.message, 'err');
    }
  }, 300);

  input.addEventListener('input', () => {
    const q = input.value.trim();
    const mine = ++seq;
    if (q.length < 2) { setOpen(false); return; }
    message('Searching…', 'muted');
    search(q, mine);
  });
  input.addEventListener('keydown', (e) => {
    const n = results.length;
    if (e.key === 'ArrowDown') { e.preventDefault(); if (list.hidden && n) setOpen(true); if (n) setActive((active + 1) % n); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); if (n) setActive((active - 1 + n) % n); }
    else if (e.key === 'Enter') { e.preventDefault(); if (!list.hidden && active >= 0 && results[active]) pick(results[active]); }
    else if (e.key === 'Escape') {
      if (!list.hidden) { e.preventDefault(); e.stopPropagation(); seq++; setOpen(false); }
      else input.value = f.game.name;
    }
  });
  input.addEventListener('blur', () => { setTimeout(() => { if (document.activeElement === input) return; seq++; setOpen(false); input.value = f.game.name; }, 120); });

  return h('div', { class: 'field tw-combo-field' }, h('label', { for: 'tw-cat', text: 'Category' }),
    h('div', { class: 'tw-combo' }, art, input, clear, list), help);
}

function tagEditor(f) {
  const input = h('input', { type: 'text', id: 'tw-tag-input', class: 'tw-tag-input', autocomplete: 'off',
    spellcheck: 'false', maxlength: 60, 'aria-describedby': 'tw-tags-help tw-tags-err' });
  const box = h('div', { class: 'tw-tags', onclick: (e) => { if (e.target === box) input.focus(); } });
  const err = h('span', { class: 'help error', id: 'tw-tags-err', role: 'alert' });
  const count = h('span', { class: 'tw-count', 'aria-hidden': 'true' });
  const live = h('span', { class: 'sr-only', role: 'status' });
  const changed = () => { if (S.els.siFoot) S.els.siFoot(); };
  const draw = () => {
    box.replaceChildren(...f.tags.map((t, i) => h('span', { class: 'tw-tag' }, h('span', { text: t }),
      h('button', { type: 'button', class: 'tw-tag-x', 'aria-label': `Remove tag ${t}`, title: 'Remove', onclick: () => {
        f.tags.splice(i, 1); draw(); live.textContent = `Removed ${t}`; input.focus(); changed();
      } }, icon('x')))), input);
    count.textContent = `${f.tags.length}/${TAGS_MAX}`;
    input.disabled = f.tags.length >= TAGS_MAX;
    input.placeholder = f.tags.length >= TAGS_MAX ? 'Tag limit reached' : f.tags.length ? 'Add a tag' : 'Add tags, e.g. English';
  };
  /** Add one tag; false (with the reason shown) if it can't be added. */
  const add = (raw) => {
    const v = String(raw).replace(/\s+/g, '').replace(/^#+/, '');
    if (!v) return true;
    if (!TAG_RE.test(v)) { err.textContent = `“${v.slice(0, 30)}” can’t be a tag: use up to 25 letters or numbers, no symbols.`; return false; }
    if (f.tags.some((t) => t.toLowerCase() === v.toLowerCase())) { err.textContent = `“${v}” is already a tag.`; return false; }
    if (f.tags.length >= TAGS_MAX) { err.textContent = `Twitch allows up to ${TAGS_MAX} tags.`; return false; }
    f.tags.push(v);
    live.textContent = `Added ${v}`;
    return true;
  };
  const commit = (text) => {
    err.textContent = '';
    const failed = [];
    for (const p of text.split(',')) if (!add(p)) failed.push(p.trim());
    draw();
    changed();
    return failed;
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      input.value = commit(input.value).join(', ');
      input.focus();
    } else if (e.key === 'Backspace' && !input.value && f.tags.length) {
      const t = f.tags.pop(); draw(); live.textContent = `Removed ${t}`; input.focus(); changed();
    }
  });
  input.addEventListener('input', () => {
    if (input.value.includes(',')) {            // pasted "a, b, c"
      const parts = input.value.split(',');
      const rest = parts.pop();
      input.value = [...commit(parts.join(',')), rest].filter(Boolean).join(', ');
      input.focus();
    } else if (err.textContent) err.textContent = '';
  });
  draw();
  return {
    el: h('div', { class: 'field tw-tags-field' },
      h('div', { class: 'row between nowrap' }, h('label', { for: 'tw-tag-input', text: 'Tags' }), count),
      box, live,
      h('span', { class: 'help', id: 'tw-tags-help', text: 'Press Enter or comma to add. Letters and numbers only; spaces are removed.' }), err),
    /** Add whatever is still typed; false if it was rejected. */
    flush: () => {
      if (!input.value.trim()) return true;
      const failed = commit(input.value);
      input.value = failed.join(', ');
      if (failed.length) { input.focus(); return false; }
      return true;
    },
  };
}

/* ------------------------------------------------------------------ chat */

function chatCard() {
  const feed = h('ol', { class: 'tw-feed scroll', role: 'log', 'aria-label': 'Chat messages', tabindex: '0' });
  const pill = h('button', { class: 'tw-pill small', type: 'button', hidden: true, onclick: () => scrollFeed(true) }, icon('chevronDown'), 'New messages');
  const reading = h('span', { class: 'tw-reading' });
  S.els.feed = feed;
  S.els.pill = pill;
  S.els.reading = reading;
  feed.addEventListener('scroll', () => { if (nearBottom()) pill.hidden = true; });
  feedKeyboard(feed);
  updateReading();
  const lg = login();
  return h('section', { class: 'card flush tw-chat', 'aria-labelledby': 'tw-chat-title' },
    h('div', { class: 'card-head' },
      h('span', { class: 'card-icon twitch' }, ic('chat')),
      h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', id: 'tw-chat-title', text: 'Chat' })),
      reading,
      lg ? extLink(`https://www.twitch.tv/popout/${lg}/chat?popout=`, 'Pop out', 'btn ghost small') : null),
    h('div', { class: 'tw-feed-wrap' }, feed, pill),
    composer());
}

function updateReading() {
  const el = S.els.reading;
  if (!el) return;
  el.replaceChildren(S.chatReading ? chip(true, 'Reading chat', `Reading #${S.channelName}`) : chip(false, 'Not reading', 'The chat reader is connecting or offline'));
}

const nearBottom = () => { const f = S.els.feed; return !f || f.scrollHeight - f.scrollTop - f.clientHeight < 48; };
function scrollFeed(force) {
  const f = S.els.feed;
  if (!f) return;
  if (force || nearBottom()) f.scrollTop = f.scrollHeight;
  if (S.els.pill) S.els.pill.hidden = true;
}

/** Arrow keys move between messages; Tab from a message reaches its actions. */
function feedKeyboard(feed) {
  const setTabbable = (li, yes) => { for (const b of li.querySelectorAll('.tw-msg-actions button')) b.tabIndex = yes ? 0 : -1; };
  feed.addEventListener('keydown', (e) => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
    const list = [...feed.querySelectorAll('.tw-msg')];
    if (!list.length) return;
    const cur = document.activeElement && document.activeElement.closest('.tw-msg');
    let i = list.indexOf(cur);
    if (e.key === 'ArrowDown') i = i < 0 ? list.length - 1 : Math.min(list.length - 1, i + 1);
    else if (e.key === 'ArrowUp') i = i < 0 ? list.length - 1 : Math.max(0, i - 1);
    else i = e.key === 'Home' ? 0 : list.length - 1;
    e.preventDefault();
    list[i].focus();
  });
  feed.addEventListener('focusin', (e) => { const li = e.target.closest('.tw-msg'); if (li) setTabbable(li, true); });
  feed.addEventListener('focusout', (e) => {
    const li = e.target.closest('.tw-msg');
    if (li && !li.contains(e.relatedTarget)) setTabbable(li, false);
  });
}

function fmtTime(ts) {
  const d = new Date((Number(ts) || Date.now() / 1000) * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function msgNode(m) {
  const user = String(m.user || '?');
  const badges = (Array.isArray(m.badges) ? m.badges : []).filter((b) => Object.prototype.hasOwnProperty.call(BADGES, b));
  const nameEl = h('span', { class: 'tw-user', text: user });
  const color = safeColor(m.color);
  if (color) nameEl.style.color = color;
  return h('li', { class: 'tw-msg' + (m.deleted ? ' deleted' : ''), tabindex: '-1' },
    h('span', { class: 'tw-time', text: fmtTime(m.ts) }),
    h('span', { class: 'tw-line' },
      badges.length ? h('span', { class: 'tw-badges' }, badges.map((b) => h('span', { class: `tw-badge b-${b}`, title: BADGES[b][1] },
        h('span', { 'aria-hidden': 'true', text: BADGES[b][0] }), h('span', { class: 'sr-only', text: `${BADGES[b][1]} ` })))) : null,
      nameEl, h('span', { class: 'tw-colon', text: ': ' }),
      h('span', { class: 'tw-text', text: m.deleted ? m.deleted : String(m.text || '') })),
    m.deleted ? null : msgActions(m));
}

function msgActions(m) {
  if (!S.auth || !S.auth.linked) return null;
  const user = String(m.user || '');
  const lg = login();
  const self = (m.badges || []).includes('broadcaster') || (lg && user.toLowerCase() === lg.toLowerCase());
  const uid = /^[0-9]{1,20}$/.test(String(m.user_id || '')) ? String(m.user_id) : '';
  const btns = [];
  const act = (iconName, label, fn, cls = '') => h('button', { class: 'ghost icon-btn small ' + cls, type: 'button', tabindex: '-1', 'aria-label': `${label}: ${user}`, title: label,
    onclick: async (e) => {
      const b = e.currentTarget;
      b.disabled = true;
      try { await fn(); } catch (err) { toast(err.message, { bad: true }); } finally { b.disabled = false; }
    } }, ic(iconName));
  if (m.id && /^[A-Za-z0-9-]{1,64}$/.test(m.id) && hasScope('moderator:manage:chat_messages')) {
    btns.push(act('trash', 'Delete message', async () => {
      await call('/api/twitch/chat/delete', { id: m.id });
      markDeleted((x) => x.id === m.id, 'Message deleted');
      toast('Message deleted', { ok: true, ms: 2000 });
    }));
  }
  if (uid && !self && hasScope('moderator:manage:banned_users')) {
    btns.push(act('clock', 'Time out 10 minutes', async () => {
      await call('/api/twitch/chat/ban', { user_id: uid, seconds: 600 });
      markDeleted((x) => String(x.user_id) === uid, 'Timed out for 10 minutes');
      toast(`${user} is timed out for 10 minutes`, { ok: true });
    }));
    btns.push(act('ban', 'Ban', () => banDialog(m, uid), 'tw-danger'));
  }
  if (!self && LOGIN_RE.test(user) && hasScope('moderator:manage:shoutouts')) {
    btns.push(act('megaphone', 'Shout out', async () => {
      await call('/api/twitch/shoutout', { login: user.toLowerCase() });
      toast(`Shoutout sent for ${user}`, { ok: true });
    }));
  }
  if (!btns.length) return null;
  return h('div', { class: 'tw-msg-actions', role: 'group', 'aria-label': `Actions for ${user}’s message` }, btns);
}

function banDialog(m, uid) {
  const user = String(m.user || 'this user');
  const reason = h('input', { type: 'text', id: 'tw-ban-reason', maxlength: 500, placeholder: 'Optional. They’ll see it.', autocomplete: 'off' });
  const err = h('p', { class: 'err', role: 'alert' });
  const ban = asyncButton({ class: 'primary danger', type: 'submit' }, async () => {
    err.textContent = '';
    try {
      await call('/api/twitch/chat/ban', { user_id: uid, permanent: true, reason: reason.value.trim() });
    } catch (e) { err.textContent = e.message; return; }
    closeModal();
    markDeleted((x) => String(x.user_id) === uid, 'Banned');
    toast(`${user} is banned`, { ok: true });
  }, ic('ban'), `Ban ${user}`);
  openModal(`Ban ${user}?`, h('form', { novalidate: true, onsubmit: (e) => { e.preventDefault(); ban.click(); } },
    h('p', { class: 'hint', text: 'They can’t chat in your channel until you unban them on Twitch. A 10-minute timeout is the gentler option.' }),
    h('label', { class: 'field mt-3' }, h('span', { class: 'label', text: 'Reason' }), reason),
    err,
    h('div', { class: 'row end mt-3' }, h('button', { class: 'ghost', type: 'button', onclick: closeModal }, 'Cancel'), ban)));
}

function markDeleted(match, label) {
  let changed = false;
  for (const m of S.chat) if (match(m) && !m.deleted) { m.deleted = label; changed = true; }
  if (changed) renderFeed(true);
}

function addChat(m) {
  if (!m || typeof m !== 'object') return;
  if (m.id) {
    if (S.chatIds.has(m.id)) return;
    S.chatIds.add(m.id);
  }
  S.chat.push(m);
  while (S.chat.length > CHAT_CAP) {
    const old = S.chat.shift();
    if (old.id) S.chatIds.delete(old.id);
  }
  const feed = S.els.feed;
  if (!feed || !feed.isConnected) return;
  const stick = nearBottom();
  const emptyEl = feed.querySelector('.tw-feed-empty');
  if (emptyEl) emptyEl.remove();
  feed.append(msgNode(m));
  while (feed.children.length > CHAT_CAP) feed.firstElementChild.remove();
  if (stick) feed.scrollTop = feed.scrollHeight;
  else if (S.els.pill) S.els.pill.hidden = false;
}

function renderFeed(keepScroll) {
  const feed = S.els.feed;
  if (!feed || !feed.isConnected) return;
  const top = feed.scrollTop, stick = nearBottom();
  if (!S.chat.length) {
    feed.replaceChildren(h('li', { class: 'tw-feed-empty' }, S.chatReading
      ? empty('No messages yet', 'New chat messages show up here as they arrive.', 'chat')
      : empty('Not reading chat yet', S.channelName ? `Connecting to #${S.channelName}…` : 'Set your channel to see chat here.', 'chat')));
    const ico = feed.querySelector('.empty-icon');
    if (ico) ico.replaceChildren(ic('chat'));
    return;
  }
  feed.replaceChildren(...S.chat.map(msgNode));
  if (keepScroll && !stick) feed.scrollTop = top;
  else feed.scrollTop = feed.scrollHeight;
}

function composer() {
  const canSend = hasScope('user:write:chat');
  const canAnnounce = hasScope('moderator:manage:announcements');
  let announce = false, color = 'primary';
  const ta = h('textarea', { class: 'tw-compose', rows: 1, maxlength: CHAT_MAX, id: 'tw-compose', disabled: !canSend,
    placeholder: canSend ? 'Send a message' : 'Re-link Twitch to send messages', 'aria-label': 'Chat message', 'aria-describedby': 'tw-compose-help' });
  const count = h('span', { class: 'tw-count', 'aria-hidden': 'true', text: `0/${CHAT_MAX}` });
  const err = h('span', { class: 'err', role: 'alert' });
  const send = h('button', { class: 'primary icon-btn', type: 'submit', 'aria-label': 'Send message', title: 'Send (Enter)', disabled: true }, icon('send'));
  const seg = h('div', { class: 'seg tw-colors', role: 'group', 'aria-label': 'Announcement color', hidden: true },
    ANNOUNCE_COLORS.map(([v, label]) => h('button', { type: 'button', 'aria-pressed': String(v === color), dataset: { color: v },
      onclick: () => { color = v; for (const b of seg.children) b.setAttribute('aria-pressed', String(b.dataset.color === v)); } },
    h('span', { class: `tw-swatch c-${v}`, 'aria-hidden': 'true' }), label)));
  let form = null;
  const toggle = canAnnounce && canSend ? switchInput(false, (on) => {
    announce = on;
    seg.hidden = !on;
    ta.placeholder = on ? 'Write an announcement' : 'Send a message';
    send.setAttribute('aria-label', on ? 'Send announcement' : 'Send message');
    form.classList.toggle('announcing', on);
  }, 'Announce') : null;
  const grow = () => { ta.style.height = 'auto'; ta.style.height = `${Math.min(ta.scrollHeight + 2, 120)}px`; };
  const sync = () => {
    const n = ta.value.length;
    count.textContent = `${n}/${CHAT_MAX}`;
    count.classList.toggle('near', n > CHAT_MAX - 50);
    send.disabled = !canSend || !ta.value.trim();
    grow();
  };
  ta.addEventListener('input', () => { err.textContent = ''; sync(); });
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  form = h('form', { class: 'tw-composer', novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    const text = ta.value.trim();
    if (!text || send.classList.contains('busy')) return;
    if (text.length > CHAT_MAX) { err.textContent = `Keep it under ${CHAT_MAX} characters.`; return; }
    send.disabled = true; send.classList.add('busy');
    try {
      if (announce) await call('/api/twitch/chat/announce', { text, color });
      else await call('/api/twitch/chat/send', { text });
      ta.value = '';
      err.textContent = '';
    } catch (e2) {
      err.textContent = e2.status === 429 ? 'Slow down: one message per second.' : e2.message;
    } finally {
      send.classList.remove('busy');
      sync();
      ta.focus();
    }
  } },
  canSend ? null : h('p', { class: 'hint tw-compose-locked' }, icon('lock'), h('span', { class: 'grow', text: 'Sending needs another permission.' }),
    h('button', { class: 'small', type: 'button', onclick: openRelink }, 'Re-link')),
  h('div', { class: 'tw-compose-row' }, ta, send),
  h('div', { class: 'tw-compose-meta' }, toggle, seg, h('span', { class: 'grow' }), err, canSend ? count : null),
  h('p', { class: 'sr-only', id: 'tw-compose-help', text: 'Enter sends. Shift+Enter adds a new line.' }));
  return form;
}

/* ------------------------------------------------------------------ chat modes */

function modesCard() {
  const body = h('div', { class: 'tw-modes-body' });
  S.els.modes = body;
  return h('section', { class: 'card tw-modes', 'aria-labelledby': 'tw-modes-title' },
    h('div', { class: 'card-head' }, h('span', { class: 'card-icon twitch' }, ic('shield')),
      h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', id: 'tw-modes-title', text: 'Chat modes' }),
        h('p', { class: 'card-sub', text: 'Who can chat right now. Changes apply instantly.' }))),
    body);
}

async function loadModes() {
  if (!hasScope('moderator:manage:chat_settings')) { renderModes(); return; }
  S.modes.status = 'loading';
  renderModes();
  try {
    S.modes.data = await call('/api/twitch/chat/settings');
    S.modes.status = 'ready';
  } catch (e) {
    S.modes.status = 'error';
    S.modes.err = e.message;
  }
  renderModes();
}

function renderModes() {
  const body = S.els.modes;
  if (!body || !body.isConnected) return;
  const st = S.modes;
  if (!hasScope('moderator:manage:chat_settings')) { body.replaceChildren(lockedNote('Re-link Twitch to switch chat modes.')); return; }
  if (st.status === 'error' && !st.data) { body.replaceChildren(loadError(`Couldn’t load chat modes. ${st.err}`, loadModes)); return; }
  if (!st.data) { body.replaceChildren(skeletonLines(5)); return; }
  const d = st.data;
  body.replaceChildren(h('ul', { class: 'tw-mode-list' }, MODES.map(([mode, label, desc]) => {
    const id = `tw-mode-${mode}`;
    const sw = switchInput(!!d[mode], (on) => setMode(mode, on, sw), null);
    const input = sw.querySelector('input');
    input.id = id;
    input.setAttribute('aria-describedby', `${id}-desc`);
    const text = mode !== 'slow' ? desc
      : d.slow && d.slow_seconds ? `People wait ${d.slow_seconds} seconds between messages.` : 'People wait between messages.';
    return h('li', { class: 'tw-mode' + (d[mode] ? ' on' : '') },
      h('label', { class: 'tw-mode-text', for: id }, h('span', { class: 'tw-mode-name', text: label }), h('span', { class: 'tw-mode-desc', id: `${id}-desc`, text })),
      mode === 'slow' ? slowPicker(d, sw) : null,
      sw);
  })));
}

const SLOW_CHOICES = [3, 5, 10, 20, 30, 60, 120];

/** Wait between messages: applied right away while slow mode is on, else used when it's switched on. */
function slowPicker(d, sw) {
  const current = d.slow && d.slow_seconds ? d.slow_seconds : (S.modes.slowPick || 30);
  const choices = SLOW_CHOICES.includes(current) ? SLOW_CHOICES : [...SLOW_CHOICES, current].sort((a, b) => a - b);
  return h('select', {
    class: 'tw-slow-pick', 'aria-label': 'Slow mode wait',
    onchange: (e) => {
      S.modes.slowPick = +e.target.value;
      if (S.modes.data && S.modes.data.slow) setMode('slow', true, sw);
    },
  }, choices.map((n) => h('option', { value: String(n), selected: n === current, text: n < 60 ? `${n}s` : `${n / 60} min` })));
}

async function setMode(mode, on, sw) {
  const st = S.modes;
  const input = sw.querySelector('input');
  const before = !!st.data[mode];
  st.data = { ...st.data, [mode]: on };          // optimistic: the switch has already moved
  input.disabled = true;
  try {
    const body = { mode, on };
    if (mode === 'slow' && on && st.slowPick) body.seconds = st.slowPick;
    st.data = await call('/api/twitch/chat/settings', body);
    toast(`${MODES.find((m) => m[0] === mode)[1]} ${on ? 'on' : 'off'}`, { ok: true, ms: 1800 });
  } catch (e) {
    st.data = { ...st.data, [mode]: before };    // roll back
    toast(e.message, { bad: true });
  }
  const focused = document.activeElement === input;
  renderModes();
  if (focused) { const again = document.getElementById(`tw-mode-${mode}`); if (again) again.focus(); }
}

/* ------------------------------------------------------------------ channel tools */

function toolsCard() {
  const note = h('p', { class: 'note tw-tools-note' }, icon('info'), h('span', { text: 'Ads, markers and clips only work while you’re live.' }));
  S.els.toolsNote = note;
  note.hidden = !!(S.live && S.live.live);
  return h('section', { class: 'card tw-tools', 'aria-labelledby': 'tw-tools-title' },
    h('div', { class: 'card-head' }, h('span', { class: 'card-icon twitch' }, icon('bolt')),
      h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', id: 'tw-tools-title', text: 'Channel tools' }),
        h('p', { class: 'card-sub', text: 'Ads, markers, clips, raids and shoutouts.' }))),
    note,
    h('div', { class: 'tw-tool-list' }, adTool(), markerTool(), clipTool(), raidTool(), shoutoutTool()));
}

function tool(iconName, title, desc, scope, controls, after) {
  const ok = hasScope(scope);
  return h('div', { class: 'tw-tool' + (ok ? '' : ' locked') },
    h('span', { class: 'tw-tool-icon', 'aria-hidden': 'true' }, ic(iconName)),
    h('div', { class: 'tw-tool-text' }, h('h3', { class: 'tw-tool-title', text: title }),
      h('p', { class: 'tw-tool-desc', text: ok ? desc : `Re-link Twitch to allow ${SCOPE_NAMES[scope]}.` })),
    h('div', { class: 'tw-tool-ctl' }, ok ? controls : h('button', { class: 'small', type: 'button', onclick: openRelink }, 'Re-link')),
    ok && after ? after : null);
}

/** Validate a username field; returns the login or '' (and explains inline). */
function readLogin(input, err) {
  const v = normaliseChannel(input.value);
  input.value = v;
  if (!LOGIN_RE.test(v)) {
    err.textContent = v ? 'That isn’t a Twitch username: 3–25 letters, numbers or _.' : 'Type a channel name first.';
    input.setAttribute('aria-invalid', 'true');
    input.focus();
    return '';
  }
  err.textContent = '';
  input.removeAttribute('aria-invalid');
  return v.toLowerCase();
}

function adTool() {
  let seconds = 60;
  const seg = h('div', { class: 'seg tw-ad-seg', role: 'group', 'aria-label': 'Ad length' },
    AD_LENGTHS.map(([v, label]) => h('button', { type: 'button', 'aria-pressed': String(v === seconds), dataset: { s: String(v) },
      'aria-label': `${v} seconds`, onclick: () => { seconds = v; for (const b of seg.children) b.setAttribute('aria-pressed', String(Number(b.dataset.s) === v)); } }, label)));
  const run = asyncButton({ class: 'small', type: 'button' }, async () => {
    const label = AD_LENGTHS.find((a) => a[0] === seconds)[1];
    if (!await confirmDialog(`Run a ${label} ad now?`, 'It starts right away for everyone watching.', { ok: 'Run ad' })) return;
    const r = await call('/api/twitch/ad', { seconds });
    toast(`Running a ${r.seconds || seconds}-second ad break`, { ok: true });
  }, icon('play'), 'Run ad');
  return tool('monitor', 'Run an ad', 'Pick a length, then start the break.', 'channel:edit:commercial', [seg, run]);
}

function markerTool() {
  const input = h('input', { type: 'text', id: 'tw-marker', placeholder: 'Description (optional)', maxlength: MARKER_MAX, autocomplete: 'off', 'aria-label': 'Marker description' });
  const go = asyncButton({ class: 'small', type: 'submit' }, async () => {
    const description = input.value.trim();
    await call('/api/twitch/marker', description ? { description } : {});
    input.value = '';
    toast('Marker added. Find it in Highlighter on Twitch.', { ok: true });
  }, icon('flag'), 'Add marker');
  return tool('flag', 'Add a marker', 'Bookmark this moment to edit later.', 'channel:manage:broadcast',
    h('form', { class: 'tw-tool-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); go.click(); } }, input, go));
}

function clipTool() {
  const btn = asyncButton({ class: 'small', type: 'button' }, async () => {
    const r = await call('/api/twitch/clip', {});
    const url = httpsOnly(r.url);
    toast(h('span', {}, 'Clip created. ', url ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, 'Edit the clip ↗') : null), { ok: true, ms: 10000 });
  }, icon('clip'), 'Create clip');
  return tool('clip', 'Create a clip', 'Captures the last 30 seconds or so.', 'clips:edit', btn);
}

function raidTool() {
  const input = h('input', { type: 'text', id: 'tw-raid', placeholder: 'Channel to raid', autocomplete: 'off', spellcheck: 'false', maxlength: 60, 'aria-label': 'Channel to raid', 'aria-describedby': 'tw-raid-err' });
  const err = h('p', { class: 'err tw-tool-err', id: 'tw-raid-err', role: 'alert' });
  const start = asyncButton({ class: 'small twitch', type: 'submit' }, async () => {
    const lg = readLogin(input, err);
    if (!lg) return;
    if (!await confirmDialog(`Raid ${lg}?`, 'Twitch counts down before it sends your viewers. You can cancel it here until then.', { ok: 'Start raid' })) return;
    try { await call('/api/twitch/raid', { login: lg }); } catch (e) { err.textContent = e.message; return; }
    toast(`Raid on ${lg} is starting`, { ok: true, ms: 5000 });
  }, 'Start raid');
  const cancel = asyncButton({ class: 'small ghost', type: 'button' }, async () => {
    await call('/api/twitch/raid/cancel', {});
    toast('Raid canceled', { ok: true });
  }, 'Cancel raid');
  return tool('users', 'Raid', 'Send your viewers to another channel.', 'channel:manage:raids',
    h('form', { class: 'tw-tool-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); start.click(); } }, input, start, cancel), err);
}

function shoutoutTool() {
  const input = h('input', { type: 'text', id: 'tw-so', placeholder: 'Channel name', autocomplete: 'off', spellcheck: 'false', maxlength: 60, 'aria-label': 'Channel to shout out', 'aria-describedby': 'tw-so-err' });
  const err = h('p', { class: 'err tw-tool-err', id: 'tw-so-err', role: 'alert' });
  const go = asyncButton({ class: 'small', type: 'submit' }, async () => {
    const lg = readLogin(input, err);
    if (!lg) return;
    try { await call('/api/twitch/shoutout', { login: lg }); } catch (e) { err.textContent = e.message; return; }
    input.value = '';
    toast(`Shoutout sent for ${lg}`, { ok: true });
  }, ic('megaphone'), 'Shout out');
  return tool('megaphone', 'Shoutout', 'Recommend a channel to your viewers.', 'moderator:manage:shoutouts',
    h('form', { class: 'tw-tool-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); go.click(); } }, input, go), err);
}

/* ------------------------------------------------------------------ live updates */

function onStatus(sn) {
  if (!sn || typeof sn !== 'object') return;
  if (sn.chat) {
    const was = S.chatReading;
    S.chatReading = !!sn.chat.connected;
    const name = sn.chat.channel ? String(sn.chat.channel) : S.channelName;
    const renamed = name !== S.channelName;
    S.channelName = name;
    if (was !== S.chatReading) { updateReading(); if (!S.chat.length) renderFeed(); }
    if (renamed && S.mode === 'setup' && S.auth && !S.celebrating) renderSetup();
  }
  if (sn.twitch && sn.twitch.live && S.mode === 'connected') { S.live = sn.twitch.live; updateLive(); }
}

/* ------------------------------------------------------------------ view lifecycle */

export async function mount(root) {
  S.root = root;
  onStatus(snapshot);
  render();
  on('twitch_auth', (a) => applyAuth(a));
  on('chat', (m) => addChat(m));
  on('status', (sn) => onStatus(sn));
  on('config', () => { if (S.visible) refreshAuth(); });
  api('/api/live/snapshot').then(onStatus).catch(() => {});
  api('/api/live/chat?n=200').then((r) => {
    const fresh = S.chat.splice(0);
    S.chatIds.clear();
    for (const m of [...(r.messages || []), ...fresh]) addChat(m);
    renderFeed();
  }).catch(() => {});
}

export function show() {
  S.visible = true;
  refreshAuth();
  clearInterval(S.ticker);
  S.ticker = setInterval(tick, 1000);
  clearInterval(S.poller);
  S.poller = setInterval(() => { if (S.mode === 'connected') loadStream(); }, 30000);
  if (S.mode === 'connected') {
    loadStream();
    if (S.si.status === 'ready' && !isDirty()) loadChannel();
    requestAnimationFrame(() => scrollFeed(true));
  }
}

export function hide() {
  S.visible = false;
  clearInterval(S.ticker);
  clearInterval(S.poller);
  S.ticker = S.poller = 0;
}
