/* Moderation page (#view=automod): auto-mod keywords (ban / time out / delete), a log of what it did
   with an Unban button, and a tester. The filter itself lives in the daemon (modules/automod.py); this
   page edits its config and shows the "automod" events. */
import { api, on } from './api.js';
import { h, icon, toast, confirmDialog, asyncButton, switchInput, clone } from './ui.js';
import { go } from './main.js';

const LOG_CAP = 100;
const PRESETS = [[60, '1 minute'], [600, '10 minutes'], [3600, '1 hour'], [86400, '1 day'], [604800, '1 week']];
const ACTIONS = [['ban', 'Ban'], ['timeout', 'Time out'], ['delete', 'Delete message']];
const MATCHES = [['word', 'Whole word'], ['anywhere', 'Anywhere']];
const EXEMPT = [
  ['subscriber', 'Subscribers, VIPs and mods'],
  ['vip', 'VIPs and mods'],
  ['moderator', 'Only mods'],
];
const STATES = { queued: ['Queued', ''], done: ['Done', 'ok'], failed: ['Failed', 'bad'], unbanned: ['Unbanned', ''] };
const FALLBACK_LIMITS = { max_terms: 200, term_max: 100, seconds_max: 1209600, min_anywhere: 3, min_word: 2 };

const M = {
  visible: false, req: 0, loading: false, loadErr: null,
  data: null, draft: null,
  saving: false, saveErr: null, conflict: false,
  testText: '', testRes: null, testBusy: false, testErr: null,
};
let host = null, page = null, els = {}, tick = null;
let customRows = new WeakSet();   // term objects whose duration is in "custom" mode

/* ------------------------------------------------------------------ lifecycle */

export function mount(root) {
  host = root;
  page = h('div', { class: 'page am-page' });
  host.replaceChildren(page);
  on('automod', (d) => onEvent(d));
}

export function show() {
  M.visible = true;
  document.title = 'Moderation · Ultimate Streaming';
  load();
  clearInterval(tick);
  tick = setInterval(() => { if (M.visible && els.log) renderLog(); }, 30000);
}

export function hide() { M.visible = false; clearInterval(tick); }

async function load() {
  const req = ++M.req;
  M.loading = !M.data;
  if (M.loading) renderAll();
  try {
    const d = await api('/api/automod');
    if (req !== M.req) return;
    M.loadErr = null;
    adopt(d, !M.data || !isDirty());
  } catch (e) {
    if (req !== M.req) return;
    if (!M.data) M.loadErr = e;
    else { toast('Couldn’t refresh: ' + e.message, { bad: true }); return; }
  }
  M.loading = false;
  renderAll();
}

function adopt(d, reset) {
  M.data = d;
  if (reset) M.draft = clone(d.config);
}

function onEvent(d) {
  const e = d && d.entry;
  if (!e || !M.data) return;
  const log = M.data.log || (M.data.log = []);
  const i = log.findIndex((x) => x.id === e.id);
  if (i >= 0) log[i] = e;
  else {
    log.unshift(e);
    log.length = Math.min(log.length, LOG_CAP);
    if (M.data.status) M.data.status.caught = (M.data.status.caught || 0) + 1;
  }
  if (M.visible && els.log) { renderLog(); renderStatus(); }
}

/* ------------------------------------------------------------------ helpers */

const limits = () => ({ ...FALLBACK_LIMITS, ...((M.data && M.data.limits) || {}) });
const terms = () => (M.draft && M.draft.terms) || [];
const isDirty = () => !!(M.data && M.draft) && JSON.stringify(M.draft) !== JSON.stringify(M.data.config);
const letters = (s) => (String(s).match(/[\p{L}\p{N}]/gu) || []).length;

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

function dur(sec) {
  const p = PRESETS.find(([s]) => s === sec);
  if (p) return p[1];
  if (sec % 86400 === 0) return `${sec / 86400} days`;
  if (sec % 3600 === 0) return `${sec / 3600} hours`;
  if (sec % 60 === 0) return `${sec / 60} minutes`;
  return `${sec} seconds`;
}

const actionText = (action, seconds) =>
  action === 'ban' ? 'Ban' : action === 'timeout' ? `Time out for ${dur(seconds || 0)}` : 'Delete message';

const wouldText = (hit) =>
  hit.action === 'ban' ? 'Would ban' : hit.action === 'timeout' ? `Would time out for ${dur(hit.seconds || 0)}` : 'Would delete the message';

function problems() {
  const L = limits(), out = [];
  terms().forEach((t, i) => {
    const w = t.term.trim() ? `“${t.term.trim()}”` : `Keyword ${i + 1}`;
    const min = t.match === 'anywhere' ? L.min_anywhere : L.min_word;
    if (!t.term.trim()) out.push(`${w}: type a word or phrase, or remove the row`);
    else if (letters(t.term) < min) out.push(`${w}: needs at least ${min} letters or digits${t.match === 'anywhere' ? ' to match anywhere' : ''}`);
    if (t.action === 'timeout') {
      const s = Number(t.seconds);
      if (!Number.isInteger(s) || s < 1 || s > L.seconds_max) out.push(`${w}: timeout must be 1 to ${L.seconds_max} seconds`);
    }
  });
  return out;
}

function touch() { M.saveErr = null; M.conflict = false; renderSaveBar(); }

/* ------------------------------------------------------------------ render: page */

function header() {
  return h('div', { class: 'page-head' },
    h('div', { class: 'page-titles' },
      h('h1', { class: 'page-title', text: 'Moderation' }),
      h('p', { class: 'page-sub', text: 'Catch spam and unwanted words in chat before anyone sees them.' })));
}

function renderAll() {
  if (!page) return;
  els = {};
  if (M.loading && !M.data) {
    page.replaceChildren(header(), h('div', { class: 'card', 'aria-busy': 'true', 'aria-label': 'Loading' }, h('span', { class: 'skeleton line' }), h('span', { class: 'skeleton block mt-3' })));
    return;
  }
  if (M.loadErr) {
    page.replaceChildren(header(), h('div', { class: 'card' },
      h('div', { class: 'empty', role: 'alert' }, h('div', { class: 'empty-icon' }, icon('warn')),
        h('div', { class: 'what', text: 'Couldn’t load moderation' }),
        h('div', { class: 'how', text: M.loadErr.message || 'Try again in a moment.' }),
        h('div', { class: 'row mt-3' },
          h('button', { onclick: () => { M.loadErr = null; load(); } }, icon('refresh'), 'Try again'),
          h('button', { class: 'ghost', onclick: () => go('live') }, 'Back to Live')))));
    return;
  }
  els.status = h('section', { class: 'card', 'aria-label': 'Auto-mod settings' });
  els.terms = h('section', { class: 'card', 'aria-label': 'Keywords' });
  els.bar = h('div', { class: 'am-savebar' });
  els.test = h('section', { class: 'card', 'aria-label': 'Test a message' });
  els.logCard = h('section', { class: 'card', 'aria-label': 'Recent actions' });
  page.replaceChildren(header(),
    h('form', { class: 'am-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); save(); } }, els.status, els.terms, els.bar),
    els.test, els.logCard);
  renderStatus();
  renderTerms();
  renderSaveBar();
  renderTest();
  renderLog();
}

/* ------------------------------------------------------------------ (a) header card */

function renderStatus() {
  if (!els.status) return;
  const c = M.draft, st = (M.data && M.data.status) || {};
  const sw = switchInput(!!c.enabled, (v) => { c.enabled = v; touch(); }, 'Hide and act on messages with these keywords');
  const sel = h('select', { id: 'am-exempt', 'aria-describedby': 'am-exempt-help', onchange: (e) => { c.exempt = e.target.value; touch(); } },
    EXEMPT.map(([v, l]) => h('option', { value: v, text: l })));
  sel.value = c.exempt;
  const notice = st.detail
    ? h('div', { class: 'banner ' + (st.ready ? 'info' : 'warn'), role: 'status' }, icon(st.ready ? 'info' : 'warn'),
      h('div', { class: 'grow', text: st.detail }),
      !st.ready && h('button', { type: 'button', class: 'small', onclick: () => go('twitch') }, 'Open Twitch'))
    : null;
  els.status.replaceChildren(
    h('div', { class: 'card-head' }, h('div', { class: 'card-icon' }, icon('flag')),
      h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: 'Auto-mod' }),
        h('p', { class: 'card-sub', text: 'Messages with these keywords never show in the BRB chat feed, the panel’s chat or bot commands.' }))),
    notice,
    h('div', { class: 'am-settings' },
      sw,
      h('div', { class: 'field am-exempt' }, h('label', { class: 'label', for: 'am-exempt', text: 'Never act on' }), sel,
        h('span', { class: 'help', id: 'am-exempt-help', text: 'Mods and you are always exempt.' })),
      h('div', { class: 'am-caught' },
        h('span', { class: 'am-caught-n', text: String(st.caught || 0) }), h('span', { class: 'hint', text: 'caught since start' }))));
}

/* ------------------------------------------------------------------ (b) terms */

function renderTerms() {
  const list = terms(), L = limits();
  const addBtn = h('button', { type: 'button', class: list.length ? '' : 'primary', disabled: list.length >= L.max_terms, onclick: addTerm,
    title: list.length >= L.max_terms ? `Up to ${L.max_terms} keywords` : null }, icon('plus'), 'Add keyword');
  const head = h('div', { class: 'card-head' }, h('div', { class: 'card-icon' }, icon('text')),
    h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: 'Keywords' }),
      h('p', { class: 'card-sub', text: 'Spacing, punctuation, capitals, lookalike letters and digit swaps are ignored.' })));
  els.rows = h('ul', { class: 'am-rows', 'aria-label': 'Keywords' }, list.map(termRow));
  const help = h('details', { class: 'am-how' }, h('summary', { text: 'How matching works' }),
    h('p', {}, h('strong', { text: 'Whole word' }), ' (the default): the letters must start and end on word boundaries. “streamboo” catches “s t r e a m b o o”, “streamboo .com” and “STREAMB00”, but not “stream boost”.'),
    h('p', {}, h('strong', { text: 'Anywhere' }), ` also matches inside other words, like “xstreamboocom”. It needs at least ${L.min_anywhere} letters or digits and is riskier for short keywords.`),
    h('p', {}, h('strong', { text: 'Actions' }), ': ban is permanent, time out uses the duration you set, delete removes just that message.'));
  if (!list.length) {
    els.terms.replaceChildren(head, h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, icon('text')),
      h('div', { class: 'what', text: 'No keywords yet' }),
      h('div', { class: 'how', text: 'Add a word or phrase you never want to see in chat, like a spam site name.' }),
      h('div', { class: 'row mt-3' }, addBtn)), help);
    return;
  }
  els.terms.replaceChildren(head, els.rows,
    h('div', { class: 'am-add row' }, addBtn, h('span', { class: 'hint', text: list.length >= L.max_terms ? `That’s the limit of ${L.max_terms} keywords.` : `${list.length} of ${L.max_terms}` })),
    help);
}

function addTerm() {
  if (terms().length >= limits().max_terms) return;
  M.draft.terms.push({ term: '', match: 'word', action: 'ban', seconds: 600, enabled: true });
  renderTerms(); touch();
  const rows = els.rows.querySelectorAll('.am-row');
  const inp = rows[rows.length - 1]?.querySelector('input[type=text]');
  if (inp) { inp.scrollIntoView({ block: 'center', behavior: 'smooth' }); inp.focus({ preventScroll: true }); }
}

function removeTerm(t) {
  M.draft.terms = terms().filter((x) => x !== t);
  renderTerms(); touch();
}

function termRow(t, i) {
  const k = 'am-t' + i, L = limits();
  const input = h('input', { id: k, type: 'text', value: t.term, maxlength: String(L.term_max), autocomplete: 'off', spellcheck: 'false',
    placeholder: 'Word or phrase', 'aria-label': `Keyword ${i + 1}`,
    oninput: (e) => { t.term = e.target.value; input.classList.remove('invalid'); touch(); } });
  const match = h('select', { 'aria-label': `Match for keyword ${i + 1}`, onchange: (e) => { t.match = e.target.value; touch(); } },
    MATCHES.map(([v, l]) => h('option', { value: v, text: l })));
  match.value = t.match;
  const durBox = h('div', { class: 'am-dur', hidden: t.action !== 'timeout' });
  function renderDur() {
    const preset = PRESETS.some(([s]) => s === t.seconds);
    const custom = customRows.has(t) || !preset;
    const sel = h('select', { 'aria-label': `Timeout length for keyword ${i + 1}`, onchange: (e) => {
      if (e.target.value === 'custom') { customRows.add(t); renderDur(); durBox.querySelector('input').focus(); }
      else { customRows.delete(t); t.seconds = Number(e.target.value); renderDur(); }
      touch();
    } }, PRESETS.map(([s, l]) => h('option', { value: String(s), text: l })), h('option', { value: 'custom', text: 'Custom…' }));
    sel.value = custom ? 'custom' : String(t.seconds);
    const num = h('input', { type: 'number', min: '1', max: String(L.seconds_max), step: '1', inputmode: 'numeric', hidden: !custom, value: t.seconds,
      'aria-label': `Custom timeout in seconds for keyword ${i + 1}`, oninput: (e) => { t.seconds = e.target.value === '' ? '' : Number(e.target.value); touch(); } });
    durBox.replaceChildren(...[sel, num, custom ? h('span', { class: 'hint', text: 'seconds' }) : null].filter(Boolean));
  }
  const action = h('select', { 'aria-label': `Action for keyword ${i + 1}`, onchange: (e) => {
    t.action = e.target.value; durBox.hidden = t.action !== 'timeout';
    if (t.action === 'timeout' && !(Number(t.seconds) >= 1)) t.seconds = 600;
    renderDur(); touch();
  } }, ACTIONS.map(([v, l]) => h('option', { value: v, text: l })));
  action.value = t.action;
  renderDur();
  const sw = switchInput(!!t.enabled, (v) => { t.enabled = v; touch(); }, 'On');
  return h('li', { class: 'am-row' },
    h('div', { class: 'am-term' }, input),
    h('div', { class: 'am-opts' }, match, action, durBox),
    h('div', { class: 'am-row-end' }, sw,
      h('button', { type: 'button', class: 'small ghost danger-text', 'aria-label': `Remove keyword ${t.term.trim() || i + 1}`, onclick: () => removeTerm(t) }, icon('trash'), 'Remove')));
}

/* ------------------------------------------------------------------ save bar */

function renderSaveBar() {
  if (!els.bar) return;
  const dirty = isDirty();
  const errs = M.saveErr ? (Array.isArray(M.saveErr) ? M.saveErr : [M.saveErr]) : [];
  const msg = M.conflict
    ? h('div', { class: 'banner warn', role: 'alert' }, h('span', { text: errs[0] || 'These settings changed somewhere else since the panel loaded them.' }),
      h('div', { class: 'row' }, h('button', { type: 'button', class: 'small', onclick: reload }, icon('refresh'), 'Reload and discard my edits')))
    : errs.length ? h('div', { class: 'banner bad', role: 'alert' }, errs.length > 1 && h('strong', { text: 'Fix these first' }),
      errs.length > 1 ? h('ul', { class: 'am-errs' }, errs.map((x) => h('li', { text: x }))) : h('span', { text: errs[0] })) : null;
  els.bar.replaceChildren(...(msg ? [msg] : []),
    h('div', { class: 'row am-save-row' },
      h('button', { type: 'submit', class: 'primary' + (M.saving ? ' busy' : ''), disabled: !dirty || M.saving, text: M.saving ? 'Saving…' : 'Save changes' }),
      h('button', { type: 'button', class: 'ghost', disabled: !dirty || M.saving, text: 'Discard changes', onclick: () => {
        M.draft = clone(M.data.config); M.saveErr = null; M.conflict = false; renderAll();
      } }),
      h('span', { class: 'hint', text: dirty ? 'Unsaved changes' : 'All changes saved' })));
}

async function reload() {
  M.saveErr = null; M.conflict = false; M.data = null; M.draft = null;
  await load();
}

async function save() {
  if (M.saving || !isDirty()) return;
  const bad = problems();
  if (bad.length) { M.saveErr = bad; M.conflict = false; renderSaveBar(); els.bar.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); return; }
  const config = clone(M.draft);
  config.terms.forEach((t) => { t.term = t.term.trim(); t.seconds = Number(t.seconds) || 0; });
  M.saving = true; M.saveErr = null; M.conflict = false;
  renderSaveBar();
  try {
    const r = await api('/api/automod/config', { config, hash: M.data.hash });
    M.saving = false;
    adopt(r, true);
    renderAll();
    toast('Moderation saved', { ok: true });
    return;
  } catch (e) {
    if (e.status === 409) { M.conflict = true; M.saveErr = e.message; }
    else if (e.status === 400) M.saveErr = (Array.isArray(e.errors) && e.errors.length) ? e.errors : e.message;
    else toast('Not saved: ' + e.message, { bad: true, ms: 8000 });
  }
  M.saving = false;
  renderSaveBar();
  if (M.saveErr && els.bar) els.bar.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

/* ------------------------------------------------------------------ (c) test */

function renderTest() {
  if (!els.test) return;
  const input = h('input', { id: 'am-test-in', type: 'text', value: M.testText, maxlength: '500', autocomplete: 'off', placeholder: 'Paste a chat message to try',
    'aria-label': 'Message to test', onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); btn.click(); } },
    oninput: (e) => { M.testText = e.target.value; } });
  const btn = asyncButton({ type: 'button' }, async () => {
    if (!M.testText.trim()) { input.focus(); return; }
    M.testErr = null;
    try {
      M.testRes = await api('/api/automod/test', { text: M.testText, config: clone(M.draft) });
    } catch (e) { M.testRes = null; M.testErr = e.message; }
    renderResult();
  }, icon('play'), 'Test');
  els.result = h('div', { class: 'am-result', role: 'status' });
  els.test.replaceChildren(
    h('div', { class: 'card-head' }, h('div', { class: 'card-icon' }, icon('search')),
      h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: 'Test a message' }),
        h('p', { class: 'card-sub', text: 'Uses your keywords as edited here, even before you save. Nothing is sent to chat.' }))),
    h('div', { class: 'am-test-row' }, input, btn), els.result);
  renderResult();
}

function renderResult() {
  if (!els.result) return;
  const r = M.testRes;
  if (M.testErr) { els.result.replaceChildren(h('div', { class: 'am-verdict bad' }, icon('warn'), h('span', { text: 'Couldn’t test: ' + M.testErr }))); return; }
  if (!r) { els.result.replaceChildren(); return; }
  const verdict = r.hit
    ? h('div', { class: 'am-verdict bad' }, icon('flag'), h('span', {}, h('strong', { text: wouldText(r.hit) }), ` — matched “${r.hit.term}” (${r.hit.match === 'anywhere' ? 'anywhere' : 'whole word'})`))
    : h('div', { class: 'am-verdict ok' }, icon('check'), h('span', { text: 'No match' }));
  els.result.replaceChildren(verdict, r.seen && h('div', { class: 'hint am-seen' }, 'What the filter sees: ', h('code', { text: r.seen })));
}

/* ------------------------------------------------------------------ (d) log */

function renderLog() {
  if (!els.logCard) return;
  const log = (M.data && M.data.log) || [];
  const head = h('div', { class: 'card-head' }, h('div', { class: 'card-icon' }, icon('clock')),
    h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: 'Recent actions' }),
      h('p', { class: 'card-sub', text: 'The last 100 messages auto-mod caught, newest first.' })));
  if (!log.length) {
    els.logCard.replaceChildren(head, h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, icon('check')),
      h('div', { class: 'what', text: 'Nothing caught yet.' }),
      h('div', { class: 'how', text: 'When a message matches a keyword it shows up here, with a way to undo it.' })));
    els.log = null;
    return;
  }
  els.log = h('ul', { class: 'am-log', 'aria-label': 'Recent actions' }, log.map(logRow));
  els.logCard.replaceChildren(head, els.log);
}

function logRow(e) {
  const [label, tone] = STATES[e.state] || [e.state, ''];
  const when = new Date(e.at * 1000);
  const timeout = e.action === 'timeout';
  const unbanBtn = e.can_unban && asyncButton({ type: 'button', class: 'small', 'aria-label': `${timeout ? 'Remove timeout' : 'Unban'}: ${e.user}` }, async () => {
    const ok = await confirmDialog(timeout ? `Remove ${e.user}’s timeout?` : `Unban ${e.user}?`,
      `${timeout ? 'They can chat again right away.' : 'They can chat in your channel again.'} Auto-mod also stops acting on ${e.user} until the panel’s daemon restarts, even if they use a keyword again.`,
      { ok: timeout ? 'Remove timeout' : 'Unban' });
    if (!ok) return;
    const r = await api('/api/automod/unban', { id: e.id });
    adopt(r, !isDirty());
    renderLog(); renderStatus();
    toast(`${e.user} ${timeout ? 'can chat again' : 'is unbanned'}`, { ok: true });
  }, timeout ? 'Remove timeout' : 'Unban');
  return h('li', { class: 'am-entry' },
    h('div', { class: 'am-entry-top' },
      h('span', { class: 'am-user', text: e.user }),
      h('span', { class: 'am-act', text: actionText(e.action, e.seconds) }),
      h('span', { class: 'am-matched' }, 'matched ', h('q', { text: e.term })),
      h('time', { class: 'hint am-when', datetime: when.toISOString(), title: when.toLocaleString(), text: ago(e.at) })),
    h('div', { class: 'am-text', title: e.text, text: e.text }),
    h('div', { class: 'am-entry-foot' },
      h('span', { class: 'chip ' + tone, text: label }),
      e.state === 'failed' && e.detail && h('span', { class: 'am-detail', text: e.detail }),
      unbanBtn));
}
