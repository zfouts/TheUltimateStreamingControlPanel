/* Shell: navigation between views, status chips, theme, the event stream. Each view
   module exports mount(root) (called once, lazily) and optionally show()/hide(). */
import { LAUNCH_ERROR, REMOTE, TOKEN, api, connect, forgetToken, on, onStreamState } from './api.js';
import { $, $$, h, chip, icon, closeModal, modalOpen, openModal, toast } from './ui.js';
import { showPairing } from './pairing.js';
import * as live from './live.js';
import * as deck from './app.js';
import * as twitch from './twitch.js';
import * as bot from './twitchbot.js';
import * as announce from './announce.js';
import * as discord from './discord.js';
import * as obs from './obs.js';
import * as plugins from './plugins.js';
import { maybeWelcome } from './backups.js';
import { activeTab, groupOf, navView, renderTabs } from './tabs.js';

const VIEWS = { live, deck, twitch, bot, announce, obs, discord, plugin: plugins, settings: deck.settings };
const mounted = new Set();
let current = null;
export let status = {};          // last /api/status (setup-level facts)
export let snapshot = {};        // last "status" event (live module snapshots)

/* ------------------------------------------------------------------ theme */

function applyTheme(t) {
  const light = t === 'light';
  document.documentElement.dataset.theme = light ? 'light' : 'dark';
  const btn = $('#theme-toggle');
  const label = light ? 'Switch to dark theme' : 'Switch to light theme';
  btn.setAttribute('aria-label', label);
  btn.title = label;
  btn.replaceChildren(icon(light ? 'moon' : 'sun'));
}
function initTheme() {
  let t = 'dark';
  try { t = localStorage.getItem('deck_theme') || t; } catch (e) { /* private mode */ }
  // #theme=light|dark in the URL overrides the stored choice (screenshots, sharing a view)
  const forced = new URLSearchParams(location.hash.slice(1)).get('theme');
  if (forced === 'light' || forced === 'dark') t = forced;
  applyTheme(t);
  $('#theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    try { localStorage.setItem('deck_theme', next); } catch (e) { /* ignore */ }
    applyTheme(next);
  });
}

/* ------------------------------------------------------------------ views */

const ORDER = ['live', 'deck', 'twitch', 'obs', 'settings'];

export function viewName() { return new URLSearchParams(location.hash.slice(1)).get('view') || 'live'; }

export function go(view, extra = {}) {
  const p = new URLSearchParams({ view, ...extra });
  location.hash = p.toString();
}

async function showView(name) {
  if (!VIEWS[name]) name = 'live';
  const prev = current;
  current = name;
  markCurrent();
  renderTabs($('#section-tabs'), name);
  for (const v of Object.keys(VIEWS)) $(`#view-${v}`).hidden = v !== name;
  if (prev && prev !== name && VIEWS[prev].hide) VIEWS[prev].hide();
  const mod = VIEWS[name];
  if (!mounted.has(name)) {
    mounted.add(name);
    try { await mod.mount($(`#view-${name}`)); } catch (e) { console.error(e); toast(`${name}: ${e.message}`, { bad: true }); }
  }
  if (mod.show) mod.show();
  showCurrentName();
  if (name !== 'plugin') document.title = pageTitle(name);   // the plugin page sets its own
}

/** "Bot · Twitch · Ultimate Streaming" inside a tab group, else "Live · Ultimate Streaming". */
function pageTitle(name) {
  const nav = $(`.nav button[data-view="${navView(name)}"] .txt`);
  const section = nav ? nav.textContent : name;
  const tab = groupOf(name) && activeTab(name);
  return `${tab ? tab.label + ' · ' : ''}${section} · Ultimate Streaming`;
}

/* ------------------------------------------------------------------ plugin entries */

export function pluginParam() { return new URLSearchParams(location.hash.slice(1)).get('name') || ''; }

/** The narrow top bar shows only icons, so the active view's name sits beside them. */
function showCurrentName() {
  const on = $('.nav button[aria-current="page"]');
  const lab = on && $('.txt', on);
  $('#nav-current').textContent = lab ? lab.textContent : '';
}

/** aria-current on the nav: for plugin pages only the matching plugin's button. */
function markCurrent() {
  const plug = pluginParam();
  $$('.nav button').forEach((b) => {
    const on = b.dataset.view === navView(current) && (current !== 'plugin' || b.dataset.plugin === plug);
    b.setAttribute('aria-current', on ? 'page' : 'false');
  });
  showCurrentName();
}

let pluginSig = null;
/** One sidebar button per installed plugin (status.plugins); rebuilt only when the list changes. */
function renderPluginNav() {
  const list = Array.isArray(status.plugins) ? status.plugins.filter((p) => p && typeof p.name === 'string') : [];
  const sig = JSON.stringify(list.map((p) => [p.name, p.title, p.icon, !!p.loaded]));
  if (sig === pluginSig) return;
  pluginSig = sig;
  const box = $('#plugin-nav');
  box.hidden = !list.length;
  // below Settings: a divider, a "Plugins" heading, then one entry per plugin
  box.replaceChildren(...(list.length ? [h('hr', { class: 'nav-rule' }),
    h('div', { class: 'nav-label', 'aria-hidden': 'true', text: 'Plugins' })] : []),
    ...list.map((p) => {
      const title = String(p.title || p.name);
      const b = h('button', { 'data-view': 'plugin', 'data-plugin': p.name, 'aria-current': 'false',
        'aria-label': p.loaded ? title : `${title} (didn’t load)`,
        title: p.loaded ? title : `${title}: didn’t load`, onclick: () => go('plugin', { name: p.name }) },
      icon(plugins.pluginIcon(p.icon)), h('span', { class: 'txt', text: title }),
      !p.loaded && h('span', { class: 'dot warn', title: 'This plugin didn’t load' }),
      !p.loaded && h('span', { class: 'sr-only', text: '(didn’t load)' }));
      return b;
    }));
  markCurrent();
}

/** 1-5 jump between views, unless the user is typing or a dialog is open. */
function onShortcut(e) {
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey || modalOpen()) return;
  const el = document.activeElement;
  if (el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable)) return;
  const i = '12345'.indexOf(e.key);
  if (i < 0 || e.key.length !== 1) return;
  e.preventDefault();
  if (ORDER[i] !== current) go(ORDER[i]);
}

/* ------------------------------------------------------------------ connection banner */

let bannerTimer = null;
function onStream(s) {
  const dot = $('#stream-state'), txt = $('#conn-text'), banner = $('#conn-banner');
  dot.className = 'stream-state ' + (s.connected ? 'on' : s.fatal ? 'bad' : '');
  txt.textContent = s.connected ? 'Live updates on' : s.fatal ? 'Link expired' : 'Reconnecting…';
  $('#conn').title = s.connected ? 'Live updates connected' : s.fatal ? 'Access link rejected: reopen the panel' : 'Reconnecting…';
  if (s.connected) {
    clearTimeout(bannerTimer);
    bannerTimer = null;
    banner.hidden = true;
    return;
  }
  if (s.fatal) {
    clearTimeout(bannerTimer);
    bannerTimer = null;
    banner.classList.add('bad');
    forgetToken();
    $('#conn-banner-text').textContent = 'This panel was signed out. ' + OPEN_HINT;
    banner.hidden = false;
    return;
  }
  // only show the banner when the stream has been down for a while (brief blips are normal)
  if (!bannerTimer && banner.hidden) {
    bannerTimer = setTimeout(() => {
      bannerTimer = null;
      banner.classList.remove('bad');
      $('#conn-banner-text').textContent = 'Reconnecting to the deck… Changes on the deck will show up once it’s back.';
      banner.hidden = false;
    }, 3000);
  }
}

/* ------------------------------------------------------------------ status chips */

/** One deck: its model. Several: "2 decks", the tooltip names them. */
function deckChip(st) {
  const decks = Array.isArray(st.decks) ? st.decks : [];
  if (decks.length > 1) {
    const names = decks.map((d) => (d.name && d.name !== d.model ? `${d.name} (${d.model})` : d.model || 'Stream Deck'));
    return chip(true, `${decks.length} decks`, 'Stream Decks connected: ' + names.join(', '));
  }
  return chip(!!st.deck, st.deck ? st.deck.model : 'No deck', st.deck ? 'Stream Deck connected' : 'Plug in the Stream Deck');
}

function renderChips() {
  const st = status || {}, sn = snapshot || {};
  const obsOk = sn.obs ? sn.obs.ok : st.obs && st.obs.ok;
  const outs = sn.obs && sn.obs.outputs;
  const liveNow = !!(outs && outs.stream && outs.stream.active);
  const tw = sn.twitch || st.twitch || {};
  $('#status').replaceChildren(...[
    deckChip(st),
    chip(!!obsOk, obsOk ? 'OBS' : 'OBS offline'),
    chip(!!tw.linked, tw.linked ? (tw.user && tw.user.display_name ? tw.user.display_name : 'Twitch') : 'Twitch not linked'),
    liveNow ? chip('live', 'LIVE', 'Streaming') : null].filter(Boolean));
}

/** status.config_fallback: null, or {revision, error} when config.yaml didn't load and the
    daemon runs on a backup. Persistent until the file is fixed or restored. */
function renderFallback() {
  const fb = status && status.config_fallback;
  const ce = status && status.config_error;
  const box = $('#fallback-banner');
  const ok = !!fb && typeof fb === 'object';
  // A reload of config.yaml failed on a running daemon: same banner, amber, until fixed or restored
  const warn = !ok && !!ce && typeof ce === 'object';
  box.hidden = !ok && !warn;
  box.style.background = warn ? 'color-mix(in srgb, var(--warn) 22%, transparent)' : '';
  box.style.borderBottomColor = warn ? 'color-mix(in srgb, var(--warn) 50%, transparent)' : '';
  if (warn) {
    $('#fallback-text').textContent = `Your last change to config.yaml has an error, so it wasn't applied. The deck keeps running on the config from before. ${String(ce.error || '')} — Fix it, or open Backups to restore a version.`;
    return;
  }
  if (!ok) return;
  $('#fallback-text').textContent = `config.yaml has an error, so the panel is running on your backup from ${String(fb.revision || 'an earlier version')}. ${String(fb.error || '')} — Fix it below or restore a backup.`;
}

export async function refreshStatus() {
  try { status = await api('/api/status'); } catch (e) { status = {}; }
  renderChips();
  renderFallback();
  renderPluginNav();
  document.dispatchEvent(new CustomEvent('deck:status', { detail: status }));
}

/* ------------------------------------------------------------------ boot */

const OPEN_HINT = 'Open the panel from your Stream Deck key or run `python3 main.py --open-editor` (or use “The Ultimate Streaming Control Panel for Linux” in your app menu).';

/** Why this (local) panel has no sign-in. A remote panel without one shows pairing instead. */
function noAccessText() {
  return (LAUNCH_ERROR ? LAUNCH_ERROR + '. ' : '') + OPEN_HINT;
}

function fatal(text) {
  const f = $('#fatal');
  f.hidden = false;
  f.replaceChildren(h('strong', { text: 'Can’t open the control panel. ' }), h('span', { text }));
  $$('main > section').forEach((s) => { s.hidden = true; });
  $('#section-tabs').hidden = true;
}

/* A tokenless panel on another device: replace the app with the pairing screen. */
function pairingScreen() {
  document.body.classList.add('is-pairing');   // pairing.css hides the nav and status bar: nothing works until approved
  $$('main > section').forEach((s) => { s.hidden = true; });
  $('#fatal').hidden = true;
  let box = $('#pairing');
  if (!box) { box = h('div', { id: 'pairing' }); $('main').append(box); }
  showPairing(box);
}

/* ------------------------------------------------------------------ approving a new device (local panel only) */

/* pairQueue holds every request still waiting (oldest first), whether or not it has been shown.
   Closing the prompt (Escape, X, backdrop) only marks its requests "seen": they stay in the queue
   and in Settings → Access, but don't pop up again. Requests from one address collapse into the
   newest one (the others are mentioned, and denied together with it). */
const pairQueue = [];        // {id, name, ip, code, expires}
const pairSeen = new Set();  // ids whose prompt was closed without a decision
const pairExpiry = new Map();   // id -> timer that drops the request when it runs out
let pairShown = null;        // {ids: every id the open prompt stands for}, or null
let pairRetry = null;

const sameAddr = (a, b) => (a.ip || '') === (b.ip || '');

/** Forget a request. Closes the prompt only if it is ours: pairShown is cleared whenever any other
    dialog opens, so it can never point at someone else's modal. */
function pairDrop(id) {
  const i = pairQueue.findIndex((q) => q.id === id);
  if (i >= 0) pairQueue.splice(i, 1);
  clearTimeout(pairExpiry.get(id)); pairExpiry.delete(id);
  pairSeen.delete(id);
  pairRefresh();
}

/** The queue changed: close our open prompt (not marking anything seen) so pairNext redraws it with
    the current counts. pairShown is cleared whenever any other dialog opens, so this never closes someone else's. */
function pairRefresh() { if (pairShown) { pairShown = null; closeModal(); } }

function pairRemove(id) { pairDrop(id); pairNext(); }

function pairAdd(r) {
  if (!r || typeof r.id !== 'string' || pairQueue.some((q) => q.id === r.id)) return false;
  const left = Number(r.expires) * 1000 - Date.now();
  if (!(left > 0)) return false;
  pairQueue.push(r);
  pairRefresh();
  pairExpiry.set(r.id, setTimeout(() => pairRemove(r.id), left));
  return true;
}

/** Add what the daemon says is waiting (requests made while this panel was closed or offline).
    `prune`: also forget queued requests the daemon no longer lists (decided while we were away). */
function pairSeed(prune) {
  const list = status && status.remote && Array.isArray(status.remote.pending) ? status.remote.pending : null;
  if (!list) return;
  if (prune) for (const q of [...pairQueue]) if (!list.some((p) => p.id === q.id)) pairDrop(q.id);
  for (const r of list) pairAdd(r);
  pairNext();
}

async function pairDecide(pairs) {   // pairs: [[request, allow], ...]
  for (const [r] of pairs) pairDrop(r.id);
  const res = await Promise.allSettled(pairs.map(([r, allow]) => api('/api/remote/decide', { request: r.id, allow })));
  const bad = res.filter((x) => x.status === 'rejected');
  const [first, allow] = pairs[0];
  const what = pairs.length > 1 && !allow && pairs.every((p) => !p[1]) ? `${pairs.length} requests` : first.name;
  if (bad.length) toast(`Could not ${allow ? 'allow' : 'deny'} ${what}: ${bad[0].reason.message}`, { bad: true });
  else toast(`${allow ? 'Allowed' : 'Denied'} ${what}`);
  refreshStatus();
  pairNext();
}

function pairNext() {
  clearTimeout(pairRetry); pairRetry = null;
  if (pairShown) return;
  const head = pairQueue.find((q) => !pairSeen.has(q.id));
  if (!head) return;
  if (modalOpen()) { pairRetry = setTimeout(pairNext, 1000); return; }   // never replace another dialog
  const group = pairQueue.filter((q) => sameAddr(q, head));
  const r = group[group.length - 1];             // newest from this address
  const earlier = group.slice(0, -1);
  const mine = { ids: group.map((q) => q.id) };
  pairShown = mine;
  const digits = String(r.code);
  const decideShown = (allow) => pairDecide([[r, allow], ...earlier.map((q) => [q, false])]);
  openModal('Allow a new device?', h('div', { class: 'pair-ask' },
    h('p', { class: 'pair-device' }, h('strong', { text: r.ip || 'Unknown address' }),
      h('span', { class: 'muted' }, ' · calls itself “', r.name || 'Unknown device', '”')),
    earlier.length ? h('p', { class: 'pair-more', text: `${earlier.length} earlier request${earlier.length > 1 ? 's' : ''} from this address` }) : null,
    h('p', { class: 'pair-codelabel', text: 'Check this code matches the one on that device' }),
    h('div', { class: 'pair-code', role: 'img', 'aria-label': 'Code: ' + digits.split('').join(' ') },
      `${digits.slice(0, 3)} ${digits.slice(3)}`),
    h('p', { class: 'note warn', text: 'It will be able to control this panel: run commands, OBS and your Twitch channel.' }),
    h('div', { class: 'row end pair-actions' },
      pairQueue.length > 1 ? h('button', { class: 'ghost', text: `Deny all waiting (${pairQueue.length})`,
        onclick: () => pairDecide(pairQueue.map((q) => [q, false])) }) : null,
      h('button', { class: 'ghost', text: 'Deny', onclick: () => decideShown(false) }),
      h('button', { class: 'primary', text: 'Allow', onclick: () => decideShown(true) })),
    h('p', { class: 'pair-later', text: 'Closing this keeps the request in Settings → Access.' })),
  { onClose: () => {   // Escape, X, backdrop, or another dialog taking over: seen, not decided
    if (pairShown !== mine) return;
    pairShown = null;
    for (const id of mine.ids) pairSeen.add(id);
    pairNext();
  } });
  $('#modal-close').focus();   // Allow is never the default focus
}

/* Uncaught errors go to the daemon's log (visible in tools/devserver.py output and
   journalctl), so a blank view is never silent. */
function reportError(message, stack) {
  console.error(message, stack);
  if (TOKEN) api('/api/client-error', { message: String(message).slice(0, 500), stack: String(stack || '').slice(0, 2000) }).catch(() => {});
}
window.addEventListener('error', (e) => reportError(e.message, e.error && e.error.stack));
window.addEventListener('unhandledrejection', (e) => reportError(e.reason && e.reason.message || e.reason, e.reason && e.reason.stack));

let started = false;   // boot finished: the first status is in

async function main() {
  initTheme();
  $('#remote-chip').hidden = !REMOTE;
  $$('.nav button').forEach((b) => b.addEventListener('click', () => go(b.dataset.view)));
  $('#fallback-link').addEventListener('click', () => {
    go('settings', { tab: 'backups' });
    setTimeout(() => { const c = $('#backups-card'); if (c) c.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 250);
  });
  // "Sign out all panels" (Settings): this panel is signed out too, so show what a tokenless panel shows
  document.addEventListener('deck:signedout', () => {
    forgetToken();
    if (REMOTE) pairingScreen(); else fatal(OPEN_HINT);
  });
  $('#modal-close').addEventListener('click', closeModal);
  $('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && modalOpen()) closeModal(); });
  document.addEventListener('keydown', onShortcut);
  window.addEventListener('hashchange', () => showView(viewName()));

  if (!TOKEN) {
    if (REMOTE) pairingScreen(); else fatal(noAccessText());
    return;
  }
  try { await api('/api/status'); } catch (e) {
    if (e.status === 401) forgetToken();
    fatal(e.status === 401 ? 'This panel was signed out (the daemon restarted, or all panels were signed out). ' + OPEN_HINT : e.message);
    return;
  }
  on('status', (data) => { snapshot = data || {}; renderChips(); });
  on('config', () => refreshStatus());
  on('decks', (d) => {   // plugged in or unplugged: the chip follows right away
    if (!d || !Array.isArray(d.decks)) return;
    status = { ...status, decks: d.decks, deck: d.decks[0] || null };   // the primary comes first
    renderChips();
  });
  if (!REMOTE) {
    on('pair_request', (r) => {
      if (pairAdd(r)) pairNext();
      refreshStatus();
    });
    on('pair_done', (d) => { if (d) pairRemove(d.id); refreshStatus(); });
  }
  let wasUp = false;
  onStreamState((s) => {
    onStream(s);
    if (s.connected && !wasUp && !REMOTE && started) refreshStatus().then(() => pairSeed(true));   // reconnected: catch up on what we missed
    wasUp = !!s.connected;
  });
  // Data arriving proves the stream is up: never leave a stale "Reconnecting…" banner showing.
  on('*', () => {
    const banner = $('#conn-banner');
    if (!banner.hidden && !banner.classList.contains('bad')) { clearTimeout(bannerTimer); bannerTimer = null; banner.hidden = true; }
  });
  connect();
  await refreshStatus();
  started = true;
  if (!REMOTE) pairSeed(true);
  setInterval(refreshStatus, 15000);
  await showView(viewName());
  maybeWelcome().catch(() => {});
}

main();
