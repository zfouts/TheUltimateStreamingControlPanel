/* Announcements page (#view=announce): Discord webhook destinations that get a post when Twitch
   says the channel went live. Each destination picks a named webhook from Settings → Discord; this
   page never sees a URL. Live results come from the "status" event (snapshot.announce). */
import { api, on } from './api.js';
import { h, icon, toast, confirmDialog, asyncButton, switchInput, clone } from './ui.js';
import { go, snapshot as shellSnapshot } from './main.js';

const MAX_DEST = 10;
const ID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const ROLE_RE = /^\d{17,20}$/;
const NAMES = ['channel', 'title', 'game', 'url'];
const FALLBACK_SAMPLE = { channel: 'YourChannel', title: 'Playing something fun', game: 'Just Chatting', url: 'https://twitch.tv/yourchannel' };

const A = {
  visible: false, req: 0, loading: false, loadErr: null,
  data: null,          // last server body
  draft: null,         // edited copy of data.config
  saving: false, saveErr: null, conflict: false,
  pendingTest: {},     // id -> {at, since}: a test we queued, until the status event answers
  announcing: false,
};
let host = null, page = null, els = {};
let fallbackSnap = null, tick = null;

/* ------------------------------------------------------------------ lifecycle */

export function mount(root) {
  host = root;
  page = h('div', { class: 'page ann-page' });
  host.replaceChildren(page);
  on('status', () => { if (A.visible) refreshLive(); });
}

export function show() {
  A.visible = true;
  document.title = 'Announcements · Ultimate Streaming';
  load();
  if (!shellSnapshot || !Object.keys(shellSnapshot).length) {
    api('/api/live/snapshot').then((d) => { fallbackSnap = d; if (A.visible) refreshLive(); }).catch(() => {});
  }
  clearInterval(tick);
  tick = setInterval(() => { if (A.visible) refreshLive(); }, 30000);
}

export function hide() { A.visible = false; clearInterval(tick); }

async function load() {
  const req = ++A.req;
  A.loading = !A.data;
  if (A.loading) renderAll();
  try {
    const d = await api('/api/announce');
    if (req !== A.req) return;
    A.loadErr = null;
    adopt(d, !A.data || !isDirty());
  } catch (e) {
    if (req !== A.req) return;
    if (!A.data) A.loadErr = e;
    else { toast('Couldn’t refresh: ' + e.message, { bad: true }); return; }
  }
  A.loading = false;
  renderAll();
}

function adopt(d, reset) {
  A.data = d;
  if (reset) A.draft = clone(d.config);
}

/* ------------------------------------------------------------------ helpers */

const dests = () => (A.draft && A.draft.destinations) || [];
const sample = () => ({ ...FALLBACK_SAMPLE, ...((A.data && A.data.sample) || {}) });
const hooks = () => (A.data && A.data.webhooks) || [];
const hookOf = (id) => hooks().find((w) => w.id === id) || null;
/** The destination can post: its webhook exists and has a saved URL. */
const hookReady = (d) => { const w = hookOf(d.webhook); return !!(w && w.set); };

function liveStatus() {
  const s = (shellSnapshot && shellSnapshot.announce) || (fallbackSnap && fallbackSnap.announce) || (A.data && A.data.status) || {};
  return s;
}
function destStatus(id) {
  const st = (liveStatus().destinations || {})[id] || { state: 'never', at: null, detail: null };
  const p = A.pendingTest[id];
  if (p) {
    const answered = (st.state === 'sending') ? false : (st.at !== p.since || st.state !== p.prev);
    if (st.state === 'sending') p.seen = true;
    if ((answered && (p.seen || st.at !== p.since)) || Date.now() - p.t > 30000) delete A.pendingTest[id];
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
  const base = (String(name).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'discord').slice(0, 28);
  const taken = new Set(dests().map((d) => d.id));
  let id = base, n = 2;
  while (taken.has(id) || !ID_RE.test(id)) id = `${base}-${n++}`;
  return id;
}

function pingKind(p) { return p && p.startsWith('role:') ? 'role' : (p || 'none'); }

function isDirty() {
  if (!A.data || !A.draft) return false;
  return JSON.stringify(A.draft) !== JSON.stringify(A.data.config);
}
function destDirty(d) {
  const orig = ((A.data && A.data.config.destinations) || []).find((x) => x.id === d.id);
  return !orig || JSON.stringify(orig) !== JSON.stringify(d);
}

function problems() {
  const out = [];
  const c = A.draft;
  const cd = Number(c.cooldown);
  if (String(c.cooldown).trim() === '' || !Number.isInteger(cd) || cd < 0 || cd > 1440) out.push('Cooldown: use a whole number of minutes from 0 to 1440');
  dests().forEach((d, i) => {
    const w = (d.name || '').trim() || `Destination ${i + 1}`;
    if (!(d.name || '').trim() || d.name.length > 40) out.push(`${w}: name must be 1 to 40 characters`);
    if (!(d.message || '').trim()) out.push(`${w}: write a message`);
    else if (d.message.length > 1500) out.push(`${w}: message is over 1500 characters`);
    if (d.ping.startsWith('role:') && !ROLE_RE.test(d.ping.slice(5))) out.push(`${w}: the role ID is 17 to 20 digits`);
    if (d.enabled && !d.webhook) out.push(`${w}: pick a Discord webhook, or turn the destination off`);
    else if (d.webhook && !hookOf(d.webhook)) out.push(`${w}: that webhook no longer exists, pick another`);
  });
  return out;
}

function touch() { A.saveErr = null; A.conflict = false; renderSaveBar(); renderAnnounceNow(); }

/* ------------------------------------------------------------------ render: page */

function renderAll() {
  if (!page) return;
  els = {};
  if (A.loading && !A.data) {
    page.replaceChildren(header(), h('div', { class: 'card', 'aria-busy': 'true', 'aria-label': 'Loading' }, h('span', { class: 'skeleton line' }), h('span', { class: 'skeleton block mt-3' })));
    return;
  }
  if (A.loadErr) {
    page.replaceChildren(header(), h('div', { class: 'card' },
      h('div', { class: 'empty', role: 'alert' }, h('div', { class: 'empty-icon' }, icon('warn')),
        h('div', { class: 'what', text: 'Couldn’t load announcements' }),
        h('div', { class: 'how', text: A.loadErr.message || 'Try again in a moment.' }),
        h('div', { class: 'row mt-3' },
          h('button', { onclick: () => { A.loadErr = null; load(); } }, icon('refresh'), 'Try again'),
          h('button', { class: 'ghost', onclick: () => go('live') }, 'Back to Live')))));
    return;
  }
  els.banner = h('div', { class: 'ann-banner' });
  els.settings = h('section', { class: 'card', 'aria-label': 'Announcement settings' });
  els.list = h('div', { class: 'ann-list' });
  els.bar = h('div', { class: 'ann-savebar' });
  page.replaceChildren(header(), els.banner,
    h('form', { class: 'ann-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); save(); } }, els.settings, els.list, els.bar));
  renderSettings();
  renderList();
  renderSaveBar();
  refreshLive();
}

function header() {
  return h('div', { class: 'page-head' },
    h('div', { class: 'page-titles' },
      h('h1', { class: 'page-title', text: 'Announcements' }),
      h('p', { class: 'page-sub', text: 'Posts to your Discord (and more later) when Twitch says you’re live.' })));
}

/** Parts that follow the live status: readiness banner, result lines, buttons. */
function refreshLive() {
  if (!els.banner) return;
  renderBanner();
  renderAnnounceNow();
  for (const d of dests()) updateResult(d.id);
}

function renderBanner() {
  const r = liveStatus().ready;
  if (!r || (r.twitch && r.eventsub)) { els.banner.replaceChildren(); return; }
  const line = r.detail || (r.twitch === false
    ? 'Twitch login needs fixing, so going live won’t be detected. “Announce now” still works.'
    : 'Not listening for Twitch go-live events yet, so going live won’t be detected. “Announce now” still works.');
  els.banner.replaceChildren(h('div', { class: 'banner warn', role: 'status' }, icon('warn'),
    h('div', { class: 'grow', text: line }),
    h('button', { type: 'button', class: 'small', onclick: () => go('twitch') }, 'Open Twitch')));
}

/* ------------------------------------------------------------------ render: settings card */

function renderSettings() {
  const c = A.draft;
  const sw = switchInput(!!c.enabled, (v) => { c.enabled = v; touch(); }, 'Announce when I go live');
  const cd = h('input', { id: 'ann-cooldown', type: 'number', inputmode: 'numeric', min: '0', max: '1440', step: '1', value: c.cooldown ?? '',
    'aria-describedby': 'ann-cooldown-help', oninput: (e) => { c.cooldown = e.target.value === '' ? '' : Number(e.target.value); touch(); } });
  els.now = h('div', { class: 'ann-now' });
  els.settings.replaceChildren(
    h('div', { class: 'card-head' }, h('div', { class: 'card-icon' }, icon('send')),
      h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: 'When you go live' }),
        h('p', { class: 'card-sub', text: 'One post per destination, once per stream.' }))),
    h('div', { class: 'ann-settings' },
      sw,
      h('div', { class: 'field ann-cooldown' }, h('label', { class: 'label', for: 'ann-cooldown', text: 'Cooldown (minutes)' }), cd,
        h('span', { class: 'help', id: 'ann-cooldown-help', text: 'Skip a new go-live that comes this soon after the last post. 0 turns it off.' })),
      els.now));
  renderAnnounceNow();
}

function renderAnnounceNow() {
  if (!els.now) return;
  const dirty = isDirty();
  const live = liveStatus();
  const usable = ((A.data && A.data.config.destinations) || []).some((d) => d.enabled && hookReady(d) && (live.destinations?.[d.id]?.state !== 'broken'));
  const why = dirty ? 'Save your changes first, then post.' : !usable ? 'Needs an enabled destination whose webhook has a saved URL.' : '';
  const b = asyncButton({ type: 'button', class: 'primary', disabled: !!why || A.announcing, 'aria-describedby': why ? 'ann-now-hint' : null }, async () => {
    const n = ((A.data.config.destinations) || []).filter((d) => d.enabled && hookReady(d)).length;
    if (!(await confirmDialog('Announce now?', `This posts your go-live message to ${n} destination${n === 1 ? '' : 's'} right now, even if you’re not live and ignoring the cooldown.`, { ok: 'Announce now' }))) return;
    A.announcing = true;
    try {
      await api('/api/announce/now', {});
      toast('Announcement queued. Results show on each destination.', { ok: true });
    } finally { A.announcing = false; }
  }, icon('send'), 'Announce now');
  const last = live.last && live.last.at ? `Last post: ${ago(live.last.at)}${live.last.kind === 'manual' ? ' (manual)' : ''}.` : 'Nothing posted yet.';
  els.now.replaceChildren(b, h('span', { class: 'hint', id: 'ann-now-hint', text: why || last }));
}

/* ------------------------------------------------------------------ render: destinations */

function renderList() {
  const list = dests();
  els.cards = {};
  const addBtn = h('button', { type: 'button', class: list.length ? '' : 'primary', disabled: list.length >= MAX_DEST, onclick: addDest,
    title: list.length >= MAX_DEST ? `Up to ${MAX_DEST} destinations` : null }, icon('plus'), 'Add Discord destination');
  if (!hooks().length && !list.length) {
    els.list.replaceChildren(h('section', { class: 'card', 'aria-label': 'Destinations' },
      h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, icon('send')),
        h('div', { class: 'what', text: 'Add a Discord webhook first' }),
        h('div', { class: 'how', text: 'Webhooks now live in Settings → Discord. Add one there, then come back to choose what to post.' }),
        h('div', { class: 'row mt-3' }, h('button', { type: 'button', class: 'primary', onclick: () => go('discord') }, icon('plus'), 'Add a Discord webhook')))));
    return;
  }
  if (!list.length) {
    els.list.replaceChildren(h('section', { class: 'card', 'aria-label': 'Destinations' },
      h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, icon('send')),
        h('div', { class: 'what', text: 'No destinations yet' }),
        h('div', { class: 'how', text: 'Add a destination, then pick which of your Discord webhooks it posts to.' }),
        h('div', { class: 'row mt-3' }, addBtn))));
    return;
  }
  els.list.replaceChildren(...list.map(destCard),
    h('div', { class: 'ann-add row' }, addBtn,
      h('span', { class: 'hint', text: list.length >= MAX_DEST ? `That’s the limit of ${MAX_DEST} destinations.` : `${list.length} of ${MAX_DEST}` })));
}

function addDest() {
  if (dests().length >= MAX_DEST) return;
  const name = dests().length ? `Discord ${dests().length + 1}` : 'My Discord';
  A.draft.destinations.push({ id: slug(name), name, webhook: (hooks().find((w) => w.set) || hooks()[0] || {}).id || '', enabled: true, message: '{channel} is live! {title}\n{url}', embed: true, ping: 'none' });
  renderList(); touch(); refreshLive();
  const cards = els.list.querySelectorAll('.ann-dest');
  const inp = cards[cards.length - 1]?.querySelector('input[type=text]');
  if (inp) { inp.scrollIntoView({ block: 'center', behavior: 'smooth' }); inp.focus({ preventScroll: true }); }
}

async function removeDest(d) {
  if (!(await confirmDialog(`Delete “${d.name || 'this destination'}”?`, 'It stops posting when you save. The Discord webhook itself is kept in Settings → Discord.', { ok: 'Delete destination', danger: true }))) return;
  A.draft.destinations = dests().filter((x) => x.id !== d.id);
  renderList(); touch(); refreshLive();
}

function destCard(d) {
  const k = 'ann-' + d.id;
  const card = h('section', { class: 'card ann-dest', 'aria-label': d.name.trim() || 'New destination' });
  els.cards[d.id] = card;

  const nameIn = h('input', { id: k + '-name', type: 'text', value: d.name, maxlength: '40', autocomplete: 'off',
    oninput: (e) => { d.name = e.target.value; card.setAttribute('aria-label', d.name.trim() || 'New destination'); title.textContent = d.name.trim() || 'New destination'; touch(); } });
  const sw = switchInput(!!d.enabled, (v) => { d.enabled = v; touch(); }, 'On');

  const msg = h('textarea', { id: k + '-msg', rows: '4', maxlength: '1500', spellcheck: 'true', 'aria-describedby': k + '-chips',
    oninput: (e) => { d.message = e.target.value; updatePreview(); touch(); } });
  msg.value = d.message;
  const chips = h('div', { class: 'ann-chips', id: k + '-chips', role: 'group', 'aria-label': 'Insert a placeholder' },
    NAMES.map((n) => h('button', { type: 'button', class: 'small ghost ann-chip', title: `Insert {${n}} at the cursor`, onclick: () => {
      const s = msg.selectionStart ?? msg.value.length, e = msg.selectionEnd ?? s;
      if (msg.value.length - (e - s) + n.length + 2 > 1500) return;
      msg.setRangeText(`{${n}}`, s, e, 'end');
      msg.focus();
      d.message = msg.value; updatePreview(); touch();
    } }, `{${n}}`)));

  const preview = h('div', { class: 'ann-preview', 'aria-live': 'off' });
  function updatePreview() {
    const sm = sample();
    const text = d.message.replace(/\{([a-z]+)\}/g, (m, n) => (NAMES.includes(n) ? String(sm[n] ?? '') : m));
    const pre = d.ping === 'everyone' ? '@everyone ' : d.ping === 'here' ? '@here ' : d.ping.startsWith('role:') ? '@role ' : '';
    preview.replaceChildren(
      h('div', { class: 'ann-bubble' },
        h('div', { class: 'ann-msg' }, pre && h('span', { class: 'ann-mention', text: pre }), text.trim() ? text : h('span', { class: 'hint', text: 'Your message is empty.' })),
        d.embed && h('div', { class: 'ann-embed' },
          h('div', { class: 'ann-embed-title', text: sm.title || `${sm.channel} is live` }),
          sm.game && h('div', { class: 'ann-embed-desc', text: sm.game }),
          h('div', { class: 'ann-embed-img', 'aria-hidden': 'true' }, icon('live')))));
  }

  const embedSw = switchInput(!!d.embed, (v) => { d.embed = v; updatePreview(); touch(); }, 'Add a rich card (title, game, thumbnail)');

  const roleIn = h('input', { id: k + '-role', type: 'text', inputmode: 'numeric', autocomplete: 'off', spellcheck: 'false', maxlength: '20',
    placeholder: '17 to 20 digits', value: d.ping.startsWith('role:') ? d.ping.slice(5) : '', 'aria-describedby': k + '-role-help',
    oninput: (e) => { d.ping = 'role:' + e.target.value.trim(); roleIn.classList.toggle('invalid', !ROLE_RE.test(e.target.value.trim()) && e.target.value !== ''); updatePreview(); touch(); } });
  const roleField = h('div', { class: 'field ann-role', hidden: pingKind(d.ping) !== 'role' },
    h('label', { class: 'label', for: k + '-role', text: 'Role ID' }), roleIn,
    h('span', { class: 'help', id: k + '-role-help', text: 'In Discord, turn on Developer Mode, right-click the role, Copy Role ID.' }));
  const pingSel = h('select', { id: k + '-ping', onchange: (e) => {
    const v = e.target.value;
    d.ping = v === 'role' ? 'role:' + roleIn.value.trim() : v;
    roleField.hidden = v !== 'role';
    if (v === 'role') roleIn.focus();
    updatePreview(); touch();
  } }, [['none', 'No one'], ['everyone', '@everyone'], ['here', '@here'], ['role', 'A role…']].map(([v, l]) => h('option', { value: v, text: l })));
  pingSel.value = pingKind(d.ping);

  els.result ||= {};
  const title = h('h2', { class: 'card-title', text: (d.name || '').trim() || 'New destination' });
  const result = h('div', { class: 'ann-result', role: 'status' });
  els.result[d.id] = result;
  const testBtn = h('button', { type: 'button', class: 'small', 'data-test': d.id, onclick: () => runTest(d) }, icon('send'), 'Send test');
  els.testBtn ||= {};
  els.testBtn[d.id] = testBtn;

  card.append(
    h('div', { class: 'card-head ann-dest-head' },
      h('div', { class: 'card-titles' }, title),
      sw,
      h('button', { type: 'button', class: 'small ghost danger-text', 'aria-label': `Delete ${d.name || 'destination'}`, onclick: () => removeDest(d) }, icon('trash'), 'Delete')),
    h('div', { class: 'field' }, h('label', { class: 'label', for: k + '-name', text: 'Name' }), nameIn,
      h('span', { class: 'help', text: 'Only shown here, so you can tell destinations apart.' })),
    webhookField(d, k, card),
    h('div', { class: 'field' }, h('label', { class: 'label', for: k + '-msg', text: 'Message' }), msg, chips,
      h('span', { class: 'help', text: 'Click a placeholder to add it. Discord formatting like **bold** works.' })),
    h('div', { class: 'field' }, h('span', { class: 'label', text: 'Preview' }), preview),
    embedSw,
    h('div', { class: 'ann-ping' },
      h('div', { class: 'field' }, h('label', { class: 'label', for: k + '-ping', text: 'Ping' }), pingSel), roleField),
    h('div', { class: 'ann-foot' }, testBtn, result));
  updatePreview();
  updateResult(d.id);
  return card;
}

function webhookField(d, k) {
  const list = hooks();
  if (!list.length) {
    return h('div', { class: 'field ann-hook' }, h('span', { class: 'label', text: 'Discord webhook' }),
      h('div', { class: 'row ann-hook-row' }, h('span', { class: 'hint', text: 'No webhooks yet.' }),
        h('button', { type: 'button', class: 'small primary', onclick: () => go('discord') }, icon('plus'), 'Add a Discord webhook first')));
  }
  const sel = h('select', { id: k + '-hook', 'aria-describedby': k + '-hook-help', onchange: (e) => { d.webhook = e.target.value; touch(); refreshLive(); } },
    h('option', { value: '', text: 'Choose a webhook…' }),
    list.map((w) => h('option', { value: w.id, text: w.set ? w.name : `${w.name} (no URL saved)` })),
    d.webhook && !hookOf(d.webhook) && h('option', { value: d.webhook, text: `Missing webhook (${d.webhook})` }));
  sel.value = d.webhook || '';
  return h('div', { class: 'field ann-hook' }, h('label', { class: 'label', for: k + '-hook', text: 'Discord webhook' }),
    h('div', { class: 'row ann-hook-row' }, sel,
      h('a', { class: 'ann-manage', href: '#view=discord', onclick: (e) => { e.preventDefault(); go('discord'); } }, 'Manage webhooks')),
    h('span', { class: 'help', id: k + '-hook-help', text: 'Which channel this posts to. Add or replace webhooks in Settings → Discord.' }));
}

/* ------------------------------------------------------------------ test + results */

function updateResult(id) {
  const box = els.result && els.result[id];
  if (!box || !box.isConnected) return;
  const st = destStatus(id);
  const d = dests().find((x) => x.id === id);
  const savedDest = ((A.data && A.data.config.destinations) || []).find((x) => x.id === id);
  let tone = '', ic = 'info', text;
  switch (st.state) {
    case 'sending': tone = ''; text = 'Sending…'; break;
    case 'ok': tone = 'ok'; ic = 'check'; text = `Posted ${ago(st.at)}`.trim(); break;
    case 'error': tone = 'bad'; ic = 'warn'; text = (st.detail || 'The last post failed.') + (st.at ? ` (${ago(st.at)})` : ''); break;
    case 'broken': tone = 'bad'; ic = 'warn'; text = st.detail || 'Discord says this webhook was deleted. Replace it in Settings → Discord, or Send test if it’s working again.'; break;
    default: text = 'Not posted yet';
  }
  box.className = 'ann-result ' + tone;
  box.replaceChildren(st.state === 'sending' ? h('span', { class: 'spin', 'aria-hidden': 'true' }) : icon(ic), h('span', { text }));
  const btn = els.testBtn[id];
  const why = !savedDest || (d && destDirty(d)) ? 'Save your changes first' : !d.webhook ? 'Pick a webhook first' : !hookReady(d) ? 'That webhook has no saved URL yet (Settings → Discord)' : '';
  btn.disabled = st.state === 'sending' || !!why;
  btn.title = why || (st.state === 'sending' ? 'Sending…' : 'Posts “[Test]” and your message to this destination only');
  btn.classList.toggle('busy', st.state === 'sending');
  let hint = box.parentElement.querySelector('.ann-why');
  if (why && !hint) box.parentElement.append(hint = h('span', { class: 'hint ann-why' }));
  if (hint) { if (why) hint.textContent = why; else hint.remove(); }
}

async function runTest(d) {
  const prev = destStatus(d.id);
  A.pendingTest[d.id] = { t: Date.now(), since: prev.at, prev: prev.state, seen: false };
  updateResult(d.id);
  try {
    await api('/api/announce/test', { id: d.id });
  } catch (e) {
    delete A.pendingTest[d.id];
    updateResult(d.id);
    toast(e.status === 429 || /wait/i.test(e.message) ? e.message : 'Test not sent: ' + e.message, { bad: true });
  }
}

/* ------------------------------------------------------------------ save */

function renderSaveBar() {
  if (!els.bar) return;
  const dirty = isDirty();
  const errs = [];
  if (A.saveErr) {
    const m = /^Fix these first:\s*(.*)$/s.exec(A.saveErr);
    (m ? m[1].split(/;\s*/) : [A.saveErr]).filter(Boolean).forEach((x) => errs.push(x));
  }
  const fix = A.saveErr && /^Fix these first:/.test(A.saveErr);
  const msg = A.conflict
    ? h('div', { class: 'banner warn', role: 'alert' }, h('span', { text: A.saveErr || 'config.yaml changed since the panel loaded it.' }),
      h('div', { class: 'row' }, h('button', { type: 'button', class: 'small', onclick: reload }, icon('refresh'), 'Reload and discard my edits')))
    : errs.length ? h('div', { class: 'banner bad', role: 'alert' }, fix && h('strong', { text: 'Fix these first' }),
      fix || errs.length > 1 ? h('ul', { class: 'ann-errs' }, errs.map((x) => h('li', { text: x }))) : h('span', { text: errs[0] })) : null;
  els.bar.replaceChildren(...(msg ? [msg] : []),
    h('div', { class: 'row ann-save-row' },
      h('button', { type: 'submit', class: 'primary' + (A.saving ? ' busy' : ''), disabled: !dirty || A.saving, text: A.saving ? 'Saving…' : 'Save changes' }),
      h('button', { type: 'button', class: 'ghost', disabled: !dirty || A.saving, text: 'Discard changes', onclick: () => {
        A.draft = clone(A.data.config); A.saveErr = null; A.conflict = false; renderAll();
      } }),
      h('span', { class: 'hint', text: dirty ? 'Unsaved changes' : 'All changes saved' })));
  for (const d of dests()) updateResult(d.id);
}

async function reload() {
  A.saveErr = null; A.conflict = false; A.data = null; A.draft = null;
  await load();
}

async function save() {
  if (A.saving || !isDirty()) return;
  const bad = problems();
  if (bad.length) { A.saveErr = 'Fix these first: ' + bad.join('; '); A.conflict = false; renderSaveBar(); els.bar.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); return; }
  const config = clone(A.draft);
  config.cooldown = Number(config.cooldown);
  config.destinations.forEach((d) => { d.name = d.name.trim(); });
  A.saving = true; A.saveErr = null; A.conflict = false;
  renderSaveBar();
  try {
    const r = await api('/api/announce/config', { config, hash: A.data.hash });
    A.saving = false;
    adopt(r, true);
    renderAll();
    toast('Announcements saved', { ok: true });
    return;
  } catch (e) {
    if (e.status === 409) A.conflict = true;
    if (e.status === 409 || e.status === 400) A.saveErr = e.message;
    else toast('Not saved: ' + e.message, { bad: true, ms: 8000 });
  }
  A.saving = false;
  renderSaveBar();
  if (A.saveErr && els.bar) els.bar.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}
