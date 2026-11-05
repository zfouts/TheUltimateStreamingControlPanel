/* The key editor (Deck view) and the Settings view. Mounted by main.js. */
import { api, on } from './api.js';
import { $, h, icon, empty, clone, isObj, debounce, toast, openModal, closeModal, modalOpen, confirmDialog, asyncButton } from './ui.js';
import { go, refreshStatus as shellRefreshStatus, status as shellStatus } from './main.js';
import { historyCard } from './backups.js';
import { existingSetupCard, externalBackupsCard } from './onboarding.js';

const S = {
  cfg: null, hash: '', catalog: null, device: null, models: {}, secrets: {}, comments: 0,
  devices: [],          // attached decks ([info...], primary first; S.device is the primary)
  // With several decks the Deck view shows them all ('decks'); picking a layout in the toolbar
  // shows just that layout ('layout', always the case with one deck). In 'decks' mode S.layout and
  // S.page follow the active deck (the one last clicked); each deck keeps its own page.
  mode: 'decks', active: null, deckPages: {},
  model: {},            // layout name -> {pages: {name: {grid, dials, touch}}, start_page}
  layout: null, page: null, sel: null,
  dirty: false, undo: [], redo: [], pendingUndo: false,
  options: {}, status: null, view: 'keys', renderSeq: 0, loaded: false,
};
const PAGES_ALL = '__pages__';
const BLANK = '-';
const isBlank = (k) => k === null || k === undefined || k === BLANK || k === '';

/* ------------------------------------------------------------------ layouts & pages */

function gridOf(name) {
  const m = /^(\d+)x(\d+)$/i.exec(name || '');
  if (m) return { cols: +m[1], rows: +m[2] };
  if (S.models[name]) return { cols: S.models[name].cols, rows: S.models[name].rows };
  if (S.device) return { cols: S.device.cols, rows: S.device.rows };
  return { cols: 8, rows: 4 };
}

function modelsFor(name) {
  const g = gridOf(name);
  return Object.entries(S.models)
    .filter(([n, m]) => n === name || (m.cols === g.cols && m.rows === g.rows))
    .map(([n]) => n);
}

function extrasOf(name) {
  let dials = 0, touch = 0;
  for (const n of modelsFor(name)) {
    dials = Math.max(dials, S.models[n].dials);
    touch = Math.max(touch, S.models[n].touch_keys);
  }
  return { dials, touch };
}

function keySize(name) {
  const user = S.devices.find((d) => d.key_size && effectiveLayout(d) === name);
  if (user) return user.key_size;
  for (const n of modelsFor(name)) if (S.models[n].visual) return S.models[n].key_size;
  return [96, 96];
}

function rawPages(name) {
  if (name === PAGES_ALL) return S.cfg.pages || {};
  const L = (S.cfg.layouts || {})[name];
  if (isObj(L) && isObj(L.pages)) return L.pages;
  return isObj(L) ? L : {};
}

function normPage(page, g, ex) {
  let rows = page, dials = [], touch = [];
  if (isObj(page)) { rows = page.keys || []; dials = page.dials || []; touch = page.touch_keys || []; }
  rows = Array.isArray(rows) ? rows : [];
  if (!rows.some(Array.isArray)) {
    const flat = rows;
    rows = [];
    for (let i = 0; i < flat.length; i += g.cols) rows.push(flat.slice(i, i + g.cols));
  }
  const grid = [];
  for (let r = 0; r < g.rows; r++) {
    const row = Array.isArray(rows[r]) ? rows[r] : (rows[r] === undefined ? [] : [rows[r]]);
    for (let c = 0; c < g.cols; c++) grid.push(isBlank(row[c]) ? null : clone(row[c]));
  }
  const pad = (a, n) => { const out = a.slice(0, Math.max(n, a.length)).map((k) => (isBlank(k) ? null : clone(k))); while (out.length < n) out.push(null); return out; };
  return { grid, dials: pad(dials, ex.dials), touch: pad(touch, ex.touch) };
}

function loadModel() {
  S.model = {};
  const names = Object.keys(S.cfg.layouts || {});
  if (S.cfg.pages) names.push(PAGES_ALL);
  for (const name of names) {
    const g = gridOf(name), ex = extrasOf(name);
    const pages = {};
    for (const [pn, p] of Object.entries(rawPages(name))) pages[pn] = normPage(p, g, ex);
    const L = name === PAGES_ALL ? null : S.cfg.layouts[name];
    S.model[name] = { pages, start_page: isObj(L) && isObj(L.pages) ? L.start_page : undefined };
  }
}

function trimTrail(arr) {
  const out = arr.map((k) => (isBlank(k) ? BLANK : k));
  while (out.length && out[out.length - 1] === BLANK) out.pop();
  return out;
}

function serializePages(name) {
  const M = S.model[name], g = gridOf(name), pages = {};
  for (const [pn, P] of Object.entries(M.pages)) {
    const rows = [];
    for (let r = 0; r < g.rows; r++) rows.push(trimTrail(P.grid.slice(r * g.cols, (r + 1) * g.cols)));
    while (rows.length && rows[rows.length - 1].length === 0) rows.pop();
    const dials = trimTrail(P.dials), touch = trimTrail(P.touch);
    if (dials.length || touch.length) {
      pages[pn] = { keys: rows };
      if (dials.length) pages[pn].dials = dials;
      if (touch.length) pages[pn].touch_keys = touch;
    } else {
      pages[pn] = rows;
    }
  }
  return pages;
}

function buildConfig() {
  const cfg = clone(S.cfg);
  const layouts = {};
  for (const name of Object.keys(S.model)) {
    if (name === PAGES_ALL) { cfg.pages = serializePages(name); continue; }
    const pages = serializePages(name), sp = S.model[name].start_page;
    layouts[name] = sp ? { start_page: sp, pages } : pages;
  }
  if (Object.keys(layouts).length) cfg.layouts = layouts; else delete cfg.layouts;
  if (!S.model[PAGES_ALL]) delete cfg.pages;
  return cfg;
}

const curPages = () => (S.model[S.layout] || { pages: {} }).pages;
const curPage = () => curPages()[S.page];
const library = () => { if (!isObj(S.cfg.keys)) S.cfg.keys = {}; return S.cfg.keys; };

/** "8x4 — XL" (the models it is made for); "Shared pages (pages:)" for the top-level pages. */
function layoutBase(name) {
  const ms = name === PAGES_ALL ? [] : modelsFor(name).map((n) => n.replace('Stream Deck ', ''));
  return name === PAGES_ALL ? 'Shared pages (pages:)' : name + (ms.length ? ` — ${ms.join(', ')}` : '');
}
function layoutLabel(name) {
  let label = layoutBase(name);
  const users = usersOf(name);
  if (users.length) label += ` · used by ${users.join(', ')}`;
  return label;
}
const layoutTitle = (name) => (name === PAGES_ALL ? 'Shared pages' : name);

/* ------------------------------------------------------------------ attached decks */

/** devices.<serial> from the config being edited ({} when absent). */
function devCfg(serial) {
  const all = S.cfg && S.cfg.devices;
  return isObj(all) && isObj(all[serial]) ? all[serial] : {};
}
/** Set (or with undefined/'' remove) devices.<serial>.<prop>; empty entries are dropped. Serials may
    contain dots, so this never goes through cfgSet's dotted paths. */
function setDevCfg(serial, prop, value) {
  if (!isObj(S.cfg.devices)) S.cfg.devices = {};
  const all = S.cfg.devices;
  const entry = isObj(all[serial]) ? all[serial] : {};
  if (value === undefined || value === '') delete entry[prop]; else entry[prop] = value;
  if (Object.keys(entry).length) all[serial] = entry; else delete all[serial];
  if (!Object.keys(all).length) delete S.cfg.devices;
}
/** Editor layout name for a config/daemon layout value ("pages" = the top-level pages). */
const fromCfgLayout = (v) => (v === 'pages' ? PAGES_ALL : v);
const toCfgLayout = (v) => (v === PAGES_ALL ? 'pages' : v);

/** What “Automatic” picks for a deck: model → "<cols>x<rows>" → top-level pages → what the daemon chose. */
function autoLayout(info) {
  if (S.model[info.model]) return info.model;
  const g = `${info.cols}x${info.rows}`;
  if (S.model[g]) return g;
  if (S.model[PAGES_ALL]) return PAGES_ALL;
  return fromCfgLayout(info.layout);
}
/** The layout a deck shows with the config as edited here (unsaved choices included). */
function effectiveLayout(info) {
  const want = fromCfgLayout(devCfg(info.serial).layout);
  return want && S.model[want] ? want : autoLayout(info);
}

/** A deck's display name: its name being edited here, else the daemon's, else the model;
    the end of the serial is added when two attached decks would read the same. */
function deckName(info) {
  const base = (d) => devCfg(d.serial).name || d.name || d.model || 'Stream Deck';
  const name = base(info);
  const twins = S.devices.filter((d) => base(d) === name).length > 1;
  return twins ? `${name} (…${String(info.serial || '').slice(-4)})` : name;
}
const usersOf = (name) => S.devices.filter((d) => effectiveLayout(d) === name).map(deckName);
const joinNames = (xs) => (xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`);

function setDevices(list) {
  S.devices = Array.isArray(list) ? list.filter((d) => d && d.serial) : (S.device ? [S.device] : []);
}

/* ------------------------------------------------------------------ undo / dirty */

function snapshot() { return JSON.stringify({ cfg: S.cfg, model: S.model }); }
function pushUndo() {
  S.undo.push(snapshot());
  if (S.undo.length > 100) S.undo.shift();
  S.redo = [];
}
function restore(snap) {
  const s = JSON.parse(snap);
  S.cfg = s.cfg; S.model = s.model;
  if (!S.model[S.layout]) S.layout = Object.keys(S.model)[0];
  if (!curPages()[S.page]) S.page = Object.keys(curPages())[0];
  markDirty();
  renderAll();
}
function undo() { if (!S.undo.length) return; S.redo.push(snapshot()); restore(S.undo.pop()); }
function redo() { if (!S.redo.length) return; S.undo.push(snapshot()); restore(S.redo.pop()); }

function markDirty(d = true) {
  S.dirty = d;
  const b = $('#save');
  b.disabled = !d;
  b.classList.toggle('dirty', d);
  b.querySelector('.txt').textContent = d ? 'Save changes' : 'Saved';
  $('#undo').disabled = !S.undo.length;
  $('#redo').disabled = !S.redo.length;
}

/** Structural change: snapshot, mutate, redraw. */
function commit(fn, { inspector = true } = {}) {
  pushUndo();
  S.pendingUndo = false;
  fn();
  markDirty();
  renderDeck();
  renderLibrary();
  if (inspector) renderInspector();
}

/** Typing in a field: one undo step per field edit, redraw previews only. */
function edited() {
  if (!S.pendingUndo) { pushUndo(); S.pendingUndo = true; }
  markDirty();
  refreshPreviews();
}
document.addEventListener('focusout', () => { S.pendingUndo = false; });

/* ------------------------------------------------------------------ selection access */

/** The page a selection lives on: its own layout/page when given (drag & drop between decks),
    else the page being edited. S.sel itself never carries them. */
function pageOf(sel) {
  if (sel && sel.layout) return ((S.model[sel.layout] || {}).pages || {})[sel.page];
  return curPage();
}

function getSel(sel = S.sel) {
  if (!sel) return undefined;
  const P = pageOf(sel);
  if (sel.kind === 'lib') return library()[sel.name];
  if (!P) return undefined;
  if (sel.kind === 'cell') return P.grid[sel.index];
  if (sel.kind === 'touch') return P.touch[sel.index];
  if (sel.kind === 'dial') return P.dials[sel.index];
  return undefined;
}

function setSel(value, sel = S.sel) {
  const P = pageOf(sel);
  if (sel.kind === 'lib') { library()[sel.name] = value; return; }
  const arr = sel.kind === 'cell' ? P.grid : sel.kind === 'touch' ? P.touch : P.dials;
  arr[sel.index] = value;
}

function refName(key) {
  if (typeof key === 'string' && !isBlank(key)) return key;
  if (isObj(key) && typeof key.use === 'string') return key.use;
  return null;
}

function resolved(key) {
  const ref = refName(key);
  if (ref !== null) return { ...(library()[ref] || {}), ...(isObj(key) ? key : {}), use: undefined };
  return key;
}

function usageCount(name) {
  let n = 0;
  for (const L of Object.values(S.model)) {
    for (const P of Object.values(L.pages)) {
      for (const k of [...P.grid, ...P.touch, ...P.dials]) if (refName(k) === name) n++;
    }
  }
  return n;
}

function expanderOf(key) {
  if (!isObj(key) || !S.catalog) return null;
  return Object.keys(S.catalog.expanders).find((e) => e in key) || null;
}

/* ------------------------------------------------------------------ rendering keys */

const RENDER_BATCH = 200;   // the daemon renders at most 256 keys per request

async function renderImages(keys, size, active = false) {
  const out = [];
  for (let i = 0; i < keys.length; i += RENDER_BATCH) {
    const batch = keys.slice(i, i + RENDER_BATCH);
    try {
      const r = await api('/api/render', { keys: batch, size, library: library(), active });
      out.push(...r.images);
    } catch (e) {
      out.push(...batch.map(() => null));
    }
  }
  return out;
}

const refreshPreviews = debounce(async () => {
  await Promise.all([paintDeck(), paintLibrary(), paintInspectorPreview()]);
}, 120);

/* ---------- imported keys that aren't active yet (core/importers: needs_review / needs_action) */

const IMPORT_SOURCES = { streamcontroller: 'StreamController', elgato: 'an Elgato profile', streamdeck_ui: 'streamdeck-ui', boatswain: 'Boatswain' };

function hasReview(action) {
  if (Array.isArray(action)) return action.some(hasReview);
  return isObj(action) && 'needs_review' in action;
}

/** The needs_review wrapper of an imported state / display, or null. */
function reviewField(key, field) {
  const v = isObj(key) ? key[field] : null;
  return isObj(v) && isObj(v.needs_review) && (isObj(v.needs_review.imported_action) || Array.isArray(v.needs_review.imported_action)) ? v.needs_review : null;
}

/** 'review' (an imported action waiting for an OK), 'action' (nothing mapped yet) or null. */
function attention(key) {
  const r = key && resolved(key);
  if (!isObj(r)) return null;
  if (hasReview(r.action) || reviewField(r, 'state') || reviewField(r, 'display')) return 'review';
  if (r.needs_action) return 'action';
  return null;
}

function tile(key, { selected, wide, badge, onclick, drag, drop, label }) {
  const att = attention(key);
  const name = label ? label + (att === 'review' ? ' (imported, waiting for review)' : att === 'action' ? ' (imported, needs an action)' : '') : '';
  const el = h('button', {
    class: 'tile' + (key ? '' : ' empty') + (selected ? ' selected' : '') + (wide ? ' wide' : '') + (att ? ' attn' : ''),
    title: name, 'aria-label': name || 'empty key', onclick,
    draggable: key ? 'true' : null,
  });
  if (badge) el.append(h('span', { class: 'badge', text: badge }));
  if (drag && key) {
    el.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', JSON.stringify(drag)); e.dataTransfer.effectAllowed = 'move'; });
  }
  if (drop) {
    el.addEventListener('dragover', (e) => { e.preventDefault(); el.classList.add('drop'); });
    el.addEventListener('dragleave', () => el.classList.remove('drop'));
    el.addEventListener('drop', (e) => {
      e.preventDefault();
      el.classList.remove('drop');
      try { drop(JSON.parse(e.dataTransfer.getData('text/plain'))); } catch (err) { /* ignore */ }
    });
  }
  return el;
}

function keyTitle(key) {
  if (!key) return 'Empty key';
  const ref = refName(key);
  const r = resolved(key) || {};
  const exp = expanderOf(r);
  if (exp) return `${S.catalog.expanders[exp].label} ${r[exp]}`;
  return ((r.label || '(no label)') + (ref ? `  · saved key "${ref}"` : '')).replace(/\n/g, ' ');
}

function renderDeck() {
  const multi = syncDecksMode();
  $('#deck').parentElement.hidden = multi;
  unitsHost().hidden = !multi;
  if (multi) { renderUnits(); return; }
  renderSingle();
}

function renderSingle() {
  const P = curPage();
  const deck = $('#deck');
  const g = gridOf(S.layout);
  const ks = keySize(S.layout);
  // key size follows the room inside the stage and the bezel (10px gaps between keys)
  const stage = $('.stage'), cs = getComputedStyle(stage), bs = getComputedStyle($('.bezel'));
  const pads = [cs.paddingLeft, cs.paddingRight, bs.paddingLeft, bs.paddingRight].reduce((n, v) => n + (parseFloat(v) || 0), 0);
  const avail = Math.max(260, stage.clientWidth - pads);
  const t = Math.max(44, Math.min(96, Math.floor((avail - (g.cols - 1) * 10) / g.cols)));
  deck.style.setProperty('--tile', t + 'px');
  deck.style.setProperty('--cols', g.cols);
  deck.replaceChildren();
  $('#touch').hidden = true;
  $('#dials').hidden = true;
  deck.classList.toggle('no-page', !P);
  if (!P) { deck.append(empty('This layout has no pages yet', 'Add one with “Add page” above.', 'grid')); return; }

  const wide = ks[1] > ks[0] * 1.2;
  const dropOn = (sel) => (src) => moveKey(src, sel);
  P.grid.forEach((key, i) => {
    const sel = { kind: 'cell', index: i };
    deck.append(tile(key, {
      selected: S.sel && S.sel.kind === 'cell' && S.sel.index === i, wide,
      label: keyTitle(key),
      onclick: () => select(sel), drag: { from: 'cell', index: i }, drop: dropOn(sel),
    }));
  });

  if (P.touch.length) {
    const tc = $('#touch');
    tc.hidden = false;
    tc.replaceChildren(h('div', { class: 'touch-label', text: 'Touch keys' }));
    tc.style.setProperty('--tile', t + 'px');
    P.touch.forEach((key, i) => {
      const sel = { kind: 'touch', index: i };
      tc.append(tile(key, {
        selected: S.sel && S.sel.kind === 'touch' && S.sel.index === i, label: keyTitle(key),
        onclick: () => select(sel), drag: { from: 'touch', index: i }, drop: dropOn(sel),
      }));
    });
  }
  if (P.dials.length) {
    const dc = $('#dials');
    dc.hidden = false;
    dc.replaceChildren(h('div', { class: 'touch-label', text: 'Dials' }));
    dc.style.setProperty('--dials', P.dials.length);
    P.dials.forEach((d, i) => {
      const r = resolved(d) || {};
      const acts = ['left', 'right', 'press'].filter((k) => r[k]).map((k) => ({ left: '⟲', right: '⟳', press: '●' })[k]).join(' ');
      dc.append(h('div', {
        class: 'dial' + (S.sel && S.sel.kind === 'dial' && S.sel.index === i ? ' selected' : ''),
        role: 'button', tabindex: '0', 'aria-label': `Dial ${i + 1}`, onclick: () => select({ kind: 'dial', index: i }),
        onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select({ kind: 'dial', index: i }); } },
      }, h('div', { class: 'strip' + (d ? '' : ' unset') }, h('div', { class: 'lbl', text: d ? (r.label || `Dial ${i + 1}`) : `Dial ${i + 1}` }),
        h('div', { class: 'sub', text: d ? (acts || 'no actions') : 'not set' })),
      h('div', { class: 'knob' })));
    });
  }
  paintDeck();
}

/* ---------- every connected deck at once */

const DECKS = '__decks__';

function unitsHost() {
  let el = $('#deck-units');
  if (!el) {
    el = h('div', { id: 'deck-units', class: 'deck-units', hidden: true });
    $('#deck').parentElement.before(el);
  }
  return el;
}

const activeInfo = () => S.devices.find((d) => d.serial === S.active) || S.devices[0];

/** The page a deck shows in the editor: the one picked on it, else the daemon's, else its layout's first. */
function unitPage(info) {
  const L = effectiveLayout(info), M = S.model[L];
  if (!M) return null;
  for (const p of [S.deckPages[info.serial], info.page]) if (p && M.pages[p]) return p;
  const sp = M.start_page || S.cfg.start_page;
  return sp && M.pages[sp] ? sp : Object.keys(M.pages)[0] || null;
}

/** Make the edit context (S.layout/S.page) be this deck's. */
function activateDeck(info, page) {
  S.active = info.serial;
  S.layout = effectiveLayout(info);
  if (page) S.deckPages[info.serial] = page;
  S.page = unitPage(info);
  S.deckPages[info.serial] = S.page;
}

/** True when the Deck view shows every connected deck. Keeps S.layout/S.page and the active deck
    in step after edits (a new, renamed or deleted page or layout). */
function syncDecksMode() {
  if (S.devices.length < 2 || S.mode !== 'decks') return false;
  const act = activeInfo();
  if (!S.model[S.layout]) { activateDeck(act); return true; }
  if (effectiveLayout(act) === S.layout) {
    if (curPages()[S.page]) S.deckPages[act.serial] = S.page; else activateDeck(act);
    S.active = act.serial;
    return true;
  }
  const other = S.devices.find((d) => effectiveLayout(d) === S.layout);
  if (other) { S.active = other.serial; S.deckPages[other.serial] = curPages()[S.page] ? S.page : unitPage(other); return true; }
  // a layout no connected deck uses (just created, say): show it on its own
  S.mode = 'layout';
  renderToolbar();
  return false;
}

/** One key size for every deck: side by side when the keys stay at least 52px, else stacked. */
function unitTileSize(grids) {
  const stage = $('.stage'), cs = getComputedStyle(stage);
  const inner = stage.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
  const PAD = 32, GAP = 24, KG = 10;   // bezel padding (both sides), gap between decks, gap between keys
  const cols = grids.reduce((n, g) => n + g.cols, 0);
  const gaps = grids.reduce((n, g) => n + (g.cols - 1) * KG, 0);
  const side = Math.floor((inner - (grids.length - 1) * GAP - grids.length * PAD - gaps) / cols);
  if (side >= 52) return { t: Math.min(96, side), row: true };
  const widest = Math.max(...grids.map((g) => g.cols));
  return { t: Math.max(44, Math.min(96, Math.floor((inner - PAD - (widest - 1) * KG) / widest))), row: false };
}

function renderUnits() {
  const host = unitsHost();
  const units = S.devices.map((info) => {
    const L = effectiveLayout(info);
    S.deckPages[info.serial] = unitPage(info);
    return { info, L, page: S.deckPages[info.serial], g: gridOf(L) };
  });
  const { t, row } = unitTileSize(units.map((u) => u.g));
  host.classList.toggle('side', row);
  host.replaceChildren(...units.map((u) => deckUnit(u, t)));
  paintDeck();
}

function deckUnit({ info, L, page, g }, t) {
  const M = S.model[L];
  const P = M && page ? M.pages[page] : null;
  const isActive = info.serial === S.active;
  const here = L === S.layout && page === S.page;   // the page being edited (shared layouts show it twice)
  const twins = S.devices.filter((d) => d.serial !== info.serial && effectiveLayout(d) === L).map(deckName);
  const go1 = (p) => { activateDeck(info, p); S.sel = null; renderAll(); };
  const sp = M ? (M.start_page || S.cfg.start_page) : null;

  const head = h('div', { class: 'du-head' },
    h('div', { class: 'du-titles' },
      h('span', { class: 'du-icon', 'aria-hidden': 'true' }, icon('deck')),
      h('h2', { class: 'du-name', text: deckName(info) }),
      h('span', { class: 'du-meta', text: [deckName(info).startsWith(info.model || '\0') ? '' : info.model, `layout “${M ? layoutTitle(L) : (L || 'none')}”`].filter(Boolean).join(' · ') }),
      twins.length > 0 && h('span', { class: 'badge accent du-twin', title: 'Both decks show the same keys: editing one edits the other', text: `same layout as ${joinNames(twins)}` })),
    M && h('div', { class: 'du-bar' },
      h('div', { class: 'pills du-pages', role: 'tablist', 'aria-label': `Pages of ${deckName(info)}` },
        Object.keys(M.pages).map((p) => h('button', {
          role: 'tab', 'aria-selected': String(p === page), title: p === sp ? 'First page shown' : '',
          text: (p === sp ? '★ ' : '') + p, onclick: () => go1(p),
        }))),
      h('button', { class: 'small ghost icon-btn', title: 'Add page', 'aria-label': `Add a page to ${layoutTitle(L)}`, onclick: () => { activateDeck(info, page); addPage(); } }, icon('plus')),
      h('button', { class: 'small ghost icon-btn', title: 'Page options', 'aria-label': `Options for page ${page}`, onclick: () => { activateDeck(info, page); renderAll(); pageMenu(); } }, icon('dots'))));

  const grid = h('div', { class: 'deck', role: 'group', 'aria-label': `${deckName(info)} keys` });
  const touch = h('div', { class: 'touch', hidden: true });
  const dials = h('div', { class: 'dials', hidden: true });
  grid.style.setProperty('--tile', t + 'px');
  grid.style.setProperty('--cols', g.cols);
  const el = h('section', { class: 'deck-unit' + (isActive ? ' active' : ''), 'aria-label': deckName(info) }, head,
    h('div', { class: 'bezel du-bezel' }, grid, touch, dials));
  el._paint = { layout: L, page, info, grid, touch };

  if (!M) {
    grid.classList.add('no-page');
    const e = empty('This deck’s layout isn’t in the config', 'Create one for it, or pick a layout under Settings → Decks.', 'grid');
    e.append(h('div', { class: 'mt-2' }, h('button', { class: 'small', onclick: () => layoutMenu({ forDeck: info }) }, icon('plus'), 'Create a layout for this deck')));
    grid.append(e);
    return el;
  }
  if (!P) { grid.classList.add('no-page'); grid.append(empty('This layout has no pages yet', 'Add one with +.', 'grid')); return el; }

  const ks = info.key_size || keySize(L);
  const wide = ks[1] > ks[0] * 1.2;
  const at = (kind, i) => ({ kind, index: i, layout: L, page });
  const pick = (kind, i) => { activateDeck(info, page); select({ kind, index: i }); };
  const selHere = (kind, i) => here && S.sel && S.sel.kind === kind && S.sel.index === i;
  const drop = (kind, i) => (src) => { activateDeck(info, page); moveKey(src, at(kind, i)); };
  P.grid.forEach((key, i) => grid.append(tile(key, {
    selected: selHere('cell', i), wide, label: keyTitle(key),
    onclick: () => pick('cell', i), drag: { from: 'cell', index: i, layout: L, page }, drop: drop('cell', i),
  })));
  if (P.touch.length) {
    touch.hidden = false;
    touch.style.setProperty('--tile', t + 'px');
    touch.append(h('div', { class: 'touch-label', text: 'Touch keys' }));
    P.touch.forEach((key, i) => touch.append(tile(key, {
      selected: selHere('touch', i), label: keyTitle(key),
      onclick: () => pick('touch', i), drag: { from: 'touch', index: i, layout: L, page }, drop: drop('touch', i),
    })));
  }
  if (P.dials.length) {
    dials.hidden = false;
    dials.style.setProperty('--dials', P.dials.length);
    dials.append(h('div', { class: 'touch-label', text: 'Dials' }));
    P.dials.forEach((d, i) => {
      const r = resolved(d) || {};
      const acts = ['left', 'right', 'press'].filter((k) => r[k]).map((k) => ({ left: '⟲', right: '⟳', press: '●' })[k]).join(' ');
      dials.append(h('div', {
        class: 'dial' + (selHere('dial', i) ? ' selected' : ''),
        role: 'button', tabindex: '0', 'aria-label': `Dial ${i + 1}`, onclick: () => pick('dial', i),
        onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick('dial', i); } },
      }, h('div', { class: 'strip' + (d ? '' : ' unset') }, h('div', { class: 'lbl', text: d ? (r.label || `Dial ${i + 1}`) : `Dial ${i + 1}` }),
        h('div', { class: 'sub', text: d ? (acts || 'no actions') : 'not set' })),
      h('div', { class: 'knob' })));
    });
  }
  return el;
}

/** Key images for what is on screen: the single deck, or every deck unit. */
async function paintDeck() {
  const seq = ++S.renderSeq;
  let jobs;
  if (!unitsHost().hidden) {
    jobs = [...unitsHost().children].filter((u) => u._paint).map((u) => {
      const { layout, page, info, grid, touch } = u._paint;
      const P = ((S.model[layout] || {}).pages || {})[page];
      return P && { keys: [...P.grid, ...P.touch], size: info.key_size || keySize(layout), tiles: [...grid.querySelectorAll('.tile'), ...touch.querySelectorAll('.tile')] };
    }).filter(Boolean);
  } else {
    const P = curPage();
    if (!P) return;
    jobs = [{ keys: [...P.grid, ...P.touch], size: keySize(S.layout), tiles: [...$('#deck').querySelectorAll('.tile'), ...$('#touch').querySelectorAll('.tile')] }];
  }
  const all = await Promise.all(jobs.map((j) => renderImages(j.keys, j.size)));
  if (seq !== S.renderSeq) return;
  jobs.forEach((j, n) => j.tiles.forEach((el, i) => {
    const src = all[n][i], old = el.querySelector('img');
    if (src) {
      const img = old || h('img', { alt: '' });
      img.src = src;
      if (!old) el.prepend(img);
    } else if (old) old.remove();
  }));
}

/** Drag & drop: a saved key onto a key, or swap two keys (also between decks and layouts).
    `dst` may name its layout/page; the result is selected on the page being edited. */
function moveKey(src, dst) {
  if (!pageOf(dst)) return;
  const here = { kind: dst.kind, index: dst.index };
  commit(() => {
    if (src.from === 'lib') { setSel(src.name, dst); S.sel = here; return; }
    const srcSel = { kind: src.from, index: src.index, layout: src.layout || dst.layout, page: src.page || dst.page };
    if (!src.layout && !dst.layout) { delete srcSel.layout; delete srcSel.page; }
    if (!pageOf(srcSel)) return;
    if (srcSel.kind === dst.kind && srcSel.index === dst.index && srcSel.layout === dst.layout && srcSel.page === dst.page) return;
    const a = getSel(srcSel), b = getSel(dst);
    setSel(b === undefined ? null : b, srcSel);
    setSel(a, dst);
    S.sel = here;
  });
}

/* ------------------------------------------------------------------ library */

function renderLibrary() {
  const list = $('#library');
  const q = $('#lib-filter').value.toLowerCase();
  list.replaceChildren();
  const names = Object.keys(library()).filter((n) => !q || n.toLowerCase().includes(q) ||
    String((library()[n] || {}).label || '').toLowerCase().includes(q));
  if (!names.length) list.append(h('li', { class: 'lib-empty', text: q ? 'No saved keys match.' : 'No saved keys yet. Use “New”, or “Save as reusable key” on any key.' }));
  for (const name of names) {
    const lbl = String((library()[name] || {}).label || '').replace(/\\n|\n/g, ' ').trim();
    const sel = S.sel && S.sel.kind === 'lib' && S.sel.name === name;
    const li = h('li', {
      class: sel ? 'selected' : '', role: 'button', tabindex: '0', 'aria-pressed': String(!!sel),
      draggable: 'true', title: `${name}: drag onto the deck, or click to edit`, 'data-name': name,
      onclick: () => select({ kind: 'lib', name }),
      onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select({ kind: 'lib', name }); } },
    }, h('span', { class: 'ph' }), h('span', { class: 'meta' }, h('span', { class: 'name', text: name }),
      lbl && lbl !== name && h('span', { class: 'sub', text: lbl })));
    li.addEventListener('dragstart', (e) => e.dataTransfer.setData('text/plain', JSON.stringify({ from: 'lib', name })));
    list.append(li);
  }
  paintLibrary();
}

async function paintLibrary() {
  const items = [...$('#library').querySelectorAll('li[data-name]')];
  if (!items.length) return;
  const imgs = await renderImages(items.map((li) => li.dataset.name), [72, 72]);
  items.forEach((li, i) => {
    const ph = li.firstChild;
    if (imgs[i]) {
      const img = ph.tagName === 'IMG' ? ph : h('img', { alt: '' });
      img.src = imgs[i];
      if (ph !== img) ph.replaceWith(img);
    }
  });
}

function newSavedKey(fromKey) {
  const name = prompt('Name for the saved key (letters, numbers, _ and -):', '');
  if (!name) return null;
  const clean = name.trim().replace(/\s+/g, '_');
  if (!/^[\w-]+$/.test(clean)) { toast('Use only letters, numbers, _ and -', { bad: true }); return null; }
  if (library()[clean]) { toast(`There is already a saved key called "${clean}"`, { bad: true }); return null; }
  library()[clean] = fromKey ? clone(fromKey) : { label: 'New key', bg: '#1f1f24' };
  return clean;
}

/* ------------------------------------------------------------------ inspector */

function select(sel) {
  S.sel = sel;
  S.pendingUndo = false;
  renderDeck();
  renderLibrary();
  renderInspector();
}

/** An inspector card with a section header. */
function section(title, ...kids) {
  return h('section', { class: 'card insp-sec' }, h('h3', { class: 'insp-sec-title', text: title }), ...kids);
}

/** Header card + body: loose nodes are gathered into cards, sections stay as they are. */
function fillInspector(box, kicker, title, nodes) {
  const head = h('div', { class: 'card insp-head' },
    h('div', { class: 'insp-titles' }, h('span', { class: 'insp-kicker', text: kicker }), h('h2', { class: 'card-title', text: title })));
  box.append(head);
  let loose = null;
  for (const n of nodes.flat()) {
    if (!n) continue;
    if (n.id === 'insp-preview') { head.prepend(n); continue; }
    if (n.classList && n.classList.contains('banner')) { head.append(n); continue; }
    if (n.classList && n.classList.contains('insp-sec')) { loose = null; box.append(n); continue; }
    if (!loose) { loose = h('div', { class: 'card insp-sec' }); box.append(loose); }
    loose.append(n);
  }
}

function renderInspector() {
  const box = $('#inspector');
  box.replaceChildren();
  const sel = S.sel;
  if (!sel || (sel.kind !== 'lib' && !curPage())) {
    box.append(h('div', { class: 'card insp-none' }, empty('Nothing selected', 'Click a key on the deck, a dial, or a saved key to edit it.', 'pointer')));
    return;
  }
  const key = getSel(sel);
  if (sel.kind === 'lib' && key === undefined) { S.sel = null; renderInspector(); return; }

  const cols = gridOf(S.layout).cols;
  const kicker = sel.kind === 'lib' ? 'Saved key'
    : !$('#deck-units') || $('#deck-units').hidden ? `Page “${S.page}”` : `${deckName(activeInfo())} · page “${S.page}”`;
  const title = sel.kind === 'lib' ? sel.name :
    sel.kind === 'dial' ? `Dial ${sel.index + 1}` :
      sel.kind === 'touch' ? `Touch key ${sel.index + 1}` :
        `Key ${Math.floor(sel.index / cols) + 1} · ${sel.index % cols + 1}`;

  if (sel.kind === 'dial') { fillInspector(box, kicker, title, dialEditor(sel, key)); return; }
  if (key === null || key === undefined) { fillInspector(box, kicker, title, emptyKeyEditor(sel)); return; }

  const ref = sel.kind !== 'lib' ? refName(key) : null;
  if (ref !== null) { fillInspector(box, kicker, title, refKeyEditor(sel, key, ref)); return; }

  const exp = expanderOf(key);
  fillInspector(box, kicker, title, [previewRow(key), ...(exp ? slotEditor(sel, key, exp) : keyForm(key, sel))]);
}

function previewRow(key) {
  const row = h('div', { class: 'preview-row', id: 'insp-preview' },
    h('figure', {}, h('div', { class: 'ph' }), h('figcaption', { text: 'Normal' })));
  const r = resolved(key);
  if (isObj(r) && r.state) row.append(h('figure', {}, h('div', { class: 'ph' }), h('figcaption', { text: 'When on' })));
  setTimeout(paintInspectorPreview, 0);
  return row;
}

async function paintInspectorPreview() {
  const row = $('#insp-preview');
  if (!row) return;
  const key = getSel();
  if (!key) return;
  const size = keySize(S.layout);
  const figs = [...row.querySelectorAll('figure')];
  const [normal] = await renderImages([key], size);
  const active = figs.length > 1 ? (await renderImages([key], size, true))[0] : null;
  [normal, active].forEach((src, i) => {
    if (!figs[i] || !src) return;
    const ph = figs[i].firstChild;
    const img = ph.tagName === 'IMG' ? ph : h('img', { alt: '' });
    img.src = src;
    if (ph !== img) ph.replaceWith(img);
  });
}

function emptyKeyEditor(sel) {
  const out = [h('p', { class: 'hint', text: 'This key is empty. Start a new one, or reuse a saved key.' })];
  out.push(h('div', { class: 'row mt-3' },
    h('button', { class: 'primary', onclick: () => commit(() => setSel({ label: 'New key', bg: '#1f1f24' }, sel)) }, icon('plus'), 'New key')));
  const names = Object.keys(library());
  if (names.length && sel.kind !== 'lib') {
    const pick = h('select', { 'aria-label': 'Saved key' }, h('option', { value: '', text: 'Choose a saved key…' }),
      names.map((n) => h('option', { value: n, text: `${n}${library()[n].label ? ' — ' + String(library()[n].label).replace(/\n/g, ' ') : ''}` })));
    pick.addEventListener('change', () => { if (pick.value) commit(() => setSel(pick.value, sel)); });
    out.push(section('Use a saved key', field('Saved key', pick)));
  }
  const exps = Object.values(S.catalog.expanders);
  if (exps.length) {
    out.push(section('Or a key that fills itself in',
      h('div', { class: 'row' }, exps.map((e) =>
        h('button', { class: 'small', text: e.label, title: e.help || '', onclick: () => commit(() => setSel({ [e.name]: 1 }, sel)) })))));
  }
  return out;
}

function refKeyEditor(sel, key, ref) {
  const n = usageCount(ref);
  const exists = ref in library();
  const out = [previewRow(key)];
  out.push(h('div', { class: 'banner' + (exists ? '' : ' warn') },
    h('span', { text: exists ? `This is the saved key “${ref}”, used in ${n} place${n === 1 ? '' : 's'}. Edit it to change all of them, or make this one separate.`
      : `This refers to a saved key “${ref}” that doesn't exist.` }),
    h('div', { class: 'row' },
      exists && h('button', { class: 'primary small', onclick: () => select({ kind: 'lib', name: ref }) }, icon('edit'), 'Edit saved key'),
      exists && h('button', { class: 'small', text: 'Make this one separate',
        onclick: () => commit(() => setSel({ ...clone(library()[ref]), ...(isObj(key) ? clone(key) : {}), use: undefined }, sel)) }),
      h('button', { class: 'small danger', text: 'Remove from here', onclick: () => commit(() => setSel(null, sel)) }))));
  return out;
}

function slotEditor(sel, key, exp) {
  const meta = S.catalog.expanders[exp];
  const input = h('input', { type: 'number', min: '1', value: key[exp] });
  input.addEventListener('input', () => { const v = parseInt(input.value, 10); if (v > 0) { key[exp] = v; edited(); } });
  return [section(meta.label,
    h('p', { class: 'hint insp-lead', text: meta.help || 'Fills itself in while the deck runs.' }),
    field((meta.arg && meta.arg.label) || 'Number', input)),
  keyActionsRow(sel, key)];
}

/* ---------- the main key form */

const COLORS = ['#1f1f24', '#333333', '#444444', '#a01818', '#c01010', '#7d2d2d', '#5c4a1a', '#b08000',
  '#2d7d46', '#106040', '#103040', '#203050', '#1b2838', '#3a2d5c', '#6441a5', '#9147ff'];

let fieldSeq = 0;
function field(label, control, help, cls) {
  // point the label at the control (or the first input inside a compound control)
  const target = /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName) ? control : control.querySelector('input:not([type="color"]), select, textarea');
  if (target && !target.id) target.id = 'f' + (++fieldSeq);
  return h('div', { class: 'field' }, h('label', { text: label, for: target ? target.id : null }), control,
    help && h('div', { class: 'help' + (cls ? ' ' + cls : ''), text: help }));
}

function textInput(obj, prop, { placeholder, multiline, onchange } = {}) {
  const el = multiline
    ? h('textarea', { rows: '2', placeholder: placeholder || '' })
    : h('input', { type: 'text', placeholder: placeholder || '' });
  el.value = obj[prop] === undefined || obj[prop] === null ? '' : String(obj[prop]).replace(/\\n/g, '\n');
  el.addEventListener('input', () => {
    if (el.value === '') delete obj[prop]; else obj[prop] = el.value;
    (onchange || edited)();
  });
  return el;
}

function colorInput(obj, prop, fallback) {
  const txt = h('input', { type: 'text', placeholder: fallback, value: obj[prop] || '' });
  const pick = h('input', { type: 'color', value: /^#[0-9a-f]{6}$/i.test(obj[prop] || '') ? obj[prop] : fallback, 'aria-label': 'Pick color' });
  const apply = (v) => { if (v) obj[prop] = v; else delete obj[prop]; edited(); };
  pick.addEventListener('input', () => { txt.value = pick.value; apply(pick.value); });
  txt.addEventListener('input', () => { if (/^#[0-9a-f]{6}$/i.test(txt.value)) pick.value = txt.value; apply(txt.value.trim()); });
  const sw = h('div', { class: 'swatch-row' }, COLORS.map((c) => h('button', {
    class: 'swatch', title: c, 'aria-label': c, style: { background: c },
    onclick: () => { txt.value = c; pick.value = c; apply(c); },
  })));
  return h('div', { class: 'color-field' }, h('div', { class: 'row nowrap' }, txt, pick), sw);
}

function iconInput(obj, prop = 'icon') {
  const txt = h('input', { type: 'text', placeholder: 'none', value: obj[prop] || '' });
  txt.addEventListener('input', () => { if (txt.value) obj[prop] = txt.value; else delete obj[prop]; edited(); });
  const set = (v) => { txt.value = v; if (v) obj[prop] = v; else delete obj[prop]; edited(); closeModal(); };
  return h('div', {}, txt, h('div', { class: 'row mt-2' },
    h('button', { class: 'small', text: '😀 Emoji…', onclick: () => emojiPicker(set) }),
    h('button', { class: 'small', text: 'App icon…', onclick: () => appIconPicker(set) }),
    h('button', { class: 'small ghost', text: 'No icon', onclick: () => set('') })));
}

function lookFields(obj, { compact } = {}) {
  const out = [
    field('Label', textInput(obj, 'label', { multiline: true, placeholder: 'Text on the key' }), compact ? null : 'Press Enter for a second line.'),
    field('Icon', iconInput(obj)),
    field('Background', colorInput(obj, 'bg', '#000000')),
  ];
  if (!compact) {
    out.push(field('Text color', colorInput(obj, 'fg', '#ffffff')));
    const size = h('input', { type: 'number', min: '8', max: '60', placeholder: 'automatic', value: obj.size || '' });
    size.addEventListener('input', () => { const v = parseInt(size.value, 10); if (v) obj.size = v; else delete obj.size; edited(); });
    out.push(field('Text size', size, 'Leave empty to fit automatically. Bigger (e.g. 30) suits a clock.'));
  }
  return out;
}

function keyForm(key, sel) {
  const out = [];
  const review = isObj(key.action) && isObj(key.action.needs_review) ? key.action.needs_review : null;
  if (review) out.push(reviewSection(key, review, sel));     // first: what this imported key would do
  else if (isObj(key.needs_action)) out.push(needsActionBanner(key));
  for (const f of ['state', 'display']) if (reviewField(key, f)) out.push(reviewFieldSection(key, f, sel));
  out.push(section('Look', ...lookFields(key)));

  const confirmBox = h('input', { type: 'checkbox', checked: !!key.confirm });
  confirmBox.addEventListener('change', () => { if (confirmBox.checked) key.confirm = true; else delete key.confirm; edited(); });
  if (!review) {
    out.push(section('When pressed', stepsEditor(key, 'action'),
      h('label', { class: 'check' }, confirmBox, 'Ask for a second press first (for going live, ads…)')));
  }

  // live state
  const stateSec = section('Light up when…');
  out.push(stateSec);
  const hasState = isObj(key.state);
  const stateBox = h('input', { type: 'checkbox', checked: hasState });
  stateBox.addEventListener('change', () => commit(() => {
    if (stateBox.checked) {
      const first = Object.keys(S.catalog.states)[0];
      key.state = { [first]: defaultValue(S.catalog.states[first]) };
      key.active = key.active || { bg: '#c01010' };
    } else { delete key.state; delete key.active; }
  }));
  stateSec.append(h('label', { class: 'check' }, stateBox, 'Change the key’s look while something is on'));
  if (hasState) {
    stateSec.append(specEditor(key, 'state', S.catalog.states, 'Watch'));
    if (!isObj(key.active)) key.active = {};
    stateSec.append(h('div', { class: 'step' }, h('div', { class: 'step-title', text: 'Look while on (empty = unchanged)' }),
      ...lookFields(key.active, { compact: true })));
  }

  // live text
  const dispSec = section('Live text');
  out.push(dispSec);
  const hasDisplay = !!key.display;
  const dispBox = h('input', { type: 'checkbox', checked: hasDisplay });
  dispBox.addEventListener('change', () => commit(() => {
    if (dispBox.checked) key.display = { source: Object.keys(S.catalog.displays)[0], every: 2 };
    else delete key.display;
  }));
  dispSec.append(h('label', { class: 'check' }, dispBox, 'Show live information on the key'));
  if (hasDisplay) {
    if (typeof key.display === 'string') key.display = { source: key.display };
    dispSec.append(specEditor(key, 'display', S.catalog.displays, 'Show'));
  }

  out.push(h('section', { class: 'card insp-sec insp-adv' }, advancedEditor(sel)));
  out.push(keyActionsRow(sel, key));
  return out;
}

/** What an imported action does, as text: the argument of a one-step action, else the whole
    action (same as core/webapi/setup_api.py _arg_text). Shown in full, never truncated. */
function importedArg(action) {
  if (isObj(action) && Object.keys(action).length === 1) {
    const arg = Object.values(action)[0];
    return typeof arg === 'string' ? arg : JSON.stringify(arg, null, 2);
  }
  return JSON.stringify(action, null, 2);
}

function importedKinds(action) {
  const steps = Array.isArray(action) ? action : [action];
  return steps.filter(isObj).flatMap((s) => Object.keys(s));
}

function kindPhrase(kinds) {
  const words = kinds.map((k) => {
    const label = S.catalog && S.catalog.actions[k] && S.catalog.actions[k].label;
    return label && label !== k ? label.charAt(0).toLowerCase() + label.slice(1) : `run the “${k}” action`;
  });
  return words.length ? words.join(', then ') : 'do nothing';
}

/** The flat (row-major) index of grid cell `index` in config.yaml's own rows for that page,
    which is what POST /api/setup/review counts (rows on disk drop trailing blanks). */
function fileKeyIndex(layout, page, index) {
  const raw = rawPages(layout)[page];
  const rows = isObj(raw) ? raw.keys : raw;
  if (!Array.isArray(rows)) return null;
  if (!rows.some(Array.isArray)) return index;
  const cols = gridOf(layout).cols, r = Math.floor(index / cols), c = index % cols;
  let n = 0;
  for (let i = 0; i < r; i++) n += Array.isArray(rows[i]) ? rows[i].length : rows[i] === undefined ? 0 : 1;
  return n + c;
}

/** JSON exactly as Python's json.dumps(v, sort_keys=True, separators=(",", ":")) writes it
    (ensure_ascii: every non-ASCII character as a lowercase \uXXXX escape). */
export function canonicalJson(v) {
  const str = (t) => JSON.stringify(t).replace(/[\u007f-\uffff]/g, (c) => (c === '\u007f' ? c : '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0')));
  if (v === null || typeof v === 'boolean') return JSON.stringify(v);
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : JSON.stringify(v);
  if (typeof v === 'string') return str(v);
  if (Array.isArray(v)) return '[' + v.map(canonicalJson).join(',') + ']';
  if (isObj(v)) return '{' + Object.keys(v).sort().map((k) => str(k) + ':' + canonicalJson(v[k])).join(',') + '}';
  return 'null';
}

/** SHA-256 hex of the canonical JSON of an imported action: POST /api/setup/review's `expect`,
    so the server only enables the exact action the user was shown. */
async function expectOf(action) {
  const bytes = new TextEncoder().encode(canonicalJson(action));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

async function enableImported(sel, shown) {
  if (S.dirty) {
    const ok = await confirmDialog('Save your other changes first?', 'Enabling writes config.yaml, so your unsaved edits are saved first.', { ok: 'Save and continue' });
    if (!ok) return;
    await save();
    if (S.dirty) return;
  }
  const keep = { ...sel }, layout = S.layout, page = S.page;
  await load();                       // index against the file exactly as it is now
  S.layout = layout; S.page = page; S.sel = keep;
  const key = getSel(keep);
  const now = isObj(key) && isObj(key.action) && isObj(key.action.needs_review) ? key.action.needs_review.imported_action : undefined;
  if (now === undefined || canonicalJson(now) !== canonicalJson(shown)) {   // never enable something the user didn't see
    renderAll();
    toast('That key changed since you looked. Check what it runs now.', { bad: true, ms: 7000 });
    return;
  }
  const index = fileKeyIndex(layout, page, keep.index);
  if (index === null) { toast('That key couldn’t be found in config.yaml.', { bad: true }); return; }
  const body = { page, key: index, enable: true, hash: S.hash, expect: await expectOf(shown) };
  if (layout !== PAGES_ALL) body.layout = layout;
  try {
    await api('/api/setup/review', body);
  } catch (e) {
    if (e.status === 409) {   // changed since it was shown: show the new one, never retry
      await load();
      S.layout = layout; S.page = page; S.sel = keep;
      renderAll();
      toast('Not enabled: that key changed since you looked. Check what it runs now.', { bad: true, ms: 8000 });
    } else toast('Not enabled: ' + e.message, { bad: true, ms: 7000 });
    return;
  }
  await load();
  S.layout = layout; S.page = page; S.sel = keep;
  renderAll();
  toast('Enabled. This key runs its action now.', { ok: true });
}

/** Where the selected key sits in config.yaml, as POST /api/import/review addresses it
    (core/importer.review_items): ['pages', page, 'keys', 3], ['layouts', L, 'pages', P, 1, 2], ['keys', name]. */
function filePathOf(sel) {
  if (sel.kind === 'lib') return ['keys', sel.name];
  const layout = S.layout, page = S.page, raw = rawPages(layout)[page];
  if (raw === undefined) return null;
  const L = (S.cfg.layouts || {})[layout];
  const path = layout === PAGES_ALL ? ['pages', page] : ['layouts', layout, ...(isObj(L) && isObj(L.pages) ? ['pages'] : []), page];
  const part = sel.kind === 'cell' ? 'keys' : sel.kind === 'touch' ? 'touch_keys' : 'dials';
  let rows = raw;
  if (isObj(raw)) { path.push(part); rows = raw[part]; }
  else if (part !== 'keys') return null;
  if (!Array.isArray(rows)) return null;
  if (sel.kind === 'cell' && rows.some(Array.isArray)) {
    const cols = gridOf(layout).cols;
    return [...path, Math.floor(sel.index / cols), sel.index % cols];
  }
  return [...path, sel.index];
}

/** Enable an imported state / display (POST /api/import/review). Same safety as enableImported:
    save first, re-read config.yaml, and only enable exactly what the user was shown. */
async function enableImportedField(sel, field, shown) {
  if (S.dirty) {
    const ok = await confirmDialog('Save your other changes first?', 'Enabling writes config.yaml, so your unsaved edits are saved first.', { ok: 'Save and continue' });
    if (!ok) return;
    await save();
    if (S.dirty) return;
  }
  const keep = { ...sel }, layout = S.layout, page = S.page;
  await load();
  S.layout = layout; S.page = page; S.sel = keep;
  const now = reviewField(getSel(keep), field);
  if (!now || canonicalJson(now.imported_action) !== canonicalJson(shown)) {
    renderAll();
    toast('That key changed since you looked. Check what it does now.', { bad: true, ms: 7000 });
    return;
  }
  const path = filePathOf(keep);
  if (!path) { toast('That key couldn’t be found in config.yaml.', { bad: true }); return; }
  try {
    await api('/api/import/review', { path, field, hash: S.hash, expect: await expectOf(shown), enable: true });
  } catch (e) {
    if (e.status === 409) {
      await load();
      S.layout = layout; S.page = page; S.sel = keep;
      renderAll();
      toast('Not enabled: that key changed since you looked. Check what it does now.', { bad: true, ms: 8000 });
    } else toast('Not enabled: ' + e.message, { bad: true, ms: 7000 });
    return;
  }
  await load();
  S.layout = layout; S.page = page; S.sel = keep;
  renderAll();
  toast(`Enabled. This key’s ${field} is on now.`, { ok: true });
}

/** An imported state / display waits for the same kind of OK as an imported action. */
function reviewFieldSection(key, field, sel) {
  const nr = reviewField(key, field), spec = nr.imported_action;
  const noun = field === 'state' ? 'state (when this key lights up or changes)' : 'display (live text or an image on this key)';
  const argId = 'review-arg-' + (++fieldSeq);
  const kinds = field === 'display' ? [String(isObj(spec) ? spec.source : '')].filter(Boolean) : importedKinds(spec);
  return h('section', { class: 'card insp-sec review-sec', 'aria-labelledby': argId + '-t' },
    h('h3', { class: 'insp-sec-title review-title', id: argId + '-t' }, h('span', { class: 'review-dot', 'aria-hidden': 'true', text: '!' }),
      'Imported ' + field, h('span', { class: 'badge warn', text: 'Needs review' })),
    h('p', { class: 'review-lead', text: `This key’s ${noun} came from an import and is off until you enable it.` }),
    h('div', { class: 'review-label', id: argId + '-l', text: kinds.length ? `Exactly what it runs (${kinds.join(', ')})` : 'Exactly what it runs' }),
    h('pre', { class: 'review-arg', id: argId, tabindex: '0', 'aria-labelledby': argId + '-l', text: importedArg(spec) }),
    h('p', { class: 'hint', text: 'Only enable it if you recognize it and want this key to do exactly that.' }),
    h('div', { class: 'review-actions' },
      h('button', { class: 'review-enable', onclick: () => enableImportedField(sel, field, spec) }, icon('check'), 'Enable')));
}

/** An imported action that runs nothing until the user enables it, one key at a time. */
function reviewSection(key, nr, sel) {
  const action = nr.imported_action;
  const source = IMPORT_SOURCES[nr.source] || String(nr.source || 'another app');
  const kinds = importedKinds(action);
  const canEnable = sel.kind === 'cell' && (isObj(action) || Array.isArray(action));
  const argId = 'review-arg-' + (++fieldSeq);
  return h('section', { class: 'card insp-sec review-sec', 'aria-labelledby': argId + '-t' },
    h('h3', { class: 'insp-sec-title review-title', id: argId + '-t' }, h('span', { class: 'review-dot', 'aria-hidden': 'true', text: '!' }), 'Imported action: off until you enable it'),
    h('p', { class: 'review-lead' }, `Imported from ${source}: this key would `, h('strong', { text: kindPhrase(kinds) }), '.'),
    h('div', { class: 'review-label', id: argId + '-l', text: kinds.length > 1 || !isObj(action) ? 'Exactly what it runs' : 'Exactly what it runs (' + kinds[0] + ')' }),
    h('pre', { class: 'review-arg', id: argId, tabindex: '0', 'aria-labelledby': argId + '-l', text: importedArg(action) }),
    nr.original_id ? h('p', { class: 'hint', text: `Their action: ${String(nr.original_id)}` }) : null,
    h('p', { class: 'hint', text: 'Only enable it if you recognize it and want this key to do exactly that.' }),
    h('div', { class: 'review-actions' },
      canEnable
        ? h('button', { class: 'review-enable', onclick: () => enableImported(sel, action) }, icon('check'), 'Enable this action')
        : h('p', { class: 'note warn', text: 'This key can’t be enabled from here. Use “Advanced: edit as YAML” to put the action back by hand.' }),
      h('button', { class: 'small ghost', onclick: () => commit(() => { delete key.action; }) }, icon('trash'), 'Remove it and pick another action')));
}

function needsActionBanner(key) {
  const m = key.needs_action;
  return h('div', { class: 'banner warn needs-action' },
    h('div', { class: 'row nowrap' }, icon('warn'), h('strong', { class: 'grow', text: 'Pick an action' })),
    h('p', { class: 'hint', text: `This key was imported, but its action couldn’t be brought over${m.note ? `: ${String(m.note)}` : '.'}` }),
    m.original_id ? h('p', { class: 'hint', text: `Their action: ${String(m.original_id)}` }) : null,
    h('div', { class: 'row' }, h('button', { class: 'small', onclick: () => commit(() => { delete key.needs_action; }) }, icon('check'), 'Done, hide this note')));
}

function keyActionsRow(sel, key) {
  const row = h('section', { class: 'insp-sec insp-actions row' });
  if (sel.kind !== 'lib') {
    row.append(h('button', { class: 'small', onclick: () => {
      const name = newSavedKey(key);
      if (name) commit(() => { setSel(name, sel); });
    } }, icon('copy'), 'Save as reusable key'));
    row.append(h('button', { class: 'small danger', onclick: () => commit(() => setSel(null, sel)) }, icon('trash'), 'Clear key'));
  } else {
    row.append(h('button', { class: 'small', onclick: () => renameSaved(sel.name) }, icon('edit'), 'Rename'));
    row.append(h('button', { class: 'small danger', onclick: () => deleteSaved(sel.name) }, icon('trash'), 'Delete saved key'));
  }
  return row;
}

function renameSaved(old) {
  const name = (prompt('New name:', old) || '').trim().replace(/\s+/g, '_');
  if (!name || name === old) return;
  if (!/^[\w-]+$/.test(name) || library()[name]) { toast('That name is taken or not valid', { bad: true }); return; }
  commit(() => {
    const lib = library(), copy = {};
    for (const [k, v] of Object.entries(lib)) copy[k === old ? name : k] = v;
    S.cfg.keys = copy;
    for (const L of Object.values(S.model)) for (const P of Object.values(L.pages)) for (const arr of [P.grid, P.touch, P.dials]) {
      arr.forEach((k, i) => {
        if (k === old) arr[i] = name;
        else if (isObj(k) && k.use === old) k.use = name;
      });
    }
    S.sel = { kind: 'lib', name };
  });
}

function deleteSaved(name) {
  const n = usageCount(name);
  if (!confirm(n ? `“${name}” is used in ${n} place${n === 1 ? '' : 's'}. Delete it and clear those keys?` : `Delete “${name}”?`)) return;
  commit(() => {
    delete library()[name];
    for (const L of Object.values(S.model)) for (const P of Object.values(L.pages)) for (const arr of [P.grid, P.touch, P.dials]) {
      arr.forEach((k, i) => { if (refName(k) === name) arr[i] = null; });
    }
    S.sel = null;
  });
}

function advancedEditor(sel) {
  const ta = h('textarea', { class: 'mono', rows: '8', spellcheck: 'false' });
  const err = h('div', { class: 'err' });
  const det = h('details', {}, h('summary', {}, icon('code'), h('span', { text: 'Advanced: edit as YAML' })), ta, err,
    h('div', { class: 'row mt-2' }, h('button', { class: 'small', text: 'Apply YAML', onclick: async () => {
      try {
        const r = await api('/api/yaml/parse', { text: ta.value });
        if (r.value !== null && !isObj(r.value)) throw new Error('a key must be a mapping (field: value)');
        commit(() => setSel(r.value, sel));
      } catch (e) { err.textContent = e.message; }
    } })));
  det.addEventListener('toggle', async () => {
    if (det.open) ta.value = (await api('/api/yaml/dump', { value: getSel(sel) })).text;
  });
  return det;
}

/* ---------- action steps */

function stepsOf(value) {
  if (value === undefined || value === null) return [];
  const arr = Array.isArray(value) ? value : [value];
  return arr.map((s) => (typeof s === 'string' ? { run: s } : s));
}

function writeSteps(obj, prop, steps) {
  if (!steps.length) delete obj[prop];
  else obj[prop] = steps.length === 1 ? steps[0] : steps;
}

function actionSelect(current, onpick, allowed) {
  const groups = {};
  for (const a of Object.values(S.catalog.actions)) {
    if (allowed && !allowed.includes(a.name)) continue;
    (groups[a.group] = groups[a.group] || []).push(a);
  }
  const sel = h('select', { 'aria-label': 'Action' });
  if (!current) sel.append(h('option', { value: '', text: 'Choose what to do…' }));
  for (const g of Object.keys(groups).sort()) {
    sel.append(h('optgroup', { label: g }, groups[g].sort((a, b) => a.label.localeCompare(b.label))
      .map((a) => h('option', { value: a.name, text: a.label }))));
  }
  if (current && !S.catalog.actions[current]) sel.append(h('option', { value: current, text: `${current} (unknown)` }));
  else if (current && allowed && !allowed.includes(current)) {
    sel.append(h('option', { value: current, text: `${S.catalog.actions[current].label} (not allowed here)` }));
  }
  sel.value = current || '';
  sel.addEventListener('change', () => onpick(sel.value));
  return sel;
}

function defaultValue(meta) {
  if (!meta) return true;
  if (meta.default !== undefined) return clone(meta.default);
  const f = meta.arg || (meta.args && meta.scalar && meta.args[meta.scalar]);
  if (f && f.type === 'choice' && f.options.length) return f.options[0].value;
  if (f && f.type === 'number') return f.min || 1;
  if (meta.args && !meta.scalar) return {};
  return '';
}

/** The key inspector's action steps: structural edits go through commit() (undo + redraw). */
function stepsEditor(obj, prop) {
  return stepsUI(obj, prop, { structural: (fn) => commit(fn), edited, tryIt: true });
}

/** Action steps for other views (the Twitch Bot's rewards): the same form, but changes only
    call onchange() (the deck isn't marked dirty) and there is no "Try it" button. `allowed`
    limits the action menu to those names; `field(kind, name, f, value, set)` may return a
    control for one argument instead of the default widget. */
export function actionStepsEditor(obj, prop, { onchange, allowed, field: fieldFor } = {}) {
  const done = () => { if (onchange) onchange(); };
  const host = h('div');
  // a structural edit (add, remove, reorder, another action) redraws the list in place
  const o = { edited: done, allowed, fieldFor, structural: (fn) => { fn(); draw(); done(); } };
  const draw = () => host.replaceChildren(...stepsUI(obj, prop, o).childNodes);
  draw();
  return host;
}

/** Load the action catalog (GET /api/state) if the Deck view hasn't yet. */
export async function ensureCatalog() {
  if (S.catalog) return S.catalog;
  const st = await api('/api/state');
  if (!S.catalog) S.catalog = st.catalog;
  return S.catalog;
}

function stepsUI(obj, prop, o) {
  const wrap = h('div');
  const steps = stepsOf(obj[prop]);
  steps.forEach((step, i) => {
    const kind = isObj(step) ? Object.keys(step)[0] : null;
    const meta = kind && S.catalog.actions[kind];
    const head = h('div', { class: 'step-head' },
      steps.length > 1 && h('span', { class: 'num', text: i + 1 }),
      actionSelect(kind, (k) => o.structural(() => {
        steps[i] = { [k]: defaultValue(S.catalog.actions[k]) };
        writeSteps(obj, prop, steps);
      }), o.allowed),
      steps.length > 1 && i > 0 && h('button', { class: 'small ghost icon-btn', title: 'Move up', 'aria-label': 'Move up', text: '↑',
        onclick: () => o.structural(() => { [steps[i - 1], steps[i]] = [steps[i], steps[i - 1]]; writeSteps(obj, prop, steps); }) }),
      h('button', { class: 'small ghost icon-btn', title: 'Remove step', 'aria-label': 'Remove step', text: '✕',
        onclick: () => o.structural(() => { steps.splice(i, 1); writeSteps(obj, prop, steps); }) }));
    const body = h('div');
    if (kind && !meta) body.append(...argFields(null));
    if (kind && meta) {
      if (meta.help) body.append(h('div', { class: 'help', text: meta.help }));
      if (meta.on_release && steps.length > 1) body.append(h('div', { class: 'help warn', text: 'This only works when it is the key’s only step.' }));
      body.append(...argFields(meta, step[kind], (v) => { step[kind] = v; writeSteps(obj, prop, steps); o.edited(); },
        () => step[kind], o.fieldFor && ((name, f, value, set) => o.fieldFor(kind, name, f, value, set))));
    }
    wrap.append(h('div', { class: 'step' }, head, body));
  });
  wrap.append(h('div', { class: 'row' },
    h('button', { class: 'small', onclick: () => o.structural(() => { steps.push({}); writeSteps(obj, prop, steps); }) },
      icon('plus'), steps.length ? 'Then…' : 'Add an action'),
    o.tryIt && steps.length > 0 && h('button', { class: 'small ghost', title: 'Run these actions now', onclick: () => tryAction(obj[prop]) }, icon('play'), 'Try it')));
  return wrap;
}

async function tryAction(action) {
  if (!action) return;
  try {
    const r = await api('/api/run', { action });
    if (r.ok) toast(r.running ? 'Started (still running)…' : 'Done ✓');
    else toast('Failed: ' + r.error, { bad: true, ms: 6000 });
  } catch (e) { toast(e.message, { bad: true }); }
}

/** Fields for an action / state / display argument, described by its ui metadata. */
function argFields(meta, value, set, get, fieldFor) {
  if (!meta) return [h('div', { class: 'help warn', text: 'No form for this action; use “Advanced: edit as YAML”.' })];
  // fieldFor(name, f, value, set): another view's control for one argument (null: the default widget)
  const control = (name, f, v, s) => (fieldFor && fieldFor(name, f, v, s)) || widget(f, v, s);
  if (meta.arg) return [control(null, meta.arg, value, set)];
  if (meta.args) {
    let obj = value;
    if (!isObj(obj)) {
      if (meta.scalar && obj !== undefined && obj !== '' && obj !== null && typeof obj !== 'object') obj = { [meta.scalar]: obj };
      else if (meta.scalar || obj === undefined || obj === null || obj === '') obj = {};
      else return [h('div', { class: 'help warn', text: 'Written in a form this editor can’t show; use “Advanced”.' })];
    }
    const write = () => {
      const clean = {};
      for (const [k, v] of Object.entries(obj)) if (v !== undefined && v !== '' && v !== null && v !== false) clean[k] = v;
      const keys = Object.keys(clean);
      set(meta.scalar && keys.length === 1 && keys[0] === meta.scalar ? clean[meta.scalar] : clean);
    };
    return Object.entries(meta.args).map(([name, f]) => control(name, f, obj[name], (v) => { obj[name] = v; write(); }));
  }
  // Modules that don't describe their arguments: edit the value as YAML.
  return [widget({ type: 'yaml', label: 'Value', help: 'This action has no form; write its value as YAML.' }, value, set)];
}

function widget(f, value, set) {
  const label = f.label || '';
  let control;
  switch (f.type) {
    case 'number': {
      control = h('input', { type: 'number', value: value === undefined ? '' : value, min: f.min, max: f.max, step: f.step || 'any' });
      control.addEventListener('input', () => set(control.value === '' ? undefined : Number(control.value)));
      break;
    }
    case 'bool': {
      const cb = h('input', { type: 'checkbox', checked: !!value });
      cb.addEventListener('change', () => set(cb.checked || undefined));
      return h('label', { class: 'check' }, cb, label);
    }
    case 'choice':
      control = choiceControl(f.options, value, set, f.custom);
      break;
    case 'source':
      control = sourceControl(f.source, value, set);
      break;
    case 'yaml':
      control = yamlControl(value, set);
      break;
    default: {
      control = f.multiline ? h('textarea', { rows: '3', placeholder: f.placeholder || '' })
        : h('input', { type: 'text', placeholder: f.placeholder || '' });
      control.value = value === undefined || value === null ? '' : String(value);
      control.addEventListener('input', () => set(control.value === '' ? undefined : control.value));
    }
  }
  const help = f.help || (f.type === 'command' ? 'Runs as you, through the shell.' : null);
  return field(label, control, help, f.type === 'command' ? 'warn' : null);
}

function choiceControl(options, value, set, custom) {
  const sel = h('select');
  const known = options.findIndex((o) => String(o.value) === String(value));
  const unset = value === undefined || value === null || value === '';
  if (unset) sel.append(h('option', { value: 'unset', text: '(not set)' }));
  options.forEach((o, i) => sel.append(h('option', { value: String(i), text: o.label })));
  let other = null;
  if (custom) {
    sel.append(h('option', { value: 'other', text: 'Other…' }));
    other = h('input', { type: 'text', placeholder: 'type a value', value: known < 0 && value !== undefined ? String(value) : '' });
    other.hidden = !(known < 0 && value !== undefined && value !== '');
    other.addEventListener('input', () => set(other.value === '' ? undefined : (isNaN(other.value) || other.value.trim() === '' ? other.value : Number(other.value))));
  } else if (known < 0 && value !== undefined && value !== '') {
    sel.append(h('option', { value: 'keep', text: `${value} (current)` }));
  }
  sel.value = known >= 0 ? String(known) : unset ? 'unset' : (custom ? 'other' : 'keep');
  sel.addEventListener('change', () => {
    if (sel.value === 'other') { other.hidden = false; other.focus(); return; }
    if (other) other.hidden = true;
    if (sel.value === 'unset') set(undefined);
    else if (sel.value !== 'keep') set(options[+sel.value].value);
  });
  return other ? h('div', {}, sel, other) : sel;
}

const CLIENT_SOURCES = {
  pages: () => [...Object.keys(curPages()).map((p) => ({ value: p, label: p })), { value: 'back', label: '← back to the previous page' }],
  polls: () => ((S.cfg.brb || {}).polls || []).map((p, i) => ({ value: i, label: `${i + 1}. ${p.question || '(no question)'}` })),
  saved_keys: () => Object.keys(library()).map((n) => ({ value: n, label: n })),
};

async function loadOptions(name) {
  if (CLIENT_SOURCES[name]) return CLIENT_SOURCES[name]();
  const hit = S.options[name];
  if (hit && Date.now() - hit.at < 15000) return hit.list;
  const r = await api('/api/options?name=' + encodeURIComponent(name));
  S.options[name] = { at: Date.now(), list: r.options };
  return r.options;
}

function sourceControl(name, value, set) {
  const sel = h('select', {}, h('option', { text: 'Loading…' }));
  const other = h('input', { type: 'text', placeholder: 'type a name', hidden: true });
  const note = h('div', { class: 'help' });
  other.addEventListener('input', () => set(other.value === '' ? undefined : other.value));
  loadOptions(name).then((opts) => {
    sel.replaceChildren();
    if (value === undefined || value === '') sel.append(h('option', { value: '', text: 'Choose…' }));
    opts.forEach((o, i) => sel.append(h('option', { value: String(i), text: o.label })));
    const idx = opts.findIndex((o) => String(o.value) === String(value));
    if (idx < 0 && value !== undefined && value !== '') sel.append(h('option', { value: 'keep', text: `${value} (current)` }));
    sel.append(h('option', { value: 'other', text: 'Type a name…' }));
    sel.value = idx >= 0 ? String(idx) : (value !== undefined && value !== '' ? 'keep' : '');
    if (!opts.length) note.textContent = 'Nothing found yet. You can type a name instead.';
    sel.onchange = () => {
      other.hidden = sel.value !== 'other';
      if (sel.value === 'other') { other.focus(); return; }
      if (sel.value === 'keep' || sel.value === '') return;
      set(opts[+sel.value].value);
    };
  }).catch((e) => {
    sel.replaceChildren(h('option', { value: '', text: value !== undefined ? String(value) : '(type below)' }));
    other.hidden = false;
    other.value = value === undefined ? '' : String(value);
    note.textContent = `Couldn’t load the list (${e.message}). Type the name instead.`;
    note.className = 'help warn';
  });
  return h('div', {}, sel, other, note);
}

function yamlControl(value, set) {
  const ta = h('textarea', { class: 'mono', rows: '3', spellcheck: 'false', placeholder: 'key: value' });
  const err = h('div', { class: 'err' });
  if (value !== undefined) api('/api/yaml/dump', { value }).then((r) => { ta.value = r.text.trim(); });
  ta.addEventListener('change', async () => {
    try {
      const r = await api('/api/yaml/parse', { text: ta.value });
      err.textContent = '';
      set(r.value === null ? undefined : r.value);
    } catch (e) { err.textContent = 'Not valid: ' + e.message; }
  });
  return h('div', {}, ta, err);
}

/* ---------- state / display specs */

function specEditor(obj, prop, registry, verb) {
  const spec = obj[prop];
  const isDisplay = prop === 'display';
  const kind = isDisplay ? spec.source : Object.keys(spec).find((k) => registry[k]);
  const meta = registry[kind];
  const groups = {};
  for (const m of Object.values(registry)) (groups[m.group] = groups[m.group] || []).push(m);
  const sel = h('select', { 'aria-label': verb });
  for (const g of Object.keys(groups).sort()) {
    sel.append(h('optgroup', { label: g }, groups[g].map((m) => h('option', { value: m.name, text: m.label }))));
  }
  if (kind && !registry[kind]) sel.append(h('option', { value: kind, text: `${kind} (unknown)` }));
  sel.value = kind || '';
  sel.addEventListener('change', () => commit(() => {
    const every = spec.every;
    obj[prop] = isDisplay ? { source: sel.value } : { [sel.value]: defaultValue(registry[sel.value]) };
    if (every) obj[prop].every = every;
  }));
  const out = [field(verb, sel)];
  if (meta && meta.arg && !isDisplay) out.push(widget(meta.arg, spec[kind], (v) => { spec[kind] = v === undefined ? '' : v; edited(); }));
  if (meta && meta.extra) {
    for (const [name, f] of Object.entries(meta.extra)) {
      out.push(widget(f, spec[name], (v) => { if (v === undefined) delete spec[name]; else spec[name] = v; edited(); }));
    }
  }
  if (meta && meta.help) out.push(h('div', { class: 'help', text: meta.help }));
  const every = h('input', { type: 'number', min: '0.5', step: '0.5', placeholder: '2', value: spec.every || '' });
  every.addEventListener('input', () => { if (every.value) spec.every = Number(every.value); else delete spec.every; edited(); });
  out.push(field('Check every (seconds)', every));
  return h('div', { class: 'step' }, out);
}

/* ---------- dials */

function dialEditor(sel, dial) {
  const out = [];
  if (dial === null || dial === undefined) {
    out.push(h('p', { class: 'hint', text: 'This dial does nothing on this page.' }),
      h('div', { class: 'row mt-3' }, h('button', { class: 'primary', onclick: () => commit(() => setSel({ label: 'Volume', left: { volume: 'down' }, right: { volume: 'up' }, press: { volume: 'mute' } }, sel)) }, icon('plus'), 'Set up this dial')));
    return out;
  }
  const ref = refName(dial);
  if (ref !== null) return out.concat(refKeyEditor(sel, dial, ref).slice(1));
  out.push(section('Look', field('Label', textInput(dial, 'label', { multiline: true, onchange: () => { edited(); renderDeckSoon(); } }), 'Shown on the touch strip above the dial.')));
  for (const [prop, title] of [['left', 'Turn left'], ['right', 'Turn right'], ['press', 'Press (or tap the strip)']]) {
    out.push(section(title, stepsEditor(dial, prop)));
  }
  out.push(h('section', { class: 'insp-sec insp-actions row' },
    h('button', { class: 'small danger', onclick: () => commit(() => setSel(null, sel)) }, icon('trash'), 'Clear dial')));
  return out;
}
const renderDeckSoon = debounce(() => renderDeck(), 300);

/* ------------------------------------------------------------------ pickers */

const EMOJI = ('🔴 ⏺️ ⏸️ ▶️ ⏹️ ⏮️ ⏭️ 🔁 💾 📍 🎬 🎥 📹 📷 📸 🎙️ 🎤 🔇 🔈 🔉 🔊 🎧 🎵 🎶 📻 📺 🖥️ 💻 ⌨️ 🖱️ 🎮 🕹️ 👾 ' +
  '💬 🗨️ 👀 👋 🙌 👍 👎 😀 😂 😎 🤔 😴 🔥 ⭐ 💜 ❤️ 💯 ✨ 🎉 🎊 🏆 🎯 🎲 🧩 💰 💎 🛒 🔗 ✏️ 📝 🛠️ ⚙️ 🔧 🔨 ' +
  '⏳ ⌛ ⏱️ ⏰ 🕒 📅 ☕ 🍕 🍿 🌙 ☀️ 💡 🔦 🔒 🔓 🔑 🚪 🏠 🛋️ 🌀 ❄️ 🌡️ 🚀 ✈️ 🚗 📦 📁 📂 🗂️ 📊 📈 🧪 ✂️ 📋 🗳️ ' +
  '⬅️ ➡️ ⬆️ ⬇️ ↩️ ↪️ 🔄 ➕ ➖ ✅ ❌ ⚠️ 🛑 🆗 🆕 🔔 🔕 📶 📡 🌐 📣 📢 🎞️ 🖼️ 🧭 🗺️ 🐢 🐇 🦊 🐱 🐶 🦄 👻 💀 🤖').split(' ');

function emojiPicker(set) {
  const input = h('input', { type: 'text', placeholder: 'Or paste any emoji here and press Enter' });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && input.value.trim()) set('emoji:' + input.value.trim()); });
  openModal('Choose an emoji', h('div', {}, input, h('div', { class: 'emoji-grid mt-3' },
    EMOJI.map((e) => h('button', { text: e, title: e, 'aria-label': e, onclick: () => set('emoji:' + e) })))));
}

function appIconPicker(set) {
  const input = h('input', { type: 'search', placeholder: 'Search installed app icons, e.g. firefox, discord, obs' });
  const grid = h('div', { class: 'icon-grid' });
  const note = h('p', { class: 'hint', text: 'Type at least two letters.' });
  const search = debounce(async () => {
    const q = input.value.trim();
    if (q.length < 2) { grid.replaceChildren(); return; }
    note.textContent = 'Searching…';
    const { icons } = await api('/api/icons?q=' + encodeURIComponent(q));
    note.textContent = icons.length ? '' : 'No icons found. You can also type a full path to an image file in the Icon box.';
    const imgs = await renderImages(icons.map((n) => ({ icon: n, bg: '#000000' })), [64, 64]);
    grid.replaceChildren(...icons.map((n, i) => h('button', { onclick: () => set(n), title: n },
      imgs[i] ? h('img', { src: imgs[i], alt: '' }) : null, h('span', { text: n }))));
  }, 250);
  input.addEventListener('input', search);
  openModal('Choose an app icon', h('div', {}, input, note, grid));
}

/* ------------------------------------------------------------------ toolbar: layouts & pages */

function renderToolbar() {
  const sel = $('#layout-select');
  const all = S.devices.length > 1;
  const showAll = all && S.mode === 'decks';
  const opts = Object.keys(S.model).map((n) => h('option', { value: n, text: layoutLabel(n) }));
  if (all) sel.replaceChildren(h('option', { value: DECKS, text: `All connected decks (${S.devices.length})` }), h('optgroup', { label: 'Or edit one layout' }, opts));
  else sel.replaceChildren(...opts);
  sel.value = showAll ? DECKS : S.layout;
  $('.pagebar').hidden = showAll;   // each deck has its own page pills
  const note = $('#layout-note');
  note.className = 'page-sub';
  const users = usersOf(S.layout);
  if (showAll) {
    const shared = Object.keys(S.model).some((n) => usersOf(n).length > 1);
    note.textContent = `Editing ${joinNames(S.devices.map(deckName))}.` + (shared ? ' Decks that share a layout show the same keys.' : '');
  } else if (!S.devices.length) note.textContent = 'No Stream Deck connected: editing anyway.';
  else if (users.length) note.textContent = `Editing the layout ${joinNames(users)} ${users.length > 1 ? 'use' : 'uses'}.`;
  else {
    const others = S.devices.map((d) => `${deckName(d)} uses “${layoutTitle(effectiveLayout(d))}”`);
    note.textContent = `No connected deck uses this layout: ${others.join(', ')}.`;
    note.className = 'page-sub note warn';
  }
  const tabs = $('#page-tabs');
  const sp = startPage();
  tabs.replaceChildren(...Object.keys(curPages()).map((p) => h('button', {
    role: 'tab', 'aria-selected': String(p === S.page), title: p === sp ? 'First page shown' : '',
    text: (p === sp ? '★ ' : '') + p, onclick: () => { S.page = p; S.sel = null; renderAll(); },
  })));
}

function startPage() {
  const M = S.model[S.layout];
  return (M && M.start_page) || S.cfg.start_page || Object.keys(curPages())[0];
}

function pageMenu() {
  const name = S.page;
  if (!name) return;
  const body = h('div', { class: 'stack menu-list' },
    h('button', { onclick: () => { closeModal(); renamePage(); } }, icon('edit'), 'Rename page'),
    h('button', { onclick: () => { closeModal(); duplicatePage(); } }, icon('copy'), 'Duplicate page'),
    h('button', { onclick: () => { closeModal(); commit(() => { if (S.layout === PAGES_ALL || !S.model[S.layout].start_page) S.cfg.start_page = name; else S.model[S.layout].start_page = name; }); renderToolbar(); } }, icon('flag'), 'Make this the first page'),
    h('button', { class: 'danger', onclick: () => { closeModal(); deletePage(); } }, icon('trash'), 'Delete page'));
  openModal(`Page “${name}”`, body);
}

function validPageName(n) {
  n = (n || '').trim().replace(/\s+/g, '_');
  if (!n) return null;
  if (!/^[\w-]+$/.test(n)) { toast('Use only letters, numbers, _ and -', { bad: true }); return null; }
  if (curPages()[n]) { toast(`There is already a page called “${n}”`, { bad: true }); return null; }
  return n;
}

function addPage() {
  const n = validPageName(prompt('Name of the new page:', ''));
  if (!n) return;
  const g = gridOf(S.layout), ex = extrasOf(S.layout);
  commit(() => {
    curPages()[n] = normPage([], g, ex);
    // a Back key in the bottom-right corner, so the new page is never a dead end
    curPages()[n].grid[g.cols * g.rows - 1] = library().back ? 'back' : { label: 'Back', icon: 'emoji:↩️', bg: '#444444', action: { page: 'back' } };
    S.page = n;
    S.sel = null;
  });
  renderToolbar();
}

function renamePage() {
  const old = S.page;
  const n = validPageName(prompt('New name:', old));
  if (!n) return;
  commit(() => {
    const pages = curPages(), copy = {};
    for (const [k, v] of Object.entries(pages)) copy[k === old ? n : k] = v;
    S.model[S.layout].pages = copy;
    // update page links in this layout and in saved keys
    const fix = (a) => {
      for (const s of stepsOf(a)) if (isObj(s) && s.page === old) s.page = n;
    };
    for (const P of Object.values(copy)) for (const k of [...P.grid, ...P.touch, ...P.dials]) {
      if (isObj(k)) ['action', 'left', 'right', 'press'].forEach((f) => { if (k[f]) { const st = stepsOf(k[f]); fix(st); k[f] = st.length === 1 ? st[0] : st; } });
    }
    if (S.model[S.layout].start_page === old) S.model[S.layout].start_page = n;
    if (S.cfg.start_page === old) S.cfg.start_page = n;
    S.page = n;
  });
  renderToolbar();
}

function duplicatePage() {
  const n = validPageName(prompt('Name for the copy:', S.page + '_copy'));
  if (!n) return;
  commit(() => { curPages()[n] = clone(curPage()); S.page = n; });
  renderToolbar();
}

function deletePage() {
  if (Object.keys(curPages()).length < 2) { toast('A layout needs at least one page', { bad: true }); return; }
  if (!confirm(`Delete the page “${S.page}” and its keys?`)) return;
  commit(() => {
    delete curPages()[S.page];
    S.page = Object.keys(curPages())[0];
    S.sel = null;
  });
  renderToolbar();
}

/** The layouts dialog. From Settings (`forDeck`), it only offers a new layout for that deck's
    grid, copied from the layout the deck shows now, and assigns it to the deck. */
function layoutMenu({ forDeck } = {}) {
  const body = h('div', {});
  const actions = h('div', { class: 'stack menu-list' });
  const options = new Set();
  const first = forDeck || S.device;
  if (first) options.add(`${first.cols}x${first.rows}`);
  for (const m of Object.values(S.models)) options.add(`${m.cols}x${m.rows}`);
  const missing = [...options].filter((g) => !S.model[g]);
  const from = forDeck && S.model[effectiveLayout(forDeck)] ? effectiveLayout(forDeck) : S.layout;
  if (missing.length) {
    const pick = h('select', {}, missing.map((g) => h('option', { value: g, text: layoutLabel(g) })));
    if (first && missing.includes(`${first.cols}x${first.rows}`)) pick.value = `${first.cols}x${first.rows}`;
    const copy = h('input', { type: 'checkbox', checked: true });
    const create = () => {
      closeModal();
      S.layout = from;
      createLayout(pick.value, copy.checked, forDeck && ((name) => setDevCfg(forDeck.serial, 'layout', toCfgLayout(name))));
      if (forDeck) {
        renderSettings();
        toast(h('div', {}, h('span', { text: `New layout “${pick.value}” created for ${deckName(forDeck)}. ` }),
          h('button', { class: 'small', onclick: () => go('deck') }, 'Edit its keys', icon('arrowRight'))), { ok: true, ms: 7000 });
      }
    };
    body.append(h('h3', { text: 'New layout' }), field('For decks with', pick),
      h('label', { class: 'check' }, copy, `Start from a copy of “${layoutTitle(from)}”`),
      h('div', { class: 'row end mt-3' }, h('button', { class: 'primary', text: 'Create layout', onclick: create })));
  } else if (forDeck) {
    body.append(h('p', { class: 'muted', text: 'Every deck size already has a layout. Pick one from the list.' }));
  }
  if (forDeck) { openModal(`New layout for ${deckName(forDeck)}`, body); return; }
  if (S.layout !== PAGES_ALL) {
    actions.append(h('button', { class: 'danger', onclick: () => {
      closeModal();
      if (Object.keys(S.model).length < 2) { toast('You need at least one layout', { bad: true }); return; }
      if (!confirm(`Delete the whole “${S.layout}” layout and all its pages?`)) return;
      commit(() => { delete S.model[S.layout]; S.layout = Object.keys(S.model)[0]; S.page = Object.keys(curPages())[0]; S.sel = null; });
      renderToolbar();
    } }, icon('trash'), `Delete layout “${S.layout}”`));
    body.append(h('h3', { text: 'This layout' }), actions);
  }
  openModal('Layouts', body);
}

function createLayout(name, copyCurrent, then) {
  const g = gridOf(name), ex = extrasOf(name), from = gridOf(S.layout);
  commit(() => {
    const pages = {};
    if (copyCurrent) {
      for (const [pn, P] of Object.entries(curPages())) {
        const np = normPage([], g, ex);
        const off = Math.max(0, Math.floor((g.cols - from.cols) / 2));
        for (let r = 0; r < Math.min(g.rows, from.rows); r++) {
          for (let c = 0; c < Math.min(g.cols - off, from.cols); c++) np.grid[r * g.cols + c + off] = clone(P.grid[r * from.cols + c]);
        }
        np.dials = np.dials.map((d, i) => clone(P.dials[i]) || null);
        np.touch = np.touch.map((d, i) => clone(P.touch[i]) || null);
        pages[pn] = np;
      }
    } else {
      pages.main = normPage([], g, ex);
    }
    S.model[name] = { pages, start_page: undefined };
    S.layout = name;
    S.page = Object.keys(pages)[0];
    S.sel = null;
    if (then) then(name);   // same undo step
  });
  renderToolbar();
  if (copyCurrent && (g.cols < from.cols || g.rows < from.rows)) toast('The new deck is smaller, so keys that didn’t fit were left out.', { ms: 6000 });
}

/* ------------------------------------------------------------------ settings view */

function cfgGet(path) {
  return path.split('.').reduce((o, k) => (isObj(o) ? o[k] : undefined), S.cfg);
}
function cfgSet(path, value) {
  const parts = path.split('.');
  let o = S.cfg;
  for (const k of parts.slice(0, -1)) { if (!isObj(o[k])) o[k] = {}; o = o[k]; }
  const last = parts[parts.length - 1];
  if (value === undefined || value === '') delete o[last]; else o[last] = value;
}

function sField(path, label, f = {}, help) {
  const value = cfgGet(path);
  const set = (v) => { if (!S.pendingUndo) { pushUndo(); S.pendingUndo = true; } cfgSet(path, v); markDirty(); };
  const w = widget({ type: 'text', ...f, label, help }, value, set);
  return w;
}

function checkItem(ok, what, how, action) {
  // 'wait': blocked by an earlier item (not a failure of its own), shown neutral
  const wait = ok === 'wait';
  return h('li', {}, h('span', { class: 'status-icon ' + (wait ? 'wait' : ok ? 'ok' : 'bad') }, icon(wait ? 'clock' : ok ? 'check' : 'x')),
    h('div', { class: 'status-text' }, h('div', { class: 'status-title', text: what }), how && h('div', { class: 'status-how' }, how),
      ok !== true && action && h('div', { class: 'status-action' }, action)));
}

/** A settings card: icon, title, optional subtitle and header action, then the body. */
function sCard(iconName, title, sub, headAction, ...body) {
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('span', { class: 'card-icon' + (iconName === 'twitch' ? ' twitch' : '') }, icon(iconName)),
      h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: title }), sub && h('p', { class: 'card-sub', text: sub })),
      headAction),
    ...body);
}

/* ----- Decks card: one row per attached deck, then the remembered ones that are unplugged */

const NEW_LAYOUT = '__new__';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** One undo step per focused control (like sField), then mark dirty. */
function devEdit(fn) {
  if (!S.pendingUndo) { pushUndo(); S.pendingUndo = true; }
  fn();
  markDirty();
}
/** A discrete change (a select): its own undo step. */
function devCommit(fn) {
  pushUndo();
  S.pendingUndo = false;
  fn();
  markDirty();
}

function redrawDecks() {
  const old = $('#decks-card');
  if (old) old.replaceWith(decksCard());
  renderToolbar();
}

function deckLayoutSelect(info) {
  const want = devCfg(info.serial).layout;
  const auto = autoLayout(info);
  const sel = h('select', {},
    h('option', { value: '', text: `Automatic (${auto ? layoutTitle(auto) : 'none yet'})` }),
    Object.keys(S.model).map((n) => h('option', { value: toCfgLayout(n), text: layoutBase(n) })),
    want && !S.model[fromCfgLayout(want)] && h('option', { value: want, text: `${want} (missing: Automatic is used)` }),
    !S.model[`${info.cols}x${info.rows}`] && h('option', { value: NEW_LAYOUT, text: 'Create a new layout for this deck…' }));
  sel.value = want || '';
  sel.addEventListener('change', () => {
    if (sel.value === NEW_LAYOUT) { sel.value = want || ''; layoutMenu({ forDeck: info }); return; }
    devCommit(() => {
      setDevCfg(info.serial, 'layout', sel.value || undefined);
      // a start page the new layout doesn't have would be ignored: drop it
      const sp = devCfg(info.serial).start_page;
      if (sp && !(S.model[effectiveLayout(info)] || { pages: {} }).pages[sp]) setDevCfg(info.serial, 'start_page', undefined);
    });
    redrawDecks();
  });
  return sel;
}

function layoutFitHelp(info) {
  const L = effectiveLayout(info);
  if (!L || L === PAGES_ALL || !(/^\d+x\d+$/i.test(L) || S.models[L])) return null;
  const g = gridOf(L);
  return g.cols === info.cols && g.rows === info.rows ? null : `Made for ${g.cols}×${g.rows} keys; this deck has ${info.cols}×${info.rows}.`;
}

function deckStartSelect(info) {
  const L = effectiveLayout(info), M = S.model[L] || { pages: {} };
  const pages = Object.keys(M.pages);
  const first = (M.start_page && M.pages[M.start_page] ? M.start_page : null) || (S.cfg.start_page && M.pages[S.cfg.start_page] ? S.cfg.start_page : null) || pages[0];
  const want = devCfg(info.serial).start_page;
  const sel = h('select', {},
    h('option', { value: '', text: first ? `Layout’s first page (${first})` : 'Layout’s first page' }),
    pages.map((p) => h('option', { value: p, text: p })),
    want && !M.pages[want] && h('option', { value: want, text: `${want} (not in this layout)` }));
  sel.value = want || '';
  sel.addEventListener('change', () => devCommit(() => setDevCfg(info.serial, 'start_page', sel.value || undefined)));
  return sel;
}

function deckBrightness(info) {
  const own = devCfg(info.serial).brightness;
  const fallback = typeof S.cfg.brightness === 'number' ? S.cfg.brightness : (info.brightness ?? 100);
  const range = h('input', { type: 'range', min: 0, max: 100, step: 5, value: own ?? fallback });
  const val = h('span', { class: 'dk-bright-val' });
  const reset = h('button', { class: 'small ghost', type: 'button', text: 'Use default' });
  const show = () => {
    const set = devCfg(info.serial).brightness !== undefined;
    val.textContent = `${range.value} %${set ? '' : ' (default)'}`;
    reset.hidden = !set;
  };
  // the deck follows the slider right away, so you can judge it; Save keeps it
  const preview = debounce((v) => api('/api/live/brightness', { deck: info.serial, level: v }).catch(() => {}), 150);
  range.addEventListener('input', () => {
    devEdit(() => setDevCfg(info.serial, 'brightness', Number(range.value)));
    show();
    preview(Number(range.value));
  });
  reset.addEventListener('click', () => {
    devCommit(() => setDevCfg(info.serial, 'brightness', undefined));
    range.value = fallback;
    show();
    preview(fallback);
  });
  show();
  return h('div', { class: 'dk-bright' }, range, val, reset);
}

function identifyButton(info) {
  const btn = h('button', { class: 'small', type: 'button', title: 'Flash this deck’s keys so you can tell which one it is' }, icon('bolt'), 'Identify');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.replaceChildren(icon('bolt'), 'Flashing…');
    const until = sleep(1500);
    try { await api('/api/devices/identify', { serial: info.serial }); await until; } catch (e) { toast(`Couldn’t flash ${deckName(info)}: ${e.message}`, { bad: true }); }
    btn.disabled = false;
    btn.replaceChildren(icon('bolt'), 'Identify');
  });
  return btn;
}

function deckRow(info) {
  const title = h('h3', { class: 'dk-row-title', text: deckName(info) });
  const name = h('input', { type: 'text', value: devCfg(info.serial).name || '', placeholder: info.model || 'Stream Deck', maxlength: 40, autocomplete: 'off' });
  name.addEventListener('input', () => {
    devEdit(() => setDevCfg(info.serial, 'name', name.value.trim() || undefined));
    title.textContent = deckName(info);
    renderToolbar();
  });
  const help = layoutFitHelp(info);
  return h('li', { class: 'dk-row' },
    h('div', { class: 'dk-row-head' },
      h('span', { class: 'dk-row-icon', 'aria-hidden': 'true' }, icon('deck')),
      h('div', { class: 'dk-row-titles' }, title,
        h('p', { class: 'dk-row-sub' }, h('span', { text: info.model || 'Stream Deck' }), ' · ', h('span', { class: 'mono', text: info.serial }))),
      identifyButton(info)),
    h('div', { class: 'grid cols-2 fields' },
      field('Name', name, 'Shown in the panel and in the status bar.'),
      field('Layout', deckLayoutSelect(info), help, help ? 'warn' : null),
      field('Start page', deckStartSelect(info)),
      field('Brightness', deckBrightness(info))));
}

function offlineRow(serial) {
  const c = devCfg(serial);
  return h('li', { class: 'dk-row offline' },
    h('div', { class: 'dk-row-head' },
      h('span', { class: 'dk-row-icon', 'aria-hidden': 'true' }, icon('deck')),
      h('div', { class: 'dk-row-titles' }, h('h3', { class: 'dk-row-title', text: c.name || 'Stream Deck' }),
        h('p', { class: 'dk-row-sub' }, h('span', { class: 'badge', text: 'Not connected' }), ' ', h('span', { class: 'mono', text: serial }))),
      h('button', { class: 'small ghost', type: 'button', title: 'Remove this deck’s name, layout and brightness from the config',
        onclick: () => { devCommit(() => { delete S.cfg.devices[serial]; if (!Object.keys(S.cfg.devices).length) delete S.cfg.devices; }); redrawDecks(); } },
      icon('trash'), 'Forget')));
}

function decksCard() {
  const known = new Set(S.devices.map((d) => d.serial));
  const offline = Object.keys(isObj(S.cfg.devices) ? S.cfg.devices : {}).filter((s) => !known.has(s));
  const n = S.devices.length;
  const sub = n ? `${n} connected. Each deck has its own name, layout and brightness.` : 'No deck connected right now.';
  const body = n || offline.length
    ? h('ul', { class: 'dk-rows' }, S.devices.map(deckRow), offline.map(offlineRow))
    : empty('No Stream Deck connected', 'Plug one in and it shows up here.', 'deck');
  const card = sCard('deck', 'Decks', sub, null, body);
  card.id = 'decks-card';
  return card;
}

function renderSettings() {
  const v = $('#view-settings');
  const st = S.status || {};
  const obs = st.obs || {}, tw = st.twitch || {};
  let twShown = false;   // one "Open" button, on the first Twitch item that needs attention
  const toTwitch = (ok) => {
    if (ok || twShown) return null;
    twShown = true;
    return h('button', { class: 'small', onclick: () => go('twitch') }, 'Open the Twitch page', icon('arrowRight'));
  };

  // ----- checklist
  const hasAudio = !!(obs.audio && obs.audio.length) && obs.music !== false;   // music: the radio's source
  const addAudioButton = () => h('button', { class: 'small primary', onclick: async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; btn.classList.add('busy');
    try {
      const r = await api('/api/obs/build', { audio: true });
      toast(r.ok ? 'Microphone and desktop audio added to every scene' : (r.output || 'Couldn’t add audio'), { ok: r.ok, bad: !r.ok, ms: 6000 });
    } catch (err) { toast(err.message, { bad: true }); }
    btn.disabled = false; btn.classList.remove('busy');
    refreshStatus();
  } }, icon('mic'), 'Add microphone & desktop audio');
  const checks = [
    [!!st.deck, S.devices.length > 1 ? `${S.devices.length} Stream Decks connected: ${S.devices.map(deckName).join(', ')}`
      : st.deck ? `Stream Deck connected: ${st.deck.model}` : 'Stream Deck not found',
      st.deck ? null : 'Plug it in. If it stays dark, see README → Device permissions.'],
    [!!obs.ok, obs.ok ? 'OBS is reachable' : 'OBS isn’t reachable',
      obs.ok ? null : 'Start OBS, then Tools → WebSocket Server Settings → check “Enable WebSocket server”. That’s OBS’s built-in remote control, which this panel uses to start your stream and switch scenes.'],
    [!!obs.scenes_built, obs.scenes_built ? `OBS scenes are set up (“${obs.collection_name}”)` : 'OBS scenes not created yet',
      obs.scenes_built ? null : 'Use “Create OBS scenes” (OBS must be running).'],
    ...(obs.ok ? [[hasAudio, hasAudio ? `OBS has your audio (${obs.audio.join(', ')})` : (obs.audio && obs.audio.length ? 'OBS has no music source for the radio' : 'OBS has no microphone or desktop audio'),
      hasAudio ? null : (obs.audio && obs.audio.length ? 'The Radio keys need it. This adds it to every scene (it stays silent until you turn the radio on).' : 'Your stream would be silent and the mute keys can’t work. This adds your microphone, desktop sound and the radio’s music source to every scene.'),
      hasAudio ? null : addAudioButton()]] : []),
    [!!tw.channel, tw.channel ? 'Twitch channel set' : 'Twitch channel not set', tw.channel ? null : 'Set it on the Twitch page.', toTwitch(!!tw.channel)],
    [!!tw.client_id, tw.client_id ? 'Twitch app (client ID) set' : 'Twitch app not set up', tw.client_id ? null : 'The Twitch page walks you through it.', toTwitch(!!tw.client_id)],
    [!!tw.linked, tw.linked ? 'Twitch account linked' : 'Twitch account not linked', tw.linked ? null : 'Link it on the Twitch page.', toTwitch(!!tw.linked)],
    tw.chat || tw.channel
      ? [!!tw.chat, tw.chat ? 'Reading Twitch chat' : 'Not reading chat', tw.chat ? null : 'Check that the channel name is right and the daemon is running.']
      : ['wait', 'Reading Twitch chat', 'Waiting on the Twitch channel'],
  ];
  const done = checks.filter((c) => c[0] === true).length;
  const list = h('ul', { class: 'status-list' }, checks.map((c) => checkItem(...c)));
  const checklist = sCard('check', 'Setup checklist', done === checks.length ? 'Everything is ready.' : `${done} of ${checks.length} ready`,
    h('button', { class: 'small ghost', title: 'Check again', onclick: refreshStatus }, icon('refresh'), 'Re-check'),
    h('div', { class: 'checklist-progress', 'aria-hidden': 'true' }, h('div', { class: 'progress' }, h('span', { style: { width: `${Math.round(100 * done / checks.length)}%` } }))),
    list);

  // ----- Twitch (lives on its own page)
  const twitchCard = sCard('twitch', 'Twitch', tw.linked && tw.user && tw.user.display_name ? `Linked as ${tw.user.display_name}` : null, null,
    h('p', { class: 'muted', text: 'Linking, title and chat live on the Twitch page.' }),
    h('div', { class: 'card-foot' }, h('button', { class: 'twitch', onclick: () => go('twitch') }, icon('twitch'), 'Open the Twitch page')));

  // ----- OBS scenes
  const out = h('pre', { class: 'output', hidden: true });
  const obsCard = sCard('layers', 'OBS scenes', 'Creates a new OBS profile and scene collection (Starting Soon, Screen, Game, Camera, BRB, Ending). Your existing ones are not changed.', null,
    h('div', { class: 'grid cols-2 fields' },
      sField('obs_setup.profile', 'Profile name', { placeholder: 'Stream Deck' }),
      sField('obs_setup.collection', 'Scene collection name', { placeholder: 'Stream Deck' }),
      sField('obs_setup.encoder', 'Video encoder', { type: 'choice', options: [
        { value: 'x264', label: 'x264 (CPU, works everywhere)' }, { value: 'nvenc', label: 'NVIDIA (nvenc)' },
        { value: 'qsv', label: 'Intel (qsv)' }, { value: 'amd', label: 'AMD (amd)' }] }),
      sField('obs_setup.bitrate', 'Bitrate (kbps)', { type: 'number', min: 500 }, '6000 is Twitch’s maximum for 1080p60.'),
      sField('obs_setup.fps', 'Frames per second', { type: 'choice', options: [{ value: 60, label: '60' }, { value: 30, label: '30' }] }),
      sField('obs_setup.recordings_dir', 'Recordings folder', { placeholder: '~/Videos/Stream' }),
      sField('obs_setup.webcam', 'Webcam device', { placeholder: '/dev/video0' }),
      sField('obs_setup.screen', 'Monitor to capture (0 = first)', { type: 'number', min: 0 })),
    h('div', { class: 'card-foot' }, h('button', { class: 'primary', disabled: !obs.ok, onclick: async (e) => {
      const btn = e.currentTarget;
      if (S.dirty) await save();
      btn.disabled = true;
      out.hidden = false;
      out.textContent = 'Working… (OBS will switch profile and scene collection)';
      try {
        const r = await api('/api/obs/build', {});
        out.textContent = r.output || (r.ok ? 'Done.' : 'Failed.');
      } catch (err) { out.textContent = err.message; }
      btn.disabled = false;
      refreshStatus();
    } }, icon('layers'), 'Create OBS scenes'), !obs.ok && h('span', { class: 'note warn', text: 'Start OBS first (see the checklist).' })), out);

  // ----- BRB
  const polls = (cfgGet('brb.polls') || []);
  const pollBox = h('div');
  const drawPolls = () => {
    pollBox.replaceChildren(...polls.map((p, i) => {
      const q = h('input', { type: 'text', value: p.question || '', placeholder: 'Question' });
      const o = h('textarea', { rows: '3', placeholder: 'One answer per line (2 to 9)' });
      o.value = (p.options || []).join('\n');
      q.addEventListener('input', () => { p.question = q.value; cfgSet('brb.polls', polls); edited(); });
      o.addEventListener('input', () => { p.options = o.value.split('\n').map((s) => s.trim()).filter(Boolean); cfgSet('brb.polls', polls); edited(); });
      return h('div', { class: 'poll' }, field(`Poll ${i + 1} question`, q), field('Answers', o),
        h('div', { class: 'row' }, h('button', { class: 'small danger', onclick: () => { pushUndo(); polls.splice(i, 1); cfgSet('brb.polls', polls.length ? polls : undefined); markDirty(); drawPolls(); } }, icon('trash'), 'Remove poll')));
    }), h('button', { class: 'small', onclick: () => { pushUndo(); polls.push({ question: '', options: [] }); cfgSet('brb.polls', polls); markDirty(); drawPolls(); } }, icon('plus'), 'Add poll'));
  };
  drawPolls();
  const brbCard = sCard('clock', 'BRB screen', 'What viewers see while you’re away.', null,
    h('div', { class: 'grid cols-2 fields' },
      sField('brb.media_dir', 'Media folder', { placeholder: '~/Stream/brb' }, 'Put videos in its “videos” folder; they play while you’re away.'),
      sField('brb.chat_lines', 'Chat lines shown', { type: 'number', min: 1, max: 20 }),
      sField('brb.scene', 'BRB scene in OBS', { type: 'source', source: 'obs_scenes' }),
      sField('radio.dir', 'Radio folder', { placeholder: '~/Stream/radio' }, 'Each folder inside is a station.')),
    h('h3', { class: 'subhead', text: 'Chat polls' }), pollBox);

  // ----- Deck
  const deckCard = sCard('deck', 'Deck', S.devices.length > 1 ? 'Defaults for every deck. Each deck can change them under Decks.'
    : st.deck ? `This deck: ${st.deck.model}` : 'No deck connected', null,
    h('div', { class: 'grid cols-2 fields' },
      sField('brightness', 'Brightness (%)', { type: 'number', min: 0, max: 100 }),
      sField('device.serial', 'Only use the deck with serial', { placeholder: 'any' }),
      sField('editor.port', 'Editor port', { type: 'number', min: 1024, max: 65535 }, 'Changing it moves this page to the new port.')));

  // ----- Home Assistant
  const haToken = h('input', { type: 'password', placeholder: S.secrets.ha_token ? '•••••••• (saved; type to replace)' : 'long-lived access token', autocomplete: 'off' });
  const haCard = sCard('home', 'Home Assistant', 'Optional: control lights and scenes from keys.', null,
    h('div', { class: 'grid cols-2 fields' },
      sField('home_assistant.url', 'Address', { placeholder: 'http://homeassistant.local:8123' }),
      field('Access token', haToken, 'Profile → Security → Long-lived access tokens. Stored in its own private file, not in the config.')),
    h('div', { class: 'card-foot' }, h('button', { class: 'small', onclick: () => saveSecret('ha_token', haToken.value) }, icon('lock'), 'Save token')));

  // ----- OBS remote
  const obsPw = h('input', { type: 'password', placeholder: S.secrets.obs_password ? '•••••••• (saved)' : 'only for OBS on another computer', autocomplete: 'off' });
  const remoteCard = sCard('monitor', 'OBS on another computer', 'Optional. Leave empty when OBS runs on this computer: the connection details are read from OBS automatically.', null,
    h('div', { class: 'grid cols-2 fields' },
      sField('obs.host', 'OBS computer address', { placeholder: 'localhost' }),
      sField('obs.port', 'Port', { type: 'number', placeholder: '4455' }),
      field('WebSocket password', obsPw)),
    h('div', { class: 'card-foot' }, h('button', { class: 'small', onclick: () => saveSecret('obs_password', obsPw.value) }, icon('lock'), 'Save password')));

  // ----- panel access
  const accessCard = sCard('lock', 'Panel access', 'Every open copy of this panel, on any device, holds its own sign-in.', null,
    h('p', { class: 'muted', text: 'Use this if you left a panel open somewhere you don’t trust, or shared a link by mistake.' }),
    h('div', { class: 'card-foot' }, asyncButton({ class: 'small danger' }, signOutAll, icon('lock'), 'Sign out all panels')));

  const wrap = h('div', { class: 'settings' },
    h('div', { class: 'page-head' }, h('div', { class: 'page-titles' }, h('h1', { class: 'page-title', text: 'Settings' }),
      h('p', { class: 'page-sub', text: 'Connections, OBS scenes and the BRB screen. Changes are saved with Save.' }))),
    h('div', { class: 'settings-aside' }, checklist, existingSetupCard(), twitchCard, accessCard),
    h('div', { class: 'settings-main' }, decksCard(), obsCard, brbCard, deckCard, haCard, remoteCard),
    historyCard(), externalBackupsCard());
  v.replaceChildren(wrap);
}

async function signOutAll() {
  const ok = await confirmDialog('Sign out all panels?',
    'Every open panel, including this one, will need to be opened again from your Stream Deck key or main.py --open-editor.',
    { ok: 'Sign out all panels', danger: true });
  if (!ok) return;
  await api('/api/session/revoke', {});
  document.dispatchEvent(new CustomEvent('deck:signedout'));   // main.js shows the "open the panel again" message
}

async function saveSecret(name, value) {
  try {
    const r = await api('/api/secret', { name, value });
    S.secrets[name] = !!r.path;
    if (r.path) { pushUndo(); cfgSet(`${r.section}.${r.field}`, r.path); markDirty(); }
    toast(r.path ? 'Saved privately. Now press Save to use it.' : 'Removed.');
    renderSettings();
  } catch (e) { toast(e.message, { bad: true }); }
}

/* ------------------------------------------------------------------ status, save, load */

async function refreshStatus() {
  await shellRefreshStatus();
  S.status = shellStatus;
  if (Array.isArray(S.status.decks)) { setDevices(S.status.decks); if (S.loaded) renderToolbar(); }
  if (S.view === 'settings' && !document.activeElement.closest('#view-settings')) renderSettings();
}

async function save() {
  if (S.comments) {
    const n = S.comments;
    if (!confirm(`Your config.yaml has ${n} comment${n === 1 ? '' : 's'} (# notes). The editor can't keep comments, so saving removes them.\n\nA copy of the current file is kept in the backups folder first.\n\nOK: save anyway\nCancel: don't save`)) return;
  }
  try {
    const r = await api('/api/save', { config: buildConfig(), hash: S.hash });
    S.hash = r.hash;
    S.comments = 0;
    markDirty(false);
    if (r.problems && r.problems.length) {
      toast(h('div', {}, 'Saved, but check these:', h('ul', {}, r.problems.slice(0, 8).map((p) => h('li', { text: p })))), { ms: 9000 });
    } else toast('Saved. The deck updates in a moment.', { ok: true });
    S.options = {};
  } catch (e) {
    if (e.status === 409) {
      if (confirm('config.yaml was changed outside the editor since you opened it.\n\nOK: reload it (your unsaved edits here are lost)\nCancel: keep editing here')) await load();
    } else toast('Not saved: ' + e.message, { bad: true, ms: 8000 });
  }
}

function renderAll() {
  renderToolbar();
  renderDeck();
  renderLibrary();
  renderInspector();
  if (S.view === 'settings') renderSettings();
}

async function load() {
  const st = await api('/api/state');
  S.cfg = st.config || {};
  S.hash = st.hash;
  S.comments = st.comments || 0;
  S.catalog = st.catalog;
  S.device = st.device;
  setDevices(st.devices);
  S.models = st.models;
  S.secrets = st.secrets || {};
  S.undo = []; S.redo = [];
  loadModel();
  if (!Object.keys(S.model).length) {
    const g = S.device ? `${S.device.cols}x${S.device.rows}` : '8x4';
    S.model[g] = { pages: { main: normPage([], gridOf(g), extrasOf(g)) } };
  }
  if (S.devices.length > 1 && S.mode === 'decks') {
    if (!S.devices.some((d) => d.serial === S.active)) S.active = S.devices[0].serial;
    if (!S.model[S.layout]) S.layout = null;   // activateDeck() below via syncDecksMode()
  }
  const primary = S.devices[0] || S.device;   // open on the layout the primary deck shows
  const active = primary ? effectiveLayout(primary) : null;
  if (!S.model[S.layout]) S.layout = S.model[active] ? active : Object.keys(S.model)[0];
  if (!curPages()[S.page]) S.page = (S.device && curPages()[st.page]) ? st.page : startPage();
  S.sel = null;
  markDirty(false);
  renderAll();
}

/* ------------------------------------------------------------------ wiring */

function wire() {
  $('#save').addEventListener('click', save);
  $('#undo').addEventListener('click', undo);
  $('#redo').addEventListener('click', redo);
  $('#layout-select').addEventListener('change', (e) => {
    S.sel = null;
    if (e.target.value === DECKS) { S.mode = 'decks'; activateDeck(activeInfo()); renderAll(); return; }
    S.mode = 'layout';
    S.layout = e.target.value; S.page = startPage(); renderAll();
  });
  $('#layout-menu').addEventListener('click', () => layoutMenu());
  $('#page-add').addEventListener('click', addPage);
  $('#page-menu').addEventListener('click', pageMenu);
  $('#lib-new').addEventListener('click', () => {
    let name = null;
    commit(() => { name = newSavedKey(); if (name) S.sel = { kind: 'lib', name }; });
  });
  $('#lib-filter').addEventListener('input', renderLibrary);
  window.addEventListener('resize', debounce(renderDeck, 150));
  window.addEventListener('beforeunload', (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ''; } });
  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    const mod = e.ctrlKey || e.metaKey;
    if (modalOpen()) return;
    if (mod && e.key.toLowerCase() === 's') { e.preventDefault(); if (S.dirty) save(); return; }
    if (typing) return;
    if (mod && e.key.toLowerCase() === 'z') { e.preventDefault(); if (e.shiftKey) redo(); else undo(); }
    else if (mod && e.key.toLowerCase() === 'y') { e.preventDefault(); redo(); }
    else if ((e.key === 'Delete' || e.key === 'Backspace') && S.sel && S.sel.kind !== 'lib' && getSel()) {
      e.preventDefault();
      commit(() => setSel(null));
    }
  });
}

let wired = false;
async function ensureLoaded() {
  if (!wired) { wired = true; wire(); on('config', onConfigChanged); on('decks', onDecks); }
  if (!S.loaded) { await load(); S.loaded = true; }
}

/** A deck was plugged in or unplugged (or the config reloaded). */
function onDecks(data) {
  if (!data || !Array.isArray(data.decks)) return;
  const before = S.devices.length;
  setDevices(data.decks);
  if (!S.loaded) return;
  if (S.devices.length > 1 && before < 2) { S.mode = 'decks'; S.sel = null; }   // a second deck: show both
  if (S.view === 'keys') renderDeck();
  renderToolbar();
  const card = $('#decks-card');
  if (S.view === 'settings' && card && !card.contains(document.activeElement)) redrawDecks();
}

function onConfigChanged(data) {
  if (!S.loaded || !data || data.hash === S.hash || S.dirty) return;
  load().catch((e) => toast('Couldn’t reload the config: ' + e.message, { bad: true }));
}

/** Open the Deck editor on one key (the setup assistant's "Show in the Deck editor"). row/col
    are as the importer counted them; a flat page is row 0. */
export async function showKey({ layout, page, row, col }) {
  await ensureLoaded();
  if (!S.dirty) await load();
  const L = layout === null || layout === undefined ? PAGES_ALL : layout;
  if (!S.model[L] || !S.model[L].pages[page]) { toast('That key isn’t in the config any more.', { bad: true }); return; }
  S.mode = 'layout';
  S.layout = L;
  S.page = page;
  S.sel = { kind: 'cell', index: (Number(row) || 0) * gridOf(L).cols + (Number(col) || 0) };
  renderAll();
}

/** Deck view (key editor). */
export async function mount() { await ensureLoaded(); }
export function show() {
  S.view = 'keys';
  $('#editor-actions').hidden = false;
  if (S.loaded) renderDeck();
}
export function hide() { $('#editor-actions').hidden = true; }

/** Settings view shares the editor's state (the config being edited). */
export const settings = {
  async mount() { await ensureLoaded(); S.status = shellStatus; renderSettings(); },
  show() { S.view = 'settings'; $('#editor-actions').hidden = false; S.status = shellStatus; if (S.loaded) renderSettings(); refreshStatus(); },
  hide() { $('#editor-actions').hidden = true; },
};
