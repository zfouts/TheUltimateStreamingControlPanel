/* Discord page (#view=discord, nested under Settings): the one place Discord webhooks live.
   Features (Announcements, plugins) pick a webhook by id. URLs are secret: the API only says
   {set, host}, and a new value is sent once on save. Live results come from the "status" event
   (snapshot.discord.webhooks). */
import { api, on } from './api.js';
import { h, icon, toast, confirmDialog, clone } from './ui.js';
import { go, snapshot as shellSnapshot, status as shellStatus } from './main.js';

const MAX_HOOKS = 20;
const ID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const HOSTS = ['discord.com', 'discordapp.com', 'ptb.discord.com', 'canary.discord.com'];
/** "Used by" labels start with the feature's name ("Announcements: …", "<plugin title>: …"):
    the open action for one we can link to, else null. Plugins come from /api/status, so any
    installed plugin's label links to its page without this file knowing about it. */
function usedLink(label) {
  if (/^Announcements\b/.test(label)) return () => go('announce');
  const plugins = Array.isArray(shellStatus && shellStatus.plugins) ? shellStatus.plugins : [];
  const p = plugins.find((x) => x && x.has_page && typeof x.title === 'string' && x.title
    && (label === x.title || label.startsWith(x.title + ':')));
  return p ? () => go('plugin', { name: p.name }) : null;
}

const D = {
  visible: false, req: 0, loading: false, loadErr: null,
  data: null,          // last server body
  draft: null,         // edited copy of data.config
  urlDraft: {},        // id -> string (new/replacement URL) | null (remove saved URL)
  saving: false, saveErr: null, conflict: false,
  pendingTest: {},     // id -> {t, since, prev, seen}: a test we queued, until the status event answers
};
let page = null, els = {}, tick = null;
let fallbackSnap = null;

/* ------------------------------------------------------------------ lifecycle */

export function mount(root) {
  page = h('div', { class: 'page dc-page' });
  root.replaceChildren(page);
  on('status', () => { if (D.visible) refreshLive(); });
}

export function show() {
  D.visible = true;
  document.title = 'Discord · Ultimate Streaming';
  load();
  if (!shellSnapshot || !Object.keys(shellSnapshot).length) {
    api('/api/live/snapshot').then((d) => { fallbackSnap = d; if (D.visible) refreshLive(); }).catch(() => {});
  }
  clearInterval(tick);
  tick = setInterval(() => { if (D.visible) refreshLive(); }, 30000);
}

export function hide() { D.visible = false; clearInterval(tick); }

async function load() {
  const req = ++D.req;
  D.loading = !D.data;
  if (D.loading) renderAll();
  try {
    const d = await api('/api/discord');
    if (req !== D.req) return;
    D.loadErr = null;
    adopt(d, !D.data || !isDirty());
  } catch (e) {
    if (req !== D.req) return;
    if (!D.data) D.loadErr = e;
    else { toast('Couldn’t refresh: ' + e.message, { bad: true }); return; }
  }
  D.loading = false;
  renderAll();
}

function adopt(d, reset) {
  D.data = d;
  if (reset) { D.draft = clone(d.config); D.urlDraft = {}; }
}

/* ------------------------------------------------------------------ helpers */

const hooks = () => (D.draft && D.draft.webhooks) || [];
const urlInfo = (id) => ((D.data && D.data.urls) || {})[id] || { set: false, host: null };
const usedBy = (id) => ((D.data && D.data.used_by) || {})[id] || [];
const isSaved = (id) => ((D.data && D.data.config.webhooks) || []).some((w) => w.id === id);

function liveStatus() {
  const s = (shellSnapshot && shellSnapshot.discord) || (fallbackSnap && fallbackSnap.discord);
  return (s && s.webhooks) || (D.data && D.data.status) || {};
}
function hookStatus(id) {
  const st = liveStatus()[id] || { state: 'never', at: null, detail: null };
  const p = D.pendingTest[id];
  if (p) {
    if (st.state === 'sending') p.seen = true;
    const answered = st.state !== 'sending' && (st.at !== p.since || st.state !== p.prev);
    if ((answered && (p.seen || st.at !== p.since)) || Date.now() - p.t > 30000) delete D.pendingTest[id];
    else return { state: 'sending', at: null, detail: null };
  }
  return st;
}

function ago(epoch) {
  if (!epoch) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (s < 45) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const hr = Math.round(m / 60);
  if (hr < 48) return `${hr} hr ago`;
  return `${Math.round(hr / 24)} days ago`;
}

function slug(name) {
  const base = (String(name).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'webhook').slice(0, 28);
  const taken = new Set(hooks().map((w) => w.id));
  let id = base, n = 2;
  while (taken.has(id) || !ID_RE.test(id)) id = `${base}-${n++}`;
  return id;
}

function isDirty() {
  if (!D.data || !D.draft) return false;
  return JSON.stringify(D.draft) !== JSON.stringify(D.data.config) || Object.keys(D.urlDraft).length > 0;
}
function hookDirty(w) {
  const orig = ((D.data && D.data.config.webhooks) || []).find((x) => x.id === w.id);
  return !orig || JSON.stringify(orig) !== JSON.stringify(w) || w.id in D.urlDraft;
}

function webhookOk(u) {
  try {
    const x = new URL(u.trim());
    return x.protocol === 'https:' && HOSTS.includes(x.hostname) && !x.port && !x.username && !x.hash && /^\/api\/webhooks\/\d+\/[\w-]+$/.test(x.pathname);
  } catch (e) { return false; }
}

function problems() {
  const out = [];
  const seen = new Set();
  hooks().forEach((w, i) => {
    const nm = (w.name || '').trim();
    const label = nm || `Webhook ${i + 1}`;
    if (!nm || w.name.length > 40) out.push(`${label}: name must be 1 to 40 characters`);
    else if (seen.has(nm.toLowerCase())) out.push(`${label}: another webhook already has this name`);
    seen.add(nm.toLowerCase());
    const u = D.urlDraft[w.id];
    const typed = typeof u === 'string' && u.trim();
    if (typed && !webhookOk(u)) out.push(`${label}: that doesn’t look like a Discord webhook URL`);
    if (!typed && u !== null && !urlInfo(w.id).set) out.push(`${label}: paste its webhook URL, or remove the webhook`);
  });
  return out;
}

function touch() { D.saveErr = null; D.conflict = false; renderSaveBar(); }

/* ------------------------------------------------------------------ render: page */

function header() {
  return h('div', { class: 'page-head' },
    h('div', { class: 'page-titles' },
      h('h1', { class: 'page-title', text: 'Discord' }),
      h('p', { class: 'page-sub', text: 'Discord webhooks your features post to. Each one is a channel; give it a name and pick it in Announcements or a plugin.' })));
}

function renderAll() {
  if (!page) return;
  els = { cards: {}, result: {}, testBtn: {} };
  if (D.loading && !D.data) {
    page.replaceChildren(header(), h('div', { class: 'card', 'aria-busy': 'true', 'aria-label': 'Loading' }, h('span', { class: 'skeleton line' }), h('span', { class: 'skeleton block mt-3' })));
    return;
  }
  if (D.loadErr) {
    page.replaceChildren(header(), h('div', { class: 'card' },
      h('div', { class: 'empty', role: 'alert' }, h('div', { class: 'empty-icon' }, icon('warn')),
        h('div', { class: 'what', text: 'Couldn’t load Discord webhooks' }),
        h('div', { class: 'how', text: D.loadErr.message || 'Try again in a moment.' }),
        h('div', { class: 'row mt-3' },
          h('button', { onclick: () => { D.loadErr = null; load(); } }, icon('refresh'), 'Try again'),
          h('button', { class: 'ghost', onclick: () => go('live') }, 'Back to Live')))));
    return;
  }
  els.list = h('div', { class: 'dc-list' });
  els.bar = h('div', { class: 'dc-savebar' });
  page.replaceChildren(header(),
    h('form', { class: 'dc-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); save(); } }, els.list, els.bar));
  renderList();
  renderSaveBar();
}

function refreshLive() {
  if (!els.list) return;
  for (const w of hooks()) updateResult(w.id);
}

function howTo() {
  return h('p', { class: 'hint dc-howto', text: 'In Discord: Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL' });
}

function renderList() {
  const list = hooks();
  els.cards = {};
  const full = list.length >= MAX_HOOKS;
  const addBtn = h('button', { type: 'button', class: list.length ? '' : 'primary', disabled: full, onclick: addHook,
    title: full ? `Up to ${MAX_HOOKS} webhooks` : null }, icon('plus'), 'Add webhook');
  if (!list.length) {
    els.list.replaceChildren(h('section', { class: 'card', 'aria-label': 'Webhooks' },
      h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, icon('send')),
        h('div', { class: 'what', text: 'No Discord webhooks yet' }),
        h('div', { class: 'how', text: 'A webhook lets this panel post to one Discord channel. Create one in Discord, then add its URL here.' }),
        howTo(),
        h('div', { class: 'row mt-3' }, addBtn))));
    return;
  }
  els.list.replaceChildren(...list.map(hookCard),
    howTo(),
    h('div', { class: 'dc-add row' }, addBtn,
      h('span', { class: 'hint', text: full ? `That’s the limit of ${MAX_HOOKS} webhooks.` : `${list.length} of ${MAX_HOOKS}` })));
}

function addHook() {
  if (hooks().length >= MAX_HOOKS) return;
  const name = hooks().length ? `Webhook ${hooks().length + 1}` : 'My channel';
  const w = { id: slug(name), name };
  D.draft.webhooks.push(w);
  D.urlDraft[w.id] = '';
  renderList(); touch();
  const card = els.cards[w.id];
  const inp = card && card.querySelector('input[type=text]');
  if (inp) { card.scrollIntoView({ block: 'center', behavior: 'smooth' }); inp.focus({ preventScroll: true }); inp.select(); }
}

async function removeHook(w) {
  const used = usedBy(w.id);
  const body = used.length
    ? `It is used by: ${used.join(', ')}. Saving is refused until you pick another webhook there first.`
    : 'Its saved URL is deleted from this computer when you save. You will need the URL again to add it back.';
  if (!(await confirmDialog(`Delete “${w.name || 'this webhook'}”?`, body, { ok: 'Delete webhook', danger: true }))) return;
  D.draft.webhooks = hooks().filter((x) => x.id !== w.id);
  delete D.urlDraft[w.id];
  renderList(); touch();
}

function hookCard(w) {
  const k = 'dc-' + w.id;
  const card = h('section', { class: 'card dc-hook', 'aria-label': w.name.trim() || 'New webhook' });
  els.cards[w.id] = card;

  const nameIn = h('input', { id: k + '-name', type: 'text', value: w.name, maxlength: '40', autocomplete: 'off',
    oninput: (e) => { w.name = e.target.value; card.setAttribute('aria-label', w.name.trim() || 'New webhook'); title.textContent = w.name.trim() || 'New webhook'; touch(); } });

  const title = h('h2', { class: 'card-title', text: w.name.trim() || 'New webhook' });
  const result = h('div', { class: 'dc-result', role: 'status' });
  els.result[w.id] = result;
  const testBtn = h('button', { type: 'button', class: 'small', onclick: () => runTest(w) }, icon('send'), 'Send test');
  els.testBtn[w.id] = testBtn;

  const used = usedBy(w.id);
  const usedRow = h('div', { class: 'field dc-used' },
    h('span', { class: 'label', text: 'Used by' }),
    used.length
      ? h('ul', { class: 'dc-chips', 'aria-label': 'Features using this webhook' }, used.map((u) => {
        const open = usedLink(u);
        return h('li', {}, open
          ? h('button', { type: 'button', class: 'small ghost dc-chip', title: 'Open this feature', onclick: open }, u)
          : h('span', { class: 'dc-chip plain', text: u }));
      }))
      : h('span', { class: 'hint', text: 'Nothing yet. Pick it in Announcements or a plugin page.' }));

  card.append(
    h('div', { class: 'card-head dc-hook-head' },
      h('div', { class: 'card-titles' }, title,
        !isSaved(w.id) && h('span', { class: 'badge warn', text: 'Not saved yet' })),
      h('button', { type: 'button', class: 'small ghost danger-text', 'aria-label': `Delete ${w.name || 'webhook'}`, onclick: () => removeHook(w) }, icon('trash'), 'Delete')),
    h('div', { class: 'field' }, h('label', { class: 'label', for: k + '-name', text: 'Name' }), nameIn,
      h('span', { class: 'help', text: 'Shown when you pick a webhook, so name it after the channel.' })),
    urlField(w, k),
    usedRow,
    h('div', { class: 'dc-foot' }, testBtn, result));
  updateResult(w.id);
  return card;
}

function urlField(w, k) {
  const wrap = h('div', { class: 'field dc-url' });
  const info = urlInfo(w.id);
  const pending = D.urlDraft[w.id];
  const paint = () => { const n = urlField(w, k); wrap.replaceWith(n); const i = n.querySelector('input'); if (i) i.focus(); touch(); updateResult(w.id); };
  if (info.set && pending === undefined) {
    wrap.append(h('span', { class: 'label', text: 'Webhook URL' }),
      h('div', { class: 'row' },
        h('span', { class: 'badge ok' }, icon('lock'), `Saved (${info.host || 'discord.com'})`),
        h('button', { type: 'button', class: 'small', 'aria-label': `Replace webhook URL for ${w.name}`, onclick: () => { D.urlDraft[w.id] = ''; paint(); } }, 'Replace'),
        h('button', { type: 'button', class: 'small ghost danger-text', 'aria-label': `Remove webhook URL for ${w.name}`, onclick: async () => {
          if (!(await confirmDialog('Remove this webhook URL?', 'It is deleted from this computer when you save. Anything using this webhook stops posting until you add a new URL.', { ok: 'Remove URL', danger: true }))) return;
          D.urlDraft[w.id] = null; paint();
        } }, 'Remove')),
      h('span', { class: 'help', text: 'Hidden for safety. Anyone with this URL can post to your channel.' }));
    return wrap;
  }
  const removing = pending === null;
  const input = h('input', { id: k + '-url', type: 'password', autocomplete: 'off', spellcheck: 'false', value: typeof pending === 'string' ? pending : '',
    placeholder: 'https://discord.com/api/webhooks/…', 'aria-describedby': k + '-url-help',
    oninput: (e) => { D.urlDraft[w.id] = e.target.value; touch(); updateResult(w.id); } });
  wrap.append(h('label', { class: 'label', for: k + '-url', text: 'Webhook URL' }),
    h('div', { class: 'row dc-url-row' }, input,
      info.set && h('button', { type: 'button', class: 'small ghost', onclick: () => { delete D.urlDraft[w.id]; paint(); } }, 'Keep saved URL')),
    h('span', { class: 'help', id: k + '-url-help', text: removing ? 'The saved URL will be removed when you save. Paste a new one to replace it instead.'
      : 'Paste it here. Once saved it is hidden and can only be replaced.' }));
  return wrap;
}

/* ------------------------------------------------------------------ test + results */

function updateResult(id) {
  const box = els.result && els.result[id];
  if (!box || !box.isConnected) return;
  const st = hookStatus(id);
  const w = hooks().find((x) => x.id === id);
  let tone = '', ic = 'info', text;
  switch (st.state) {
    case 'sending': text = 'Sending…'; break;
    case 'ok': tone = 'ok'; ic = 'check'; text = `Last post worked ${ago(st.at)}`.trim(); break;
    case 'error': tone = 'bad'; ic = 'warn'; text = (st.detail || 'The last post failed.') + (st.at ? ` (${ago(st.at)})` : ''); break;
    case 'broken': tone = 'bad'; ic = 'warn'; text = st.detail || 'Discord says this webhook was deleted. Paste a new URL, or Send test if it’s working again.'; break;
    default: text = 'Not tested yet';
  }
  box.className = 'dc-result ' + tone;
  box.replaceChildren(st.state === 'sending' ? h('span', { class: 'spin', 'aria-hidden': 'true' }) : icon(ic), h('span', { text }));
  const btn = els.testBtn[id];
  const why = !isSaved(id) || (w && hookDirty(w)) ? 'Save your changes first' : !urlInfo(id).set ? 'Add a webhook URL first' : '';
  btn.disabled = st.state === 'sending' || !!why;
  btn.title = why || (st.state === 'sending' ? 'Sending…' : 'Posts a short test message to this channel');
  btn.classList.toggle('busy', st.state === 'sending');
  let hint = box.parentElement.querySelector('.dc-why');
  if (why && !hint) box.parentElement.append(hint = h('span', { class: 'hint dc-why' }));
  if (hint) { if (why) hint.textContent = why; else hint.remove(); }
}

async function runTest(w) {
  const prev = hookStatus(w.id);
  D.pendingTest[w.id] = { t: Date.now(), since: prev.at, prev: prev.state, seen: false };
  updateResult(w.id);
  try {
    await api('/api/discord/test', { id: w.id });
  } catch (e) {
    delete D.pendingTest[w.id];
    updateResult(w.id);
    toast(e.status === 429 || /wait/i.test(e.message) ? e.message : 'Test not sent: ' + e.message, { bad: true });
  }
}

/* ------------------------------------------------------------------ save */

function renderSaveBar() {
  if (!els.bar) return;
  const dirty = isDirty();
  const errs = [];
  if (D.saveErr) {
    const m = /^Fix these first:\s*(.*)$/s.exec(D.saveErr);
    (m ? m[1].split(/;\s*/) : [D.saveErr]).filter(Boolean).forEach((x) => errs.push(x));
  }
  const fix = D.saveErr && /^Fix these first:/.test(D.saveErr);
  const msg = D.conflict
    ? h('div', { class: 'banner warn', role: 'alert' }, h('span', { text: D.saveErr || 'config.yaml changed since the panel loaded it.' }),
      h('div', { class: 'row' }, h('button', { type: 'button', class: 'small', onclick: reload }, icon('refresh'), 'Reload and discard my edits')))
    : errs.length ? h('div', { class: 'banner bad', role: 'alert' }, fix && h('strong', { text: 'Fix these first' }),
      fix || errs.length > 1 ? h('ul', { class: 'dc-errs' }, errs.map((x) => h('li', { text: x }))) : h('span', { text: errs[0] })) : null;
  els.bar.replaceChildren(...(msg ? [msg] : []),
    h('div', { class: 'row dc-save-row' },
      h('button', { type: 'submit', class: 'primary' + (D.saving ? ' busy' : ''), disabled: !dirty || D.saving, text: D.saving ? 'Saving…' : 'Save changes' }),
      h('button', { type: 'button', class: 'ghost', disabled: !dirty || D.saving, text: 'Discard changes', onclick: () => {
        D.draft = clone(D.data.config); D.urlDraft = {}; D.saveErr = null; D.conflict = false; renderAll();
      } }),
      h('span', { class: 'hint', text: dirty ? 'Unsaved changes' : 'All changes saved' })));
  for (const w of hooks()) updateResult(w.id);
}

async function reload() {
  D.saveErr = null; D.conflict = false; D.data = null; D.draft = null; D.urlDraft = {};
  await load();
}

async function save() {
  if (D.saving || !isDirty()) return;
  const bad = problems();
  if (bad.length) { D.saveErr = 'Fix these first: ' + bad.join('; '); D.conflict = false; renderSaveBar(); els.bar.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); return; }
  const config = clone(D.draft);
  config.webhooks.forEach((w) => { w.name = w.name.trim(); });
  const urls = {};
  for (const [id, v] of Object.entries(D.urlDraft)) {
    if (!config.webhooks.some((w) => w.id === id)) continue;
    if (v === null) urls[id] = null;
    else if (typeof v === 'string' && v.trim()) urls[id] = v.trim();
  }
  D.saving = true; D.saveErr = null; D.conflict = false;
  renderSaveBar();
  try {
    const r = await api('/api/discord/config', { config, urls, hash: D.data.hash });
    D.saving = false;
    adopt(r, true);
    renderAll();
    toast('Discord webhooks saved', { ok: true });
    return;
  } catch (e) {
    if (e.status === 409) D.conflict = true;
    if (e.status === 409 || e.status === 400) D.saveErr = e.message;
    else toast('Not saved: ' + e.message, { bad: true, ms: 8000 });
  }
  D.saving = false;
  renderSaveBar();
  if (D.saveErr && els.bar) els.bar.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}
