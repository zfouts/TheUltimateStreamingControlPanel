/* Plugin page: one view for every installed plugin (#view=plugin&name=<name>). Plugins ship no
   markup; they describe their page (actions + settings fields) and this module draws it with the
   panel's own components. Live status comes from the "status" event (snapshot[key].card). */
import { api, on } from './api.js';
import { h, icon, toast, confirmDialog, asyncButton, switchInput, debounce } from './ui.js';
import { go, snapshot as shellSnapshot, pluginParam } from './main.js';

const NAME_RE = /^[a-z][a-z0-9_]{1,31}$/;
const PLUGIN_ICONS = new Set(['live', 'deck', 'twitch', 'obs', 'settings', 'sun', 'moon', 'x', 'check', 'warn', 'info', 'play', 'stop', 'pause',
  'mic', 'volume', 'send', 'refresh', 'camera', 'link', 'clip', 'flag', 'dots', 'undo', 'redo', 'plus', 'search', 'trash', 'copy', 'edit', 'grid',
  'layers', 'clock', 'home', 'monitor', 'external', 'lock', 'bolt', 'eye', 'text', 'code', 'dial', 'pointer', 'bot', 'sparkle', 'key']);
const TONES = { ok: 'ok', warn: 'warn', error: 'bad', idle: '' };
const TONE_LABEL = { ok: 'OK', warn: 'Warning', error: 'Error', idle: 'Idle' };

/** A plugin-supplied icon name the panel knows, else "bolt". */
export const pluginIcon = (n) => (PLUGIN_ICONS.has(n) ? n : 'bolt');

const P = {
  visible: false, name: '', req: 0,
  data: null, loadErr: null, loading: false,
  draft: {},          // field key -> edited value (only fields that differ from the server's)
  secretDraft: {},    // secret key -> new text (replace mode; '' while the box is open but empty)
  saving: false, saveErr: null, conflict: false,
};
let host = null;
let statusBox = null, actionsBox = null, formBox = null;
let lastCardSig = null;
let fallbackSnap = null;   // /api/live/snapshot, used until the event stream has delivered one

/* ------------------------------------------------------------------ view lifecycle */

export function mount(root) {
  host = root;
  host.replaceChildren(h('div', { class: 'page plug-page' }));
  on('status', () => { if (P.visible) renderStatus(); });
}

export function show() {
  P.visible = true;
  const name = pluginParam();
  if (name !== P.name || !P.data) {
    P.name = name;
    P.data = null;
    P.loadErr = null;
    P.draft = {}; P.secretDraft = {}; P.saveErr = null; P.conflict = false;
    lastCardSig = null;
    renderAll();
    if (!NAME_RE.test(name)) { P.loadErr = { status: 404, message: 'That plugin isn’t installed.' }; renderAll(); return; }
  }
  load();
  if (!shellSnapshot || !Object.keys(shellSnapshot).length) {
    api('/api/live/snapshot').then((d) => { fallbackSnap = d; if (P.visible) renderStatus(); }).catch(() => {});
  }
}

export function hide() { P.visible = false; }

async function load() {
  const name = P.name;
  if (!NAME_RE.test(name)) return;
  const req = ++P.req;
  P.loading = !P.data;
  if (P.loading) renderAll();
  try {
    const d = await api('/api/plugins/' + name);
    if (req !== P.req || name !== P.name) return;
    P.loadErr = null;
    adopt(d, { reset: !P.data || !isDirty() });
  } catch (e) {
    if (req !== P.req || name !== P.name) return;
    if (!P.data) P.loadErr = e;
    else { toast('Couldn’t refresh: ' + e.message, { bad: true }); return; }
  }
  P.loading = false;
  renderAll();
}

/** Take a server body (GET or save response) as the truth; `reset` drops the unsaved edits. */
function adopt(d, { reset }) {
  P.data = d;
  if (reset) { P.draft = {}; P.secretDraft = {}; }
  document.title = `${d.title || d.name} · Ultimate Streaming`;
}

/* ------------------------------------------------------------------ helpers */

const fields = () => (P.data && P.data.fields) || [];
const valueOf = (f) => (f.key in P.draft ? P.draft[f.key] : (P.data.values || {})[f.key]);
const fid = (k) => 'plug-f-' + k.replace(/\./g, '-');

function isDirty() {
  if (!P.data) return false;
  return Object.keys(P.draft).length > 0 || Object.values(P.secretDraft).some((v) => typeof v === 'string' && v.trim() !== '');
}

function setDraft(f, v) {
  const orig = (P.data.values || {})[f.key];
  if (v === orig || (f.type === 'number' && v !== '' && Number(v) === orig)) delete P.draft[f.key];
  else P.draft[f.key] = v;
  P.saveErr = null; P.conflict = false;
  renderSaveBar();
}

/** Client-side checks that mirror the server's, so common typos don't need a round trip. */
function problems() {
  const out = [];
  for (const f of fields()) {
    if (f.type !== 'number' || !(f.key in P.draft)) continue;
    const raw = P.draft[f.key];
    const n = typeof raw === 'number' ? raw : Number(String(raw).trim());
    if (String(raw).trim() === '' || !Number.isFinite(n)) out.push(`${f.label}: enter a number`);
    else if (f.whole !== false && !Number.isInteger(n)) out.push(`${f.label}: use a whole number`);
    else if ((typeof f.min === 'number' && n < f.min) || (typeof f.max === 'number' && n > f.max)) {
      out.push(`${f.label}: must be ${typeof f.min === 'number' ? 'at least ' + f.min : ''}${typeof f.min === 'number' && typeof f.max === 'number' ? ' and ' : ''}${typeof f.max === 'number' ? 'at most ' + f.max : ''}`);
    }
  }
  return out;
}

/* ------------------------------------------------------------------ render: page */

function renderAll() {
  if (!host) return;
  const page = host.firstChild;
  if (P.loading && !P.data) {
    page.replaceChildren(h('div', { class: 'card', 'aria-busy': 'true' }, h('span', { class: 'skeleton line' }), h('span', { class: 'skeleton block mt-3' })));
    return;
  }
  if (P.loadErr) {
    const missing = P.loadErr.status === 404;
    page.replaceChildren(h('div', { class: 'card' },
      h('div', { class: 'empty', role: 'alert' }, h('div', { class: 'empty-icon' }, icon('warn')),
        h('div', { class: 'what', text: missing ? 'That plugin isn’t installed' : 'Couldn’t load this plugin' }),
        h('div', { class: 'how', text: missing ? 'It may have been removed. Pick another from the sidebar.' : (P.loadErr.message || 'Try again in a moment.') }),
        h('div', { class: 'row mt-3' },
          !missing && h('button', { onclick: () => { P.loadErr = null; load(); } }, icon('refresh'), 'Try again'),
          h('button', { class: 'ghost', onclick: () => go('live') }, 'Back to Live')))));
    return;
  }
  const d = P.data;
  if (!d) return;
  statusBox = h('div', { class: 'plug-status' });
  actionsBox = h('div', { class: 'plug-actions-wrap' });
  formBox = h('div', { class: 'plug-form' });
  page.replaceChildren(header(d));
  if (!d.loaded) {
    page.append(notLoaded(d));
    return;
  }
  page.append(statusBox, actionsBox, formBox);
  renderStatus();
  renderActions();
  renderForm();
}

function header(d) {
  const sub = d.intro || d.description || '';
  return h('div', { class: 'page-head' },
    h('div', { class: 'plug-title' },
      h('div', { class: 'card-icon' }, icon(pluginIcon(d.icon))),
      h('div', { class: 'page-titles' },
        h('h1', { class: 'page-title' }, d.title || d.name,
          d.version && h('span', { class: 'badge plug-version', title: 'Plugin version', text: 'v' + String(d.version).replace(/^v/i, '') })),
        sub && h('p', { class: 'page-sub', text: sub }),
        d.intro && d.description && d.description !== d.intro && h('p', { class: 'hint', text: d.description }))),
    h('div', { class: 'page-actions' },
      !d.loaded && h('span', { class: 'badge bad', text: 'Not loaded' })));
}

function notLoaded(d) {
  return h('section', { class: 'card', 'aria-label': 'Why this plugin didn’t load' },
    h('div', { class: 'card-head' }, h('div', { class: 'card-icon live' }, icon('warn')),
      h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: 'This plugin didn’t load' }),
        h('p', { class: 'card-sub', text: 'It stays installed, but its page and controls are off until this is fixed.' }))),
    h('div', { class: 'banner bad', role: 'alert' }, h('span', { class: 'plug-error mono', text: d.error || 'No error was reported.' })),
    h('h3', { class: 'subhead', text: 'How to fix it' }),
    h('ol', { class: 'steps' },
      h('li', {}, 'Install what it needs: ', h('code', { text: 'python3 tools/plugin.py deps' })),
      h('li', {}, 'Still failing? Reinstall it: ', h('code', { text: 'python3 tools/plugin.py install ' + d.name })),
      h('li', { text: 'Then reload this page.' })),
    h('div', { class: 'card-foot' }, h('button', { onclick: () => load() }, icon('refresh'), 'Check again')));
}

/* ------------------------------------------------------------------ render: live status */

const liveSnap = () => (shellSnapshot && Object.keys(shellSnapshot).length ? shellSnapshot : fallbackSnap);

function cardOf(key) {
  const c = liveSnap() && liveSnap()[key] && typeof liveSnap()[key] === 'object' ? liveSnap()[key].card : null;
  if (!c || typeof c !== 'object' || typeof c.title !== 'string' || !(c.tone in TONES) || !Array.isArray(c.rows)) return null;
  const rows = c.rows.filter((r) => Array.isArray(r) && r.length === 2 && typeof r[0] === 'string' && typeof r[1] === 'string').slice(0, 12);
  return { tone: c.tone, rows, note: typeof c.note === 'string' ? c.note : '', progress: typeof c.progress === 'number' && Number.isFinite(c.progress) ? Math.max(0, Math.min(100, c.progress)) : null };
}

const refreshActionsSoon = debounce(() => refreshActions(), 600);

function renderStatus() {
  if (!statusBox || !P.data || !P.data.loaded) return;
  const key = P.data.key;
  const card = key ? cardOf(key) : null;
  const sig = JSON.stringify(card);
  if (lastCardSig !== null && sig !== lastCardSig) refreshActionsSoon();   // buttons' enabled state follows the printer/device state
  lastCardSig = sig;

  const waiting = !liveSnap() || Object.keys(liveSnap()).length === 0;
  let body;
  if (card) {
    const [headLabel, headValue] = card.rows[0] || ['', card.title];
    body = [
      h('div', { class: 'plug-headline' },
        h('div', { class: 'plug-headline-text' },
          headLabel && h('span', { class: 'stat-label', text: headLabel }),
          h('span', { class: 'plug-big', text: headValue })),
        h('span', { class: 'badge ' + TONES[card.tone], text: TONE_LABEL[card.tone] })),
      card.progress !== null && h('div', { class: 'plug-progress' },
        h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(Math.round(card.progress)), 'aria-label': 'Progress' },
          h('span', { style: { width: card.progress + '%' } })),
        h('span', { class: 'plug-pct', text: Math.round(card.progress) + '%' })),
      card.rows.length > 1 && h('div', { class: 'plug-tiles' }, card.rows.slice(1).map(([k, v]) =>
        h('div', { class: 'stat plug-stat' }, h('span', { class: 'stat-label', text: k }), h('span', { class: 'stat-value', title: v, text: v })))),
      card.note && h('div', { class: 'banner ' + (card.tone === 'error' ? 'bad' : card.tone === 'warn' ? 'warn' : ''), text: card.note }),
    ];
  } else if (waiting) {
    body = [h('div', { class: 'plug-waiting', 'aria-busy': 'true' }, h('span', { class: 'spin', 'aria-hidden': 'true' }), h('span', { text: 'Waiting for the first status…' }))];
  } else {
    body = [h('div', { class: 'plug-waiting' }, icon('info'), h('span', { text: fields().length ? 'No live status yet. Fill in the settings below and save to get started.' : 'This plugin isn’t reporting any live status.' }))];
  }
  statusBox.replaceChildren(h('section', { class: 'card plug-status-card', 'aria-label': 'Live status' },
    h('div', { class: 'card-head' }, h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: 'Status' }), h('p', { class: 'card-sub', text: 'Updates live' }))),
    ...body));
}

/* ------------------------------------------------------------------ render: actions */

function renderActions() {
  if (!actionsBox) return;
  const acts = (P.data && P.data.actions) || [];
  if (!acts.length) { actionsBox.replaceChildren(); return; }
  actionsBox.replaceChildren(h('section', { class: 'card', 'aria-label': 'Controls' },
    h('div', { class: 'card-head' }, h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: 'Controls' }))),
    h('div', { class: 'plug-actions' }, acts.map(actionButton))));
}

function actionButton(a) {
  const cls = a.tone === 'primary' ? 'primary' : a.tone === 'danger' ? 'danger' : '';
  const off = a.enabled === false;
  const b = asyncButton({ class: cls, title: a.hint || null, disabled: off, 'aria-disabled': off ? 'true' : null }, async () => {
    if (a.confirm && !(await confirmDialog(a.label, a.confirm, { ok: a.label, danger: a.tone === 'danger' }))) return;
    const name = P.name;
    try {
      const r = await api(`/api/plugins/${name}/action`, { id: a.id });
      toast(r.message || a.label + ': done', { ok: true });
    } finally { refreshActions(); }
  }, a.icon && icon(pluginIcon(a.icon)), a.label);
  if (off && a.hint) b.append(h('span', { class: 'sr-only', text: ` (${a.hint})` }));
  return b;
}

/** Re-read the page description so the buttons' enabled state / labels are current. */
async function refreshActions() {
  const name = P.name;
  if (!P.visible || !NAME_RE.test(name)) return;
  try {
    const d = await api('/api/plugins/' + name);
    if (name !== P.name || !P.data) return;
    P.data = { ...P.data, actions: d.actions || [] };
    renderActions();
  } catch (e) { /* the next status push retries */ }
}

/* ------------------------------------------------------------------ render: settings form */

function renderForm() {
  if (!formBox) return;
  const fs = fields();
  if (!fs.length) { formBox.replaceChildren(); return; }
  const groups = new Map();
  for (const f of fs) {
    const g = f.group || 'Settings';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(f);
  }
  P.els = {};
  const cards = [...groups].map(([g, list]) => h('section', { class: 'card', 'aria-label': g },
    h('div', { class: 'card-head' }, h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: g }))),
    h('div', { class: 'plug-fields' }, list.map(fieldEl))));
  P.els.bar = h('div', { class: 'plug-savebar' });
  formBox.replaceChildren(h('form', { class: 'plug-formel', novalidate: true, onsubmit: (e) => { e.preventDefault(); save(); } }, ...cards, P.els.bar));
  renderSaveBar();
}

function fieldEl(f) {
  const id = fid(f.key);
  const helpId = id + '-help';
  const help = f.help ? h('span', { class: 'help', id: helpId, text: f.help }) : null;
  const common = { id, 'aria-describedby': help ? helpId : null };
  const req = f.required && f.type !== 'bool' ? h('span', { class: 'plug-req', text: 'required' }) : null;
  const label = (forId = id) => h('label', { for: forId, class: 'label' }, f.label, req);

  if (f.type === 'bool') {
    const sw = switchInput(!!valueOf(f), (v) => setDraft(f, v), f.label);
    sw.querySelector('input').id = id;
    if (help) sw.querySelector('input').setAttribute('aria-describedby', helpId);
    return h('div', { class: 'field plug-bool' }, sw, help);
  }
  if (f.type === 'secret') return secretEl(f, id, label, help);

  if (f.type === 'discord_webhook') return webhookEl(f, id, label, help, common);

  let ctl;
  if (f.type === 'select') {
    const cur = valueOf(f);
    ctl = h('select', { ...common, onchange: (e) => setDraft(f, e.target.value) },
      (f.options || []).map(([v, l]) => h('option', { value: v, selected: v === cur, text: l })));
    ctl.value = cur;
  } else if (f.type === 'number') {
    const v = valueOf(f);
    ctl = h('input', { ...common, type: 'number', inputmode: f.whole === false ? 'decimal' : 'numeric', value: v ?? '', placeholder: f.placeholder || null,
      min: typeof f.min === 'number' ? f.min : null, max: typeof f.max === 'number' ? f.max : null, step: f.whole === false ? 'any' : '1',
      oninput: (e) => setDraft(f, e.target.value) });
  } else {
    ctl = h('input', { ...common, type: 'text', value: valueOf(f) ?? '', placeholder: f.placeholder || null, maxlength: f.max_length || null,
      autocomplete: 'off', spellcheck: 'false', oninput: (e) => setDraft(f, e.target.value) });
  }
  return h('div', { class: 'field' }, label(), ctl, help);
}

/** A Discord webhook id picked from the registry in Settings → Discord (the panel never sees URLs). */
function webhookEl(f, id, label, help, common) {
  const list = (P.data && P.data.discord_webhooks) || [];
  const cur = valueOf(f) || '';
  const ctl = h('select', { ...common, onchange: (e) => setDraft(f, e.target.value) },
    h('option', { value: '', text: 'None' }),
    list.map((w) => h('option', { value: w.id, text: w.set ? w.name : `${w.name} (no URL saved)` })),
    cur && !list.some((w) => w.id === cur) && h('option', { value: cur, text: `Missing webhook (${cur})` }));
  ctl.value = cur;
  return h('div', { class: 'field' }, label(), h('div', { class: 'plug-hook-row' }, ctl,
    h('a', { class: 'plug-manage', href: '#view=discord', onclick: (e) => { e.preventDefault(); go('discord'); } }, list.length ? 'Manage webhooks' : 'Add a webhook')), help);
}

function secretEl(f, id, label, help) {
  const s = ((P.data.secrets || {})[f.key]) || { set: false, source: null };
  const editing = f.key in P.secretDraft;
  const wrap = h('div', { class: 'field plug-secret' });
  const row = h('div', { class: 'plug-secret-row' });
  const rerender = () => { const n = secretEl(f, id, label, help); wrap.replaceWith(n); const i = n.querySelector('input'); if (i) i.focus(); renderSaveBar(); };

  if (s.set && s.source === 'env' && !editing) {
    row.append(h('span', { class: 'badge ok' }, icon('lock'), 'Set by environment'),
      h('span', { class: 'hint', text: f.env ? `The ${f.env} variable is in use here. Unset it to manage this value from the panel.` : 'Managed outside the panel.' }));
    wrap.append(h('span', { class: 'label' }, f.label, f.required ? h('span', { class: 'plug-req', text: 'required' }) : null), row);
    return wrap;
  }
  if (s.set && !editing) {
    row.append(h('span', { class: 'badge ok' }, icon('check'), 'Saved (hidden)'),
      h('button', { type: 'button', class: 'small', 'aria-label': `Replace ${f.label}`, onclick: () => { P.secretDraft[f.key] = ''; rerender(); } }, 'Replace'),
      h('button', { type: 'button', class: 'small ghost danger-text', 'aria-label': `Remove ${f.label}`, onclick: () => removeSecret(f) }, 'Remove'));
    wrap.append(h('span', { class: 'label' }, f.label, f.required ? h('span', { class: 'plug-req', text: 'required' }) : null), row, help);
    return wrap;
  }
  // not set, or replacing: a password box
  const input = h('input', { id, type: 'password', autocomplete: 'new-password', spellcheck: 'false', value: P.secretDraft[f.key] || '',
    placeholder: f.placeholder || (s.set ? 'New value' : 'Not set'), 'aria-describedby': help ? id + '-help' : null,
    oninput: (e) => { P.secretDraft[f.key] = e.target.value; P.saveErr = null; P.conflict = false; renderSaveBar(); } });
  row.append(input);
  if (!s.set) row.append(h('span', { class: 'badge', text: 'Not set' }));
  if (s.set) row.append(h('button', { type: 'button', class: 'small ghost', onclick: () => { delete P.secretDraft[f.key]; rerender(); } }, 'Cancel'));
  wrap.append(label(), row, help);
  return wrap;
}

async function removeSecret(f) {
  if (!(await confirmDialog(`Remove ${f.label}?`, 'The saved value is deleted from this computer. You will need to enter it again to use this plugin.', { ok: 'Remove', danger: true }))) return;
  try {
    const r = await api(`/api/plugins/${P.name}/settings`, { values: {}, secrets: { [f.key]: null }, hash: P.data.hash });
    adopt(r, { reset: false });
    delete P.secretDraft[f.key];
    renderStatus(); renderActions(); renderForm();
    toast(`${f.label} removed`, { ok: true });
  } catch (e) {
    if (e.status === 409) { P.conflict = true; P.saveErr = e.message; renderSaveBar(); } else toast('Not removed: ' + e.message, { bad: true, ms: 8000 });
  }
}

/* ------------------------------------------------------------------ save */

function renderSaveBar() {
  const bar = P.els && P.els.bar;
  if (!bar) return;
  const dirty = isDirty();
  const errs = [];
  if (P.saveErr) {
    const m = /^Fix these first:\s*(.*)$/s.exec(P.saveErr);
    if (m) m[1].split(/;\s*/).filter(Boolean).forEach((x) => errs.push(x));
    else errs.push(P.saveErr);
  }
  const msg = P.conflict
    ? h('div', { class: 'banner warn', role: 'alert' }, h('span', { text: P.saveErr || 'config.yaml changed since the panel loaded it.' }),
      h('div', { class: 'row' }, h('button', { type: 'button', class: 'small', onclick: () => reload() }, icon('refresh'), 'Reload and discard my edits')))
    : errs.length ? h('div', { class: 'banner bad', role: 'alert' },
      P.saveErr && /^Fix these first:/.test(P.saveErr) ? h('strong', { text: 'Fix these first' }) : null,
      errs.length > 1 || /^Fix these first:/.test(P.saveErr) ? h('ul', { class: 'plug-errs' }, errs.map((x) => h('li', { text: x }))) : h('span', { text: errs[0] }))
      : null;
  bar.replaceChildren(...(msg ? [msg] : []),
    h('div', { class: 'row plug-save-row' },
      h('button', { type: 'submit', class: 'primary' + (P.saving ? ' busy' : ''), disabled: !dirty || P.saving, text: P.saving ? 'Saving…' : 'Save changes' }),
      h('button', { type: 'button', class: 'ghost', disabled: !dirty || P.saving, text: 'Discard changes', onclick: () => { P.draft = {}; P.secretDraft = {}; P.saveErr = null; P.conflict = false; renderForm(); } }),
      h('span', { class: 'hint', text: dirty ? 'Unsaved changes' : 'All changes saved' })));
}

async function reload() {
  P.draft = {}; P.secretDraft = {}; P.saveErr = null; P.conflict = false;
  P.data = null;
  await load();
}

async function save() {
  if (P.saving || !isDirty()) return;
  const bad = problems();
  if (bad.length) { P.saveErr = 'Fix these first: ' + bad.join('; '); P.conflict = false; renderSaveBar(); return; }
  const values = {};
  for (const f of fields()) {
    if (!(f.key in P.draft)) continue;
    const v = P.draft[f.key];
    values[f.key] = f.type === 'number' ? Number(String(v).trim()) : v;
  }
  const secrets = {};
  for (const [k, v] of Object.entries(P.secretDraft)) if (typeof v === 'string' && v.trim() !== '') secrets[k] = v;
  const name = P.name;
  P.saving = true; P.saveErr = null; P.conflict = false;
  renderSaveBar();
  try {
    const r = await api(`/api/plugins/${name}/settings`, { values, secrets, hash: P.data.hash });
    if (name !== P.name) return;
    adopt(r, { reset: true });
    P.saving = false;
    renderStatus(); renderActions(); renderForm();
    toast(`${P.data.title || name} saved`, { ok: true });
    return;
  } catch (e) {
    if (e.status === 409) P.conflict = true;
    if (e.status === 409 || e.status === 400) P.saveErr = e.message;
    else toast('Not saved: ' + e.message, { bad: true, ms: 8000 });
  }
  P.saving = false;
  renderSaveBar();
  if (P.saveErr && P.els.bar) P.els.bar.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}
